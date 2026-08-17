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
    python3 fd_census.py --all --watch 30       # rank EVERY browser process by growth
    python3 fd_census.py <pid> --build          # which fd fixes are IN this binary

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

  build                          WHICH FIX the running code contains, read out of the
                                 process's own mapped binaries. A leak rate is
                                 uninterpretable without it: "still leaking at
                                 92/min" means "the fix does not work" or "the fix
                                 was not in this build", and those need opposite next
                                 steps. Reports "cannot tell" rather than guessing.

Exit status is 0 for a census, 1 if it could not read the process.
"""

import argparse
import os
import re
import struct
import subprocess
import sys
import time

SOCKET_RE = re.compile(r"^socket:\[(\d+)\]$")

# A peer process holding at most this many of our sockets is the ordinary IPC mesh
# (each pair of browser processes keeps a couple of long-lived connections), not a
# producer sitting on unfinished responses. Ulf's healthy WebContent showed exactly
# 2 each to ladybird, Compositor, RequestServer and ImageDecoder.
IPC_MESH_MAX = 4


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


# The symbols that say which fix a RUNNING browser was built with. Read out of the
# binaries the process has mapped, so the census answers "was the patch in it?"
# instead of asking the person running it.
#
# WHY THIS EXISTS. Ulf reported ~92 leaked fds/min from a WebContent I could not tell
# had my patch in it, and the rate was close enough to the pre-patch 97/min to be
# consistent with EITHER "the patch is not in that binary" or "the patch is
# irrelevant to this leak". Those two demand opposite next moves, and I could not
# separate them, so the next step was going to be a question -- another round trip,
# answered from memory, about a build that had already happened. But the answer is
# not in anyone's memory: it is in the binary, and the binary is still mapped by the
# process being censused. A measurement that reports a leak rate without reporting
# WHICH CODE was running is not reproducible by anyone, including me.
#
# Each entry: symbol -> (what its presence means, what its absence means).
FIX_SYMBOLS = (
    # 0004: closes the response fd where the body is proven complete.
    ("release_response_fd", "0004 (release the response fd on completion)"),
    # 0003 / upstream's equivalent: drops the callbacks so the Request is collectable.
    ("defer_teardown", "0003 (tear down the request when the body is delivered)"),
)

# Present in EVERY build of this library, patched or not. Without it, a "symbol not
# found" result means the symbols were not readable at all (stripped, LTO-inlined,
# statically linked into a binary we did not look at) -- NOT that the fix is missing.
# Reporting "fix absent" for an unreadable binary would be a false negative that
# sends the next round of work in exactly the wrong direction, which is the error
# this whole probe exists to prevent.
CONTROL_SYMBOL = "set_up_internal_stream_data"

# The Request code lives here; when Ladybird is built statically it is in the
# executable instead, so the executable is always probed too.
FIX_LIBRARY_HINT = "lagom-requests"


def elf_symbol_names(path):
    """Every symbol name in an ELF file's string tables, or None if unreadable.

    Pure Python on purpose: this runs on someone else's machine, where `nm` and
    `readelf` are a binutils install I should not require to answer a question the
    file already contains. Reads .dynstr/.strtab wholesale rather than walking symbol
    tables -- the question is only ever "does this name appear", and substring
    matching a string table cannot report a name that is not in the file.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(64)
            if len(header) < 64 or header[:4] != b"\x7fELF":
                return None
            if header[4] != 2:  # not ELF64; nothing here is 32-bit
                return None
            shoff, = struct.unpack_from("<Q", header, 0x28)
            shentsize, shnum, shstrndx = struct.unpack_from("<HHH", header, 0x3A)
            if not shnum or shstrndx >= shnum:
                return None
            f.seek(shoff)
            table = f.read(shentsize * shnum)
            if len(table) < shentsize * shnum:
                return None

            def entry(i):
                off = i * shentsize
                name_off, = struct.unpack_from("<I", table, off)
                sec_off, sec_size = struct.unpack_from("<QQ", table, off + 0x18)
                return name_off, sec_off, sec_size

            _, str_off, str_size = entry(shstrndx)
            f.seek(str_off)
            shstrtab = f.read(str_size)

            blob = b""
            for i in range(shnum):
                name_off, sec_off, sec_size = entry(i)
                end = shstrtab.find(b"\0", name_off)
                name = shstrtab[name_off:end if end >= 0 else None]
                if name in (b".dynstr", b".strtab") and sec_size < 64 * 1024 * 1024:
                    f.seek(sec_off)
                    blob += f.read(sec_size)
            return blob or None
    except (OSError, struct.error, ValueError):
        return None


