# Ladybird → Bazel: the generated workspace

The Bazel workspace overlay produced by the any2bazel parity loop for
[Ladybird](https://ladybird.org) (CMake + vcpkg, C++23). The narrative — why
each of these files looks the way it does, and the 33 findings the migration
produced — is in [`docs/CASE-ladybird-migration.md`](../../docs/CASE-ladybird-migration.md).
This directory is the *artifact*: what you would drop into a Ladybird checkout.

**What it achieves today:** all five processes (`ladybird` UI, WebContent,
Compositor, RequestServer, ImageDecoder) are Bazel-built, all 51 code
generators run under Bazel with output **byte-identical to CMake's**
(1,408/1,408 files, checked by `Meta/bazel_parity_harness.py`, which accounts for
every one of the build's 586 ninja `CUSTOM_COMMAND`s — 51 covered, 535 excluded
with a stated reason, **0 unhandled**), the result renders pages
(`--headless=text` and `--headless=layout-tree` match the CMake reference byte
for byte), and **the 77 vcpkg dependencies are fetched and built by Bazel with
zero network access**, then *consumed* from Bazel's own output tree: with the
CMake vcpkg tree deleted from the machine, a clean build of all six binaries
still renders `--headless=layout-tree` byte-identically to the reference
(findings 30–33).

**It is not yet a clean-machine build.** See [Known gaps](#known-gaps).

## Layout

`workspace/` mirrors the paths these files occupy inside a Ladybird checkout.

| Path | What it is |
|------|-----------|
| `MODULE.bazel` | bzlmod deps: `rules_cc`, `platforms`, `rules_shell`, `rules_qt` (fetched from upstream, see below), plus the 76 vcpkg distfile repos via a generated module extension |
| `Meta/vcpkg_assets.tsv` | **The dependency pin.** 76 `(url, sha512, dst)` rows captured from vcpkg itself via its `x-script` asset hook. Everything below is generated from this, with no vcpkg, no CMake and no network |
| `vcpkg_distfiles.bzl`, `vcpkg_index.bzl`, `vcpkg_extension.bzl`, `vcpkg_git_archives.bzl` | One `http_file` per distfile, the sha512→label index the asset script resolves through, the module extension that creates the repos, and the 4 `vcpkg_from_git` archives. **Generated** by `Meta/emit_vcpkg_bazel.py` |
| `vcpkg.bzl`, `Meta/vcpkg_build.sh` | `vcpkg_tree`: builds the whole dep tree as an ordinary Bazel action with `x-block-origin`, so it reaches the network zero times. Deliberately not a `repository_rule` |
| `Meta/vcpkg_capture_assets.sh` | Records the pin, by *being* vcpkg's asset cache. The one run allowed to fetch |
| `bazelrc.txt` | → `.bazelrc`. Global copts/defines/linkopts mirrored from `Meta/CMake/compile_options.cmake` |
| `BUILD.bazel` | Root package: 34 libraries, the 5 executables, Qt moc/rcc genrules. **Generated** by `Meta/emit_build_bazel.py` |
| `codegen_root.bzl` | Non-LibWeb generator genrules (IPC endpoints, LibJS Bytecode/Op, HSTS table, WebGL replayer, TIFF tag tables, the two SPIR-V shader headers, and the chained `generate_interpreter_layout` → `flapc` interpreter assembly). **Generated** by `Meta/emit_root_codegen_bazel.py` |
| `Libraries/LibWeb/BUILD.bazel`, `generated_srcs.bzl` | LibWeb (~1,961 compile inputs). **Generated** by `Meta/emit_libweb_bazel.py` |
| `Libraries/LibWeb/codegen.bzl` | LibWeb's 26 generator genrules + the bindings mega-genrule (661 `.idl` → 1,331 files). **Generated** by `Meta/emit_codegen_bazel.py` |
| `Meta/emit_*.py` | The emitters. They read CMake's `build.ninja` + the File API codemodel and write the four generated files above |
| `Meta/bazel_parity_harness.py` | Buckets every ninja `CUSTOM_COMMAND` as covered / excluded-with-reason / unhandled, re-runs the covered ones and byte-compares against CMake's tree. Non-zero unhandled is a failure |
| `Meta/BUILD.bazel` | `//Meta:generators` filegroup (the generator scripts, as genrule inputs) |
| `Meta/vcpkg/BUILD.bazel` | The 41 `vcpkg_lib` targets Ladybird's libraries depend on, backed by `//:vcpkg_installed`. Hand-written and stable (one target per port); this is the whole interface between Ladybird and its dependencies |
| `Build/full/**/BUILD.bazel` | Shims over the *reference CMake build tree*: the prebuilt Rust archives and the `flapc` binary (a cargo crate). **This is the remaining Ring 2 debt** — the vcpkg shims are gone, and no generated *source* is shimmed either |

The four generated files are reproducible: re-running each emitter against the
same `Build/full` reproduces them byte for byte — **on the same machine**. They
are not host-independent: a host include path can leak into a generated `copts`
(regenerating `Libraries/LibWeb/BUILD.bazel` on a machine with libdrm headers
adds `-I/usr/include/libdrm`), which is gap 3 below, not an emitter bug.

## Reproducing

```sh
git clone https://github.com/LadybirdBrowser/ladybird && cd ladybird
# 1. Reference CMake build. Still needed for the RUST artifacts (gap 1) and for
#    the 696 generated files the parity harness diffs against. NOT for vcpkg any
#    more: Bazel fetches and builds the whole dep tree itself, and the build has
#    been verified with Build/full/vcpkg_installed deleted.
cmake --preset default -B Build/full -DENABLE_GUI_TARGETS=ON && ninja -C Build/full
# 1b. Merge the per-crate cargo archives into the one combined archive the Rust
#     shim cc_imports (see gap 1). They have circular cross-crate references, so
#     a flat link cannot resolve them by ordering.
(cd Build/full/cargo/build/x86_64-unknown-linux-gnu/release && \
 { echo "create librust_combined.a"; for a in liblib*_rust.a; do echo "addlib $a"; done; \
   echo save; echo end; } | ar -M)
# 2. Drop in the overlay.
cp -r .../examples/ladybird/workspace/. . && mv bazelrc.txt .bazelrc
git apply .../examples/ladybird/patches/*.patch   # generator determinism (gap 7)
# 2b. The vcpkg dependency tree — Bazel fetches all 76 distfiles and builds all 77
#     ports with zero network access. Not a separate step any more: the libraries
#     depend on it through //Meta/vcpkg:<port>, so step 4 builds it. Build it
#     alone if you want to time it (~45 min cold).
bazel build //:vcpkg_installed
# 3. Regenerate the BUILD files from the reference build (optional — they are checked in).
python3 Meta/emit_build_bazel.py > BUILD.bazel
python3 Meta/emit_codegen_bazel.py Libraries/LibWeb > Libraries/LibWeb/codegen.bzl
python3 Meta/emit_root_codegen_bazel.py > codegen_root.bzl
python3 Meta/emit_libweb_bazel.py > Libraries/LibWeb/BUILD.bazel
# The vcpkg rules regenerate from the committed capture alone — no vcpkg, no network.
python3 Meta/emit_vcpkg_bazel.py --assets Meta/vcpkg_assets.tsv --distfiles > vcpkg_distfiles.bzl
python3 Meta/emit_vcpkg_bazel.py --assets Meta/vcpkg_assets.tsv --index     > vcpkg_index.bzl
python3 Meta/emit_vcpkg_bazel.py --assets Meta/vcpkg_assets.tsv --extension > vcpkg_extension.bzl
# 4. Build, and check every generator against CMake.
bazel build //:ladybird //:WebContent //:RequestServer //:ImageDecoder //:Compositor
python3 Meta/bazel_parity_harness.py     # expects 1408/1408 identical, 0 UNHANDLED
# Sweep hash seeds: a generator that iterates a set matches under some seeds only.
for s in 0 1 7 42; do python3 Meta/bazel_parity_harness.py --seed $s; done
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

1. **External deps: Bazel now fetches AND builds them; the shims still read the
   CMake tree.** `bazel build //:vcpkg_installed` fetches all 76 distfiles as
   `http_file`s (hashes from a committed capture of what vcpkg actually
   downloads) and builds all 77 ports with `x-block-origin` — **zero network
   access**. Verified against CMake's reference tree, not merely built: identical
   5,018-file listing, 4,740 byte-identical including *every* header/`.cmake`/`.pc`,
   and all 179 differing binaries have byte-identical exported symbol tables (the
   diffs are embedded build paths — traced to a 74-byte `.dynstr` delta, plus
   `__FILE__` strings). Then verified by **removal**: reference tree moved out of
   the build path, Bazel's put in its place, all 5 binaries relink and render
   `--headless=text`/`--headless=layout-tree` byte-identically, with JS running.
   Findings 30–32.

   **The plumbing now lands too: nothing reads the CMake vcpkg tree any more.**
   The 34 `cc_import` shims are gone. `cc_import` takes a *file* and `vcpkg_tree`
   declares a *directory*, so instead of enumerating ~40 per-file outputs, the
   deps are consumed through `//Meta/vcpkg:<port>` (`vcpkg_lib`, 41 targets): a
   rule returning a `CcInfo` whose compilation context carries the tree as a
   header input plus `-isystem <tree>/<triplet>/include`, and whose linking
   context carries `-L<tree>/lib -l<port>` with **the tree itself as
   `additional_inputs`** — which is what makes the directory a declared input of
   every link that depends on it. Unlike `cc_import` this also fixes the SONAME
   problem for free (`cc_import` stages `libfmt.so` under its *file* name while
   the loader asks for `libfmt.so.12`; a search path into the real tree has every
   version symlink in place). The tree attr is pinned `cfg = "exec"` in both
   configurations, so the 45-minute build happens **once** rather than once per
   configuration for byte-identical output.

   Verified by **removal**, the only test that counts here: `Build/full/vcpkg_installed`
   moved off the machine, `bazel clean`, then all 6 binaries rebuilt from scratch
   (4,390 actions) — Bazel built the 76 ports itself, and `--headless=layout-tree`
   is byte-identical to the CMake reference. `ldd` resolves 46 libraries out of
   Bazel's output tree and **0** out of `Build/full`. The finding-32 control
   agrees: the `libSDL3` the binary loads has the 58 PulseAudio symbols only
   Bazel's tree has. The four global `.bazelrc` escapes into `Build/full`
   (`-isystem`, `-L`, `-rpath-link`, `-rpath`, each duplicated as `--host_*`) are
   deleted, not relocated.

   That also closes part of the shim-granularity debt below: the vcpkg include
   dirs are no longer a *global* `-isystem` every TU gets whether it asked or not.
   `include/skia`, `include/harfbuzz` and `include/libxml2` now ride on the port
   that owns them, so a TU including `<skia/...>` must depend on `//Meta/vcpkg:skia`.

   What remains is Rust: the crates are still a 260 MB prebuilt
   `librust_combined.a` (10 cargo archives pre-merged with `ar`, because they have
   circular cross-crate references a flat link cannot order), and `flapc` is still
   the reference build's binary. Consequence: **you must still run the CMake build
   first — but for Rust, no longer for vcpkg.**

   Wrapping vcpkg is explicitly a stepping stone, not the destination. It keeps
   vcpkg as the *recipe* (it encodes the patches, configure flags and feature sets
   Ladybird pins) while Bazel owns fetching, hashing and sandboxing — deliberately
   not the BCR, which would change versions/patches/features and destroy the
   parity baseline. But that baseline is weaker than it looks: finding 32 caught
   the reference `libSDL3` missing a PulseAudio driver that Bazel's has, because
   `libpulse-dev` was installed on this machine 29 minutes *after* the reference
   built SDL3 — SDL's CMake sniffs the host, ungated by any vcpkg feature. So
   byte-parity against a vcpkg tree is a function of the host's `-dev` packages,
   and the real fix is building the deps with Bazel's own toolchain and declared
   sysroot.

   Remaining shim debt: each `vcpkg_lib` still declares the *whole tree* as its
   header input, so any change in the dep tree re-hashes every C++ compile. The
   include *paths* are now per-port; the input *set* is not. Making it per-port
   needs the tree split into per-port outputs, which vcpkg's `.list` manifests
   would give us (finding 24) — worth doing for a remote cache, invisible locally.
2. **The exec configuration's flags are a duplicated list.** Now that Bazel
   *runs* a tool it built (`//:generate_interpreter_layout`), that tool and the AK
   it links are built in the exec configuration, a separate flag namespace that
   `--cxxopt`/`--copt`/`--linkopt` do not reach — so every global flag is repeated
   as `--host_cxxopt`/`--host_copt`/`--host_linkopt`, plus
   `--host_compilation_mode=fastbuild` (the exec default of `opt` adds
   `-D_FORTIFY_SOURCE=1`, which collides with our `=3` under `-Werror`).

   The duplication is ugly but the *split* is the point, and here it is a
   correctness requirement: that tool prints struct offsets and sizes which
   `flapc` bakes into the interpreter assembly, so it must see the same headers
   with the same defines and the same ABI flags as the code that will use those
   offsets. A host/target flag skew is silent memory corruption at run time.
   CMake has no such namespace at all — a host tool and a target library are
   compiled from the same variables by default, which is convenient until you
   cross-compile, at which point the skew is silent. Bazel makes the two
   namespaces explicit and thereby makes the duplication visible; the right fix
   is a shared `.bzl` flag list or a custom toolchain, not more `--host_*` lines.
3. **`.bazelrc` still has host escapes:** `--action_env=CPLUS_INCLUDE_PATH=/usr/include/libdrm`,
   `-L/usr/lib/x86_64-linux-gnu`, and `-L`/`-rpath` into
   `Build/full/vcpkg_installed/`. All three are consequences of (1) plus libdrm
   being host-provided.
4. **`flapc` is a prebuilt binary, not a Bazel-built one.** Bazel *runs* it as a
   declared genrule tool, so the interpreter assembly it emits is genuinely Bazel
   output — but the compiler binary itself comes from the reference cargo build,
   because `Libraries/LibJS/Flap` is a Rust crate (51 `.rs` files, a crates.io
   dep) and building it needs the same rules_rust ring as the prebuilt archives
   in (1). The other half of that pair, `generate_interpreter_layout`, *is* a real
   `cc_binary` Bazel builds and runs.
5. **Running the UI needs manual staging** (the commands above). Ladybird's UI
   spawns its service binaries by looking next to itself, and `share/Lagom` for
   resources. A real Bazel setup would express this with `data` + runfiles;
   doing so means teaching Ladybird's process-launch path about runfiles, so it
   is a change to the target, not just to the BUILD files.
6. **`rules_qt` is not on the BCR.** `MODULE.bazel` uses an `archive_override`
   pointing at kklochkov/rules_qt v2.0.1's release tarball (stock upstream, no
   patches). The BCR's `rules_qt` module is Vertexwahn's unrelated `rules_qt6`.
   Qt itself *is* host-portable: `qt.local_repo` discovers the host Qt via
   `qmake -query`, so no Qt SDK is vendored.
7. **Three upstreamable Ladybird fixes.** Two are applied in the checkout, not here:
   the `sorted()` determinism fix in `Meta/Generators/libweb_bindings/to_idl_value.py`
   (filed as [ladybird#10899](https://github.com/LadybirdBrowser/ladybird/issues/10899),
   fixed upstream) and a one-line `#include <UI/Qt/Tab.h>` in `UI/Qt/TabBar.h`
   to make the header self-contained (still to file).

   A **third** one is now needed, in the same function as the first: the topological
   sort in `dictionaries_in_dependency_order` iterates a *set* of dependency names,
   so two dictionaries that do not depend on each other (`AudioConfiguration` and
   `VideoConfiguration`, both reached from `MediaConfiguration`) emit in hash order
   and `Bindings/MediaCapabilities.h` varies with `PYTHONHASHSEED`. A topological
   sort constrains dependency-before-dependent; the order among independent
   siblings must be pinned separately. Found by the harness's seed sweep, not by
   any single run — the inherited-seed run was clean. The fix is in
   [`patches/0001-libweb-bindings-deterministic-dictionary-order.patch`](patches/0001-libweb-bindings-deterministic-dictionary-order.patch);
   apply it in the checkout before running the harness.
