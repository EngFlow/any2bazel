#!/usr/bin/env bash
# Reproduce the Ladybird tree this migration builds, on any machine.
#
# The overlay is not a fork: it is a pinned upstream Ladybird commit + two
# patches + a set of Bazel files that live alongside CMake's. This script is the
# executable form of that sentence, because "copy the workspace directory over a
# clone" is a recipe with three ways to be silently wrong:
#
#   * the LADYBIRD COMMIT. The generated BUILD.bazel files name ~1,961 LibWeb
#     compile inputs and 665 IDL bindings by path. They were generated from ONE
#     upstream tree; against a different one the build fails on a moved file, or
#     worse, silently omits a new one. Nothing in the overlay recorded which
#     commit that was until this script did.
#   * the PATCHES. Two upstream defects have to be fixed or the build is
#     nondeterministic (generator emits dictionaries in PYTHONHASHSEED order) or
#     does not compile at all under per-header moc (TabBar.h is not
#     self-contained). Both are filed upstream; until they land they are patches.
#   * the ORDER. Two overlay files must not exist before the vcpkg prefetch runs.
#     Build/vcpkg/BUILD.bazel makes the directory Build/vcpkg EXIST, and upstream's
#     Meta/Utils/build_vcpkg.py treats "the directory is there" as "the checkout is
#     there": it skips the clone and runs `git -C Build/vcpkg rev-parse HEAD`, which
#     -- there being no .git inside -- WALKS UP to Ladybird's own repo and returns
#     Ladybird's HEAD. It then tries to check vcpkg's baseline out of the Ladybird
#     repo and dies with `fatal: unable to read tree`. So the prefetch has to happen
#     BEFORE that file is staged, and this script does the two in that order.
#
#   * the RENAME. bazelrc.txt has to become .bazelrc. It is stored under a
#     different name so that a `cp -r` of the overlay into a clone cannot be
#     mistaken for a working build -- and a rename that a human does by hand is a
#     rename a human forgets.
#
# Everything here is pinned and checkable, so a failure is a message rather than
# a mystery. Usage:
#
#   ./apply_overlay.sh /path/to/ladybird           # branch + commits (see below)
#   ./apply_overlay.sh --verify /path/to/existing  # check a tree, change nothing
#
# Both prefetches run here (Meta/ladybird.py vcpkg, then
# Meta/fetch_vcpkg_git_archives.py); after this, `bazel build` is the next command.
# The second one used to be PRINTED rather than run, which cost Ulf a 20-minute
# build: everything the script does succeeded, so the obvious next step was
# `bazel build`, and it failed inside the vcpkg action with "no git-sourced
# externals at ./Meta/CMake/vcpkg/git-archives". A setup script that stops one
# required step short of a working build has not set anything up -- the closing
# message is not a substitute for doing it (finding 35 again: an instruction the
# reader must remember is a step the script decided not to take).
#
# ---------------------------------------------------------------------------
# WHAT THIS LEAVES BEHIND: a BRANCH, with COMMITS. It used to leave a detached
# HEAD with 45 untracked files, which Ulf correctly called idiotic:
#
#   * a detached HEAD is not a place you can work. Every git verb that composes
#     -- rebase, merge, cherry-pick, pull --rebase -- needs a named ref, so the
#     overlay could not be moved onto the branch the reader actually has.
#   * 45 untracked files means `git status` is 45 lines of noise forever, `git
#     diff` shows nothing (untracked files are not diffed), `git log` says
#     nothing happened, and a stray `git clean -fd` deletes the entire overlay.
#   * worst, it ignored what the reader already had. Their branch, their commits,
#     their tree: the script walked past all of it to a floating checkout.
#
# So the overlay is now expressed the way every other change to a git repo is:
# as commits on a branch you can name, inspect, rebase and merge.
#
#   patches/*.patch  ->  one commit each, keeping the patch's own subject
#   workspace/*      ->  one commit ("Bazel overlay: ...")
#
# and by default they go on a branch named after the pin, based on the pin, so a
# repin gets its own branch and your previous one is untouched. Nothing is
# detached and `git status` is clean when it finishes. To put the overlay on top
# of work you already have instead, use --onto-current.
#
#   --qt-prefix DIR  the Qt SDK to build against. MODULE.bazel's qt.local_repo
#                    `paths` line is the ONE line in the overlay that is a fact
#                    about your machine, and copying the overlay over it is how a
#                    re-apply used to silently repoint a working build at the
#                    system Qt. Ulf builds against Qt 6.9.2 in a VENV while his
#                    system Qt is 6.4.2, i.e. the hardcoded /usr/lib/qt6 is not
#                    merely different for him, it is BELOW Ladybird's 6.9 floor --
#                    so the re-apply turned a working tree into a failing one.
#                    Resolution order (first wins), all reported:
#                      1. --qt-prefix DIR
#                      2. the `paths` line already in the target's MODULE.bazel
#                      3. the qmake first on PATH (a venv/aqt SDK puts its own
#                         there, which is exactly the right answer for one)
#                      4. /usr/lib/qt6, the historical default
#                    --verify treats that line as expected-to-differ for the same
#                    reason: it is yours, not ours.
#   --branch NAME    the branch to build (default: ladybird-bazel-<short pin>)
#   --onto-current   base it on your current HEAD, not on the pinned commit --
#                    i.e. apply the overlay ON TOP of your own work. The pin
#                    check becomes a warning, because you are deliberately
#                    building against a tree the generated BUILD files were not
#                    generated from.
#   --no-commit      the OLD behaviour: mutate the working tree, commit nothing.
#                    Kept because a throwaway build directory does not want a
#                    branch, but it is no longer what you get by default.
#
# The one file that is NOT committed is Build/vcpkg/BUILD.bazel: Ladybird's own
# .gitignore ignores `Build*/`, so committing it would need -f and would fight
# upstream's intent. Being ignored, it also produces no `git status` noise, so
# leaving it out costs nothing.
set -euo pipefail

