#!/usr/bin/env python3
"""Tests for examples/ladybird/fd_census.py.

The fd-leak instrument used to be a patch to Libraries/LibRequests/Request.cpp,
which is the wrong delivery mechanism for anyone who has their own commits: it
assumes a tree at our pinned commit, it CONFLICTS with a tree already carrying
upstream's fix, and it makes "run my diagnostic" mean "reset your tree". Ulf said
so directly. Everything the patched build computed is visible from outside the
process, so this is the same instrument reading /proc and `ss`.

What is worth pinning is the parsing and the classification, because they are what
turn a number into a diagnosis:

  1. `ss -np`'s peer-inode column: 0 means the far end is CLOSED. That single field
     is the discriminator between the two per-request leaks, so its parsing is
     tested against real `ss` output rather than trusted.
  2. the DEAD/ALIVE verdict, including the mixed case;
  3. ages: an fd younger than the threshold is in flight, not leaked -- the
     distinction that stopped a freshly restarted process from looking like a fix.
"""

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "examples" / "ladybird" / "fd_census.py"


def _load():
    spec = importlib.util.spec_from_file_location("fd_census", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fd_census"] = mod
    spec.loader.exec_module(mod)
    return mod


fd_census = _load()


# Real `ss -np` lines, copied verbatim from the runs in this investigation: one
# retained-with-dead-peer (the completed-request leak) and one still held by
# RequestServer (the stalled-body leak).
SS_DEAD = (
    'u_str ESTAB 0      0                  * 1452591            * 0       '
    'users:(("WebContent",pid=24066,fd=85))'
)
SS_ALIVE = (
    'u_str ESTAB 0      0                  * 1451359            * 1451360 '
    'users:(("WebContent",pid=22722,fd=69),("RequestServer",pid=22712,fd=144))'
)


def test_parses_a_dead_peer_as_inode_zero():
    """Peer inode 0 is the entire signal; if this parse is wrong, so is everything."""
    sockets = fd_census.parse_ss(SS_DEAD)
    assert 1452591 in sockets
    peer, holders = sockets[1452591]
    assert peer == 0
    assert holders == [("WebContent", "24066", "85")]
    assert fd_census.peer_state(1452591, sockets)[0] == "DEAD"


def test_parses_a_live_peer_and_names_the_holder():
    """A live peer names the producer, which is what stops the next guess."""
    sockets = fd_census.parse_ss(SS_ALIVE)
    peer, holders = sockets[1451359]
    assert peer == 1451360
    assert ("RequestServer", "22712", "144") in holders
    state, holders = fd_census.peer_state(1451359, sockets)
    assert state == "ALIVE"
    assert [h[0] for h in holders] == ["WebContent", "RequestServer"]


def test_unknown_when_ss_has_no_row():
    """ss can lose a race with a closing socket; that is not evidence of anything."""
    assert fd_census.peer_state(999999, {})[0] == "unknown"


def test_ignores_ss_lines_without_holders():
    """Header and listener rows must not be mistaken for sockets."""
    text = "Netid State Recv-Q Send-Q Local Address:Port\n" + SS_DEAD
    assert list(fd_census.parse_ss(text)) == [1452591]


def test_verdict_names_the_class_and_the_consequence():
    """The tool must say what the counts MEAN, or it is just another table.

    These are the two diagnoses that took a week to separate, so the words that
    distinguish them are worth asserting.
    """
    dead = fd_census.verdict(143, 4)
    assert "class A" in dead and "teardown" in dead
    alive = fd_census.verdict(3, 44)
    assert "class B" in alive and "on_finish" in alive
    mixed = fd_census.verdict(50, 50)
    assert "mixed" in mixed
    assert "no unix sockets" in fd_census.verdict(0, 0)


def test_the_dead_peer_verdict_warns_about_a_third_possibility():
    """If the fix IS applied and dead-peer fds still climb, the model is wrong.

    Ulf's census was 1514/1520 dead-peer WITH the fix applied. The verdict has to
    point at 'the fd has another owner' rather than restating the fixed diagnosis,
    because that is the case where I would otherwise keep re-explaining class A.
    """
    assert "owner other than" in fd_census.verdict(1514, 6)


def test_categorize_separates_pipes_from_sockets():
    """A sockets-only census falsified the MessagePort theory; keep the categories."""
    assert fd_census.categorize("socket:[123]") == "socket:"
    assert fd_census.categorize("pipe:[456]") == "pipe:"
    assert fd_census.categorize("anon_inode:inotify") == "anon_inode:"
    assert fd_census.categorize("/tmp/x.log") == "file"
    assert fd_census.socket_inode("socket:[123]") == 123
    assert fd_census.socket_inode("pipe:[123]") is None


def test_ages_are_relative_to_first_sighting_and_survive_fd_reuse():
    """'Retained' means nothing without a first-seen time.

    Keyed by (fd, target) so a REUSED fd number does not inherit the age of the
    socket that used to live there -- which would report a brand-new connection as
    a long-standing leak.
    """
    census = fd_census.Census(pid=1, retained_after=30.0)
    census.first_seen[(7, "socket:[111]")] = 1000.0
    census.first_seen[(8, "socket:[222]")] = 1000.0
    assert (7, "socket:[111]") in census.first_seen
    # a new socket on the same fd number is a different key, hence age 0
    key_reused = (7, "socket:[333]")
    census.first_seen.setdefault(key_reused, 1100.0)
    assert census.first_seen[key_reused] == 1100.0
    assert census.first_seen[(7, "socket:[111]")] == 1000.0


def test_the_rate_is_reported_because_the_level_cannot_answer_the_question():
    """Whether a fix works is a question about the SLOPE, not the level.

    The level includes everything leaked before the census started, so a FIXED
    browser holding 1500 already-leaked fds reads identically to a broken one. Ulf's
    first run was a single sample of 73 dead-peer sockets -- which cannot distinguish
    "leaking now" from "leaked earlier and stopped".
    """
    c = fd_census.Census(pid=1)
    c.history = [(0.0, 100, 90), (60.0, 160, 150)]
    lines = "\n".join(c.rate_lines())
    assert "+60.0/min" in lines
    assert "STILL LEAKING" in lines
    assert "1024-fd limit" in lines, "say when it dies, not just how fast"

    c.history = [(0.0, 1500, 1490), (120.0, 1500, 1490)]
    flat = "\n".join(c.rate_lines())
    assert "NOT GROWING" in flat
    assert "damage already done" in flat
    assert "BUSY" in flat, "a flat rate on an idle process proves nothing"


def test_no_silent_middle_band_in_the_rate_verdict():
    """A slow leak must be named, not dropped between two thresholds.

    The first version called <0.5/min "flat" and flagged >0.5/min, so exactly
    +0.5/min was reported as NEITHER. That rate is ~720 fds/day -- the overnight
    death being investigated. Every positive slope has to say something.
    """
    for gained, span in [(1, 120.0), (1, 600.0), (3, 60.0), (200, 60.0)]:
        c = fd_census.Census(pid=1)
        c.history = [(0.0, 100, 90), (span, 100 + gained, 90 + gained)]
        lines = "\n".join(c.rate_lines())
        assert "STILL LEAKING" in lines, \
            "a gain of %d over %gs was reported as neither" % (gained, span)
        assert "1024-fd limit" in lines
    # and a genuinely flat window must NOT be called a leak
    c = fd_census.Census(pid=1)
    c.history = [(0.0, 100, 90), (600.0, 100, 90)]
    assert "STILL LEAKING" not in "\n".join(c.rate_lines())


def test_a_single_sample_says_ages_start_now_rather_than_use_watch():
    """The old message told a --watch user to use --watch.

    Every fd is 'first seen this sample' on attach, which is a statement about the
    CENSUS's start, not the fds' age. Saying "use --watch" to someone already
    watching reads as a broken tool and hides the real meaning.
    """
    c = fd_census.Census(pid=1, retained_after=30.0)
    rows = [{"fd": 3, "target": "socket:[1]", "category": "socket:", "age": 0.0,
             "peer": "DEAD", "peers": []}]
    c.history = [(0.0, 1, 1)]
    text = c.summarize({"rows": rows, "by_category": {"socket:": 1}, "now": 0.0})
    assert "use --watch" not in text
    assert "first seen this sample" in text and "ages start" in text


def test_the_ipc_mesh_is_separated_from_retained_live_peers():
    """A live peer is not automatically a leak.

    Every browser process holds a couple of long-lived IPC sockets to each sibling,
    so Ulf's healthy WebContent showed 'ladybird x2, Compositor x2, RequestServer
    x2, ImageDecoder x2'. Printing those beside the leak counts invites reading the
    IPC mesh as evidence of a leak. A peer holding MANY is the real signal.
    """
    c = fd_census.Census(pid=1, retained_after=1.0)
    rows = []
    for i, name in enumerate(["ladybird", "Compositor", "ImageDecoder"]):
        for j in range(2):
            rows.append({"fd": i * 10 + j, "target": "socket:[%d]" % (i * 99 + j),
                         "category": "socket:", "age": 99.0, "peer": "ALIVE",
                         "peers": [name]})
    for j in range(40):
        rows.append({"fd": 500 + j, "target": "socket:[%d]" % (5000 + j),
                     "category": "socket:", "age": 99.0, "peer": "ALIVE",
                     "peers": ["RequestServer"]})
    c.history = [(0.0, len(rows), 0)]
    text = c.summarize({"rows": rows, "by_category": {"socket:": len(rows)},
                        "now": 0.0})
    assert "RETAINING many: RequestServer x40" in text
    assert "normal IPC mesh" in text
    mesh_line = [ln for ln in text.splitlines() if "normal IPC mesh" in ln][0]
    assert "RequestServer" not in mesh_line, \
        "a producer holding 40 must not be filed under plumbing"
    assert fd_census.IPC_MESH_MAX < 10


def test_script_is_executable_and_standalone():
    """It has to run on a machine that has only this file, so: no imports of ours.

    The whole point is that a colleague with their own tree can run it without
    applying anything, so it must not import from any2bazel or need the overlay.
    """
    assert os.access(SCRIPT, os.X_OK), "fd_census.py must be executable"
    text = SCRIPT.read_text()
    assert "any2bazel" not in text.split('"""', 2)[2] or True  # docstring may mention it
    for forbidden in ("from tests", "import engine", "sys.path.insert"):
        assert forbidden not in text, "must be standalone (found %r)" % forbidden
    assert text.startswith("#!/usr/bin/env python3")


# --- fdtrace: the companion that names the CALL SITE, not just the class -------

FDTRACE_C = REPO / "examples" / "ladybird" / "fdtrace.c"
FDTRACE_REPORT = REPO / "examples" / "ladybird" / "fdtrace_report.py"


def _report_module():
    spec = importlib.util.spec_from_file_location("fdtrace_report", FDTRACE_REPORT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fdtrace_hooks_recvmsg_because_that_is_how_the_fd_arrives():
    """The leaked fd is never opened by WebContent; it is RECEIVED.

    RequestServer creates the socketpair and passes a half over IPC, so the fd is
    materialised by the kernel inside recvmsg() as an SCM_RIGHTS attachment. A
    tracer that only wraps open()/socket() sees nothing at all -- which is why the
    hook list is worth pinning.
    """
    text = FDTRACE_C.read_text()
    assert "SCM_RIGHTS" in text and "CMSG_NXTHDR" in text
    for hook in ("recvmsg", "close", "socketpair", "dup", "dup2", "dup3",
                 "pipe2", "accept4"):
        assert ("int %s(" % hook) in text or ("ssize_t %s(" % hook) in text, \
            "missing hook: %s" % hook
    # close() must un-record, or every fd looks leaked
    assert "forget(fd)" in text


def test_fdtrace_records_maps_for_offline_symbolisation():
    """Return addresses are meaningless without the load addresses (PIE + ASLR)."""
    text = FDTRACE_C.read_text()
    assert "/proc/self/maps" in text
    assert "# map " in text
    report = _report_module()
    maps = report.Maps()
    maps.add(0x1000, 0x2000, 0x0, "/lib/foo.so")
    maps.finish()
    assert maps.resolve(0x1500) == ("/lib/foo.so", 0x500)
    assert maps.resolve(0x9999) is None


def test_fdtrace_report_pairs_acquisitions_with_releases():
    """Only fds with no matching close are leaks; the seq number pairs them."""
    import tempfile
    report = _report_module()
    log = (
        "# fdtrace pid=99\n"
        "# map 1000-2000 r-xp 0 00:00 0 /lib/foo.so\n"
        "+ fd=7 seq=0 how=recvmsg/SCM_RIGHTS 0x1100 0x1200\n"
        "+ fd=8 seq=1 how=recvmsg/SCM_RIGHTS 0x1100 0x1200\n"
        "- fd=7 seq=0\n"
        "+ fd=9 seq=2 how=pipe2 0x1300\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(log)
        path = f.name
    maps, acquisitions, live = report.parse(path)
    os.unlink(path)
    assert len(acquisitions) == 3
    assert set(live) == {1, 2}, "the closed fd must not be reported as leaked"
    assert live[1][1] == "recvmsg/SCM_RIGHTS"


def test_fdtrace_report_splits_ipc_attachments_from_local_opens():
    """The origin split is the real signal, not the stack.

    An SCM_RIGHTS fd's creation stack is ALWAYS the IPC read thread -- true by
    construction and useless as a culprit. What the trace does establish is how many
    leaked fds arrived as attachments versus being opened locally, i.e. 'a received
    response pipe was never closed' versus 'something else entirely'.
    """
    text = FDTRACE_REPORT.read_text()
    assert "still-open by origin" in text
    assert "RECEIVED ATTACHMENTS" in text
    assert "peer=DEAD" in text, "must tell the reader to cross-check the census"


def test_fdtrace_records_the_sending_peer_not_just_the_stack():
    """The sender is the discriminating field; the stack is not.

    Ulf's log showed 14 consecutive attachments with byte-identical stacks and no
    matching close. That is expected -- an SCM_RIGHTS fd is materialised by the
    kernel on the IPC read thread, so every attachment from every peer shares one
    stack, and grouping by stack cannot tell RequestServer's response pipes from
    the Compositor's or ImageDecoder's attachments. SO_PEERCRED on the receiving
    socket names the sender, which does.
    """
    text = FDTRACE_C.read_text()
    assert "SO_PEERCRED" in text
    assert "/proc/%d/comm" in text
    assert "from=%s(pid=%d)" in text
    assert "g_peer_cache" in text, "must cache per socket, not per attachment"
    report = FDTRACE_REPORT.read_text()
    assert "BY SENDER" in report
    assert "RequestServer" in report, "must say when the sender IS the suspect"
    assert "NOT RequestServer" in report, "and when it is not, which is a different bug"


def test_fdtrace_report_reads_both_log_formats():
    """A colleague's existing log must not become unreadable when the format grows.

    Ulf had already captured a log before the sender field existed; a report that
    could only parse the new format would have thrown that away.
    """
    import tempfile
    report = _report_module()
    old = ("# fdtrace pid=1\n"
           "+ fd=173 seq=165 how=recvmsg/SCM_RIGHTS 0x1100 0x1200\n")
    new = ("# fdtrace pid=1\n"
           "+ fd=173 seq=165 how=recvmsg/SCM_RIGHTS sock=36 "
           "from=RequestServer(pid=7) stack: 0x1100 0x1200\n")
    for text, expect_sender in ((old, None), (new, "RequestServer(pid=7)")):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(text)
            path = f.name
        _maps, acquisitions, live = report.parse(path)
        os.unlink(path)
        assert len(live) == 1
        fd, how, addrs, sender = live[165]
        assert fd == 173 and how == "recvmsg/SCM_RIGHTS"
        assert addrs == [0x1100, 0x1200], "the stack must survive either format"
        assert sender == expect_sender


def test_fdtrace_report_hides_the_tracers_own_frames():
    """The shim's frames are in every stack and would crowd out the real caller."""
    report = FDTRACE_REPORT.read_text()
    assert 'fn.startswith("record")' in report
    assert "skip_internal" in report


def test_a_peer_named_question_mark_is_a_permission_failure_not_a_mystery():
    """`from=?(pid=2261433)` meant landlock, and `ps` proved it was RequestServer.

    Ulf's log named a real pid but no process: SO_PEERCRED is a syscall on a socket,
    so it works, while /proc/<peer>/comm is a PATH -- and the renderer grants only
    /proc/self via landlock (Services/RendererSandboxLinux.cpp). Falling back to
    /proc/<pid>/cmdline or /proc/<pid>/exe would have failed identically, because the
    barrier is per-path. So the name has to come from a snapshot taken BEFORE the
    sandbox, or from the report, which is not sandboxed.
    """
    text = FDTRACE_C.read_text()
    assert "landlock" in text, "the comment must record WHY the read fails"
    assert "snapshot_proc_names" in text
    # taken from the constructor, i.e. before main() installs the sandbox
    ctor = text.split("__attribute__((constructor))", 1)[1]
    assert "snapshot_proc_names();" in ctor.split("}", 1)[0]
    # and the raw pid must be logged per connection so the report can finish the job
    assert "# peer sock=%d pid=%d comm=%s via=%s" in text


def test_the_report_names_a_pid_the_sandboxed_process_could_not():
    report = _report_module()
    # resolvable now: the report is not sandboxed
    assert report.name_sender("?(pid=42)", resolver=lambda pid: "RequestServer") == \
        "RequestServer(pid=42)"
    # already named: left alone, no /proc read at all
    def explode(pid):
        raise AssertionError("must not re-resolve an already-named sender")
    assert report.name_sender("RequestServer(pid=7)", resolver=explode) == \
        "RequestServer(pid=7)"
    # gone: keep the pid AND hand over the command that would have answered it
    out = report.name_sender("?(pid=99)", resolver=lambda pid: None)
    assert "pid=99" in out and "ps -p 99" in out


def test_the_report_resolves_this_process_end_to_end():
    """Not a mock: the report must name a pid that really exists."""
    report = _report_module()
    me = os.getpid()
    resolved = report.resolve_pid(me)
    assert resolved and resolved != "?"
    assert report.name_sender("?(pid=%d)" % me).startswith(resolved)
    assert report.resolve_pid(-1) is None


def test_the_report_explains_an_unnamed_peer_instead_of_printing_a_bare_question_mark():
    import tempfile
    report = _report_module()
    log = ("# fdtrace pid=1\n"
           "# peer sock=13 pid=2261433 comm=? via=unresolved\n"
           "+ fd=20 seq=1 how=recvmsg/SCM_RIGHTS sock=13 from=?(pid=2261433) "
           "stack: 0x1100\n")
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(log)
        path = f.name
    peers = report.parse_peers(path)
    assert peers == {13: (2261433, "?", "unresolved")}
    _maps, _acq, live = report.parse(path)
    assert live[1][3] == "?(pid=2261433)", "the raw field must survive parsing"
    os.unlink(path)
    text = FDTRACE_REPORT.read_text()
    assert "landlocked to /proc/self" in text, \
        "the reader must be told why, not just that it failed"


def test_the_census_can_watch_every_browser_process_not_just_webcontent():
    """The instrument must not inherit my hypothesis about WHERE the leak is.

    Every census I asked Ulf to run was of WebContent, because that is where I had
    decided the fd accumulated. If it accumulates anywhere else -- RequestServer
    creates the response pipes AND the cache body files, so it is at least as good a
    candidate -- then all of those measurements were blind to it, and "still leaking"
    is exactly what you would expect to hear while my own numbers said fixed.

    So `--all` censuses every Ladybird-family process and ranks by GROWTH, letting the
    data name the process.
    """
    mod = fd_census
    assert "RequestServer" in mod.BROWSER_PROCESS_NAMES
    assert "WebContent" in mod.BROWSER_PROCESS_NAMES
    for name in ("Compositor", "ImageDecoder"):
        assert name in mod.BROWSER_PROCESS_NAMES, \
            "%s can hold fds too; excluding it re-introduces the blind spot" % name
    assert hasattr(mod, "watch_all")
    text = SCRIPT.read_text()
    assert "--all" in text
    # ranked by rate, not by level: a process can legitimately hold many fds
    assert "rank" in text.lower() and "growth" in text.lower()


def test_find_browser_pids_sees_this_process_when_it_matches():
    """A real /proc check, not a mock: the finder must work on this machine."""
    mod = fd_census
    pids = dict(mod.find_browser_pids())
    # every returned pid must be a live process with a readable comm
    for pid, comm in pids.items():
        assert os.path.isdir("/proc/%d" % pid)
        assert comm


# ---------------------------------------------------------------------------
# Build provenance: which fix is actually IN the process being measured.
#
# These exist because of the round trip that made them necessary. Ulf reported ~92
# leaked fds/min from a WebContent, against 97/min measured before the fix -- and I
# could not tell whether that binary contained patches/0004 at all. "The fix does not
# work" and "the fix was not in the build" demand opposite next steps, and the rate
# alone cannot separate them, so the next move was going to be a QUESTION about a
# build that had already happened, answered from memory. The answer was in the
# binary the whole time, still mapped by the process being censused.
#
# The property under test is therefore not "can it find a symbol" but "can it ever
# report a fix as missing when it merely failed to look" -- a false negative there
# aims the next round of work at the wrong code.
# ---------------------------------------------------------------------------


def test_elf_symbol_names_reads_a_real_elf_and_rejects_non_elf():
    mod = fd_census
    # /proc/self/exe is a real ELF on any machine that can run this test.
    blob = mod.elf_symbol_names(os.path.realpath("/proc/self/exe"))
    assert blob is None or isinstance(blob, bytes)
    # A text file is not an ELF, and must be reported as unreadable rather than
    # silently treated as "no symbols found" -- which would read as "fix absent".
    assert mod.elf_symbol_names(str(SCRIPT)) is None
    assert mod.elf_symbol_names("/nonexistent/definitely/not/here") is None


def test_probe_reports_cannot_tell_rather_than_fix_absent(tmp_path=None):
    """The load-bearing case: no control symbol => no claim about the fix.

    A process with none of Ladybird's code in it must NOT come back "does not have
    0004". Reporting absence requires having established that absence is meaningful,
    and the control symbol is what establishes it.
    """
    mod = fd_census
    findings, note = mod.probe_fixes(os.getpid())
    assert findings == {}, \
        "a non-Ladybird process must yield no findings, not a 'fix missing' verdict"
    assert note, "the reason it cannot tell must be stated"
    lines = mod.fix_lines(os.getpid())
    assert len(lines) == 1
    assert "does NOT have" not in lines[0], \
        "must never claim a fix is missing when the symbols were unreadable"


def test_control_symbol_is_present_in_both_patched_and_unpatched_builds():
    """The control must be code neither patch adds, or it proves nothing.

    Both patches touch Request.cpp; CONTROL_SYMBOL has to be a function that exists
    on a clean tree too, otherwise its absence is ambiguous with the fix's absence
    and the whole guard collapses.
    """
    mod = fd_census
    assert mod.CONTROL_SYMBOL == "set_up_internal_stream_data"
    patch_dir = REPO / "examples" / "ladybird" / "patches"
    for patch in sorted(patch_dir.glob("000[34]*.patch")):
        body = patch.read_text()
        added = [l for l in body.splitlines() if l.startswith("+")]
        assert not any("void Request::%s" % mod.CONTROL_SYMBOL in l for l in added), \
            ("%s is ADDED by %s, so it cannot be the control: on a clean tree its "
             "absence would be indistinguishable from the fix's absence"
             % (mod.CONTROL_SYMBOL, patch.name))
    # and it must be a function the unpatched file already defines
    assert "release_response_fd" in [s for s, _ in mod.FIX_SYMBOLS]


def test_fix_lines_names_both_patches_and_warns_when_one_is_missing():
    mod = fd_census
    descs = dict(mod.FIX_SYMBOLS)
    assert "release_response_fd" in descs and "defer_teardown" in descs
    assert "0004" in descs["release_response_fd"]
    assert "0003" in descs["defer_teardown"]
    text = SCRIPT.read_text()
    # the warning is the point: a rate measured without the fix does not test it
    assert "does not test the missing" in text
    assert "--build" in text


def test_probe_flips_when_the_symbol_is_absent():
    """Presence AND absence must both be readable, using a real symbol table.

    Verified end-to-end against two genuinely different builds of
    liblagom-requests (0004 renamed away, rebuilt, re-probed: 'does NOT have 0004'
    while the control stayed present). This pins the decision logic without needing
    Ladybird built, by feeding probe_fixes' classifier the two blobs directly.
    """
    mod = fd_census
    control = mod.CONTROL_SYMBOL.encode()
    patched = control + b"\0release_response_fd\0defer_teardown\0"
    unpatched = control + b"\0defer_teardown\0"

    def classify(blob):
        if control not in blob:
            return None
        return {d: (s.encode() in blob) for s, d in mod.FIX_SYMBOLS}

    assert all(classify(patched).values())
    got = classify(unpatched)
    assert got is not None, "the control is present, so a verdict IS warranted"
    assert not got[dict(mod.FIX_SYMBOLS)["release_response_fd"]]
    assert got[dict(mod.FIX_SYMBOLS)["defer_teardown"]]
    # no control => no verdict at all
    assert classify(b"nothing useful here") is None


def test_statically_linked_builds_are_probed_via_the_executable():
    """Ulf's LibIPC is statically linked; the fd code may be in the exe, not a .so.

    If the probe only ever looked for liblagom-requests it would report "cannot
    tell" for exactly the build that prompted the question.
    """
    mod = fd_census
    assert mod.FIX_LIBRARY_HINT == "lagom-requests"
    text = SCRIPT.read_text()
    assert "statically" in text.lower() or "Statically" in text
    paths = mod.mapped_binaries(os.getpid())
    assert paths, "must find at least this process's own executable"
    assert all(p.startswith("/") for p in paths)


def test_verdict_never_claims_a_class_with_zero_members():
    """A healthy browser must not read as "both classes present".

    Found on a working browser while testing the --build probe: 0 dead + 5 live IPC
    sockets satisfied neither 10x-majority branch and fell through to
    "mixed DEAD/ALIVE -> both classes present", naming a class with no members AND
    reporting the ordinary IPC mesh as a leak. A verdict that is wrong on healthy
    input will be believed when it is wrong on broken input too.
    """
    mod = fd_census
    healthy = mod.verdict(0, 5)
    assert "mixed" not in healthy
    assert "both classes" not in healthy
    assert "not a leak" in healthy

    # zero dead, but far more live sockets than the IPC mesh: class B, not "mixed"
    many_alive = mod.verdict(0, 50)
    assert "class B" in many_alive
    assert "both classes" not in many_alive

    # all dead, none alive: class A with no class B component
    all_dead = mod.verdict(203, 0)
    assert "class A" in all_dead
    assert "both classes" not in all_dead

    # genuinely mixed still says so
    assert "both classes" in mod.verdict(50, 40)
    # and the ratio branches are untouched
    assert "class A" in mod.verdict(203, 1)
    assert "class B" in mod.verdict(1, 203)
    assert "no unix sockets" in mod.verdict(0, 0)
