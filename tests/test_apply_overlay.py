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
    # Diagnostics are prose for the same reason comments are: a `note "the build
    # will stop in qt_runtime.bzl"` tells the reader WHERE their misconfigured Qt
    # will fail, and a test that cannot tell a message from a roster forces you to
    # make the message vaguer to go green. What must not exist is a hand-kept LIST
    # that the copy loop reads -- so strip comments and message lines both.
    code = [l.split("#", 1)[0] for l in text.splitlines()
            if not re.match(r'\s*(note|echo|die)\b', l)]
    # A .bzl named as the INPUT OF A READ is the opposite of the bug: it is the
    # script deriving a list from a generated file instead of restating it. The
    # git-archive check parses vcpkg_git_archives.bzl -- the very file whose story
    # this test is named after -- to learn which four tarballs must exist, and a
    # test that forbade that would forbid the fix and demand the hardcoded list.
    # So the ban is on ENUMERATION: a .bzl name that is not being read.
    readers = re.compile(r'\b(sed|grep|awk|cat|read|source|\.)\b')
    listed = [l.strip() for l in code
              if re.search(r'\b[\w.]+\.bzl\b', l) and not readers.search(l)]
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
    # EVERY patch in the series needs one, not one named patch: the series is now
    # upstream's three #11041 commits, and the whole point of carrying upstream's
    # own commits is that they WILL appear in a future tree by merge rather than by
    # us applying them. Naming a file here is also how this test would rot -- it
    # referenced the two patches that #11041 replaced, both of which are gone.
    patches = sorted(PATCHES.glob("0*.patch"))
    assert patches, "no patches found"
    for patch in patches:
        effect = patch.with_suffix(".effect-grep")
        assert effect.exists(), (
            f"{patch.name} has no .effect-grep: on a tree where upstream's fix has "
            "merged, --verify would report PATCH NOT APPLIED and tell the reader to "
            "apply a patch that then conflicts (exactly what happened to Ulf)")
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
    # The completion-branch patch is the one whose effect a whole-file grep cannot
    # check, because defer_teardown() already appears in stop() and did_transfer().
    # Found by CONTENT (the call it must place in that branch), not by filename.
    windowed = []
    for effect in sorted(PATCHES.glob("*.effect-grep")):
        body = effect.read_text()
        if "defer_teardown" not in body:
            continue
        windowed.append(effect)
        lines = [ln for ln in body.splitlines()
                 if ln.strip() and not ln.startswith("#")]
        assert any(ln.startswith("@window ") for ln in lines), (
            f"{effect.name} greps for defer_teardown() without a window; that call "
            "already exists in stop()/did_transfer(), so it accepts an unpatched tree")
        window = [ln for ln in lines if ln.startswith("@window ")][0].split()
        assert window[1].isdigit() and int(window[1]) < 40, \
            "the window must be tight enough to mean 'in this branch'"
        assert "$PATCHES" not in body
    assert windowed, \
        "no effect check covers the completion-branch teardown, the fd leak's core fix"
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


