#!/usr/bin/env python3
"""Classify a process's leaking fds from OUTSIDE it: no patch, no rebuild, no pin.

This is the fd-leak instrument, reachable from any tree. The earlier version was a
patch to `Libraries/LibRequests/Request.cpp`, which is the wrong delivery mechanism
for anyone who has their own commits: it assumes a tree at our pinned commit, it
conflicts with a tree that already carries the upstream fix, and it makes "run my
diagnostic" mean "reset your tree". Everything that build computed is visible from
outside, so this computes it from /proc and ss against a RUNNING process.

    python3 fd_census.py <pid>                  # one census
    python3 fd_census.py <pid> --watch 30       # every 30s until interrupted
    python3 fd_census.py --find WebContent      # locate the pid yourself-free

What it reports, and why each column exists:

  total / socket / pipe          the census that separated the two leaks in the
                                 first place: MessagePort leaks 4 pipes per socket,
                                 so a sockets-only census falsifies it outright.

  peer DEAD vs ALIVE             the discriminator between the two per-request
                                 leaks. RequestServer closes its half of the
                                 response socketpair when the request completes, so
                                 `ss` shows peer inode 0:
                                   DEAD  -> the request COMPLETED and WebContent is
                                            retaining a corpse. Fixed by the
                                            teardown patch / upstream's equivalent.
                                   ALIVE -> the peer still holds it: on_finish never
                                            ran, so no teardown in that branch can
                                            fire. Needs the GC-root/RefPtr cycle
                                            broken at the Response end instead.

  retained vs in-flight (by age) an fd younger than the threshold is not a leak, it
                                 is a request. Counting fds without ages is how a
                                 freshly restarted process once looked like a fix.
                                 Ages need >= 2 samples, so --watch earns them.

  peer process                   who holds the other end, when it is alive. Names
                                 the producer instead of guessing.

Exit status is 0 for a census, 1 if it could not read the process.
"""

import argparse
import os
import re
import subprocess
import sys
import time

SOCKET_RE = re.compile(r"^socket:\[(\d+)\]$")


def read_fd_targets(pid):
    """{fd: symlink target} for a live pid. Racy by nature; skip what vanishes."""
    targets = {}
    fd_dir = "/proc/%s/fd" % pid
    for name in os.listdir(fd_dir):
        try:
            targets[int(name)] = os.readlink(os.path.join(fd_dir, name))
        except (OSError, ValueError):
            continue  # closed under us, or not a number
    return targets


def categorize(target):
    """The category names are the ones the /proc census prints, deliberately."""
    if target.startswith("socket:"):
        return "socket:"
    if target.startswith("pipe:"):
        return "pipe:"
    if target.startswith("anon_inode:"):
        return "anon_inode:"
    if target.startswith("/memfd:"):
        return "memfd:"
    if target.startswith("/"):
        return "file"
    return "other"


def socket_inode(target):
    m = SOCKET_RE.match(target)
    return int(m.group(1)) if m else None


def parse_ss(text):
    """Map socket inode -> (peer_inode, [holder names]) from `ss -np` output.

    ss lays out unix sockets as `... <local-inode> * <peer-inode> users:(...)`, and
    a peer inode of 0 means the far end is closed -- which is the whole point of
    running ss rather than just counting /proc entries.
    """
    sockets = {}
    for line in text.splitlines():
        if "users:(" not in line:
            continue
        head, users = line.split("users:(", 1)
        fields = head.split()
        inodes = [f for f in fields if f.isdigit()]
        if len(inodes) < 2:
            continue
        local_inode, peer_inode = int(inodes[-2]), int(inodes[-1])
        holders = re.findall(r'\("([^"]+)",pid=(\d+),fd=(\d+)\)', users)
        sockets[local_inode] = (peer_inode, holders)
    return sockets


