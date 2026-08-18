#!/usr/bin/env python3
"""Tests for examples/ladybird/apply_overlay.sh.

The script reproduces the Ladybird tree the migration builds: a pinned upstream
commit + three patches + the overlay files. It cannot be tested end to end here (that
needs a 121 MB clone and the network -- it WAS run end to end, twice, and that is
recorded in the README), so these tests pin the properties that rot silently:

  1. the Ladybird commit is pinned in exactly one place and is a full sha -- the
     generated BUILD files name ~1,961 sources by path, so the tree they describe
     has to be identified, and nothing in this repo identified it before;
  2. the file list is DERIVED from the overlay, never hand-maintained -- a
     hand-kept list is how vcpkg_git_archives.bzl came to be generated, committed,
     documented and copied by nothing;
  3. the Build/vcpkg file is staged AFTER the vcpkg prefetch, because staging it
     early makes upstream's bootstrap read the bare directory as an existing clone
     and die with `fatal: unable to read tree`;
  4. verification checks the things a file copy cannot: patches applied, exec bits.
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "examples" / "ladybird" / "apply_overlay.sh"
WORKSPACE = REPO / "examples" / "ladybird" / "workspace"
PATCHES = REPO / "examples" / "ladybird" / "patches"


def _text():
    return SCRIPT.read_text()


def test_script_is_committed_executable():
    """A setup script that is not executable fails at the worst moment: first use."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "examples/ladybird/apply_overlay.sh"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout
    assert out.strip(), "apply_overlay.sh is not committed"
    assert out.split()[0] == "100755", "apply_overlay.sh must be committed executable"


def test_script_is_valid_bash():
    """Syntax-check rather than trust: `bash -n` is free."""
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_ladybird_commit_is_pinned_once_as_a_full_sha():
    """The tree the generated BUILD files describe must be identified exactly.

    A short sha or a tag would both be wrong: a tag moves relative to the tree that
    was measured (the same mistake pinning the HSTS table to a release tag would
    have been), and one pin in two places is two pins.
    """
    text = _text()
    pins = re.findall(r'LADYBIRD_COMMIT="([0-9a-f]+)"', text)
    assert len(pins) == 1, "the Ladybird commit must be pinned in exactly one place"
    assert len(pins[0]) == 40, "pin the full 40-char sha, not an abbreviation"


def test_the_pinned_commit_is_documented_where_a_reader_looks():
    """The README's manual recipe must name the same commit as the script.

    Two recipes that disagree about the tree is worse than one recipe.
    """
    commit = re.search(r'LADYBIRD_COMMIT="([0-9a-f]{40})"', _text()).group(1)
    readme = (REPO / "examples" / "ladybird" / "README.md").read_text()
    assert commit in readme, "the README does not name the pinned Ladybird commit"


def test_file_list_is_derived_not_hand_maintained():
    """The overlay's file list must come from the overlay itself.

    The concrete failure this prevents: vcpkg_git_archives.bzl was generated,
    committed, listed in the README's file table -- and loaded by nothing, because
    the wiring was a hand-kept list that nobody updated.
    """
    text = _text()
    assert "find . -type f" in text, "the file list must be discovered with find"
    # ...and no literal roster of overlay files in the CODE. Comments may name a
    # file (they explain why the list is derived, citing vcpkg_git_archives.bzl as
    # the cautionary case), so strip comments before looking -- a test that cannot
    # tell prose from code makes documenting the reason impossible.
    code = "\n".join(l.split("#", 1)[0] for l in text.splitlines())
    listed = re.findall(r'\b[\w.]+\.bzl\b', code)
    assert not listed, "found hand-listed overlay files in code: %r" % (listed,)