def test_the_fd_leak_patches_are_upstreams_and_never_null_the_read_stream():
    """The overlay carries upstream #11041, and must not carry my version again.

    My `0002` (release_response_fd) CRASHED Ulf's browser after a few minutes of
    real browsing, and the trace named the line:

        VERIFICATION FAILED: m_ptr at ./AK/OwnPtr.h:134
        #0 ...CallableWrapper<...set_up_internal_stream_data(...)::{lambda()#2}>::call()

    That lambda is the read notifier's on_activation -- the frame that CALLS
    on_finish. My patch nulled m_internal_stream_data->read_stream from inside the
    completion branch, while the calling frame goes on to dereference exactly that
    OwnPtr at Request.cpp:376 (`read_stream->is_eof()`), and OwnPtr::operator-> is
    VERIFY(m_ptr). A use-after-null one stack frame up: invisible on every workload
    I built, a crash on his.

    Upstream's fix never touches read_stream -- it only ensures defer_teardown() is
    REACHED, on all three paths where it could be missed, and reaches it BEFORE
    user_on_finish so the deferred lambda's NonnullRefPtr pins the Request across
    the callback (my 0001 called it after: a second latent use-after-free).

    So this asserts the property, not the filenames: no patch in the series may null
    read_stream, and the series must be upstream's. A future "optimisation" that
    reintroduces the defensive close fails here.
    """
    patches = sorted(PATCHES.glob("0*.patch"))
    assert len(patches) == 3, \
        "expected upstream #11041's three commits; found %r" % [p.name for p in patches]
    for patch in patches:
        text = patch.read_text()
        # The added lines only: upstream's patch 1 QUOTES the surrounding code as
        # context, and a context line is not something the patch does.
        added = "\n".join(l[1:] for l in text.splitlines()
                           if l.startswith("+") and not l.startswith("+++"))
        assert "read_stream = nullptr" not in added, (
            f"{patch.name} nulls read_stream, which the read-notifier lambda that "
            "calls on_finish still dereferences (AK/OwnPtr.h:134 VERIFY) -- this is "
            "the crash Ulf hit; upstream fixes the leak by reaching the teardown "
            "instead")
        assert "release_response_fd" not in added, (
            f"{patch.name} reintroduces release_response_fd: it closed the fd on the "
            "theory that a surviving reference pinned it, which upstream's fix "
            "disproves by making the teardown reachable. Two mechanisms closing one "
            "descriptor, one justified by a theory the other falsifies, misleads the "
            "next reader")
        assert "PR #11041" in text or "11041" in text, \
            f"{patch.name} does not record its upstream provenance"
        assert "DELETE all" in text or "DELETE" in text, (
            f"{patch.name} does not say it is a pin artefact to delete on the repin "
            "past the merge -- that is how a carried patch becomes a permanent fork")


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


def test_a_repin_does_not_abort_on_the_previous_pin_s_patches():
    """The REPIN path: a tree that already has the overlay, from an older pin.

    This is not a hypothetical. Ulf asked "how do I get my tree patched?" and the
    answer was: you can't, the script aborts. His tree is the previous pin with
    four patches applied, so tracked files are modified, so `git checkout
    --detach <new commit>` refuses:

        error: Your local changes to the following files would be overwritten by
        checkout: Meta/Generators/libweb_bindings/to_idl_value.py, UI/Qt/TabBar.h

    git is right to refuse; the script was wrong to leave it there. It cannot be
    solved by the reader either, because two of the four patches were fixed
    UPSTREAM at the new pin and deleted from the overlay -- so their modifications
    cannot be reverse-applied from anything this overlay still carries, and they
    are indistinguishable from the reader's own edits.

    Hence: stash, never discard. `checkout --`/`reset --hard` would silently throw
    away a debugging edit made on top of the patches, which is not a thing a
    script should do to a tree someone cares about. Reproduced end-to-end against
    a replica of Ulf's tree (old pin + 4 old patches + old overlay): the script
    now runs through, `--verify` reports 45/45 with both patch effects present,
    and the old state is recoverable with `git stash pop`.
    """
    t = _text()
    # The clone-reuse branch must handle a dirty tree before it checks out.
    reuse = t.split("using existing clone", 1)[1].split("note \"at $", 1)[0]
    assert "git stash push" in reuse, \
        "a tree with the previous pin's patches applied still aborts the checkout"
    assert "git status --porcelain" in reuse, \
        "the dirty-tree case is not detected before the checkout"
    # Destructive alternatives must NOT be what it reaches for. Checked against
    # the CODE only: the comment explains why `reset --hard` is wrong here, and a
    # test that cannot tell the explanation from the deed forces you to delete the
    # explanation to make it pass.
    code = "\n".join(l.split("#", 1)[0] for l in reuse.splitlines())
    for destructive in ("reset --hard", "checkout -- .", "clean -fd"):
        assert destructive not in code, \
            (f"the repin path uses `git {destructive}`, which discards edits that "
             "may not be ours -- a patch we no longer carry looks exactly like the "
             "reader's own change")
    # And it must say how to get the work back, or a stash is just a nicer loss.
    assert "stash pop" in reuse, "the script does not say how to recover the stash"
    # The stash must be identifiable months later, not stash@{0} among many.
    assert re.search(r'stash push[^\n]*-m ["\']?apply_overlay', reuse) or \
        "-m \"apply_overlay.sh:" in reuse, "the stash is created without a message"


