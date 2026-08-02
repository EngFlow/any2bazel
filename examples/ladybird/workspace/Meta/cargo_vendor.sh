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

# --- FFI_OUTPUT_DIR is a SCRATCH dir, not the declared output dir ------------
# Shared by both drivers because the reason is the same for both: cbindgen runs
# from a build script, every build script in the crate's dependency graph writes
# into the SAME $FFI_OUTPUT_DIR, and the header names COLLIDE (eight crates emit
# a file called `RustFFI.h`). So the declared outputs are resolved out of the
# OWNING crate's own OUT_DIR afterwards -- see sync_ffi_headers.
FFI_SCRATCH="$WORK/ffi"
mkdir -p "$FFI_SCRATCH"
export FFI_OUTPUT_DIR="$FFI_SCRATCH"

# sync_ffi_headers <out-dir> [header...]
#
# Copy each declared FFI header into the declared output dir, resolved from the
# crate that OWNS it. Mirrors Meta/CMake/sync_rust_ffi_header.cmake, which exists
# for exactly this reason: liburl_rust path-depends on libregex_rust, so building
# liburl_rust runs BOTH build scripts against one FFI_OUTPUT_DIR and the
# surviving RustFFI.h is whichever ran last -- we shipped libregex_rust's URL
# header for one build and only caught it by byte-comparing against CMake's tree.
#
# CMake finds the owning crate's OUT_DIR via `build/<crate>-*/root-output`; so
# does this, preferring it and falling back to the shared scratch dir for a
# header written by a DEPENDENCY's build script (libweb_rust's
# HTMLTokenizerRustFFI.h comes from libweb_html_tokenizer and collides with
# nothing, which is exactly why CMake never noticed it was undeclared).
#
# Failing loudly is the point: Bazel deletes an undeclared output and fails an
# action that does not write a declared one, so a missing header is reported here
# with what cargo DID write rather than surfacing as a file-not-found in a C++
# compile a thousand actions later.
sync_ffi_headers() {
    local ffi_out="$1"; shift
    [ $# -gt 0 ] || return 0
    local build_dir="$TARGET_DIR/$TRIPLE/release/build"
    local missing=() h ro cand src
    mkdir -p "$ffi_out"
    for h in "$@"; do
        mkdir -p "$ffi_out/$(dirname "$h")"
        src=""
        # The crate's own OUT_DIR, newest first: a rebuild can leave several.
        for ro in $(ls -t "$build_dir/$CRATE"-*/root-output 2>/dev/null); do
            cand="$(cat "$ro")/$h"
            [ -f "$cand" ] && { src="$cand"; break; }
        done
        # Else a dependency's build script wrote it to the shared dir.
        [ -z "$src" ] && [ -f "$FFI_SCRATCH/$h" ] && src="$FFI_SCRATCH/$h"
        if [ -z "$src" ]; then
            missing+=("$h")
            continue
        fi
        cp "$src" "$ffi_out/$h"
    done
    if [ ${#missing[@]} -gt 0 ]; then
        echo "cargo: $CRATE declared FFI headers cargo never wrote: ${missing[*]}" >&2
        echo "cargo: what it DID write under $FFI_SCRATCH:" >&2
        (cd "$FFI_SCRATCH" && find . -type f | sed 's|^\./|  |') >&2
        echo "cargo: and in its own OUT_DIRs:" >&2
        for ro in "$build_dir/$CRATE"-*/root-output; do
            [ -f "$ro" ] || continue
            (cd "$(cat "$ro")" && find . -type f -name "*.h" | sed 's|^\./|  |') >&2
        done
        return 1
    fi
}
