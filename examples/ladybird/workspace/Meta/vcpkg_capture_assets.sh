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
#
# This does a FULL vcpkg build (~50 min), because a download-only run cannot reach
# every download -- see the CAPTURE_ONLY_DOWNLOADS comment below the recorder.
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
# vcpkg's own output, kept so the driver can detect a HALTED portfile -- which
# loses downloads exactly like a failed fetch, but produces no asset-script call
# (the step was never reached) and no nonzero exit.
VCPKG_LOG=$(mktemp /tmp/vcpkg-capture-log-XXXXXX)

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
# This run does the FULL BUILD, and that is not a preference -- `--only-downloads`
# cannot produce a complete capture, which cost a re-capture to learn. In Download
# Mode vcpkg refuses to execute anything, and a portfile that stops executing
# stops downloading:
#
#   CMake Error at scripts/cmake/vcpkg_execute_required_process.cmake:23
#     This command cannot be executed in Download Mode.
#     Halting portfile execution.
#   ... x_vcpkg_get_python_packages(...) angle/portfile.cmake:86
#
# angle downloads gni-to-cmake.py, sets up a python venv to run it, and THEN
# downloads four more WebKit files (include_CMakeLists.txt,
# WebKitCompilerFlags.cmake, DetectSSE2.cmake, WebKitMacros.cmake) at lines
# 123-153. The venv is line 86, so in Download Mode those four URLs are
# unreachable -- which is exactly the five-row shortfall (72 vs 76) that a
# download-only re-capture produced while reporting success. The committed 76-row
# capture could not have been made this way; it came from a full build, and the
# comment that used to sit here claiming --only-downloads "is enough because the
# asset hook fires during resolution" was invented, not measured.
#
# Note how WIDESPREAD the halting is: in that run 58 of the 77 ports halted, most
# of them at vcpkg_cmake_configure (harmless -- every download precedes the
# configure) but some, like angle, mid-download-sequence. Nothing in the log
# distinguishes the two cases; only the portfile knows whether a download follows
# the step that halted. So "halted" cannot be triaged into safe and unsafe, and a
# capture containing any halt is not emittable.
#
# CAPTURE_ONLY_DOWNLOADS=1 therefore exists only to refresh URLs you already know
# are reachable without running anything (it is minutes instead of ~50). It is
# opt-in, and the halt check below will refuse to bless its output -- as it must,
# since that mode's whole speed advantage IS the skipped execution.
if [ -n "${CAPTURE_ONLY_DOWNLOADS:-}" ]; then
    echo "capture: --only-downloads requested: FAST but structurally" >&2
    echo "capture: INCOMPLETE. Ports halt where they would execute, losing every" >&2
    echo "capture: download after that point. Use it to refresh known-reachable" >&2
    echo "capture: URLs only; the result will be reported as not emittable." >&2
    set -- --only-downloads "$@"
fi
# --binarysource=clear is MANDATORY for a capture, and it is the third way this
# script silently lost rows. vcpkg's binary cache is keyed by each port's ABI
# hash; on a hit it unpacks the archive and NEVER RUNS THE PORTFILE, so the port
# asks for none of its downloads and contributes nothing to the capture. Unlike a
# failed fetch or a halt, this leaves no trace at all: no error, no halt, exit 0.
#
# Measured, because I did not believe it was this bad: two runs of a zlib-only
# manifest against a warm ~/.cache/vcpkg/archives, second run with a fresh install
# root. Run 1 built zlib and captured 3 rows. Run 2 printed "Restored 3
# package(s)", "All requested installations completed successfully in: 1.54 ms",
# exited 0 -- and captured ZERO. The old capture had no --binarysource at all, so
# it inherited whatever cache the machine had; that is enough on its own to
# explain a re-capture that cannot reproduce the committed rows.
#
# It goes BEFORE "$@" so a caller can still override it deliberately (last flag
# wins), and the floor check below is what catches it if they do.
#
# `set -euo pipefail` is on, so the pipeline's status is vcpkg's when vcpkg fails
# and tee's only when tee does -- without pipefail the tee would swallow a vcpkg
# failure outright.
"${VCPKG_ROOT:?set VCPKG_ROOT}/vcpkg" install \
    --x-asset-sources="clear;x-script,$REC {url} {sha512} {dst}" \
    --binarysource=clear \
    "$@" 2>&1 | tee "$VCPKG_LOG"