# The upstream commit every generated BUILD file in this overlay was generated
# from, and the only tree they are known to describe. A tag would be wrong here
# for the same reason it was wrong for the HSTS table (see hsts_preload.bzl): a
# tag is a pin to a different tree than the one measured.
LADYBIRD_COMMIT="71fb301a851e4a098e863a7a67e6666599e1cab7"
LADYBIRD_REPO="https://github.com/LadybirdBrowser/ladybird.git"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$HERE/workspace"
PATCHES="$HERE/patches"

die() { echo "error: $*" >&2; exit 1; }
note() { echo "==> $*"; }

VERIFY=0
PREFETCH=1
COMMIT=1
ONTO_CURRENT=0
BRANCH=""
QT_PREFIX=""
# The value baked into workspace/MODULE.bazel, i.e. what a plain copy would impose.
QT_DEFAULT="/usr/lib/qt6"
USAGE="usage: $(basename "$0") [--verify] [--no-prefetch] [--branch NAME]
       [--onto-current] [--no-commit] [--qt-prefix DIR] <ladybird-tree>"
while [ $# -gt 1 ]; do
    case "${1:-}" in
        --verify) VERIFY=1; shift ;;
        --no-prefetch) PREFETCH=0; shift ;;
        --no-commit) COMMIT=0; shift ;;
        --onto-current) ONTO_CURRENT=1; shift ;;
        --branch) BRANCH="${2:-}"; [ -n "$BRANCH" ] || die "$USAGE"; shift 2 ;;
        --branch=*) BRANCH="${1#--branch=}"; shift ;;
        --qt-prefix) QT_PREFIX="${2:-}"; [ -n "$QT_PREFIX" ] || die "$USAGE"; shift 2 ;;
        --qt-prefix=*) QT_PREFIX="${1#--qt-prefix=}"; shift ;;
        -h|--help) echo "$USAGE"; exit 0 ;;
        -*) die "unknown flag: $1
$USAGE" ;;
        *) break ;;
    esac
done
# A lone --verify/--help with no path still has to be accepted by the loop above,
# which stops at $# -eq 1; catch the flag-only forms here.
case "${1:-}" in
    --verify) VERIFY=1; shift ;;
    -h|--help) echo "$USAGE"; exit 0 ;;
esac
[ $# -eq 1 ] || die "$USAGE"
TARGET="$1"

# The default branch name carries the pin, so a repin lands on a NEW branch and
# the one you built last time still exists, still builds, and is still yours.
[ -n "$BRANCH" ] || BRANCH="ladybird-bazel-${LADYBIRD_COMMIT:0:12}"

[ -d "$WORKSPACE" ] || die "no workspace/ next to this script ($WORKSPACE)"

# ---------------------------------------------------------------------------
# The file list is derived from the overlay itself, never hand-maintained: a
# hand-kept list is how a newly added .bzl gets committed, documented, and then
# copied by nothing (which is exactly what happened to vcpkg_git_archives.bzl --
# it was generated, committed, listed in the README table, and loaded by no one).
# Written to a temp file rather than consumed through process substitution:
# `< <(...)` needs /dev/fd, which some sandboxes and minimal shells do not give
# you, and this script has to run on the machine the reader has, not mine.
FILE_LIST="$(mktemp)"
trap 'rm -f "$FILE_LIST"' EXIT

overlay_files() {
    (cd "$WORKSPACE" && find . -type f ! -name '*.pyc' -printf '%P\n' | sort) > "$FILE_LIST"
    cat "$FILE_LIST"
}

# bazelrc.txt -> .bazelrc is the one path that differs between the overlay and
# the tree. Keep the mapping in ONE place; both copy and verify read it.
target_path() {
    case "$1" in
        bazelrc.txt) echo ".bazelrc" ;;
        *) echo "$1" ;;
    esac
}

# ---------------------------------------------------------------------------
# The Qt prefix: the one line in the overlay that is a fact about YOUR machine.
#
# Everything else here is a fact about Ladybird at the pin, identical on every
# host. `qt.local_repo(paths = {"linux-x86_64": ...})` is not: it names an SDK, and
# copying the overlay over it silently replaced a working answer with the answer
# from the capturing machine. Ulf: "We're using Qt (6.9.2) from a VENV, and system
# Qt is 6.4.2" -- so the copy did not just change his configuration, it moved him
# BELOW Ladybird's 6.9 floor, turning a working tree into a failing one on re-apply.
_qt_line_path() { grep -nE '^\s*paths = \{"linux-x86_64":' "$1" 2>/dev/null | head -1; }

# The prefix currently configured in a tree's MODULE.bazel, if any.
qt_prefix_in_tree() {
    [ -f "$1/MODULE.bazel" ] || return 1
    sed -n 's|^[[:space:]]*paths = {"linux-x86_64": "\([^"]*\)".*|\1|p' \
        "$1/MODULE.bazel" 2>/dev/null | head -1
}

