# Ladybird's Rust crates under Bazel (the last blocker)

## Why

After finding 33, vcpkg is no longer a reason to need a CMake build first. Rust
is the only one left: the 10 production crates are consumed as a **260 MB
prebuilt `librust_combined.a`** copied out of `Build/full/cargo/...`, and `flapc`
(the Flap-DSL → interpreter-assembly compiler) is the reference build's binary.
Close this and `git clone && bazel build //:ladybird` is true.

## What was validated before writing this (do not re-derive)

Measured in this session against `~/ladybird-work`, not assumed:

1. **The pin already exists.** `Cargo.lock` carries a sha256 `checksum` for
   **all 154** registry crates (13 of the 167 `[[package]]` entries are the
   in-tree workspace members, which have no checksum because they are sources).
   Unlike vcpkg — which needed a capture harness because portfiles compute URLs at
   runtime — **there is nothing to capture**. `http_archive`/`http_file` per crate
   is derivable from `Cargo.lock` alone, offline. The crate URL is
   `https://static.crates.io/crates/<name>/<name>-<version>.crate` (a `.tar.gz`).
2. **Cargo builds fully offline from a vendor directory.** Verified with an
   *empty* `CARGO_HOME` (`CARGO_HOME=/tmp/cargo_home_empty`), `--offline`, and a
   `.cargo/config.toml` doing `[source.crates-io] replace-with =
   "vendored-sources"`. **All 10 crates built, rc=0, zero network.** Each vendored
   crate dir needs a `.cargo-checksum.json` containing `{"files": {<relpath>:
   <sha256>}, "package": <crate sha256>}` — cargo verifies it and refuses the
   directory without it.
3. **cbindgen is a build script, not a separate step.** Each crate's `build.rs`
   runs `cbindgen::generate` and writes its FFI header into `OUT_DIR` (and to
   `$FFI_OUTPUT_DIR` when set). So **one action yields both** the `.a` and the
   header(s) — no second tool to wire. The header names are not uniform: most are
   `RustFFI.h`, but `libweb_css_rust` emits **four** (`RustFFI.h`,
   `SelectorRustFFI.h`, `StyleValueRustFFI.h`, `ComputedValuesRustFFI.h`),
   `libweb_layout_rust` emits `Layout/TreeBuilderRustFFI.h`, `libweb_rust` emits
   `HTML/Parser/RustFFI.h`, and `libweb_content_blocker_rust` emits
   `ContentBlockerRustFFI.h`. Take the list from CMake's `FFI_HEADER(S)` args in
   `Libraries/*/CMakeLists.txt`, do not guess.
4. **Feature flags matter and are per crate.** `libregex_rust`, `liburl_rust`,
   `libunicode_rust` are built `--features allocator`; the rest with none. From
   `import_rust_crate(... FEATURES ...)`. Getting these wrong changes the ABI
   silently, which is the finding-23 lesson (a hand-derived dep list loses feature
   selections).
5. **`flapc` is a separate, tiny cargo workspace.** `Libraries/LibJS/Flap` is
   `exclude`d from the root workspace and has its **own `Cargo.lock` with exactly
   3 packages** (`flapc`, in-tree `bytecode_def`, and `smallvec` from crates.io —
   1 checksum). It is a `bin`, run as a genrule tool.
6. **Toolchain is pinned** in `rust-toolchain.toml`: channel `1.96.1`, i.e. a
   real pin we must honour, not "whatever rustc is on PATH".

## The shape

Mirror what worked for vcpkg (finding 33), because the two problems are the same
shape — a foreign build system that owns a recipe we do not want to reimplement:

- **fetching → module level.** An emitter, `Meta/emit_cargo_bazel.py`, reads
  `Cargo.lock` and emits one `http_archive` (or `http_file`) per crate plus a
  sha256→label index. Regenerates from `Cargo.lock` alone: no cargo, no network.
  This is *more* honest than the vcpkg equivalent, which needed a capture.
- **building → an ordinary build action**, sandboxed and cacheable, not a
  `repository_rule`. A `cargo_crate` rule that: stages the vendor dir from the
  fetched crates (writing `.cargo-checksum.json` per crate), writes the
  `.cargo/config.toml` source-replacement, runs `cargo rustc --offline` with
  `CARGO_HOME` pointed at a sandbox-local dir, and declares **both** the `.a` and
  that crate's FFI headers as outputs.
