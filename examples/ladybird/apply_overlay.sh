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
#   ./apply_overlay.sh /path/to/new/ladybird       # clone + patch + overlay
#   ./apply_overlay.sh --verify /path/to/existing  # check a tree, change nothing
#
# Then, per the README's "Reproducing": two prefetches (Meta/ladybird.py vcpkg,
# Meta/fetch_vcpkg_git_archives.py) and `bazel build`.
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
while [ $# -gt 1 ] || [ "${1:-}" = "--verify" ] || [ "${1:-}" = "--no-prefetch" ]; do
    case "${1:-}" in
        --verify) VERIFY=1; shift ;;
        --no-prefetch) PREFETCH=0; shift ;;
        *) break ;;
    esac
done
[ $# -eq 1 ] || die "usage: $(basename "$0") [--verify] [--no-prefetch] <ladybird-tree>"
TARGET="$1"

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
overlay_files > /dev/null   # populate $FILE_LIST once, for both modes

if [ "$VERIFY" -eq 1 ]; then
    [ -d "$TARGET" ] || die "$TARGET does not exist"
    cd "$TARGET"
    rc=0

    have="$(git rev-parse HEAD 2>/dev/null || echo none)"
    if [ "$have" = "$LADYBIRD_COMMIT" ]; then
        note "commit OK ($LADYBIRD_COMMIT)"
    else
        echo "MISMATCH commit: tree is at $have, overlay was generated from $LADYBIRD_COMMIT" >&2
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
    for p in "$PATCHES"/*.patch; do
        name="$(basename "$p")"
        if git apply --check -R "$p" >/dev/null 2>&1; then
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
    note "reusing existing clone at $TARGET"
    cd "$TARGET"
    git rev-parse --verify "$LADYBIRD_COMMIT^{commit}" >/dev/null 2>&1 \
        || git fetch --no-tags origin "$LADYBIRD_COMMIT"
    git checkout --detach "$LADYBIRD_COMMIT"
else
    note "cloning Ladybird (full history: vcpkg needs it, see README)"
    git clone "$LADYBIRD_REPO" "$TARGET"
    cd "$TARGET"
    git checkout --detach "$LADYBIRD_COMMIT"
fi
note "at $(git rev-parse HEAD)"

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
for p in "$PATCHES"/*.patch; do
    if git apply --check -R "$p" >/dev/null 2>&1; then
        note "  already applied: $(basename "$p")"
    else
        git apply "$p" || die "failed to apply $(basename "$p") -- patches/ is a
    series applied in order; a patch here must apply on top of the ones before it.
    If this is an ALTERNATIVE to another patch rather than an addition, it must not
    live in patches/*.patch, which is globbed and applied in full."
        note "  applied: $(basename "$p")"
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

# Phase 2: the vcpkg prefetch, then the deferred files.
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

cat <<EOF

==> done. One prefetch remains (it needs no CMake):

    cd $TARGET
    python3 Meta/fetch_vcpkg_git_archives.py    # the 4 vcpkg_from_git tarballs (~80s)
    bazel build //:ladybird //:WebContent //:RequestServer //:ImageDecoder \\
                //:Compositor //:WebWorker

    Re-check this tree at any time with:
        $(basename "$0") --verify $TARGET
EOF
