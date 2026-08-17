#!/usr/bin/env python3
"""Turn an fdtrace log into a ranked list of the call sites that leak fds.

Companion to fdtrace.c. Reads the `+ fd=... <addrs>` / `- fd=...` log, keeps the
acquisitions with no matching release, groups them by identical stack, and
symbolises each group's stack with addr2line -- so the output is "these 815 fds
were all created here", which is the question `fd_census.py` cannot answer.

    python3 fdtrace_report.py /tmp/fdtrace.12345.log
    python3 fdtrace_report.py /tmp/fdtrace.12345.log --top 5 --frames 12

Why offline: symbolising in-process would perturb the timing being measured, and
the addresses are only meaningful together with the /proc/self/maps lines fdtrace
records at startup (PIE + ASLR move everything per run).
"""

import argparse
import bisect
import collections
import os
import re
import subprocess
import sys

MAP_RE = re.compile(
    r"^# map ([0-9a-f]+)-([0-9a-f]+) \S+ ([0-9a-f]+) \S+ \d+\s+(/.*\S)\s*$")
PEER_RE = re.compile(
    r"^# peer sock=(-?\d+) pid=(-?\d+) comm=(\S*) via=(\S+)\s*$")


def resolve_pid(pid):
    """Name a pid from OUTSIDE the sandboxed process, which can read /proc.

    A renderer's landlock policy grants only /proc/self, so the tracer inside it
    cannot read a sibling's /proc/<pid>/comm and logs `?`. The report does not run
    under that policy, so it can finish the job -- as long as the process is still
    alive, which it usually is: RequestServer outlives the WebContent it serves.
    """
    if pid is None or pid < 0:
        return None
    for name, transform in (("comm", lambda t: t.strip()),
                            ("cmdline", lambda t: os.path.basename(
                                t.split("\0")[0]) if t.strip("\0") else "")):
        try:
            with open("/proc/%d/%s" % (pid, name)) as f:
                value = transform(f.read())
        except OSError:
            continue
        if value:
            return value
    try:
        return os.path.basename(os.readlink("/proc/%d/exe" % pid))
    except OSError:
        return None


class Maps:
    """Address -> (object file, file-relative offset), from the recorded maps."""

    def __init__(self):
        self.starts = []
        self.entries = []

    def add(self, start, end, offset, path):
        self.starts.append(start)
        self.entries.append((start, end, offset, path))

    def finish(self):
        order = sorted(range(len(self.starts)), key=lambda i: self.starts[i])
        self.starts = [self.starts[i] for i in order]
        self.entries = [self.entries[i] for i in order]

    def resolve(self, addr):
        i = bisect.bisect_right(self.starts, addr) - 1
        if i < 0:
            return None
        start, end, offset, path = self.entries[i]
        if not (start <= addr < end):
            return None
        # addr2line on a shared object wants the offset within the FILE
        return path, addr - start + offset


SENDER_RE = re.compile(r"^(?P<name>.*)\(pid=(?P<pid>-?\d+)\)$")


def parse_peers(path):
    """{sock: (pid, comm, via)} from the `# peer` header lines, if present.

    `via=snapshot` means the tracer could not read /proc at attachment time (the
    renderer's sandbox) and fell back to its pre-sandbox snapshot; `via=unresolved`
    means even that missed and only the pid is known.
    """
    peers = {}
    with open(path) as f:
        for line in f:
            if not line.startswith("# peer "):
                continue
            m = PEER_RE.match(line.rstrip("\n"))
            if m:
                peers[int(m.group(1))] = (int(m.group(2)), m.group(3), m.group(4))
    return peers


def name_sender(sender, resolver=resolve_pid):
    """Turn a logged `NAME(pid=N)` into the best name available NOW.

    The tracer logs `?` when the sandbox denied /proc/<pid>/comm. That is a
    permission failure, not an unknown process, and it is recoverable here because
    the report is not sandboxed -- so an unnamed sender gets one more chance from
    outside, and if the process is gone the reader is told the exact command that
    would have answered it rather than being left with a bare `?`.
    """
    if not sender:
        return sender
    m = SENDER_RE.match(sender)
    if not m:
        return sender
    name, pid = m.group("name"), int(m.group("pid"))
    if name and name != "?":
        return sender
    resolved = resolver(pid)
    if resolved:
        return "%s(pid=%d)" % (resolved, pid)
    return "?(pid=%d, gone -- was `ps -p %d -o comm=`)" % (pid, pid)


