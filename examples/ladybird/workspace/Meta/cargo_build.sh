#!/bin/bash
# Build ONE of Ladybird's Rust staticlib crates with cargo, offline, from crates
# Bazel already fetched. Driver for the cargo_crate build action (see cargo.bzl).
#
# Ring 2 part 3, and the last one: this is what removes the prebuilt 260 MB
# librust_combined.a and the `ar -M` merge step (README step 1b) from the build.
#
# Contract:
#   $1  crate name (the cargo package, e.g. libweb_css_rust)
#   $2  manifest path, relative to the source root (Libraries/.../Cargo.toml)
#   $3  comma-separated cargo features, or "" for none
#   $4  the rust sysroot dir (rustc + cargo + rust-std, one Bazel-fetched tree)
#   $5  the crate index: "<name> <version> <sha256> <dir>" per line
#   $6  output .a path
#   $7  output dir for the FFI headers (becomes $FFI_OUTPUT_DIR)
#   $8+ the FFI header paths to check for, relative to $7
#
# The staging (vendor dir, CARGO_HOME, toolchain) is in cargo_vendor.sh, which
# explains why each piece is the way it is. Three things specific to THIS driver:
#
#   1. **--offline is not a claim on its own.** It stops cargo from *choosing* to
#      hit the network; the action also runs with block-network, so there is no
#      network to hit. Cargo's resolution then fails loudly on a crate missing
#      from the vendor dir ("no matching package named `yuv` found") -- the
#      x-block-origin equivalent, verified by deleting one. `--locked` adds the
#      third guarantee: cargo may not even rewrite the lock file it was given.
#   2. **cbindgen is a build script, so one action yields the .a AND the
#      headers.** Each crate's build.rs runs cbindgen::generate and writes its FFI
#      header into OUT_DIR and into $FFI_OUTPUT_DIR when set -- which is how CMake
#      gets them too. So there is no second tool to wire; there IS a check that
#      every declared header appeared, because Bazel deletes an undeclared output
#      and fails an action that does not write a declared one. (That check is how
#      HTMLTokenizerRustFFI.h -- written by a dependency's build script, declared
#      by nobody -- turned up.) Which crate's copy of a colliding header name
#      wins is sync_ffi_headers in cargo_vendor.sh, and it is the subtlest thing
#      in this ring.
#   3. **The cargo invocation mirrors CMake's exactly**: same subcommand
#      (`cargo rustc --lib`), same --target/--release, same trailing rustc flags
#      (-Cdefault-linker-libraries=yes -D warnings). Not for tidiness -- those
#      flags are part of the archive's contents, and the parity claim is against
#      the archive CMake produces.
set -euo pipefail

CRATE="${1:?crate name}"
MANIFEST="${2:?manifest path}"
FEATURES="${3-}"
SYSROOT="${4:?rust sysroot}"
INDEX="${5:?crate index}"
OUT_LIB="${6:?output .a}"
FFI_OUT="${7:?ffi output dir}"
shift 7
FFI_HEADERS=("$@")

# bazel-out/<cfg>/bin/<pkg>/<name>/<triple>/release/lib<crate>.a -- the triple is
# taken from the output path rather than passed again, so the declared output and
# the cargo --target can never disagree.
TRIPLE="$(basename "$(dirname "$(dirname "$OUT_LIB")")")"

source "${CARGO_VENDOR_LIB:?path to cargo_vendor.sh}"

FEATURE_FLAGS=()
[ -n "$FEATURES" ] && FEATURE_FLAGS=("--features=$FEATURES")

mkdir -p "$FFI_OUT"

cd "$SRC"
"$SYSROOT/bin/cargo" rustc \
    --offline --locked \
    --lib \
    "${FEATURE_FLAGS[@]+"${FEATURE_FLAGS[@]}"}" \
    --target="$TRIPLE" \
    --package "$CRATE" \
    --manifest-path "$SRC/$MANIFEST" \
    --target-dir "$TARGET_DIR" \
    --release \
    -- \
    -Cdefault-linker-libraries=yes \
    -D warnings

cp "$TARGET_DIR/$TRIPLE/release/lib$CRATE.a" "$OUT_LIB"

# The declared headers, resolved out of the OWNING crate's OUT_DIR -- see
# sync_ffi_headers in cargo_vendor.sh for why $FFI_OUTPUT_DIR alone is not enough
# (eight crates emit a file called RustFFI.h into one shared directory).
sync_ffi_headers "$FFI_OUT" "${FFI_HEADERS[@]+"${FFI_HEADERS[@]}"}"