def ss_sockets():
    try:
        out = subprocess.run(["ss", "-np"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_ss(out)


def peer_state(inode, sockets):
    """DEAD / ALIVE / unknown for one socket inode."""
    entry = sockets.get(inode)
    if entry is None:
        return "unknown", []
    peer_inode, holders = entry
    if peer_inode == 0:
        return "DEAD", holders
    return "ALIVE", holders


def find_pids(name):
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry) as f:
                comm = f.read().strip()
        except OSError:
            continue
        if name in comm:
            try:
                count = len(os.listdir("/proc/%s/fd" % entry))
            except OSError:
                count = 0
            out.append((count, int(entry), comm))
    return sorted(out, reverse=True)


class Census:
    """Samples a pid over time so fds can be aged.

    The ages are the reason this is a class and not a function: "retained" only
    means anything relative to a first-seen time, and the first sample cannot know
    one. Keyed by (fd, target) so fd reuse does not inherit an age.
    """

    def __init__(self, pid, retained_after=30.0):
        self.pid = pid
        self.retained_after = retained_after
        self.first_seen = {}

    def sample(self, now=None):
        now = time.time() if now is None else now
        targets = read_fd_targets(self.pid)
        sockets = ss_sockets()

        live_keys = set()
        by_category = {}
        rows = []
        for fd, target in sorted(targets.items()):
            key = (fd, target)
            live_keys.add(key)
            self.first_seen.setdefault(key, now)
            age = now - self.first_seen[key]

            category = categorize(target)
            by_category[category] = by_category.get(category, 0) + 1

            inode = socket_inode(target)
            state, holders = ("n/a", [])
            if inode is not None:
                state, holders = peer_state(inode, sockets)
            rows.append({
                "fd": fd, "target": target, "category": category, "age": age,
                "peer": state,
                "peers": [h[0] for h in holders if int(h[1]) != int(self.pid)],
            })

        for key in list(self.first_seen):
            if key not in live_keys:
                del self.first_seen[key]

        return {"rows": rows, "by_category": by_category,
                "samples_seen": max(1, len(self.first_seen) and 1)}

    def summarize(self, snapshot):
        rows = snapshot["rows"]
        out = []
        total = len(rows)
        out.append("fds: total=%d  %s" % (
            total, "  ".join("%s=%d" % (k, v) for k, v in
                             sorted(snapshot["by_category"].items(),
                                    key=lambda kv: -kv[1]))))

        socks = [r for r in rows if r["category"] == "socket:"]
        dead = [r for r in socks if r["peer"] == "DEAD"]
        alive = [r for r in socks if r["peer"] == "ALIVE"]
        unknown = [r for r in socks if r["peer"] == "unknown"]
        out.append("sockets: %d  peer DEAD=%d  peer ALIVE=%d  unknown=%d" % (
            len(socks), len(dead), len(alive), len(unknown)))

        retained = [r for r in socks if r["age"] >= self.retained_after]
        in_flight = [r for r in socks if r["age"] < self.retained_after]
        aged = any(r["age"] > 0 for r in socks)
        if aged:
            out.append("sockets by age: in_flight(<%gs)=%d  retained(>=%gs)=%d" % (
                self.retained_after, len(in_flight),
                self.retained_after, len(retained)))
            rd = len([r for r in retained if r["peer"] == "DEAD"])
            ra = len([r for r in retained if r["peer"] == "ALIVE"])
            out.append("  of retained: peer DEAD=%d  peer ALIVE=%d" % (rd, ra))
        else:
            out.append("sockets by age: (need a second sample -- use --watch)")

        holders = {}
        for r in alive:
            for name in r["peers"]:
                holders[name] = holders.get(name, 0) + 1
        if holders:
            out.append("live peers held by: %s" % ", ".join(
                "%s x%d" % (k, v) for k, v in
                sorted(holders.items(), key=lambda kv: -kv[1])))

        out.append(verdict(len(dead), len(alive)))
        return "\n".join(out)


def verdict(dead, alive):
    """Say what the counts MEAN, so the reply is a diagnosis and not a table."""
    if dead + alive == 0:
        return "verdict: no unix sockets to classify."
    if dead > 10 * max(alive, 1):
        return ("verdict: overwhelmingly peer=DEAD -> completed requests retained "
                "(class A). The teardown fix addresses exactly this; if it is "
                "applied and this count still climbs, the fd has an owner other "
                "than Requests::Request.")
    if alive > 10 * max(dead, 1):
        return ("verdict: overwhelmingly peer=ALIVE -> the producer still holds its "
                "end, so on_finish never ran (class B). No teardown in that branch "
                "can fire; the GC-root/RefPtr cycle has to be broken at the "
                "Response end.")
    return ("verdict: mixed DEAD/ALIVE -> both classes present; fix them "
            "separately and re-census between.")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pid", nargs="?", type=int)
    p.add_argument("--find", metavar="NAME",
                   help="list pids whose comm contains NAME, fd-heaviest first")
    p.add_argument("--watch", type=float, metavar="SECONDS",
                   help="re-census every SECONDS (ages need >= 2 samples)")
    p.add_argument("--retained-after", type=float, default=30.0,
                   help="an fd older than this is retained, not in flight")
    args = p.parse_args(argv)

    if args.find:
        for count, pid, comm in find_pids(args.find):
            print("pid=%-8d fds=%-6d %s" % (pid, count, comm))
        return 0

    if args.pid is None:
        p.error("give a pid, or --find NAME")

    census = Census(args.pid, retained_after=args.retained_after)
    while True:
        try:
            snapshot = census.sample()
        except OSError as e:
            print("cannot read /proc/%d/fd: %s" % (args.pid, e), file=sys.stderr)
            return 1
        print("=== %s pid=%d ===" % (time.strftime("%H:%M:%S"), args.pid))
        print(census.summarize(snapshot))
        if not args.watch:
            return 0
        sys.stdout.flush()
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
