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
# APPEND to an existing capture rather than truncating it, and share vcpkg's
# downloads/ across runs (pass --downloads-root at a stable path). The run takes
# tens of minutes and reaches the network for every byte, so it WILL be
# interrupted -- a sandbox restart, a dead mirror, a timeout. Truncating means
# every interruption costs the whole run, which is how the first two attempts
# died (todo c2affe6b: long jobs must be resumable and append-only). The final
# `sort -u` dedupes, so a tuple recorded twice is free; a tuple recorded once and
# then thrown away is another 40 minutes.
touch "$OUT"
echo "capture: appending to $OUT ($(wc -l < "$OUT") rows already recorded)" >&2

# Where the recorder reports downloads it could not complete. A file rather than a
# counter, because the recorder runs as a separate PROCESS per download: nothing
# it sets in a variable can reach this script.
FAILED=$(mktemp /tmp/vcpkg-capture-failed-XXXXXX)

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
# Bound STALLS, not total transfer time. This was --max-time 120, which is a cap
# on how long a download may legitimately take -- so it killed OpenGL-Registry at
# 22MB of a working transfer, then reported it as "FAILED to fetch", then fell
# through to the origin, on a repeat, forever: a capture that cannot finish and
# blames the mirror. --speed-time/--speed-limit is the property actually wanted
# ("no progress for 60s"), and it cannot mistake a big file for a dead one.
if curl -sSL --fail --speed-time 60 --speed-limit 1024 -o "\$3" "\$1"; then exit 0; fi
alt=\$(printf '%s' "\$1" | sed \
  -e 's|https://ftpmirror.gnu.org/gnu/|https://www.mirrorservice.org/sites/ftp.gnu.org/gnu/|' \
  -e 's|https://ftp.gnu.org/pub/gnu/|https://www.mirrorservice.org/sites/ftp.gnu.org/gnu/|')
if [ "\$alt" != "\$1" ]; then
  echo "capture: primary failed, trying mirror \$alt" >&2
  curl -sSL --fail --speed-time 60 --speed-limit 1024 -o "\$3" "\$alt" && exit 0
fi
# Leave NO partial file behind. curl -o creates the destination before it knows
# the transfer will fail, and vcpkg's downloads/ is keyed by name: a surviving
# 0-byte file makes every LATER request for that distfile hash-mismatch, and
# vcpkg then silently retries under a disambiguated name with the expected
# SHA512's first 8 hex chars spliced into it. That is how one dead mirror
# renamed four unrelated distfiles in the capture. (finding 30)
rm -f "\$3"
echo "capture: FAILED to fetch \$1" >&2
# Tell the DRIVER, not just the log. A failed download halts its portfile, so
# every vcpkg_download_distfile after it in that port is never requested and so
# never captured -- and vcpkg still exits 0 saying "All requested installations
# completed successfully". Without this file the driver cannot know.
printf '%s\n' "\$1" >> "$FAILED"
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
trap 'rm -f "$FAILED"' EXIT

# A capture that lost a download is INCOMPLETE, and vcpkg does not tell you so in
# its exit code: with --only-downloads it printed "All requested installations
# completed successfully in: 49 min" and exited 0 having FAILED to download
# angle's gni-to-cmake.py (a transient TLS error -- this sandbox's clock was
# briefly behind the certificate's validity window, "certificate is not yet
# valid"). That is not a cosmetic loss: the failure HALTED angle's portfile, so
# the FOUR vcpkg_download_distfile calls after it were never made, never
# requested, and so never captured. The result is a pin that is missing five URLs
# and looks complete -- the worst possible shape, since the emitted rules would
# then fetch nothing for angle and the failure would surface much later as a
# build error inside a port.
#
# The recorder logs the tuple BEFORE fetching, so a failed download is still in
# the capture; what is lost is everything the halted portfile would have asked for
# next. The recorder therefore also appends each failed URL to a sentinel file,
# which is the thing checked here -- not vcpkg's exit code, which lies, and not a
# grep of stderr, which depends on how the caller redirected it.
#
# Dedupe FIRST so the row count reported below is the real one. Dedupe on
# (url, sha512) -- NOT the whole line: the third column is the raw {dst}, which
# carries a pid and so differs on every retry, and sorting whole lines leaves the
# same distfile in the file several times over.
sort -u -t$'\t' -k1,2 -o "$OUT" "$OUT"
if [ -s "$FAILED" ]; then
    echo "capture: INCOMPLETE -- $(wc -l < "$FAILED") download(s) failed:" >&2
    sed 's/^/capture:   /' "$FAILED" >&2
    echo "capture: A failed download HALTS its portfile, so every later" >&2
    echo "capture: vcpkg_download_distfile in that port was never requested and is" >&2
    echo "capture: MISSING from $OUT. Do not emit rules from this file." >&2
    echo "capture: Re-run; it appends and shares downloads/, so it resumes." >&2
    exit 1
fi
echo "captured $(wc -l < "$OUT") distfiles -> $OUT" >&2
