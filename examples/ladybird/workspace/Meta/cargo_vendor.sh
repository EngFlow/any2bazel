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

# Shared staging for the two cargo drivers (cargo_build.sh, cargo_binary_build.sh).
# Sourced, not executed: it sets up the environment those two then run cargo in.
#
# Everything here is what makes an OFFLINE cargo build possible inside a Bazel
# sandbox, and every line of it was established by running it rather than by
# reading cargo's docs:
#
#   1. **A vendor directory needs a .cargo-checksum.json per crate or cargo
#      refuses it** ("no matching package named X found ... directory source").
#      The file is {"files": {...}, "package": "<the crate's sha256>"}. `files`
#      may be EMPTY -- cargo only consults it to detect edits to vendored files --
#      but `package` may not: cargo compares it against the lock file and
#      hard-errors "checksum for `yuv v0.8.13` changed between lock files" on a
#      mismatch. Verified by tampering with one. So the hash Bazel already
#      verified at fetch time is re-asserted here and cargo re-checks it, which
#      is what makes the boundary checkable rather than asserted.
#   2. **Source replacement goes in $CARGO_HOME/config.toml, not the source
#      tree.** A `.cargo/config.toml` beside the workspace root works too, but
#      the source root here is Bazel's execroot -- an INPUT -- and writing into it
#      would be non-hermetic and racy across the 11 crates that share it.
#      CARGO_HOME is per-action scratch, so the config lives there.
#   3. **Symlinks, not copies.** The fetched crate trees are already unpacked in
#      Bazel's output base and are read-only; copying 166 MB per crate build,
#      eleven times, is pure cost. The .cargo-checksum.json cannot live in the
#      fetched repo (it is not part of the .crate, and the repo is shared by every
#      crate build), so each vendor entry is a directory of symlinks plus that one
#      real file.
#   4. **cargo reads $HOME** and Bazel deliberately does not pass one through
#      (that is the sandbox doing its job: anything read from a real home
#      directory is an undeclared input). It gets an action-local one that cannot
#      leak state between builds -- the same fix vcpkg_build.sh needed.
#   5. **rustc finds its sysroot relative to argv[0]**, so the single merged
#      toolchain tree (bin/rustc next to lib/rustlib/<triple>) is what makes this
#      work with no rustup and no PATH rustc.

SRC="$PWD"                      # the execroot: cargo wants absolute paths
SYSROOT="$SRC/$SYSROOT"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- the vendor directory (notes 1 and 3) -----------------------------------
VENDOR="$WORK/vendor"
mkdir -p "$VENDOR"
while read -r name version sha dir; do
    [ -n "${name:-}" ] || continue
    d="$VENDOR/$name-$version"
    mkdir -p "$d"
    # One symlink per TOP-LEVEL entry, so a crate's subdirectories come along
    # without walking them. `ln -s` on a directory is what makes this O(entries)
    # rather than O(files) -- 154 crates is ~30k files and only ~600 top-level
    # entries.
    for entry in "$SRC/$dir"/* "$SRC/$dir"/.[!.]*; do
        [ -e "$entry" ] || continue
        ln -sfn "$entry" "$d/$(basename "$entry")"
    done
    printf '{"files":{},"package":"%s"}\n' "$sha" > "$d/.cargo-checksum.json"
done < "$INDEX"

# --- CARGO_HOME: source replacement + action-local scratch (notes 2 and 4) ---
export CARGO_HOME="$WORK/cargo_home"
mkdir -p "$CARGO_HOME"
cat > "$CARGO_HOME/config.toml" <<EOF
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "$VENDOR"
EOF

export HOME="$WORK/home"
mkdir -p "$HOME"

# --- the pinned toolchain (note 5) ------------------------------------------
export PATH="$SYSROOT/bin:$PATH"
export RUSTC="$SYSROOT/bin/rustc"
export CARGO_BUILD_RUSTC="$RUSTC"

# Mirror CMake's environment (Meta/CMake/rust_crate.cmake) so a crate with a
# C/C++ build script uses the same compiler and archiver as the rest of the
# build instead of sniffing the host. No crate here has one today; `cc` is a
# transitive dep of several, so this is one upstream bump away from mattering.
TRIPLE_US="${TRIPLE//-/_}"
TRIPLE_UP="$(echo "$TRIPLE_US" | tr 'a-z' 'A-Z')"
export CC_${TRIPLE_US}="${CC:-cc}"
export CXX_${TRIPLE_US}="${CXX:-c++}"
export AR_${TRIPLE_US}="${AR:-ar}"
export CARGO_TARGET_${TRIPLE_UP}_LINKER="${CC:-cc}"

TARGET_DIR="$WORK/target"
