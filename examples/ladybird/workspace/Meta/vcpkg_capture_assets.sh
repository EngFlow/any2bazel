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
# {dst} is a temp name like "foo.tar.gz.12345.part"; strip the suffix vcpkg
# appends so the recorded filename is the one it finally stores.
name=\$(basename "\$3"); name=\${name%.part}; name=\${name%.[0-9]*}
printf '%s\t%s\t%s\n' "\$1" "\$2" "\$name" >> "$OUT"
curl -sSL --fail -o "\$3" "\$1"
EOF
chmod +x "$REC"

# Deliberately NO x-block-origin here: this is the one run allowed to fetch.
"${VCPKG_ROOT:?set VCPKG_ROOT}/vcpkg" install \
    --x-asset-sources="clear;x-script,$REC {url} {sha512} {dst}" \
    "$@"

rm -f "$REC"
sort -u -o "$OUT" "$OUT"
echo "captured $(wc -l < "$OUT") distfiles -> $OUT" >&2
