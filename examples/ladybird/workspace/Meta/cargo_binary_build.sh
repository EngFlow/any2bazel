#!/bin/bash
# Build a cargo BINARY crate offline. Driver for the cargo_binary build action
# (see cargo.bzl). There are two, and they are two different SHAPES of problem:
#
#   * **flapc**, the Flap-DSL -> interpreter-assembly compiler: a pure build
#     tool. It was the last artifact this migration still took from the reference
#     CMake build -- Bazel already RAN it as a declared genrule tool, so the
#     interpreter assembly was Bazel's own output, but the compiler itself came
#     out of Build/full/bin/ because Libraries/LibJS/Flap is a Rust crate. Its
#     workspace is `exclude`d from the root one and has its own lock with 3
#     packages, whose single registry crate is the same smallvec 1.15.1 (same
#     checksum, checked by the emitter) the big workspace already pins.
#   * **cranelift-compiler**, LibWasm's AOT WebAssembly compiler: a RUNTIME
#     tool, spawned by the browser (Core::Process::spawn in CraneliftBridge.cpp),
#     and one that ALSO emits an FFI header its C++ caller includes. It was
#     absent from the Bazel graph entirely -- CMake declares it with
#     build_rust_binary(), which the emitter did not parse, so every browser
#     binary failed on `CraneliftFFI.h: No such file or directory`.
#
# The second one is why this driver takes FFI arguments at all: a binary crate's
# build script runs cbindgen exactly as a staticlib crate's does, and the header
# is a declared output for the same reason (Bazel deletes what nothing declares).
#
# Contract:
#   $1  crate name (the cargo package)
#   $2  manifest path, relative to the source root
#   $3  comma-separated cargo features, or ""
#   $4  the rust sysroot dir
#   $5  the crate index: "<name> <version> <sha256> <dir>" per line
#   $6  output binary path
#   $7  the --bin name
#   $8  output dir for the FFI headers, or "" if the crate emits none
#   $9+ the FFI header paths to check for, relative to $8
#
# See cargo_vendor.sh for why the staging looks the way it does.
set -euo pipefail

CRATE="${1:?crate name}"
MANIFEST="${2:?manifest path}"
FEATURES="${3-}"
SYSROOT="${4:?rust sysroot}"
INDEX="${5:?crate index}"
OUT_BIN="${6:?output binary}"
BIN="${7:?bin name}"
FFI_OUT="${8-}"
shift $(( $# < 8 ? $# : 8 ))
FFI_HEADERS=("$@")

# A binary output is a single file with no triple in its path (unlike the
# staticlib, whose declared output mirrors cargo's own layout), so the triple
# comes from the sysroot's own rustlib dir -- i.e. from the toolchain that will
# do the compiling, which is the only thing that could disagree with it.
TRIPLE="$(basename "$(ls -d "$PWD/$SYSROOT"/lib/rustlib/*-*-* | head -1)")"

source "${CARGO_VENDOR_LIB:?path to cargo_vendor.sh}"

FEATURE_FLAGS=()
[ -n "$FEATURES" ] && FEATURE_FLAGS=("--features=$FEATURES")

cd "$SRC"
"$SYSROOT/bin/cargo" rustc \
    --offline --locked \
    --bin "$BIN" \
    "${FEATURE_FLAGS[@]+"${FEATURE_FLAGS[@]}"}" \
    --target="$TRIPLE" \
    --package "$CRATE" \
    --manifest-path "$SRC/$MANIFEST" \
    --target-dir "$TARGET_DIR" \
    --release \
    -- \
    -Cdefault-linker-libraries=yes \
    -D warnings

cp "$TARGET_DIR/$TRIPLE/release/$BIN" "$OUT_BIN"
chmod +x "$OUT_BIN"

# The declared headers, resolved out of the OWNING crate's OUT_DIR -- the same
# collision-safe copy the staticlib driver does, shared in cargo_vendor.sh.
if [ -n "$FFI_OUT" ]; then
    sync_ffi_headers "$FFI_OUT" "${FFI_HEADERS[@]+"${FFI_HEADERS[@]}"}"
fi