def mapped_binaries(pid):
    """The executable plus every mapped .so, as real paths we can open."""
    paths = []
    exe = "/proc/%d/exe" % pid
    try:
        paths.append(os.path.realpath(exe))
    except OSError:
        pass
    try:
        with open("/proc/%d/maps" % pid) as f:
            for line in f:
                parts = line.split(None, 5)
                if len(parts) < 6:
                    continue
                path = parts[5].strip()
                if path.startswith("/") and path not in paths:
                    paths.append(path)
    except OSError:
        pass
    return paths


def probe_fixes(pid):
    """Which fd fixes are compiled into the code this pid is RUNNING.

    Returns (findings, note). findings maps a description to True/False; note is set
    when the answer is "cannot tell", so a caller never prints a missing fix it did
    not actually establish is missing.
    """
    candidates = [p for p in mapped_binaries(pid) if FIX_LIBRARY_HINT in p]
    # Statically linked builds (Ulf's is one) have no such library: the code is in
    # the executable. Probe it rather than reporting nothing.
    if not candidates:
        candidates = mapped_binaries(pid)[:1]

    blob = b""
    for path in candidates:
        names = elf_symbol_names(path)
        if names:
            blob += names
    if not blob:
        return {}, ("could not read symbols from this process's binaries "
                    "(no readable ELF among %d mapped paths)" % len(candidates))
    if CONTROL_SYMBOL.encode() not in blob:
        return {}, ("symbols unreadable: the control symbol %s is absent too, so a "
                    "missing fix here would prove nothing (stripped binary, LTO, or "
                    "the code is in a binary not probed)" % CONTROL_SYMBOL)
    return ({desc: (sym.encode() in blob) for sym, desc in FIX_SYMBOLS}, None)


def fix_lines(pid):
    """The build-provenance block: what code is actually running."""
    findings, note = probe_fixes(pid)
    if note:
        return ["build: %s" % note]
    lines = []
    for desc, present in findings.items():
        lines.append("build: %s %s" % ("HAS" if present else "does NOT have", desc))
    if not all(findings.values()):
        lines.append("  -> a leak measured on this binary does not test the missing "
                     "fix. Rebuild with it before concluding the fix does not work.")
    return lines


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


BROWSER_PROCESS_NAMES = ("Ladybird", "ladybird", "WebContent", "RequestServer",
                         "ImageDecoder", "Compositor", "WebWorker")