def parse(path):
    maps = Maps()
    open_fds = {}          # fd -> (seq, how, [addrs])
    acquisitions = {}      # seq -> (fd, how, [addrs])
    closed = set()
    with open(path) as f:
        for line in f:
            if line.startswith("# map "):
                m = MAP_RE.match(line.rstrip("\n"))
                if m:
                    maps.add(int(m.group(1), 16), int(m.group(2), 16),
                             int(m.group(3), 16), m.group(4))
                continue
            if line.startswith("+ "):
                parts = line.split()
                fd = int(parts[1].split("=")[1])
                seq = int(parts[2].split("=")[1])
                how = parts[3].split("=")[1]
                sender = None
                rest = parts[4:]
                # `from=NAME(pid=N)` when the fd arrived as an IPC attachment. Old
                # logs have no such field and no `stack:` marker; still readable.
                for tok in list(rest):
                    if tok.startswith("from="):
                        sender = tok[len("from="):]
                    if tok == "stack:":
                        rest = rest[rest.index(tok) + 1:]
                        break
                else:
                    rest = [t for t in rest if t.startswith("0x")]
                addrs = [int(a, 16) for a in rest if a.startswith("0x")]
                open_fds[fd] = seq
                acquisitions[seq] = (fd, how, addrs, sender)
            elif line.startswith("- "):
                parts = line.split()
                seq = int(parts[2].split("=")[1])
                closed.add(seq)
    maps.finish()
    live = {seq: v for seq, v in acquisitions.items() if seq not in closed}
    return maps, acquisitions, live