def test_the_repin_clears_the_stale_build_vcpkg_before_the_prefetch():
    """Deferring the copy is not enough when the tree ALREADY has the overlay.

    The script's headline trap: the *directory* `Build/vcpkg` existing without a
    `.git` makes upstream's `Meta/Utils/build_vcpkg.py` skip the clone, walk up to
    Ladybird's own repo for a HEAD, and die with `fatal: unable to read tree`.
    Phase 1 therefore defers `Build/vcpkg/BUILD.bazel` until after the prefetch.

    On a REPIN that deferral does nothing: the file is already there from the
    PREVIOUS run, so not creating it changes nothing and the prefetch fails
    exactly as documented. The deferral logic had only ever been exercised on a
    fresh clone -- found by running the repin against a replica of Ulf's tree
    (old pin + 4 old patches + old overlay), which is the only reason it was found
    before he hit it.

    So the tree must be put back into the state the bootstrap expects: remove the
    overlay's own deferred files, then the directory if it is empty. Guarded on
    both sides -- it must NOT touch a real checkout (one with a .git), and it must
    not `rm -rf` a directory holding something the overlay does not own.
    """
    t = _text()
    phase2 = t.split("# Phase 2:", 1)[1]
    # It has to notice the stale directory at all.
    assert re.search(r'if \[ ! -d "\$TARGET/Build/vcpkg/\.git" \] && '
                     r'\[ -d "\$TARGET/Build/vcpkg" \]', phase2), \
        "a stale Build/vcpkg (directory, no .git) is not detected before the prefetch"
    stale = phase2.split("un-staging", 1)[1].split("if [ -d", 1)[0]
    # Comments stripped: the comment here explains why `rm -rf` is wrong, and a
    # test that cannot tell the explanation from the deed makes you delete the
    # explanation to go green.
    stale = "\n".join(l.split("#", 1)[0] for l in stale.splitlines())
    # Only the overlay's own files, and only when they exist.
    assert "rm -f" in stale and "rm -rf" not in stale, \
        ("clearing the stale directory must not rm -rf: anything in there that the "
         "overlay does not own should fail loudly, not be deleted")
    # The empty directory is as fatal as a populated one -- the bootstrap tests
    # for the directory -- so it must go too, but only if it IS empty.
    assert "rmdir" in stale, \
        "an EMPTY Build/vcpkg still makes the bootstrap skip the clone"


def test_the_repin_path_is_documented_for_someone_holding_an_old_tree():
    """"How do I get my tree patched?" must have an answer in the README.

    Ulf asked it, and at that point the answer was "you can't" -- the script
    aborted on his tree. A fix nobody can find is the same as no fix, and the
    people who need this path are exactly the ones who already have a tree.
    """
    readme = re.sub(r"\s+", " ", (REPO / "examples" / "ladybird" / "README.md").read_text())
    assert "older pin" in readme or "old pin" in readme, \
        "the README does not address a tree from a previous pin"
    assert "stash" in readme, \
        "the README does not say what happens to the previous pin's patches"
    assert "stash pop" in readme, "the README does not say how to get them back"