def find_browser_pids():
    """Every Ladybird-family process, whatever it is called.

    Deliberately NOT just WebContent. Every census I asked Ulf for was of WebContent,
    because that is where I had already decided the leak was -- so a leak in
    RequestServer, the Compositor or the UI process would have been invisible to all
    of them, and "still leaking" is consistent with that. An instrument should not
    inherit my hypothesis: watch every process and let the growth say which one.
    """
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry) as f:
                comm = f.read().strip()
        except OSError:
            continue
        if any(n in comm for n in BROWSER_PROCESS_NAMES):
            out.append((int(entry), comm))
    return sorted(out)


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
        # (timestamp, socket count, dead count) per sample. The RATE is the number
        # that actually matters -- "is it still leaking, and how fast" -- and a
        # single census cannot answer it. Reporting a raw count invites the mistake
        # I already made once: reading a freshly restarted process as a fix.
        self.history = []

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

        socks = [r for r in rows if r["category"] == "socket:"]
        self.history.append((now, len(socks),
                             len([r for r in socks if r["peer"] == "DEAD"])))
        return {"rows": rows, "by_category": by_category, "now": now}

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
            # Every fd was first seen in THIS sample. With --watch that means the
            # process was just attached to, not that the fds are new -- ages are
            # relative to when the census started, and it cannot see further back.
            out.append("sockets by age: all %d first seen this sample (ages start "
                       "now; the next sample will age them)" % len(socks))

        out.extend(self.rate_lines())

        # A live peer is NOT automatically a leak: every process in the browser is
        # connected to its siblings by long-lived IPC sockets, so a healthy
        # WebContent shows ~2 each to ladybird/Compositor/RequestServer/
        # ImageDecoder. Reporting those next to the leak counts invites reading the
        # IPC mesh as evidence. Separate the two: N-of-a-kind at the baseline level
        # is plumbing; a peer holding MANY is a producer that has not let go.
        holders = {}
        for r in alive:
            for name in r["peers"]:
                holders[name] = holders.get(name, 0) + 1
        if holders:
            plumbing = {k: v for k, v in holders.items() if v <= IPC_MESH_MAX}
            suspect = {k: v for k, v in holders.items() if v > IPC_MESH_MAX}
            if suspect:
                out.append("live peers RETAINING many: %s" % ", ".join(
                    "%s x%d" % (k, v) for k, v in
                    sorted(suspect.items(), key=lambda kv: -kv[1])))
            if plumbing:
                out.append("live peers (normal IPC mesh, not the leak): %s" %
                           ", ".join("%s x%d" % (k, v) for k, v in
                                     sorted(plumbing.items(), key=lambda kv: -kv[1])))

        out.append(verdict(len(dead), len(alive)))
        # LAST, next to the verdict, because the verdict is only interpretable
        # together with which code produced it: "still leaking at 92/min" means
        # "the fix does not work" or "the fix was not in the binary" depending on
        # this line alone, and the two need opposite next steps.
        out.extend(fix_lines(self.pid))
        return "\n".join(out)


    def rate_lines(self):
        """Growth since the first sample, as a rate. The decisive measurement.

        Whether a fix works is a question about the SLOPE, not the level: the level
        includes everything leaked before the census started, so a fixed browser
        with 1500 already-leaked fds looks identical to a broken one until you
        watch it.
        """
        if len(self.history) < 2:
            return ["growth: (first sample; the next one gives a rate)"]
        t0, s0, d0 = self.history[0]
        t1, s1, d1 = self.history[-1]
        span = t1 - t0
        if span <= 0:
            return []
        per_min = (s1 - s0) * 60.0 / span
        dead_per_min = (d1 - d0) * 60.0 / span
        lines = ["growth over %.0fs (%d samples): sockets %+d (%+.1f/min), "
                 "of which peer DEAD %+d (%+.1f/min)"
                 % (span, len(self.history), s1 - s0, per_min, d1 - d0,
                    dead_per_min)]
        # No silent middle band. An earlier version called <0.5/min "flat" and only
        # flagged >0.5/min, so a rate of exactly +0.5/min fell through reported as
        # neither -- and +0.5/min is ~720 fds/day, which is precisely the overnight
        # death being investigated. Any positive slope gets named, with the time to
        # the fd limit as the unit that means something.
        if s1 - s0 <= 0:
            lines.append("  -> NOT GROWING in this window. A high count with a flat "
                         "rate is damage already done, not an active leak -- and "
                         "the process must be BUSY for that to mean anything "
                         "(load some pages, then re-census).")
        else:
            worst = max(dead_per_min, per_min)
            to_limit = 1024.0 / worst if worst > 0 else float("inf")
            unit = "min" if to_limit < 120 else "hours"
            eta = to_limit if to_limit < 120 else to_limit / 60.0
            which = ("completed requests (peer DEAD)" if dead_per_min > 0
                     else "sockets (peer still alive)")
            lines.append("  -> STILL LEAKING %s at %.1f/min: a 1024-fd limit in "
                         "~%.0f %s. Slow is not safe; this is the shape that dies "
                         "overnight." % (which, worst, eta, unit))
        return lines


def verdict(dead, alive):
    """Say what the counts MEAN, so the reply is a diagnosis and not a table."""
    if dead + alive == 0:
        return "verdict: no unix sockets to classify."
    # A class with ZERO members is not "present". The ratio tests below both need a
    # 10x majority, so a healthy process (0 dead, ~5 live IPC sockets) fell through
    # to "mixed DEAD/ALIVE -> both classes present" -- naming a class with no members
    # and reading the ordinary IPC mesh as a leak. Observed on a working browser
    # while testing something else, which is the only reason it was caught: a verdict
    # that is wrong on healthy input will be believed when it is wrong on broken
    # input too.
    if dead == 0:
        if alive <= IPC_MESH_MAX + 1:
            return ("verdict: no retained corpses (0 peer=DEAD) and only %d live "
                    "socket(s) -- that is the normal IPC mesh, not a leak. Load "
                    "pages and watch the RATE before concluding anything." % alive)
        return ("verdict: 0 peer=DEAD, %d peer=ALIVE -> nothing completed is being "
                "retained; any leak here is class B (the producer still holds its "
                "end, so on_finish never ran)." % alive)
    if alive == 0:
        return ("verdict: all %d socket(s) peer=DEAD -> completed requests retained "
                "(class A), with no in-flight class B component." % dead)
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


