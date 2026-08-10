#!/bin/bash
# Regenerate the vcpkg_from_git pin, without CMake and without a full build.
#
# THE PROBLEM. Four of vcpkg's inputs are not distfiles: skia and angle pull
# sub-dependencies with `vcpkg_from_git`, which bypasses the asset cache
# entirely. `x-asset-sources` never sees them, so Meta/vcpkg_capture_assets.sh
# (which records the other 76 by BEING the asset cache) cannot record them, and
# `x-block-origin` does not govern them either -- they are a plain `git fetch`
# inside a portfile.
#
# WHY NOT JUST READ THE PORTFILES. Because which git externals are used is
# decided by CMake *evaluation*, not by the text. skia declares ten with
# `declare_external_from_git` and then calls
# `get_externals(${required_externals})`, where `required_externals` is built up
# under feature and platform `if()`s -- so a static scan of skia's portfile
# yields 8 where 4 are real. And libyuv's archive comes from the libyuv *port*
# calling vcpkg_from_git directly, which a scan of skia+angle misses entirely. I
# wrote that scanner first; it was wrong in both directions at once.
#
# THE INSTRUMENT. `vcpkg install --only-downloads` runs the portfiles' *fetch*
# phase and stops. That is enough to make vcpkg_from_git produce its tarballs, at
# the refs the real resolution picks, in ~6 minutes with no compilation and no
# CMake configure of Ladybird. Same tactic as the asset capture and as
# scripts/npm_instrument: do not predict what the foreign build system will ask
# for -- run it and record the answer.
#
# THE ONE THAT ESCAPES EVEN THIS. angle's zlib is fetched by `checkout_in_path`
# from angle's *build* phase, not its fetch phase, so --only-downloads does not
# produce it (verified: 3 of the 4 appear). Its (url, ref) is a literal in the
# overlay portfile, so Meta/fetch_vcpkg_git_archives.py resolves and reproduces
# it with git, and the SHA512 below is what proves the two agree. Recording that
# asymmetry here is the point: "--only-downloads gets them all" would be the
# false version of this comment.
#
# Usage: Meta/vcpkg_capture_git_archives.sh [outfile]
#   Requires: Build/vcpkg (python3 Meta/ladybird.py vcpkg) and network access.
#   Prints name<TAB>sha512 for every PORT-<40hex>.tar.gz vcpkg produced.
set -uo pipefail

SRC="${LADYBIRD_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
OUT="${1:-/dev/stdout}"
ROOT="$SRC/Build/vcpkg"

if [ ! -x "$ROOT/vcpkg" ]; then
    echo "capture: no bootstrapped vcpkg at $ROOT" >&2
    echo "capture: get one with 'python3 Meta/ladybird.py vcpkg' (no CMake needed)" >&2
    exit 1
fi

SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/vcpkg-git-capture-XXXXXX")
trap 'rm -rf "$SCRATCH"' EXIT INT TERM

mkdir -p "$SCRATCH/m/Meta/CMake/vcpkg"
cp "$SRC/vcpkg.json" "$SCRATCH/m/"
cp "$SRC/vcpkg-configuration.json" "$SCRATCH/m/" 2>/dev/null
cp -r "$SRC/Meta/CMake/vcpkg/overlay-ports" "$SCRATCH/m/Meta/CMake/vcpkg/"

# The manifest is copied VERBATIM, never reconstructed: the baseline plus the 45
# overrides are what select the port versions, and a reconstruction that drifts
# would silently capture refs for different versions than the build uses.
cd "$SCRATCH/m" || exit 1
VCPKG_ROOT="$ROOT" "$ROOT/vcpkg" install \
    --only-downloads \
    --x-manifest-root="$SCRATCH/m" \
    --overlay-ports="$SCRATCH/m/Meta/CMake/vcpkg/overlay-ports" \
    --triplet=x64-linux-dynamic \
    --host-triplet=x64-linux \
    --x-install-root="$SCRATCH/out" \
    --x-buildtrees-root="$SCRATCH/bt" \
    --downloads-root="$SCRATCH/dl" >&2
rc=$?
if [ $rc -ne 0 ]; then
    echo "capture: vcpkg install --only-downloads failed (rc=$rc)" >&2
    exit $rc
fi

# vcpkg names these DOWNLOADS/${PORT}-${sanitized_ref}.tar.gz, which is exactly
# the shape below; every other file in downloads/ is an ordinary distfile already
# covered by Meta/vcpkg_assets.tsv.
found=0
: > "$OUT"
for f in "$SCRATCH/dl"/*.tar.gz; do
    name=$(basename "$f")
    [[ "$name" =~ ^[a-z0-9]+-[0-9a-f]{40}\.tar\.gz$ ]] || continue
    printf '%s\t%s\n' "$name" "$(sha512sum "$f" | cut -d' ' -f1)" >> "$OUT"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "capture: no PORT-<ref>.tar.gz produced -- vcpkg_from_git may have changed" >&2
    exit 1
fi
echo "capture: recorded $found git-sourced externals" >&2
echo "capture: NB angle's zlib is fetched in angle's BUILD phase, so it does not" >&2
echo "capture: appear here; Meta/fetch_vcpkg_git_archives.py reproduces it from" >&2
echo "capture: the portfile's literal (url, ref) and verifies it against the pin." >&2