- **consuming → `CcInfo`, exactly as `vcpkg_lib` does.** The archive plus an
  include dir for the FFI headers. This is where the circular-reference problem
  gets *solved* rather than papered over: today the 10 archives are pre-merged
  with `ar -M` into one blob because they reference each other's symbols and a
  flat link cannot order them. In Bazel the honest fix is to put all 10 archives
  in **one linker input** (or use `--start-group/--end-group`), so the linker
  resolves the cycle itself and the `ar` merge step disappears.

## Acceptance criteria

Ordered by what actually proves something. **Verify by removal, not inspection** —
every bug this migration found was an *absence*.

1. `bazel build //:ladybird //:WebContent //:RequestServer //:ImageDecoder
   //:Compositor //:WebWorker` succeeds with **`Build/full/cargo` moved off the
   machine**, after a `bazel clean`. This is the only criterion that matters; the
   rest are its preconditions.
2. `--headless=text` **and** `--headless=layout-tree` on a page exercising
   HTML+CSS+JS are **byte-identical** to the CMake reference (`Build/full/bin/Ladybird`).
   Rust owns URL parsing, the CSS parser, the HTML tokenizer, regex and text
   codecs, so a layout tree that matches is a real signal here.
3. **Zero network during the build.** `cargo` must run `--offline` with a
   sandbox-local `CARGO_HOME`; a fetch attempt must be a hard error, not a silent
   success (the `x-block-origin` equivalent). State how this was checked.
4. `flapc` is built by Bazel from `Libraries/LibJS/Flap` and used as the genrule
   tool for `gen_interpreter_asm`, so `interpreter_x86_64.S` no longer depends on
   a cargo artifact. Its 3-package lock gets the same treatment as the big one.
5. `librust_combined.a` and the `ar -M` merge step are **gone**, along with the
   `Build/full/cargo/.../BUILD.bazel` shim package and the README step 1b that
   tells the user to run `ar`.
6. `Meta/emit_cargo_bazel.py` regenerates the fetch rules from `Cargo.lock` with
   **no cargo, no network, no CMake**, and is idempotent (run twice, byte-identical).
7. Tests in `tests/test_emit_cargo.py`, run the way this repo runs tests (exec the
   module, call its `test_*` functions — there is no pytest). Cover at least: the
   154/13 registry-vs-workspace split, the per-crate feature flags, the
   non-uniform FFI header lists, and `.cargo-checksum.json` shape. Whole suite
   green (currently 91).
8. `docs/CASE-ladybird-migration.md` gets finding 34, and
   `examples/ladybird/README.md`'s gap 1 + "Reproducing" are updated to say the
   CMake build is no longer needed **at all** (if that is true — say so only if
   criterion 1 passed with both `Build/full/vcpkg_installed` *and* `Build/full/cargo`
   absent; note the parity harness still wants `Build/full` for its diff baseline,
   which is a different claim).

## Rules that govern this work

- **Never hand-edit a generated BUILD file.** Edit `Meta/emit_*.py` and
  regenerate. Check the emitter reproduces the checked-in file byte-for-byte
  before and after your change (`python3 Meta/emit_x.py | diff - file`).
- The overlay in `examples/ladybird/workspace/` and the live checkout
  `~/ladybird-work` must stay in sync; `~/ladybird-work` is its own git repo and
  is **not** part of any2bazel. `source ~/lb-env.sh` first.
- **Check a green check's coverage.** Compare against ground truth that neither
  the emitter nor its verifier produced.
- Long builds: sandbox restarts have killed runs 3× (symptoms: `uptime` resets,
  log tail is NUL bytes, RC=143). Wrap in a retry loop, log to `~` not `/tmp`,
  and poll with `for i in $(seq 1 40); do grep -qa RC= ~/x.log && break; sleep 30;
  done` with `timeout_seconds: 870` — the `bash` tool's `sleep` is capped at 60s
  unless you pass `timeout_seconds`.
- Render check needs `export XDG_RUNTIME_DIR=~/lbrun/runtime` (chmod 700) and the
  4 services staged into `$(bazel info execution_root)/bazel-out/k8-fastbuild/libexec/`.
  A Vulkan `-9` warning is expected.
- Disk fills up (`ar` failed with no message at 92% full once). `~/.cache/bazel-disk`
  is the thing to delete.
