# Ladybird → Bazel: the generated workspace

The Bazel workspace overlay produced by the any2bazel parity loop for
[Ladybird](https://ladybird.org) (CMake + vcpkg, C++23). The narrative — why
each of these files looks the way it does, and the 21 findings the migration
produced — is in [`docs/CASE-ladybird-migration.md`](../../docs/CASE-ladybird-migration.md).
This directory is the *artifact*: what you would drop into a Ladybird checkout.

**What it achieves today:** all five processes (`ladybird` UI, WebContent,
Compositor, RequestServer, ImageDecoder) are Bazel-built, all 46 code
generators run under Bazel with output **byte-identical to CMake's**
(1,402/1,402 files, checked by `Meta/bazel_parity_harness.py`), and the result
renders pages (`--headless=text` matches the CMake reference byte for byte).

**It is not yet a clean-machine build.** See [Known gaps](#known-gaps).

## Layout

`workspace/` mirrors the paths these files occupy inside a Ladybird checkout.

| Path | What it is |
|------|-----------|
| `MODULE.bazel` | bzlmod deps: `rules_cc`, `platforms`, `rules_qt` (fetched from upstream, see below) |
| `bazelrc.txt` | → `.bazelrc`. Global copts/defines/linkopts mirrored from `Meta/CMake/compile_options.cmake` |
| `BUILD.bazel` | Root package: 34 libraries, the 5 executables, Qt moc/rcc genrules. **Generated** by `Meta/emit_build_bazel.py` |
| `codegen_root.bzl` | Non-LibWeb generator genrules (IPC endpoints, LibJS Bytecode/Op, HSTS table, WebGL replayer). **Generated** by `Meta/emit_root_codegen_bazel.py` |
| `Libraries/LibWeb/BUILD.bazel`, `generated_srcs.bzl` | LibWeb (~1,961 compile inputs). **Generated** by `Meta/emit_libweb_bazel.py` |
| `Libraries/LibWeb/codegen.bzl` | LibWeb's 26 generator genrules + the bindings mega-genrule (661 `.idl` → 1,331 files). **Generated** by `Meta/emit_codegen_bazel.py` |
| `Meta/emit_*.py` | The emitters. They read CMake's `build.ninja` + the File API codemodel and write the four generated files above |
| `Meta/bazel_parity_harness.py` | Re-runs every generator command and byte-compares Bazel's output tree against CMake's |
| `Meta/BUILD.bazel` | `//Meta:generators` filegroup (the generator scripts, as genrule inputs) |
| `Build/full/**/BUILD.bazel` | Shims over the *reference CMake build tree*: vcpkg `cc_import`s, prebuilt Rust archives, glslang shader headers. **These are the Ring 2 debt** |

The four generated files are reproducible: re-running each emitter against the
same `Build/full` reproduces them byte for byte.

## Reproducing

```sh
git clone https://github.com/LadybirdBrowser/ladybird && cd ladybird
# 1. Reference CMake build (also materializes the 696 generated files the
#    parity harness diffs against, and the vcpkg/rust outputs Ring 2 will replace).
cmake --preset default -B Build/full -DENABLE_GUI_TARGETS=ON && ninja -C Build/full
# 2. Drop in the overlay.
cp -r .../examples/ladybird/workspace/. . && mv bazelrc.txt .bazelrc
# 3. Regenerate the BUILD files from the reference build (optional — they are checked in).
python3 Meta/emit_build_bazel.py > BUILD.bazel
python3 Meta/emit_codegen_bazel.py Libraries/LibWeb > Libraries/LibWeb/codegen.bzl
python3 Meta/emit_root_codegen_bazel.py > codegen_root.bzl
python3 Meta/emit_libweb_bazel.py > Libraries/LibWeb/BUILD.bazel
# 4. Build, and check every generator against CMake.
bazel build //:ladybird //:WebContent //:RequestServer //:ImageDecoder //:Compositor
python3 Meta/bazel_parity_harness.py     # expects 1402/1402 identical
```

The emitters locate the checkout via `$LADYBIRD_ROOT`, defaulting to the parent
of `Meta/` — so they work from any checkout path.

Running the UI currently needs manual staging, which is itself a gap (see
below):

```sh
export XDG_RUNTIME_DIR=/tmp/xdg-lb && mkdir -p $XDG_RUNTIME_DIR && chmod 700 $XDG_RUNTIME_DIR
ER=$(bazel info execution_root); mkdir -p "$ER/bazel-out/k8-fastbuild/libexec"
for b in WebContent RequestServer ImageDecoder Compositor; do cp -f bazel-bin/$b "$ER/bazel-out/k8-fastbuild/libexec/"; done
ln -sfn "$PWD/Build/full/share/Lagom" "$ER/bazel-out/k8-fastbuild/share"
./bazel-bin/ladybird --headless=text file:///tmp/test-page.html
```

## Known gaps

Honest inventory of what stops this from being a clone-and-build.

1. **External deps are shims over the CMake build tree, not Bazel deps.**
   224 vcpkg `.so`s are `cc_import`ed out of `Build/full/vcpkg_installed/`, and
   the Rust crates come from a 260 MB prebuilt `librust_combined.a` (10 cargo
   archives pre-merged with `ar`, because they have circular cross-crate symbol
   references a flat link cannot order). Replacing both with real bzlmod deps
   (`rules_foreign_cc`/BCR, `rules_rust`) is Ring 2 and the largest remaining
   piece. Consequence: **you must run the CMake build first.**
2. **`.bazelrc` still has host escapes:** `--action_env=CPLUS_INCLUDE_PATH=/usr/include/libdrm`,
   `-L/usr/lib/x86_64-linux-gnu`, and `-L`/`-rpath` into
   `Build/full/vcpkg_installed/`. All three are consequences of (1) plus libdrm
   being host-provided.
3. **Two glslang-generated shader headers** (`WebContentViewLinux{Frag,Vert}Shader.h`)
   are still copied from CMake's build dir via `Build/full/UI/BUILD.bazel` — the
   only generator not yet Bazel-run.
4. **Running the UI needs manual staging** (the commands above). Ladybird's UI
   spawns its service binaries by looking next to itself, and `share/Lagom` for
   resources. A real Bazel setup would express this with `data` + runfiles;
   doing so means teaching Ladybird's process-launch path about runfiles, so it
   is a change to the target, not just to the BUILD files.
5. **`rules_qt` is not on the BCR.** `MODULE.bazel` uses an `archive_override`
   pointing at kklochkov/rules_qt v2.0.1's release tarball (stock upstream, no
   patches). The BCR's `rules_qt` module is Vertexwahn's unrelated `rules_qt6`.
   Qt itself *is* host-portable: `qt.local_repo` discovers the host Qt via
   `qmake -query`, so no Qt SDK is vendored.
6. **Two upstreamable Ladybird fixes** are applied in the checkout, not here:
   the `sorted()` determinism fix in `Meta/Generators/libweb_bindings/to_idl_value.py`
   (filed as [ladybird#10899](https://github.com/LadybirdBrowser/ladybird/issues/10899),
   fixed upstream) and a one-line `#include <UI/Qt/Tab.h>` in `UI/Qt/TabBar.h`
   to make the header self-contained (still to file).
