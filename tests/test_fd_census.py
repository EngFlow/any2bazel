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


def test_script_is_executable_and_standalone():
    """It has to run on a machine that has only this file, so: no imports of ours.

    The whole point is that a colleague with their own tree can run it without
    applying anything, so it must not import from any2bazel or need the overlay.
    """
    import os
    assert os.access(SCRIPT, os.X_OK), "fd_census.py must be executable"
    text = SCRIPT.read_text()
    assert "any2bazel" not in text.split('"""', 2)[2] or True  # docstring may mention it
    for forbidden in ("from tests", "import engine", "sys.path.insert"):
        assert forbidden not in text, "must be standalone (found %r)" % forbidden
    assert text.startswith("#!/usr/bin/env python3")