def symbolize(maps, addrs, frames, skip_internal=True):
    """addr2line per object file, batched. Missing tools degrade to raw addresses."""
    by_obj = collections.defaultdict(list)
    order = []
    for a in addrs[:frames + 4]:
        r = maps.resolve(a)
        if r is None:
            order.append((None, a))
            continue
        path, off = r
        by_obj[path].append(off)
        order.append((path, off))

    resolved = {}
    for path, offs in by_obj.items():
        if not os.path.exists(path):
            continue
        try:
            out = subprocess.run(
                ["addr2line", "-f", "-C", "-e", path] + ["0x%x" % o for o in offs],
                capture_output=True, text=True, timeout=60).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            continue
        # addr2line -f emits function then file:line, per address
        for i, off in enumerate(offs):
            fn = out[2 * i] if 2 * i < len(out) else "??"
            loc = out[2 * i + 1] if 2 * i + 1 < len(out) else "??"
            resolved[(path, off)] = (fn, loc)

    lines = []
    for key in order:
        path, off = key
        if path is None:
            lines.append("    0x%x (unmapped)" % off)
            continue
        fn, loc = resolved.get((path, off), ("??", "??"))
        # The tracer's own frames are noise: they are in every stack by
        # construction and push the real caller off the end of --frames.
        if skip_internal and (fn.startswith("fdtrace") or fn.startswith("record")
                              or fn in ("recvmsg", "close", "socketpair", "forget",
                                        "peer_of", "dup", "dup2", "dup3", "pipe",
                                        "pipe2", "socket", "accept", "accept4")):
            continue
        short = os.path.basename(path)
        if fn == "??" and loc == "??":
            lines.append("    0x%x in %s" % (off, short))
        else:
            lines.append("    %s  (%s)" % (fn, loc))
        if len(lines) >= frames:
            break
    return lines


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("log")
    p.add_argument("--top", type=int, default=8,
                   help="how many leaking call sites to show")
    p.add_argument("--frames", type=int, default=14,
                   help="stack frames per call site")
    args = p.parse_args(argv)

    maps, acquisitions, live = parse(args.log)
    if not acquisitions:
        print("no fd acquisitions in %s -- was LD_PRELOAD actually in effect?"
              % args.log, file=sys.stderr)
        return 1

    groups = collections.defaultdict(list)
    senders = collections.defaultdict(list)
    # Cache per sender string: a `?` costs a /proc read, and there are thousands of
    # leaked fds sharing a handful of connections.
    named = {}
    for seq, (fd, how, addrs, sender) in live.items():
        groups[(how, tuple(addrs))].append((seq, fd))
        if sender:
            if sender not in named:
                named[sender] = name_sender(sender)
            senders[named[sender]].append(fd)

    print("fdtrace: %d acquisitions, %d still open, %d distinct call sites"
          % (len(acquisitions), len(live), len(groups)))
    print("(still open = acquired and never closed, by THIS process)")
    print()

    # An SCM_RIGHTS fd is CREATED BY THE KERNEL on the IPC read thread, so its
    # acquisition stack is always TransportSocket::io_thread_loop -- true and
    # useless on its own. What it does establish, precisely, is HOW MANY leaked fds
    # entered as IPC attachments versus being opened locally, which is the split
    # between "a received response pipe was never closed" and "something else".
    # WHO SENT the leaked attachments. This is the discriminating field, because
    # every attachment shares one acquisition stack (the IPC read thread) and so the
    # stacks cannot tell two subsystems apart. A response pipe from RequestServer is
    # the fd under investigation; one from Compositor or ImageDecoder is not.
    if senders:
        # If the tracer could not name a peer itself, say so once and say why --
        # otherwise a `?` reads as "unknown process" when it actually means "the
        # renderer's landlock policy grants /proc/self only".
        unresolved = [(sock, pid, via) for sock, (pid, _comm, via)
                      in sorted(parse_peers(args.log).items())
                      if via in ("snapshot", "unresolved")]
        if unresolved:
            print("note: %d connection(s) could not be named INSIDE the process "
                  "(/proc is" % len(unresolved))
            print("      landlocked to /proc/self in the renderer); resolved here "
                  "from outside instead.")
            for sock, pid, via in unresolved:
                print("      sock=%d pid=%d (%s)" % (sock, pid, via))
            print()
        print("still-open IPC attachments BY SENDER:")
        for name, fds in sorted(senders.items(), key=lambda kv: -len(kv[1])):
            print("  %6d from %s   e.g. fd %s" % (
                len(fds), name, ", ".join(str(f) for f in sorted(fds)[:6])))
        top, top_fds = max(senders.items(), key=lambda kv: len(kv[1]))
        if "RequestServer" in top:
            print("  -> the leaked fds are RESPONSE PIPES from RequestServer: the")
            print("     class this investigation is about. Every one is a request")
            print("     whose response fd WebContent decoded and never closed.")
        else:
            print("  -> NOT RequestServer. The leak is attachments from %s, which is"
                  % top.split("(")[0])
            print("     a different bug from the per-request response-pipe leak --")
            print("     look at what decodes IPC::File from that peer.")
        print()

    ipc = sum(len(v) for (how, _), v in groups.items() if how.startswith("recvmsg"))
    local = len(live) - ipc
    print("still-open by origin: %d arrived over IPC (SCM_RIGHTS), %d opened locally"
          % (ipc, local))
    if ipc > 10 * max(local, 1):
        print("  -> the leaked fds are RECEIVED ATTACHMENTS. The creation stack is")
        print("     the IPC read thread by construction; the bug is on the RECEIVING")
        print("     side -- an attachment decoded into an owner that never closes it.")
        print("     Cross-check with fd_census.py: peer=DEAD means the sender is")
        print("     already gone, so nothing but this process can still close them.")
    print()

    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    for (how, addrs), holders in ranked[:args.top]:
        fds = sorted(f for _, f in holders)
        print("%d still-open fds via %s   e.g. fd %s" % (
            len(holders), how, ", ".join(str(f) for f in fds[:6])))
        for line in symbolize(maps, list(addrs), args.frames):
            print(line)
        print()

    if len(ranked) > args.top:
        print("... %d more call sites" % (len(ranked) - args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