# The SDK whose qmake is first on PATH. For a venv/aqt Qt that is precisely the
# right answer -- activating the venv is how you say which Qt you mean -- and it is
# how rules_qt would find it if `paths` were not hardcoded.
qt_prefix_from_qmake() {
    command -v qmake6 >/dev/null 2>&1 && q=qmake6 || q=qmake
    command -v "$q" >/dev/null 2>&1 || return 1
    p="$("$q" -query QT_INSTALL_PREFIX 2>/dev/null)" || return 1
    [ -n "$p" ] && [ -d "$p" ] && echo "$p"
}

qt_version_at() {
    for q in "$1/bin/qmake6" "$1/bin/qmake"; do
        [ -x "$q" ] && "$q" -query QT_VERSION 2>/dev/null && return 0
    done
    return 1
}

# Resolve, in the documented order, and SAY which rule won: a silent default is
# what made this a bug in the first place.
#
# Sets the GLOBALS QT_RESOLVED/QT_SOURCE rather than echoing: called through
# `$(...)` the assignment to QT_SOURCE would happen in a subshell and be lost --
# which it was, printing "(from )" on the first run of this code.
resolve_qt_prefix() {
    local tree="$1" from
    if [ -n "$QT_PREFIX" ]; then
        QT_RESOLVED="$QT_PREFIX"; QT_SOURCE="--qt-prefix"; return
    fi
    if from="$(qt_prefix_in_tree "$tree")" && [ -n "$from" ]; then
        QT_RESOLVED="$from"; QT_SOURCE="the paths line already in your MODULE.bazel"; return
    fi
    if from="$(qt_prefix_from_qmake)" && [ -n "$from" ]; then
        QT_RESOLVED="$from"; QT_SOURCE="the qmake first on your PATH"; return
    fi
    QT_RESOLVED="$QT_DEFAULT"; QT_SOURCE="the overlay's default"
}

# Rewrite the copied MODULE.bazel's paths line in place.
set_qt_prefix_in() {
    local file="$1" prefix="$2"
    grep -qE '^\s*paths = \{"linux-x86_64":' "$file" \
        || die "MODULE.bazel has no qt.local_repo \`paths\` line to point at your Qt.
    The overlay's shape changed; --qt-prefix cannot be applied. (File left alone.)"
    # | as the sed delimiter: a path contains / and must not need escaping.
    sed -i "s|^\([[:space:]]*\)paths = {\"linux-x86_64\": \"[^\"]*\"|\1paths = {\"linux-x86_64\": \"$prefix\"|" "$file"
}

# ---------------------------------------------------------------------------
overlay_files > /dev/null   # populate $FILE_LIST once, for both modes

# Resolved BEFORE the copy phase, because rule 2 reads the value in the target's
# MODULE.bazel and the copy is about to overwrite it.
QT_SOURCE=""
QT_RESOLVED=""
if [ "$VERIFY" -eq 0 ]; then
    resolve_qt_prefix "$TARGET"
fi

