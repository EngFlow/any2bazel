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
#      wins is note 4, and it is the subtlest thing in this file.
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

# FFI_OUTPUT_DIR is a SCRATCH dir, not the declared output dir -- see note 4
# below for why that distinction is load-bearing.
FFI_SCRATCH="$WORK/ffi"
mkdir -p "$FFI_SCRATCH" "$FFI_OUT"
export FFI_OUTPUT_DIR="$FFI_SCRATCH"

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

# --- note 4: resolve each header from the OWNING crate's OUT_DIR -------------
#
# $FFI_OUTPUT_DIR is shared by every build script in the crate's dependency
# graph, and the header names COLLIDE: six crates each emit a file called
# `RustFFI.h`. liburl_rust path-depends on libregex_rust, so building liburl_rust
# runs BOTH build scripts against the same FFI_OUTPUT_DIR and the surviving
# RustFFI.h is whichever ran last -- we shipped libregex_rust's URL header for
# one build and it was only caught by byte-comparing against CMake's tree.
#
# CMake hits this too, and has a whole script for it
# (Meta/CMake/sync_rust_ffi_header.cmake): after cargo runs it copies the header
# out of the OWNING crate's own OUT_DIR, found via
# `build/<crate>-*/root-output`, and that copy is what the compiler sees. Mirror
# that: prefer the crate's own OUT_DIR, and fall back to the shared scratch dir
# for a header written by a DEPENDENCY's build script (libweb_rust's
# HTMLTokenizerRustFFI.h comes from libweb_html_tokenizer and collides with
# nothing, which is exactly why CMake never noticed it was undeclared).
BUILD_DIR="$TARGET_DIR/$TRIPLE/release/build"
missing=()
for h in "${FFI_HEADERS[@]+"${FFI_HEADERS[@]}"}"; do
    mkdir -p "$FFI_OUT/$(dirname "$h")"
    src=""
    # The crate's own OUT_DIR, newest first: a rebuild can leave several.
    for ro in $(ls -t "$BUILD_DIR/$CRATE"-*/root-output 2>/dev/null); do
        cand="$(cat "$ro")/$h"
        [ -f "$cand" ] && { src="$cand"; break; }
    done
    # Else a dependency's build script wrote it to the shared dir.
    [ -z "$src" ] && [ -f "$FFI_SCRATCH/$h" ] && src="$FFI_SCRATCH/$h"
    if [ -z "$src" ]; then
        missing+=("$h")
        continue
    fi
    cp "$src" "$FFI_OUT/$h"
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "cargo_build: $CRATE declared FFI headers cargo never wrote: ${missing[*]}" >&2
    echo "cargo_build: what it DID write under $FFI_SCRATCH:" >&2
    (cd "$FFI_SCRATCH" && find . -type f | sed 's|^\./|  |') >&2
    echo "cargo_build: and in its own OUT_DIRs:" >&2
    for ro in "$BUILD_DIR/$CRATE"-*/root-output; do
        [ -f "$ro" ] || continue
        (cd "$(cat "$ro")" && find . -type f -name "*.h" | sed 's|^\./|  |') >&2
    done
    exit 1
fi