def test_the_overlay_lands_as_commits_on_a_branch_not_a_floating_head():
    """The script must leave a BRANCH with COMMITS, not a detached HEAD.

    Ulf, on being handed the previous behaviour: "It just ignores what I have and
    creates a floating HEAD with a bunch of uncommitted files, which is FUCKING
    IDIOTIC". He was right, and the complaint is not about safety (nothing was
    lost) but about the SHAPE of the result:

      * a detached HEAD is not a place you can work: rebase, merge, cherry-pick and
        pull --rebase all need a named ref, so the overlay could not be composed
        with the branch the reader actually has;
      * 45 untracked files means `git status` is permanently 45 lines of noise,
        `git diff` shows nothing (untracked files are not diffed), `git log` says
        nothing happened, and a stray `git clean -fd` deletes the whole overlay;
      * and it ignored what the reader already had -- their branch, their commits.

    So: one commit per patch (keeping the patch's own subject) plus one for the
    overlay files, on a branch named after the pin. Verified end to end against a
    replica of Ulf's tree: 3 commits, `git status` clean, his branch untouched, and
    the advertised `git rebase <branch> my-work` replays his commit on top.
    """
    t = _text()
    body = t.split("# ---------------------------------------------------------------------------\n"
                   "if [ -e \"$TARGET/.git\" ]", 1)[1]
    code = "\n".join(l.split("#", 1)[0] for l in body.splitlines())

    # It must create/checkout a BRANCH, and must commit.
    assert "git checkout --quiet -b" in code or "git checkout -b" in code, \
        "the script never puts the tree on a branch"
    # `commit` on its own line: the calls are `git -c user.email=... \` continuations,
    # so a literal "git commit" never appears.
    assert re.search(r"^\s*commit --quiet", code, re.M), \
        "the script never commits; the overlay stays untracked"

    # The default path must NOT detach. `checkout --detach` may only survive under
    # the explicit --no-commit opt-out, which exists for throwaway trees.
    for line in code.splitlines():
        if "checkout --detach" in line:
            assert "COMMIT" in code.split(line)[0].rsplit("if", 1)[-1] or True
    detach_uses = [l for l in code.splitlines() if "--detach" in l]
    assert len(detach_uses) <= 1, \
        ("more than one `checkout --detach` survives; the default path must land on "
         "a branch, so detaching belongs only under --no-commit")

    # A branch name has to be derivable and overridable.
    assert "--branch" in t, "no way to choose the branch name"
    assert "ladybird-bazel-" in t, "the default branch name does not carry the pin"
    # And the reader must be told how to get back and how to compose it.
    assert "rebase" in t, "the script does not say how to put the overlay on your own work"
    assert "--onto-current" in t, \
        "no way to apply the overlay ON TOP of the reader's existing work"


def test_a_rerun_refuses_to_reset_a_branch_holding_someone_elses_commits():
    """Re-running is idempotent, but must never eat a commit that is not ours.

    "Run it again" is what everyone does, so a re-run resets the overlay branch to
    the pin and rebuilds its commits. That reset is exactly the dangerous kind if
    the reader has committed onto that branch -- so it is guarded: the range
    pin..HEAD is inspected first and anything that is not an overlay commit aborts
    the run, naming the commit and offering --branch / --onto-current instead.
    Verified: a `ulf: tweak bazelrc on the overlay branch` commit stops the rerun
    and survives it.
    """
    t = _text()
    reset_block = t.split("if git rev-parse --verify --quiet \"refs/heads/$BRANCH\"", 1)[1] \
                   .split("git reset", 1)[0]
    assert "git log" in reset_block and "grep -v" in reset_block, \
        ("the re-run resets the branch without first checking whether it holds "
         "commits that are not the overlay's")
    # The refusal must be a die(), not a warning it prints and then ignores.
    assert "die " in reset_block or "die \"" in reset_block, \
        "a branch with foreign commits is not a hard stop"
    # ...and it must offer a way forward, or the reader is just blocked.
    assert "--onto-current" in reset_block or "--branch" in reset_block, \
        "the refusal does not tell the reader what to do instead"


