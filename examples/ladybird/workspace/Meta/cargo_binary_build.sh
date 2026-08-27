#!/bin/bash
# Copyright 2026 EngFlow Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Build a cargo BINARY crate offline: flapc, the Flap-DSL -> interpreter-assembly
# compiler. Driver for the cargo_binary build action (see cargo.bzl).
#
# flapc was the last artifact this migration still took from the reference CMake
# build. Bazel already RAN it as a declared genrule tool, so the interpreter
# assembly was Bazel's own output -- but the compiler itself came out of
# Build/full/bin/, because Libraries/LibJS/Flap is a Rust crate. It gets exactly
# the same treatment as the staticlib crates, and needs no extra machinery: its
# workspace is `exclude`d from the root one and has its own lock with 3 packages,
# whose single registry crate is the same smallvec 1.15.1 (same checksum, checked
# by the emitter) that the big workspace already pins.
#
# Contract:
#   $1  crate name (the cargo package)
#   $2  manifest path, relative to the source root
#   $3  comma-separated cargo features, or ""
#   $4  the rust sysroot dir
#   $5  the crate index: "<name> <version> <sha256> <dir>" per line
#   $6  output binary path
#   $7  the --bin name
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