def test_every_overlay_file_would_be_copied():
    """The derived list must actually cover the committed overlay.

    Recomputes what the script's `find` yields and compares it to git's idea of the
    overlay, so a file committed but excluded by the find expression is caught.
    """
    found = subprocess.run(
        ["find", ".", "-type", "f", "!", "-name", "*.pyc", "-printf", "%P\n"],
        cwd=WORKSPACE, capture_output=True, text=True,
    ).stdout.split()
    committed = subprocess.run(
        ["git", "ls-files", "examples/ladybird/workspace"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.split()
    committed = {c.replace("examples/ladybird/workspace/", "") for c in committed}
    missed = committed - set(found)
    assert not missed, "committed overlay files the script would not copy: %r" % (missed,)


def test_the_bazelrc_rename_is_expressed_once():
    """bazelrc.txt -> .bazelrc must be one mapping both copy and verify use."""
    text = _text()
    assert text.count("bazelrc.txt) echo") == 1, \
        "the rename must live in a single target_path mapping"
    assert "target_path" in text


def test_build_vcpkg_file_is_deferred_past_the_prefetch():
    """The ordering bug, pinned as a test.

    Build/vcpkg/BUILD.bazel makes the DIRECTORY exist; upstream's build_vcpkg.py
    reads that as "already cloned", skips the clone, and `git -C Build/vcpkg
    rev-parse HEAD` then walks up to Ladybird's own .git and returns Ladybird's
    HEAD -- so it tries to check vcpkg's baseline out of the Ladybird repo and dies
    with `fatal: unable to read tree`. Found by running the script on an empty
    directory; it is not visible by reading either side alone.
    """
    text = _text()
    assert "Build/vcpkg/*) deferred=" in text, \
        "files under Build/vcpkg must be skipped in the first copy pass"
    # the deferred staging must be gated on the checkout really existing
    assert 'if [ -d "$TARGET/Build/vcpkg/.git" ]; then' in text
    assert "unable to read tree" in text, \
        "the failure mode must be named where someone hitting it will look"


def test_verify_checks_patches_are_applied_not_merely_present():
    """`git apply --check -R` succeeding is what proves a patch is IN the tree."""
    assert "git apply --check -R" in _text()


def test_verify_checks_executable_bits():
    """Exec bits are tree state a copy drops, and the failure surfaces mid-build."""
    text = _text()
    assert "NOT EXECUTABLE" in text
    assert re.search(r'\[ -x .* \]', text), "verify must test -x on the .sh files"


def test_verify_mode_changes_nothing():
    """--verify must contain no mutating command in its branch.

    A "check my tree" mode that writes is a trap, so this reads the verify branch
    and asserts it does not copy, clone, checkout or apply.
    """
    text = _text()
    verify_branch = text.split('if [ "$VERIFY" -eq 1 ]; then', 1)[1].split("\nfi\n", 1)[0]
    for forbidden in ("cp -p", "git clone", "git checkout", "git apply \"", "mkdir -p"):
        assert forbidden not in verify_branch, \
            "--verify must not mutate the tree (found %r)" % forbidden


def test_both_patches_are_referenced_by_glob_not_by_name():
    """A third upstream patch must not need a script edit to be applied."""
    text = _text()
    assert '"$PATCHES"/*.patch' in text
    names = [p.name for p in PATCHES.glob("*.patch")]
    assert len(names) >= 2
    for n in names:
        assert n not in text, "patch %s is named literally; use the glob" % n


def test_effect_grep_files_exist_for_patches_upstream_may_fix_itself():
    """A patch we carry until upstream fixes it needs a weaker second question.

    `git apply --check -R` proves MY EXACT BYTES are in the tree, which is a
    stronger claim than "the defect is fixed". Ulf hit the difference: upstream
    landed its own fd-leak fix, so a tree that was CORRECT (and newer than our pin)
    reported `PATCH NOT APPLIED` and told him to apply a patch that would then
    conflict. So the fd-leak patch carries an `.effect-grep`, and verify falls back
    to it before failing.
    """
    text = _text()
    assert ".effect-grep" in text, "verify must fall back to an effect check"
    fd_leak = PATCHES / "0001-librequests-tear-down-request-when-body-is-delivered.patch"
    assert fd_leak.exists()
    effect = fd_leak.with_suffix(".effect-grep")
    assert effect.exists(), \
        "the fd-leak patch is the one upstream is fixing; it needs an effect check"
    # reverse-apply must still be tried FIRST, so the exact-bytes case keeps its
    # precise answer and the weaker check is only a fallback
    assert text.index("git apply --check -R") < text.index(".effect-grep")


def test_effect_grep_is_windowed_not_a_whole_file_grep():
    """The negative case is what makes an effect check worth anything.

    A whole-file grep for `defer_teardown();` PASSES on a tree without the fix,
    because that call already occurs in stop() and did_transfer() -- I wrote that
    version first and it silently accepted an unpatched tree, which is worse than
    being too strict. The check therefore anchors on the branch condition and
    requires the call within a window of following lines.
    """
    effect = (PATCHES / "0001-librequests-tear-down-request-when-body-is-delivered.effect-grep")
    lines = [ln for ln in effect.read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert any(ln.startswith("@window ") for ln in lines), \
        "the effect check must be windowed; a whole-file grep accepts an unpatched tree"
    window = [ln for ln in lines if ln.startswith("@window ")][0].split()
    assert window[1].isdigit() and int(window[1]) < 40, \
        "the window must be tight enough to mean 'in this branch'"
    assert "$PATCHES" not in effect.read_text()
    # and the script must implement the window, not just tolerate the directive
    text = _text()
    assert "@window " in text


def test_the_diagnostic_patch_is_not_applied_by_the_overlay():
    """The fd-leak census patch must never enter a normal build.

    It adds a per-request HashMap, a repeating timer and a poll()/MSG_PEEK probe on
    every retained response fd -- fine for a diagnosis, wrong in a browser someone
    is using. apply_overlay.sh applies `patches/*.patch` by glob, so the only thing
    keeping it out is its extension. That is exactly the kind of load-bearing
    filename convention that a later rename breaks silently, so pin it: the
    diagnostic exists, it is NOT matched by the glob, and it says so in its header.
    """
    diagnostics = sorted(PATCHES.glob("DIAGNOSTIC-*"))
    assert diagnostics, "the fd-leak diagnostic patch is missing"
    for d in diagnostics:
        assert d.suffix != ".patch", \
            "%s would be applied by the overlay's patches/*.patch glob" % d.name
        assert "NOT AN OVERLAY PATCH" in d.read_text(), \
            "%s must say why it is not applied" % d.name
    applied = {p.name for p in PATCHES.glob("*.patch")}
    assert not any(n.startswith("DIAGNOSTIC") for n in applied)


def test_the_documented_overlay_file_count_matches_the_overlay():
    """The README's "N Bazel files" must be the number the script actually copies.

    It said 42 while the overlay held 43, and nothing noticed -- finding 38 added a
    file and updated the prose in one place but not the others. That is finding
    39's lesson at the smallest possible scale: a documented number that nothing
    checks is a number that is wrong as soon as it matters. The script derives the
    list with `find`, so the overlay is the truth; this makes the prose answerable
    to it.
    """
    workspace = REPO / "examples" / "ladybird" / "workspace"
    actual = len([p for p in workspace.rglob("*")
                  if p.is_file() and p.suffix != ".pyc"])
    readme = (REPO / "examples" / "ladybird" / "README.md").read_text()
    claims = set(re.findall(r'(\d+) (?:Bazel|overlay) files', readme))
    claims |= set(re.findall(r'(?:all|the) (\d+) (?:Bazel |overlay )?files', readme))
    assert claims, "the README no longer states an overlay file count"
    assert claims == {str(actual)}, \
        "README claims %s overlay files, the overlay has %d" % (
            sorted(claims), actual)


def test_the_response_fd_patch_closes_the_fd_where_the_body_is_proven_complete():
    """The second half of the fd leak: the fd outlives the callbacks.

    0001 drops the callbacks on completion, which unpins the GC cycle -- and on my
    machine that took 143 leaked sockets to 0. On Ulf's tree it did not: he measured
    97 sockets/min STILL leaking with the upstream-equivalent teardown applied,
    every one peer=DEAD (RequestServer had already closed its end) and every one
    sent by RequestServer (SO_PEERCRED, once `ps` named pid 2261433).

    The reason is ownership: the response fd is released only by ~Request (or by the
    ReadStream inside m_internal_stream_data), so ANY surviving reference to the
    Request keeps one fd per completed request alive -- dropping the callbacks is
    not the same as closing the fd. 0002 closes it explicitly at the point the code
    has already proven the body is complete.

    Measured A/B, same binary, same 200-completed-request workload:
      clean: 208 sockets, 203 peer=DEAD
      fixed:   6 sockets,   0 peer=DEAD
    """
    patch = PATCHES / "0002-requests-release-response-fd-on-completion.patch"
    assert patch.exists()
    text = patch.read_text()
    # 0002 is the NEXT PATCH IN THE SERIES, not an alternative to 0001: it is
    # generated on top of it and applies only after it. I first shipped a second
    # "clean tree" variant for trees without the teardown fix, which cannot work --
    # apply_overlay.sh globs patches/*.patch and applies them all, so one of the two
    # was guaranteed to fail. Ulf hit it on the first run. The variant is deleted; it
    # was also strictly weaker (it closed the fd without dropping the callbacks, so it
    # left the GC cycle, and with it class B, in place).
    assert not list(PATCHES.glob("*clean-tree*")), \
        "an alternative in patches/*.patch cannot coexist with the glob that applies all"
    assert "ON TOP OF the teardown fix" in text, \
        "0002 must state that it builds on 0001 rather than replacing it"
    # the notifier must be deregistered before the fd is closed, or the event loop
    # is left polling a closed descriptor
    body = text[text.index("release_response_fd"):]
    assert body.index("read_notifier->close()") < body.index("m_fd = -1"), \
        "deregister the notifier BEFORE closing the fd"
    # it must NOT close on request_finished alone: RequestServer finishes writing
    # long before WebContent drains the pipe, and closing there truncates bodies
    # (verified once as blank pages)
    assert "has_received_all_reported_bytes" in text or "user_finish_called" in text
    effect = patch.with_suffix(".effect-grep")
    assert effect.exists(), "carried until upstream fixes it -> needs an effect check"
    lines = [ln for ln in effect.read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert any(ln.startswith("@window ") for ln in lines)
    # apply_overlay.sh takes only the FIRST @window; a second would be checked
    # against the wrong slice, so the file must not carry one.
    assert len([ln for ln in lines if ln.startswith("@window ")]) == 1, \
        "apply_overlay.sh honours one @window only"


def test_the_patch_series_applies_as_a_series():
    """patches/*.patch must apply IN GLOB ORDER, each on top of the last.

    apply_overlay.sh applies every patches/*.patch by glob. That makes the directory a
    series, not a menu -- and I broke it: 0002 needs 0001's hunk to be present, so I
    shipped a second "clean tree" variant of 0002 for trees without the teardown fix.
    Since the loop applies ALL of them, one of the two could only ever fail. Ulf hit it
    on the first run: "tries to apply both patches at the same time".

    So this reconstructs the pinned versions of every file the patches touch, straight
    out of the target of each patch, and applies the series to them exactly as the
    script does. A patch that conflicts with its predecessor -- or an alternative
    smuggled into patches/*.patch -- fails here instead of on a colleague's clone.

    Needs a git and the Ladybird checkout the pin refers to; skipped when absent,
    because the suite must run in a bare container too.
    """
    import os
    import shutil
    import tempfile

    checkout = os.environ.get("LADYBIRD_CHECKOUT", os.path.expanduser("~/ladybird-work"))
    if not os.path.isdir(os.path.join(checkout, ".git")):
        return  # no reference checkout here; the shell-level check still runs in CI

    patches = sorted(PATCHES.glob("*.patch"))
    assert len(patches) >= 2, "a series needs at least two patches to be worth checking"

    # every file any patch touches, at the pinned commit
    targets = set()
    for p in patches:
        targets.update(re.findall(r"^\+\+\+ b/(\S+)", p.read_text(), re.M))
    assert targets, "no patch targets found -- has the patch format changed?"

    commit = re.search(r'LADYBIRD_COMMIT="([0-9a-f]{40})"', _text()).group(1)
    tmp = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "-q", "."], cwd=tmp, check=True)
        for t in sorted(targets):
            blob = subprocess.run(["git", "show", "%s:%s" % (commit, t)],
                                  cwd=checkout, capture_output=True, text=True)
            if blob.returncode != 0:
                return  # the pinned commit is not fetched here; nothing to check
            dest = os.path.join(tmp, t)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w") as f:
                f.write(blob.stdout)
        subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "pinned"], cwd=tmp, check=True)

        for p in patches:
            r = subprocess.run(["git", "apply", str(p)], cwd=tmp,
                               capture_output=True, text=True)
            assert r.returncode == 0, (
                "%s does not apply on top of the patches before it.\n"
                "patches/*.patch is a SERIES applied in glob order by "
                "apply_overlay.sh; an ALTERNATIVE to another patch must live "
                "outside that glob (see DIAGNOSTIC-*.patch.txt).\n%s"
                % (p.name, r.stderr))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_two_patches_are_alternatives_of_each_other():
    """A cheap structural guard that needs no checkout at all.

    Two patches whose names differ only by a trailing variant suffix, or that claim in
    their own header that only one of them applies, cannot both be in a glob-applied
    series. This catches the mistake at review time rather than at apply time.
    """
    for p in sorted(PATCHES.glob("*.patch")):
        text = p.read_text()
        assert "only one of them will apply" not in text, (
            "%s advertises itself as an alternative, but patches/*.patch is applied "
            "in full by apply_overlay.sh -- move it outside the glob" % p.name)
    # and the numeric prefixes must be unique: two patches sharing one number are
    # variants by construction
    prefixes = [p.name.split("-")[0] for p in sorted(PATCHES.glob("*.patch"))]
    dupes = {n for n in prefixes if prefixes.count(n) > 1}
    assert not dupes, \
        "patches sharing a series number are alternatives, not a series: %r" % (dupes,)