# A halted portfile loses its later downloads exactly like a failed fetch does,
# and vcpkg exits 0 for it too ("Downloaded sources for angle", then "All
# requested installations completed successfully"). There is no exit code to read
# and no asset-script callback for a step never reached, so the only witness is
# the log -- hence the tee.
#
# Report the PORT, not the CMake line: the halt message names a helper script
# (vcpkg_execute_required_process.cmake:23) that is the same for every port, and
# the portfile in the call stack is a versioned path under bt/versioning_. The
# port name comes from the "Installing N/M <port>:<triplet>@<ver>" line above the
# halt, which is also the identifier a reader can act on.
#
# Match the halt case-INSENSITIVELY and on the short phrase: vcpkg has (at least)
# two spellings, "Halting portfile execution." for a refused step and "Download
# failed, halting portfile." for a fetch that did not produce the file. The second
# is normally also reported by the recorder's $FAILED, but only for fetches the
# recorder itself ran -- a SHA512 mismatch, say, is vcpkg rejecting a file the
# recorder fetched happily, and then the log is the only witness again.
awk '
  /^Installing [0-9]+\/[0-9]+ / { split($3, f, ":"); port = f[1] }
  tolower($0) ~ /halting portfile/ && port \
    { print "port " port " halted before finishing its portfile" }
' "$VCPKG_LOG" | sort -u >> "$FAILED"

# The two ways a port can skip its portfile ENTIRELY, and so contribute nothing
# while looking fine. Both are silent -- no error, no halt, exit 0 -- so the log
# is again the only witness.
#
#   "Restored N package(s) from <cache>"  -- a binary-cache hit unpacks an archive
#       instead of running the portfile. Guarded above with --binarysource=clear,
#       but a caller can override it, so verify the OUTCOME rather than trusting
#       the flag.
#   "The following packages are already installed"  -- an install root that
#       already has the port. Nothing is rebuilt, nothing is downloaded. This is
#       why a capture wants a FRESH --x-install-root: the 71fb301a capture's
#       second run had 7 ports already installed and could not have captured any
#       of their distfiles.
if grep -q "^Restored [0-9]* package" "$VCPKG_LOG"; then
    grep -o "^Restored [0-9]* package(s) from [^ ]*" "$VCPKG_LOG" \
        | sed 's/$/ -- a cache hit never runs the portfile, so those downloads were never requested/' \
        >> "$FAILED"
fi
if grep -q "The following packages are already installed" "$VCPKG_LOG"; then
    n=$(sed -n '/The following packages are already installed/,/^The following packages will be/p' \
        "$VCPKG_LOG" | grep -c "^ *[*]* *[a-z0-9]" || true)
    echo "$n package(s) were ALREADY INSTALLED -- their portfiles did not run," \
         "so their downloads were never requested; use a fresh --x-install-root" \
        >> "$FAILED"
fi

rm -f "$REC"
# Keep $VCPKG_LOG until the capture is blessed: if it is INCOMPLETE the log is the
# evidence for WHY (which port halted, at which portfile line), and deleting it
# would leave a reader with a port name and nothing to read.
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
    echo "capture: INCOMPLETE -- $(wc -l < "$FAILED") loss(es):" >&2
    sed 's/^/capture:   /' "$FAILED" >&2
    echo "capture: Every one of these means some vcpkg_download_distfile was never" >&2
    echo "capture: REQUESTED, and only requested downloads can be captured: a failed" >&2
    echo "capture: fetch and a refused step both halt the rest of their portfile, and" >&2
    echo "capture: a cache hit or an already-installed port skips the portfile whole." >&2
    echo "capture: So $OUT is missing rows and looks complete. Do not emit from it." >&2
    echo "capture: Re-run; it appends and shares downloads/, so it resumes." >&2
    echo "capture: A halt on EVERY port means CAPTURE_ONLY_DOWNLOADS was set --" >&2
    echo "capture: that mode cannot produce a complete capture; drop it." >&2
    echo "capture: vcpkg's own output is kept at $VCPKG_LOG" >&2
    exit 1
fi
rm -f "$VCPKG_LOG"
echo "captured $(wc -l < "$OUT") distfiles -> $OUT" >&2