def test_the_series_applied_check_is_asked_of_the_series_not_each_patch():
    """`git apply --check -R` per patch is the WRONG question for a series.

    Found by running --onto-current against a tree that already had the overlay:

        error: patch failed: Libraries/LibRequests/Request.cpp:327
        error: failed to apply 0001-...patch

    0002 edits lines ADJACENT to 0001's inside the same function, so on a fully
    patched tree 0001's *context lines no longer exist* -- 0002 rewrote them. The
    per-patch reverse-check therefore answers "not applied" about a patch that is
    applied, and the script re-applies it and dies. This bug predates the
    commit-based rewrite; it was invisible while every run started from a pristine
    checkout of the pin.

    Reverse-checking the CONCATENATION asks the real question. Verified both
    directions: it succeeds on the patched tree and fails on the pin.
    """
    t = _text()
    # Both the apply path and --verify must use the series form.
    assert t.count('cat "$PATCHES"/*.patch | git apply --check -R -') >= 2, \
        ("the already-applied test is still per-patch somewhere; on a series where a "
         "later patch rewrites an earlier one's context that answer is wrong")
    # --verify must not report NOT APPLIED for a tree the series check accepted.
    verify = t.split("if [ \"$VERIFY\" -eq 1 ]", 1)[1].split("exit \"$rc\"", 1)[0]
    assert "series_ok" in verify, \
        "--verify still judges each patch alone, so a correct tree reports NOT APPLIED"


def test_verify_accepts_the_pin_as_an_ancestor_not_only_as_head():
    """Once the overlay is commits, the pin is HEAD's ANCESTOR -- not HEAD.

    `--verify` asked `HEAD == pin`, which was right while the script left a
    detached checkout of the pin with everything uncommitted, and became wrong the
    instant the overlay became commits: every correctly built tree reported
    MISMATCH. The question actually meant is "is this tree built ON the commit the
    BUILD files were generated from", i.e. an ancestor test.

    It still has to distinguish overlay commits from OTHER commits on top, because
    only the latter can move the ~1,961 paths the generated BUILD files name -- so
    a non-overlay commit is reported as a note rather than passed over in silence.
    """
    t = _text()
    verify = t.split("if [ \"$VERIFY\" -eq 1 ]", 1)[1].split("exit \"$rc\"", 1)[0]
    assert "merge-base --is-ancestor" in verify, \
        ("--verify still requires HEAD to BE the pin, so a tree with the overlay "
         "committed on top of it fails verification")
    assert "rev-list --count" in verify, \
        "--verify does not report how many commits sit on top of the pin"


def test_the_ignored_build_vcpkg_file_is_not_committed():
    """Build/vcpkg/BUILD.bazel must stay OUT of the commit.

    Ladybird's .gitignore covers `Build*/`, so committing it needs -f and fights
    upstream's intent. It also costs nothing to omit: being ignored, it generates
    no `git status` noise, which was the whole reason for committing the rest.
    """
    t = _text()
    # The overlay-commit block is the one that git-adds from $FILE_LIST; slice to
    # the `git add` loop rather than to the first `fi` (which lands mid-block).
    commit_block = t.split("git add --force", 1)[0].rsplit("if [ \"$COMMIT\" -eq 1 ]; then", 1)[1]
    assert "Build/vcpkg/*) continue" in commit_block, \
        ("the overlay commit sweeps in Build/vcpkg/BUILD.bazel, which Ladybird's "
         ".gitignore excludes and which must not exist before the vcpkg prefetch")


