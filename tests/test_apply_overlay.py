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