if [ "$VERIFY" -eq 1 ]; then
    [ -d "$TARGET" ] || die "$TARGET does not exist"
    cd "$TARGET"
    rc=0

    # The pin is now an ANCESTOR of HEAD, not HEAD itself: the overlay and the
    # patches are commits on top of it. Asking `HEAD == pin` was right when the
    # script left a detached checkout of the pin with everything uncommitted, and
    # became wrong the moment the overlay became commits -- it reported MISMATCH
    # for every correctly-built tree. So ask the question that is actually meant:
    # is the tree BUILT ON the commit the BUILD files were generated from?
    have="$(git rev-parse HEAD 2>/dev/null || echo none)"
    if [ "$have" = "$LADYBIRD_COMMIT" ]; then
        note "commit OK (at the pin $LADYBIRD_COMMIT)"
    elif git merge-base --is-ancestor "$LADYBIRD_COMMIT" HEAD 2>/dev/null; then
        extra="$(git rev-list --count "$LADYBIRD_COMMIT..HEAD")"
        note "commit OK (pin $(git rev-parse --short "$LADYBIRD_COMMIT") + $extra commits on top)"
        # Commits on top are the overlay itself, but they could also be upstream
        # commits the reader merged -- which moves sources the BUILD files name.
        # Distinguish, because only the second kind is a risk.
        foreign="$(git log --format='%h %s' "$LADYBIRD_COMMIT..HEAD" \
                   | grep -vE ' (Bazel overlay:|LibRequests:|Requests:)' || true)"
        if [ -n "$foreign" ]; then
            note "  NOTE: $(echo "$foreign" | wc -l) of those are not overlay commits:"
            echo "$foreign" | sed 's/^/        /'
            note "  if any of them move or add sources, the generated BUILD files"
            note "  (which name ~1,961 paths) may be stale against this tree."
        fi
    else
        echo "MISMATCH commit: the pin $LADYBIRD_COMMIT is not an ancestor of HEAD ($have)." >&2
        echo "  the generated BUILD files name sources by path; a different tree may" >&2
        echo "  fail on a moved file or silently omit a new one." >&2
        rc=1
    fi

    missing=0; differ=0; same=0
    while IFS= read -r f; do
        t="$(target_path "$f")"
        if [ ! -e "$t" ]; then
            # A file under Build/vcpkg is legitimately absent until the prefetch has
            # run: staging it early is what breaks upstream's bootstrap.
            case "$f" in
                Build/vcpkg/*)
                    if [ ! -d "Build/vcpkg/.git" ]; then
                        note "pending (run the vcpkg prefetch, then re-apply): $t"
                        continue
                    fi ;;
            esac
            echo "MISSING $t" >&2; missing=$((missing + 1))
        elif ! cmp -s "$WORKSPACE/$f" "$t"; then
            # MODULE.bazel's qt.local_repo `paths` line is EXPECTED to differ: it
            # names the reader's Qt SDK, which is a fact about their machine and not
            # part of the overlay. Reporting it as DIFFERS told people to overwrite
            # their own correct configuration -- and following that advice is how a
            # venv Qt 6.9.2 got replaced by a system Qt 6.4.2, below Ladybird's
            # floor. So: compare with that ONE line normalised, and if the rest is
            # identical, report the prefix instead of a failure.
            if [ "$f" = "MODULE.bazel" ]; then
                a="$(mktemp)"; b="$(mktemp)"
                sed 's|^\([[:space:]]*\)paths = {"linux-x86_64": "[^"]*"|\1paths = {"linux-x86_64": "@@QT@@"|' \
                    "$WORKSPACE/$f" > "$a"
                sed 's|^\([[:space:]]*\)paths = {"linux-x86_64": "[^"]*"|\1paths = {"linux-x86_64": "@@QT@@"|' \
                    "$t" > "$b"
                if cmp -s "$a" "$b"; then
                    have_qt="$(qt_prefix_in_tree "$(dirname "$t")" || true)"
                    [ -n "$have_qt" ] || have_qt="$(sed -n 's|^[[:space:]]*paths = {"linux-x86_64": "\([^"]*\)".*|\1|p' "$t" | head -1)"
                    note "MODULE.bazel matches except the Qt SDK path (yours: ${have_qt:-?}) -- expected"
                    same=$((same + 1))
                    rm -f "$a" "$b"
                    continue
                fi
                rm -f "$a" "$b"
            fi
            echo "DIFFERS $t" >&2; differ=$((differ + 1))
        else
            same=$((same + 1))
        fi
    done < "$FILE_LIST"
    note "overlay files: $same identical, $differ differing, $missing missing"
    [ "$missing" -eq 0 ] && [ "$differ" -eq 0 ] || rc=1

    # The patches must be APPLIED, not merely present. `git apply --check -R`
    # succeeding is the proof: a patch that reverse-applies cleanly is a patch
    # already in the tree.
    #
    # But reverse-apply proves "MY EXACT BYTES are in the tree", which is a
    # stronger claim than "the defect is fixed" -- and the difference is not
    # hypothetical: upstream landed its own fix for the fd leak, so a tree that is
    # CORRECT (and newer than the pin) was reported as PATCH NOT APPLIED, telling
    # the user to apply a patch that would then conflict. A patch we carry only
    # until upstream fixes it needs a second, weaker question: is the EFFECT there?
    # `.effect-grep` next to a patch holds one extended regex per line; if every
    # one matches the file the patch touches, the effect is present however it got
    # there. Reverse-apply is still tried first, so the exact-bytes case keeps its
    # precise answer.
    #
    # Asked of the SERIES first, for the reason spelled out at the apply loop
    # below: 0002 rewrites 0001's context, so on a fully patched tree the
    # per-patch reverse-check fails on 0001 and --verify reported PATCH NOT
    # APPLIED about a correct tree.
    if cat "$PATCHES"/*.patch | git apply --check -R - >/dev/null 2>&1; then
        note "patch series applied (checked as a series)"
        series_ok=1
    else
        series_ok=0
    fi
    for p in "$PATCHES"/*.patch; do
        name="$(basename "$p")"
        if [ "$series_ok" -eq 1 ] || git apply --check -R "$p" >/dev/null 2>&1; then
            note "patch applied: $name"
            continue
        fi
        effect="${p%.patch}.effect-grep"
        if [ -f "$effect" ]; then
            target="$(sed -n 's|^+++ b/||p' "$p" | head -1)"
            if [ -f "$target" ]; then
                # An `@window <n> <regex>` directive narrows the search to the n
                # lines following the first match, because a whole-file grep is
                # too weak to be worth anything here: `defer_teardown();` already
                # occurs in stop() and did_transfer(), so an unpatched tree passed
                # a whole-file check. A test pins that negative case.
                win="$(grep -E '^@window ' "$effect" | head -1)"
                slice="$target"
                if [ -n "$win" ]; then
                    n="$(echo "$win" | awk '{print $2}')"
                    re="$(echo "$win" | cut -d' ' -f3-)"
                    start="$(grep -nE "$re" "$target" | head -1 | cut -d: -f1)"
                    if [ -z "$start" ]; then
                        echo "PATCH NOT APPLIED: $name (anchor not found: $re)" >&2; rc=1
                        continue
                    fi
                    slice="$(mktemp)"
                    sed -n "${start},$((start + n))p" "$target" > "$slice"
                fi
                unmatched=0
                while IFS= read -r re; do
                    [ -n "$re" ] || continue
                    case "$re" in \#*|@*) continue ;; esac
                    grep -Eq "$re" "$slice" || unmatched=$((unmatched + 1))
                done < "$effect"
                [ "$slice" = "$target" ] || rm -f "$slice"
                if [ "$unmatched" -eq 0 ]; then
                    note "patch effect present (not our bytes -- fixed upstream?): $name"
                    continue
                fi
            fi
        fi
        echo "PATCH NOT APPLIED: $name" >&2; rc=1
    done

    # The four vcpkg_from_git tarballs. NOT overlay files (they are 200MB of git
    # archive output, and they are .gitignored), so the file loop above cannot see
    # them -- but their absence is a guaranteed build failure, which is exactly what
    # --verify exists to find without a build. Ulf's tree passed --verify and then
    # failed the build on this.
    #
    # Checked by NAME against the committed pin, not by count: a directory with
    # three of the four in it is the interesting broken case, and `ls | wc -l` calls
    # it fine. Hashes are not re-verified here (that is minutes of sha512 over
    # ~200MB, and the fetcher already verified them at write time); --verify is the
    # cheap check you run often.
    archives_dir="Meta/CMake/vcpkg/git-archives"
    want_archives="$(sed -n "s|^[[:space:]]*'\([^']*\.tar\.gz\)':.*|\1|p" \
                     "$WORKSPACE/vcpkg_git_archives.bzl")"
    if [ -z "$want_archives" ]; then
        echo "MALFORMED: no archives parsed out of vcpkg_git_archives.bzl" >&2; rc=1
    else
        missing_archives=0
        for a in $want_archives; do
            [ -f "$archives_dir/$a" ] || { echo "MISSING $archives_dir/$a" >&2
                missing_archives=$((missing_archives + 1)); }
        done
        if [ "$missing_archives" -gt 0 ]; then
            echo "  $missing_archives of $(echo "$want_archives" | wc -w) vcpkg_from_git archives are absent." >&2
            echo "  vcpkg_from_git bypasses the asset cache (it runs \`git fetch\`), so these" >&2
            echo "  cannot be http_file'd; the build stages them from that directory and" >&2
            echo "  fails without them. Fetch them (pure git, ~80s, verified against the pin):" >&2
            echo "      cd $TARGET && python3 Meta/fetch_vcpkg_git_archives.py" >&2
            rc=1
        else
            note "vcpkg_from_git archives: $(echo "$want_archives" | wc -w)/$(echo "$want_archives" | wc -w) present"
        fi
    fi

    # Executable bits are tree state git carries and a `cp` does not always: the
    # vcpkg/cargo build scripts are run as actions, so a lost +x fails at action
    # time, deep in a build, with a confusing message.
    while IFS= read -r f; do
        case "$f" in *.sh) [ -x "$(target_path "$f")" ] || {
            echo "NOT EXECUTABLE: $(target_path "$f")" >&2; rc=1; }; ;;
        esac
    done < "$FILE_LIST"

    [ "$rc" -eq 0 ] && note "VERIFIED: this tree matches the overlay" \
                    || echo "verification FAILED" >&2
    exit "$rc"
fi

# ---------------------------------------------------------------------------
if [ -e "$TARGET/.git" ]; then
    note "using existing clone at $TARGET"
    cd "$TARGET"
    git rev-parse --verify "$LADYBIRD_COMMIT^{commit}" >/dev/null 2>&1 \
        || git fetch --no-tags origin "$LADYBIRD_COMMIT"
    # Where the reader IS, recorded before anything moves, so the summary at the
    # end can tell them how to get back and so --onto-current has a base.
    WAS_REF="$(git symbolic-ref --quiet --short HEAD || git rev-parse --short HEAD)"
    note "you are on: $WAS_REF"

    # A tree that already HAS the overlay is the normal case for a REPIN. With the
    # overlay committed on a branch this is no longer a dirty tree at all -- the
    # previous run's work is a COMMIT, so it does not collide with a checkout, and
    # this whole class of failure goes away. But a tree that has been through the
    # OLD script (or that the reader has been editing) still has modified tracked
    # files, and those must not be silently destroyed.
    #
    # STASH rather than `checkout --` / `reset --hard`: the modifications might not
    # all be ours. A patch we no longer carry is indistinguishable from the
    # reader's own debugging edit, and a script that silently discards the second
    # kind is a script nobody should run on a tree they care about. A stash is
    # recoverable and its name says who made it.
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        note "this tree has UNCOMMITTED changes to tracked files (a previous run of"
        note "this script left them loose, or they are yours). Stashing -- nothing"
        note "is discarded:"
        git status --porcelain --untracked-files=no | sed 's/^/      /'
        # No --include-untracked: the untracked files are the OVERLAY, which the
        # copy below overwrites anyway, and stashing them would hide the staged
        # Build/vcpkg state the prefetch ordering depends on.
        git stash push --quiet \
            -m "apply_overlay.sh: tree state before $BRANCH" \
            || die "could not stash local changes; commit or stash them yourself,
    then re-run. (This tree is left exactly as it was.)"
        note "stashed as: $(git stash list | head -1)"
        note "  recover with: git -C $TARGET stash pop"
    fi
else
    note "cloning Ladybird (full history: vcpkg needs it, see README)"
    git clone "$LADYBIRD_REPO" "$TARGET"
    cd "$TARGET"
    WAS_REF="$(git symbolic-ref --quiet --short HEAD || git rev-parse --short HEAD)"
fi

# ---------------------------------------------------------------------------
# Get onto a BRANCH. Never a detached HEAD: see the header. Three cases, and each
# one ends with HEAD being a symbolic ref to $BRANCH.
if [ "$COMMIT" -eq 0 ]; then
    # --no-commit keeps the old shape for a throwaway tree. Still not detached if
    # we can avoid it: if the pin is already what HEAD resolves to, stay put.
    note "--no-commit: mutating the working tree, committing nothing"
    if [ "$(git rev-parse HEAD)" != "$LADYBIRD_COMMIT" ]; then
        git checkout --detach "$LADYBIRD_COMMIT"
        note "NOTE: this leaves a DETACHED HEAD (that is what --no-commit means)."
    fi
elif [ "$ONTO_CURRENT" -eq 1 ]; then
    # Apply the overlay on top of the reader's own work. Their HEAD is the base.
    base="$(git rev-parse HEAD)"
    if [ "$base" != "$LADYBIRD_COMMIT" ]; then
        note "--onto-current: basing the overlay on YOUR HEAD ($(git rev-parse --short HEAD)),"
        note "  not on the pinned commit $LADYBIRD_COMMIT."
        note "  WARNING: the generated BUILD files name ~1,961 sources by path and were"
        note "  generated from the pin. Against another tree the build can fail on a"
        note "  moved file or silently omit a new one. That is the trade you just made."
    fi
    if [ "$(git symbolic-ref --quiet --short HEAD || true)" = "$BRANCH" ]; then
        note "already on $BRANCH"
    else
        git checkout -b "$BRANCH" 2>/dev/null \
            || die "branch '$BRANCH' already exists and is not what you are on.
    Pick another with --branch NAME, or check it out yourself first."
        note "created branch $BRANCH at $(git rev-parse --short HEAD)"
    fi
else
    # The default: a branch named after the pin, based on the pin.
    #
    # A re-run must be idempotent rather than an error, because "run it again" is
    # what everyone does. If the branch exists we RESET it to the pin and rebuild
    # the commits -- but only after checking it holds nothing but ours, since a
    # reset would otherwise discard the reader's commits on that branch.
    if git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null; then
        git checkout --quiet "$BRANCH"
        foreign="$(git log --format='%H %s' "$LADYBIRD_COMMIT..HEAD" 2>/dev/null \
                   | grep -vE ' (Bazel overlay:|LibRequests:|Requests:)' || true)"
        if [ -n "$foreign" ]; then
            die "branch '$BRANCH' has commits that are not this overlay's:

$(echo "$foreign" | sed 's/^/      /')

    I will not reset a branch holding your work. Either build the overlay on a
    fresh branch:
        $(basename "$0") --branch $BRANCH-new $TARGET
    or put the overlay on TOP of those commits:
        $(basename "$0") --onto-current $TARGET"
        fi
        note "re-running on existing branch $BRANCH: resetting to the pin to rebuild"
        note "  its commits (only overlay commits were on it; yours would have stopped this)"
        git reset --quiet --hard "$LADYBIRD_COMMIT"
    else
        git checkout --quiet -b "$BRANCH" "$LADYBIRD_COMMIT"
        note "created branch $BRANCH at the pin $LADYBIRD_COMMIT"
    fi
fi
note "at $(git rev-parse HEAD)$([ "$COMMIT" -eq 1 ] && echo " (on $(git symbolic-ref --quiet --short HEAD || echo 'DETACHED'))")"

note "applying $(ls "$PATCHES"/*.patch | wc -l) patches (all reported upstream)"
# patches/*.patch is a SERIES, applied in glob (numeric) order: a later patch may
# depend on an earlier one having landed. 0004 edits lines adjacent to 0003's inside
# the same branch, so it is generated against 0003 and only applies after it.
#
# That makes the glob load-bearing in a way it was not before. I briefly shipped two
# mutually-exclusive variants of 0004 -- one for a tree with the teardown fix, one for
# a tree without -- and since this loop applies EVERY *.patch, one of them could only
# ever fail. Ulf hit it immediately: "tries to apply both patches at the same time".
# Alternatives do not belong in this directory; a variant that is not part of the
# series goes beside DIAGNOSTIC-*.patch.txt, outside the glob. The series-applies-as-a-
# series property is now pinned by a test, so a future patch that silently conflicts
# with its predecessor fails in CI rather than on someone's clone.
#
# Each patch becomes its OWN COMMIT, keeping its own Subject: line, so `git log`
# reads as the series it is and any one of them can be reverted, rebased or
# cherry-picked on its own. `git apply` + `git commit` rather than `git am`: these
# are not mailbox files (no `From ` envelope line), and am on a non-mbox fails in
# a way that would send the reader looking for a mail bug.
#
# "Is the series already applied?" must be asked of the SERIES, not of each patch
# on its own -- and this is a bug the old script had too, found by running
# --onto-current against a tree that already had the overlay committed:
#
#   error: patch failed: Libraries/LibRequests/Request.cpp:327
#   error: failed to apply 0001-...patch
#
# 0002 edits lines ADJACENT to 0001's inside the same function, so on a fully
# patched tree `git apply --check -R 0001` fails: 0001's context lines no longer
# exist, 0002 having rewritten them. The per-patch test therefore says "not
# applied" about a patch that IS applied, and the script tries to apply it again
# and dies. Reverse-checking the concatenation asks the right question -- is the
# whole series present -- and is verified both ways: OK on the patched tree,
# correctly failing on the pin.
if cat "$PATCHES"/*.patch | git apply --check -R - >/dev/null 2>&1; then
    note "  the whole series is already applied; nothing to do"
    PATCH_LIST=""
else
    PATCH_LIST="$(ls "$PATCHES"/*.patch)"
fi
for p in $PATCH_LIST; do
    name="$(basename "$p")"
    if git apply --check -R "$p" >/dev/null 2>&1; then
        note "  already applied: $name"
        continue
    fi
    git apply "$p" || die "failed to apply $name -- patches/ is a
    series applied in order; a patch here must apply on top of the ones before it.
    If this is an ALTERNATIVE to another patch rather than an addition, it must not
    live in patches/*.patch, which is globbed and applied in full."
    note "  applied: $name"
    if [ "$COMMIT" -eq 1 ]; then
        # The patch's own Subject:, minus the [PATCH] prefix. Falls back to the
        # filename so a patch without a Subject still gets a legible commit.
        subject="$(sed -n 's/^Subject: \(\[PATCH[^]]*\] \)\?//p' "$p" | head -1)"
        [ -n "$subject" ] || subject="apply ${name%.patch}"
        # Only the files this patch touched: never `add -A`, which would sweep in
        # the reader's untracked files and the overlay that has not been copied yet.
        sed -n 's|^+++ b/||p' "$p" | sort -u | while IFS= read -r f; do
            [ -e "$f" ] && git add -- "$f"
        done
        git -c user.email="$(git config user.email || echo overlay@any2bazel)" \
            -c user.name="$(git config user.name || echo 'any2bazel overlay')" \
            commit --quiet --no-verify -m "$subject" \
            -m "From examples/ladybird/patches/$name in the any2bazel Ladybird
migration overlay. Reported upstream; see that file's header for the analysis." \
            || die "could not commit $name"
        note "    committed: $(git log -1 --format=%h) $subject"
    fi
done

# Phase 1: everything except the files under Build/vcpkg. Staging those creates the
# directory upstream's bootstrap reads as "already cloned" -- see the ORDER note at
# the top of this file.
note "copying the overlay"
n=0
deferred=0
while IFS= read -r f; do
    case "$f" in Build/vcpkg/*) deferred=$((deferred + 1)); continue ;; esac
    t="$(target_path "$f")"
    mkdir -p "$(dirname "$t")"
    cp -p "$WORKSPACE/$f" "$t"
    n=$((n + 1))
done < "$FILE_LIST"
note "$n files copied (bazelrc.txt -> .bazelrc), $deferred deferred until vcpkg exists"

# The Qt prefix, restored/set AFTER the copy overwrote MODULE.bazel. Resolved
# before the copy so rule 2 can read the value the copy is about to destroy.
if [ -n "$QT_RESOLVED" ]; then
    set_qt_prefix_in "$TARGET/MODULE.bazel" "$QT_RESOLVED"
    if [ "$QT_RESOLVED" = "$QT_DEFAULT" ]; then
        note "Qt SDK: $QT_RESOLVED (from $QT_SOURCE)"
    else
        note "Qt SDK: $QT_RESOLVED (from $QT_SOURCE) -- NOT the overlay's"
        note "  $QT_DEFAULT default; your choice was preserved, not overwritten."
    fi
    if v="$(qt_version_at "$QT_RESOLVED")" && [ -n "$v" ]; then
        note "  that Qt reports version $v"
        case "$v" in
            6.[0-8].*|[0-5].*)
                note "  WARNING: Ladybird requires Qt >= 6.9 (UI/Qt/CMakeLists.txt)."
                note "  The build will stop in qt_runtime.bzl naming this prefix." ;;
        esac
    else
        note "  NOTE: no qmake under $QT_RESOLVED/bin -- if that is wrong, pass"
        note "  --qt-prefix DIR (rules_qt runs \`qmake -query\` there)."
    fi
fi

# ...and committed, so they are tracked files in a commit rather than 45 lines of
# untracked noise in `git status`. -f is needed for .bazelrc only if a future
# upstream .gitignore covers it; the deferred Build/vcpkg file is NOT committed at
# all (Ladybird ignores Build*/ and being ignored it makes no noise anyway).
if [ "$COMMIT" -eq 1 ]; then
    while IFS= read -r f; do
        case "$f" in Build/vcpkg/*) continue ;; esac
        git add --force -- "$(target_path "$f")"
    done < "$FILE_LIST"
    if [ -n "$(git diff --cached --name-only)" ]; then
        git -c user.email="$(git config user.email || echo overlay@any2bazel)" \
            -c user.name="$(git config user.name || echo 'any2bazel overlay')" \
            commit --quiet --no-verify \
            -m "Bazel overlay: build Ladybird with Bazel alongside CMake" \
            -m "Generated BUILD.bazel/*.bzl files plus their emitters, from the
any2bazel Ladybird migration at upstream commit $LADYBIRD_COMMIT.
Adds no CMake changes: the two build systems sit side by side.

Build/vcpkg/BUILD.bazel is deliberately NOT in this commit -- Ladybird's
.gitignore covers Build*/, and it must not exist before the vcpkg
prefetch runs (it makes upstream's bootstrap read the bare directory as
an existing checkout)." \
            || die "could not commit the overlay"
        note "committed the overlay: $(git log -1 --format=%h)"
    else
        note "overlay already committed and unchanged"
    fi