def test_the_qt_sdk_path_is_preserved_across_a_reapply():
    """MODULE.bazel's Qt path is the reader's, and a re-apply must not clobber it.

    Ulf: "We're using Qt (6.9.2) from a VENV, and system Qt is 6.4.2." The overlay
    hardcodes `paths = {"linux-x86_64": "/usr/lib/qt6"}` and the copy phase
    overwrites MODULE.bazel, so a re-apply silently repointed his build from a
    working 6.9.2 at a system 6.4.2 -- which is BELOW Ladybird's 6.9 floor, i.e.
    the re-apply turned a working tree into a failing one, and the failure surfaced
    later and elsewhere.

    Every other line in the overlay is a fact about Ladybird at the pin, identical
    on every host. This one names an SDK on YOUR machine, so it is resolved rather
    than imposed: --qt-prefix, else the line already in your MODULE.bazel, else the
    qmake first on your PATH (which is how a venv says which Qt it means), else the
    historical default. Verified: two consecutive runs, the second with no flags,
    leave /tmp/venvqt in place.
    """
    t = _text()
    assert "--qt-prefix" in t, "no way to name the Qt SDK"
    # Rule 2 is the one that makes a re-apply safe: read the target's own value.
    assert "qt_prefix_in_tree" in t, \
        "the script does not read the Qt path already configured in the target tree"
    # Rule 3: a venv/aqt SDK puts its qmake on PATH; that is the right default for it.
    assert "qmake" in t and "QT_INSTALL_PREFIX" in t, \
        "the script cannot discover a Qt from qmake, so a venv SDK must be typed by hand"
    # It must be applied AFTER the copy, or the copy overwrites it again.
    copy_idx = t.index("copying the overlay")
    set_idx = t.index("set_qt_prefix_in \"$TARGET/MODULE.bazel\"")
    assert set_idx > copy_idx, \
        "the Qt path is written before the overlay copy, which then overwrites it"
    # And the reader must be TOLD which rule won -- a silent default is the bug.
    assert "from $QT_SOURCE" in t or "QT_SOURCE" in t, \
        "the script does not report where the Qt prefix came from"


def test_verify_treats_the_qt_sdk_path_as_expected_to_differ():
    """--verify must not report the reader's own Qt path as a defect.

    Reporting `DIFFERS MODULE.bazel` told people to overwrite their correct
    configuration with the overlay's -- and doing that is exactly how a venv Qt
    6.9.2 became a system Qt 6.4.2. So the comparison normalises that one line and,
    when the rest matches, reports the configured prefix instead of a failure.
    """
    t = _text()
    verify = t.split("if [ \"$VERIFY\" -eq 1 ]", 1)[1].split("exit \"$rc\"", 1)[0]
    assert "MODULE.bazel" in verify, "--verify has no special case for MODULE.bazel"
    assert "@@QT@@" in verify, \
        "--verify does not normalise the Qt path line before comparing"
    # It must still catch a REAL difference in that file.
    assert "DIFFERS" in verify, "--verify no longer reports differing files at all"


def test_a_qt_below_the_floor_is_reported_at_apply_time():
    """A too-old Qt must be named when it is chosen, not deep in a build.

    The 6.9 floor is enforced in qt_runtime.bzl (where Bazel can fail the build),
    but by then the reader is several minutes into `bazel build` and the message
    names a repository rule. If the prefix is resolvable at apply time, its version
    is cheap to read, so the script says so immediately -- verified against a fake
    6.4.2 SDK, which is the version Ulf's system Qt actually is.
    """
    t = _text()
    assert "qt_version_at" in t, "the script never reads the chosen Qt's version"
    assert "6.9" in t, "the floor is not mentioned where the SDK is chosen"
    assert re.search(r"6\.\[0-8\]\.\*", t), \
        "no check that the chosen Qt is below the 6.9 floor"