def watch_all(interval, retained_after):
    """Watch every browser process at once and rank them by fd GROWTH.

    This exists because of a mistake worth naming: I spent the whole investigation
    censusing WebContent, since that is where I believed the leak was. If the fds
    accumulate in RequestServer -- which is the process that CREATES the response
    pipes and the cache body files -- then every measurement I requested was blind to
    it, and the leak would keep being reported as "still leaking" while my numbers
    said fixed. Rank by growth, not by level: a process can hold many fds legitimately
    (the IPC mesh) and the slope is what distinguishes a leak.
    """
    censuses = {}
    names = {}
    first = True
    while True:
        for pid, comm in find_browser_pids():
            if pid not in censuses:
                censuses[pid] = Census(pid, retained_after=retained_after)
                names[pid] = comm
            try:
                censuses[pid].sample()
            except OSError:
                continue  # exited under us; its history stays for the report

        print("=== %s (interval %gs) ===" % (time.strftime("%H:%M:%S"), interval))
        rows = []
        for pid, census in censuses.items():
            if not census.history:
                continue
            t0, s0, d0 = census.history[0]
            t1, s1, d1 = census.history[-1]
            total = len(read_fd_targets(pid)) if os.path.isdir("/proc/%d/fd" % pid) else 0
            span = t1 - t0
            rate = (s1 - s0) * 60.0 / span if span > 0 else 0.0
            dead_rate = (d1 - d0) * 60.0 / span if span > 0 else 0.0
            rows.append((rate, dead_rate, pid, names.get(pid, "?"), total, s1, d1,
                         len(census.history)))
        rows.sort(reverse=True)
        for rate, dead_rate, pid, comm, total, socks, dead, n in rows:
            alive = "" if os.path.isdir("/proc/%d/fd" % pid) else "  (EXITED)"
            print("  %-14s pid=%-7d fds=%-5d sockets=%-5d dead=%-5d "
                  "%+.1f/min (dead %+.1f/min, %d samples)%s"
                  % (comm, pid, total, socks, dead, rate, dead_rate, n, alive))
        if first and len(rows):
            print("  (first sample: rates are 0 until the second one)")
        first = False
        leakers = [r for r in rows if r[0] > 0.5]
        if leakers:
            print("  -> GROWING: %s. That process is the one to investigate; the fd "
                  "is accumulating THERE, whatever my hypothesis said."
                  % ", ".join("%s(pid=%d) %+.1f/min" % (r[3], r[2], r[0])
                              for r in leakers))
            # Report the provenance of the growing process only: a rate without the
            # code that produced it cannot distinguish "the fix failed" from "the
            # fix was not in this build".
            for r in leakers:
                for line in fix_lines(r[2]):
                    print("     %s" % line)
        sys.stdout.flush()
        time.sleep(interval)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pid", nargs="?", type=int)
    p.add_argument("--find", metavar="NAME",
                   help="list pids whose comm contains NAME, fd-heaviest first")
    p.add_argument("--watch", type=float, metavar="SECONDS",
                   help="re-census every SECONDS (ages need >= 2 samples)")
    p.add_argument("--all", action="store_true",
                   help="census EVERY Ladybird-family process and rank by growth "
                        "-- use this when you do not already know which process "
                        "leaks (i.e. always, at first)")
    p.add_argument("--retained-after", type=float, default=30.0,
                   help="an fd older than this is retained, not in flight")
    p.add_argument("--build", action="store_true",
                   help="report only which fd fixes are compiled into the running "
                        "process, and exit -- no leak measurement needed")
    args = p.parse_args(argv)

    if args.build:
        if args.pid is None:
            p.error("--build needs a pid")
        for line in fix_lines(args.pid):
            print(line)
        return 0

    if args.find:
        for count, pid, comm in find_pids(args.find):
            print("pid=%-8d fds=%-6d %s" % (pid, count, comm))
        return 0

    if args.all:
        return watch_all(args.watch or 30.0, args.retained_after)

    if args.pid is None:
        p.error("give a pid, --find NAME, or --all")

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