fi

# Phase 2: the vcpkg prefetch, then the deferred files.
#
# Deferring the copy is not sufficient on a REPIN. The trap at the top of this
# file is that the DIRECTORY Build/vcpkg existing (with no .git in it) makes
# upstream's bootstrap skip the clone -- and on a tree that already has an older
# overlay, Build/vcpkg/BUILD.bazel is ALREADY THERE, put there by the previous
# run. So phase 1 not creating it changes nothing, and the prefetch dies exactly
# as documented:
#
#   fatal: unable to read tree (40f3c709...)
#   subprocess.CalledProcessError: ['git','checkout','40f3c709...'] status 128
#
# (it walks up to Ladybird's repo, gets Ladybird's HEAD, and tries to check
# vcpkg's baseline out of it). Found by running the repin against a replica of
# Ulf's tree; the deferral logic had only ever been tested on a fresh clone.
#
# So: if there is no .git, the directory is not a checkout, and anything in it is
# ours to move out of the way. Only the overlay's own deferred files are removed
# -- never a directory with a .git, and never anything the overlay does not own.
if [ ! -d "$TARGET/Build/vcpkg/.git" ] && [ -d "$TARGET/Build/vcpkg" ]; then
    while IFS= read -r f; do
        case "$f" in Build/vcpkg/*) ;; *) continue ;; esac
        if [ -e "$TARGET/$f" ]; then
            note "un-staging $f so the vcpkg bootstrap sees no checkout"
            rm -f "$TARGET/$f"
        fi
    done < "$FILE_LIST"
    # An empty Build/vcpkg is just as fatal as one with a file in it: the
    # bootstrap tests for the DIRECTORY. rmdir, not rm -rf: if anything else is
    # in there it is not ours and the failure should be loud.
    rmdir "$TARGET/Build/vcpkg" 2>/dev/null && note "removed the empty Build/vcpkg" || true
fi

if [ -d "$TARGET/Build/vcpkg/.git" ]; then
    note "vcpkg checkout already present"
elif [ "$PREFETCH" -eq 1 ]; then
    note "running the vcpkg prefetch (upstream's own bootstrap, ~70s, no CMake)"
    python3 Meta/ladybird.py vcpkg
else
    note "SKIPPING the vcpkg prefetch (--no-prefetch)"
fi

if [ -d "$TARGET/Build/vcpkg/.git" ]; then
    while IFS= read -r f; do
        case "$f" in Build/vcpkg/*) ;; *) continue ;; esac
        mkdir -p "$(dirname "$f")"
        cp -p "$WORKSPACE/$f" "$f"
        note "staged $f"
    done < "$FILE_LIST"
else
    cat >&2 <<'WARN'
==> NOT staged: Build/vcpkg/BUILD.bazel
    Stage it only AFTER the vcpkg checkout exists, or upstream's bootstrap reads
    the bare directory as an existing clone and fails with
    `fatal: unable to read tree`. Do:
        python3 Meta/ladybird.py vcpkg      # creates Build/vcpkg + its .git
    then re-run this script to stage the deferred file.
WARN
fi

# Phase 3: the SECOND prefetch -- the four vcpkg_from_git tarballs.
#
# This has to run here, not in the closing message, because there is no build
# without it. `vcpkg_from_git` shells out to `git fetch`, which no asset source
# intercepts, so these four cannot be http_file'd like the other 76 distfiles; but
# vcpkg DOES honour a pre-placed downloads/<PORT>-<REF>.tar.gz, which is what they
# become. Meta/vcpkg_build.sh hard-fails in four seconds when they are absent (it
# used to fail ~20 minutes in, inside skia's portfile, naming a googlesource URL) --
# and that fast, clear failure was still a failure Ulf hit on a tree this script
# had just reported as done. The instruction was in the closing message. He, quite
# reasonably, ran `bazel build`.
#
# It needs the vcpkg checkout (it resolves each clone URL out of the portfiles), so
# it must come after phase 2 -- that ordering is the reason it was a message in the
# first place, and ordering is a thing a script can express.
if [ -d "$TARGET/Build/vcpkg/.git" ] && [ "$PREFETCH" -eq 1 ]; then
    note "fetching the 4 vcpkg_from_git tarballs (~80s, verified against the pin)"
    python3 Meta/fetch_vcpkg_git_archives.py \
        || die "the git-archive prefetch failed. It is pure git (clone + git archive,
    checked against the SHA512s in vcpkg_git_archives.bzl), so this is a network
    or a pin problem, not a build problem. Re-run just this step with:
        cd $TARGET && python3 Meta/fetch_vcpkg_git_archives.py"
elif [ "$PREFETCH" -eq 0 ]; then
    note "SKIPPING the git-archive prefetch (--no-prefetch)"
    note "  \`bazel build\` will fail without it: python3 Meta/fetch_vcpkg_git_archives.py"
fi

cat <<EOF

==> done. Both prefetches have run; the next command is the build:

    cd $TARGET
    bazel build //:ladybird

    Re-check this tree at any time with:
        $(basename "$0") --verify $TARGET
EOF

if [ "$COMMIT" -eq 1 ] && git symbolic-ref -q HEAD >/dev/null; then
    cat <<EOF
==> the overlay is $(git rev-list --count "$LADYBIRD_COMMIT..HEAD" 2>/dev/null || echo '?') commits on branch '$(git symbolic-ref --short HEAD)':

$(git log --oneline "$LADYBIRD_COMMIT..HEAD" 2>/dev/null | sed 's/^/      /')

    \`git status\` is clean. To put these on top of your own work instead:
        git -C $TARGET rebase $(git symbolic-ref --short HEAD) $WAS_REF
    or take just the fixes and none of the Bazel files:
        git -C $TARGET cherry-pick <the LibRequests commits above>
    To go back to where you were:
        git -C $TARGET checkout $WAS_REF
EOF
fi