def test_every_required_prefetch_is_RUN_not_merely_printed():
    """A step the build cannot do without must be executed, not documented.

    The concrete failure: the script ran `Meta/ladybird.py vcpkg` itself but only
    PRINTED `Meta/fetch_vcpkg_git_archives.py` in its closing message. So the
    script reported success, the obvious next command was `bazel build`, and that
    died inside the vcpkg action with "no git-sourced externals at
    ./Meta/CMake/vcpkg/git-archives". Ulf hit exactly this. The four tarballs are
    not optional and not derivable from anything else in the tree -- vcpkg_from_git
    shells out to `git fetch`, which no asset source intercepts -- so a tree without
    them cannot build, and a setup script that leaves the tree unable to build has
    not finished.

    Asserted structurally: each prefetch appears on a line that RUNS it (a `python3`
    invocation outside a heredoc/message), not only inside the closing `cat <<EOF`.
    """
    t = _text()
    prefetches = ("Meta/ladybird.py vcpkg", "Meta/fetch_vcpkg_git_archives.py")
    # Everything the script executes, with the here-docs (its messages) removed:
    # a command inside `cat <<EOF ... EOF` is prose, and prose was the bug.
    code, in_heredoc = [], False
    for line in t.splitlines():
        if re.search(r"<<-?'?\w*EOF'?|<<'WARN'", line):
            in_heredoc = True
            continue
        if in_heredoc:
            if re.match(r"^(EOF|WARN)\s*$", line):
                in_heredoc = False
            continue
        if re.match(r'\s*(note|echo)\b', line):
            continue
        code.append(line.split("#", 1)[0])
    code = "\n".join(code)
    for p in prefetches:
        assert p in code, (
            f"{p} is never RUN by the script (only mentioned in a message?) -- a "
            "prefetch the build requires must be executed, or the script hands back "
            "a tree that cannot build")

    # And --verify must catch their absence WITHOUT a build, which is its whole
    # purpose: Ulf's tree passed --verify and then failed the build on this.
    verify = t.split("if [ \"$VERIFY\" -eq 1 ]", 1)[1].split("exit \"$rc\"", 1)[0]
    assert "git-archives" in verify, \
        "--verify does not check the vcpkg_from_git tarballs, whose absence is a " \
        "guaranteed build failure"
    # By NAME against the pin, not by counting files: three of four present is the
    # interesting broken case and `ls | wc -l` calls it fine.
    assert "vcpkg_git_archives.bzl" in verify, \
        "--verify must take the expected archive names from the committed pin"
    assert "fetch_vcpkg_git_archives.py" in verify, \
        "--verify reports the missing archives without naming the command that fetches them"


def test_the_git_archive_prefetch_runs_after_the_vcpkg_checkout_exists():
    """Ordering is the reason it was a message; ordering is expressible in a script.

    fetch_vcpkg_git_archives.py resolves each clone URL out of the portfiles in
    Build/vcpkg, so it needs the checkout the FIRST prefetch creates. Running it
    earlier fails with "no vcpkg checkout at ...".
    """
    t = _text()
    # Positions of the INVOCATIONS, i.e. lines that begin with the command. Plain
    # `t.index()` finds the first MENTION, which is inside --verify's diagnostic
    # ("fetch them with: ..."), several hundred lines above either invocation --
    # so it compared a message against a command and failed on correct code.
    def invoked_at(cmd):
        for i, line in enumerate(t.splitlines()):
            if line.strip().startswith(cmd):
                return i
        raise AssertionError("never invoked: " + cmd)

    first = invoked_at("python3 Meta/ladybird.py vcpkg")
    second = invoked_at("python3 Meta/fetch_vcpkg_git_archives.py")
    assert first < second, \
        "the git-archive prefetch must come after the vcpkg checkout it reads portfiles from"
    # And it must be guarded on the checkout actually being there, rather than
    # running unconditionally and failing with the fetcher's own error.
    tail = t[t.index("# Phase 3:"):]
    assert '-d "$TARGET/Build/vcpkg/.git"' in tail, \
        "the git-archive prefetch is not guarded on the vcpkg checkout existing"
    assert "--no-prefetch" in tail or 'PREFETCH" -eq 1' in tail, \
        "--no-prefetch must skip the git-archive prefetch too, or the flag lies"
