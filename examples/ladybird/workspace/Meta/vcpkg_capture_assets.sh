#!/bin/bash
# Record every (url, sha512, filename) vcpkg asks for, by BEING its asset cache.
#
# Why this exists: the first version of Meta/emit_vcpkg_bazel.py re-parsed
# portfile.cmake with regexes to recover each distfile's URL and SHA512. That
# gets 54 of 81 and then hits a wall, because portfiles are CMake *programs*:
# curl computes `${curl_version}` from the version, angle carries its own
# `${ANGLE_COMMIT}`, libpsl derives `${short_hash}`, vcpkg-tool-gn builds
# `${download_urls}` per platform. Recovering those needs a CMake interpreter --
# i.e. it needs to be vcpkg.
#
# So don't reimplement the resolver, INSTRUMENT it. `x-asset-sources` hands a
# script the fully-expanded {url} {sha512} {dst} for every single download, which
# is precisely the tuple an http_file needs. This is the same tactic as the npm
# extractor in scripts/npm_instrument: capture what the real build system asks
# for, rather than statically predicting it (and the same reason -- a build
# script's inputs are only knowable by running it).
#
# The capture is a fetching run (it must reach the network, once) and the file it
# writes is what makes every LATER build hermetic. Commit the capture; that is
# the pin.
#
# Usage: vcpkg_capture_assets.sh <out.tsv> [vcpkg install args...]
set -euo pipefail

OUT="${1:?usage: vcpkg_capture_assets.sh <out.tsv> [vcpkg args...]}"
shift
: > "$OUT"

REC=$(mktemp /tmp/vcpkg-record-XXXXXX.sh)
cat > "$REC" <<EOF
#!/bin/bash
# x-script receives: <url> <sha512> <dst>. Record, then fetch normally so the
# build proceeds; the RECORD is the artifact, the download is incidental.
set -euo pipefail
# Record {dst} RAW. It is tempting to normalise here -- it is a temp path like
# "foo.tar.gz.12345.part" -- but vcpkg mangles the name in a second way that
# bash cannot undo without the hash, and a lossy record cannot be repaired
# later. So the recorder stays dumb and emit_vcpkg_bazel.canonical_filename()
# does the interpretation, where it is testable. (finding 30)
printf '%s\t%s\t%s\n' "\$1" "\$2" "\$3" >> "$OUT"
# vcpkg hands x-script ONE url per call even when the portfile lists several
# mirrors, so if that one mirror is down the script fails and vcpkg falls back to
# the origin -- which under x-block-origin (the real builds) is a hard failure.
# Record the tuple regardless (the SHA512 is what matters and it is
# mirror-independent), and try the known GNU mirrors before giving up. Hit live:
# ftpmirror.gnu.org returned 502 for ~13 minutes and wedged the capture.
if curl -sSL --fail --max-time 120 -o "\$3" "\$1"; then exit 0; fi
alt=\$(printf '%s' "\$1" | sed \
  -e 's|https://ftpmirror.gnu.org/gnu/|https://www.mirrorservice.org/sites/ftp.gnu.org/gnu/|' \
  -e 's|https://ftp.gnu.org/pub/gnu/|https://www.mirrorservice.org/sites/ftp.gnu.org/gnu/|')
if [ "\$alt" != "\$1" ]; then
  echo "capture: primary failed, trying mirror \$alt" >&2
  curl -sSL --fail --max-time 120 -o "\$3" "\$alt" && exit 0
fi
# Leave NO partial file behind. curl -o creates the destination before it knows
# the transfer will fail, and vcpkg's downloads/ is keyed by name: a surviving
# 0-byte file makes every LATER request for that distfile hash-mismatch, and
# vcpkg then silently retries under a disambiguated name with the expected
# SHA512's first 8 hex chars spliced into it. That is how one dead mirror
# renamed four unrelated distfiles in the capture. (finding 30)
rm -f "\$3"
echo "capture: FAILED to fetch \$1" >&2
exit 1
EOF
chmod +x "$REC"

# Deliberately NO x-block-origin here: this is the one run allowed to fetch.
#
# --only-downloads: a capture wants the FETCHES, not the 45-minute build. Without
# it the capture takes ~50 minutes and every interruption costs the whole run
# (which is how the first two attempts died); with it the same 76 distfiles come
# down in 2 minutes. vcpkg has no fetch-only mode for the *ports* -- this is the
# closest thing, and it is enough because the asset hook fires during resolution.
"${VCPKG_ROOT:?set VCPKG_ROOT}/vcpkg" install \
    --only-downloads \
    --x-asset-sources="clear;x-script,$REC {url} {sha512} {dst}" \
    "$@"

rm -f "$REC"
# Dedupe on (url, sha512) -- NOT the whole line. The third column is the raw
# {dst}, which carries a pid and so differs on every retry; sorting whole lines
# leaves the same distfile in the file several times over.
sort -u -t$'\t' -k1,2 -o "$OUT" "$OUT"
echo "captured $(wc -l < "$OUT") distfiles -> $OUT" >&2
