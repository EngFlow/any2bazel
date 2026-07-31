# Ladybird → Bazel: the generated workspace

The Bazel workspace overlay produced by the any2bazel parity loop for
[Ladybird](https://ladybird.org) (CMake + vcpkg, C++23). The narrative — why
each of these files looks the way it does, and the 34 findings the migration
produced — is in [`docs/CASE-ladybird-migration.md`](../../docs/CASE-ladybird-migration.md).
This directory is the *artifact*: what you would drop into a Ladybird checkout.

**What it achieves today:** all five processes (`ladybird` UI, WebContent,
Compositor, RequestServer, ImageDecoder) are Bazel-built, all 51 code
generators run under Bazel with output **byte-identical to CMake's**
(1,408/1,408 files, checked by `Meta/bazel_parity_harness.py`, which accounts for
every one of the build's 586 ninja `CUSTOM_COMMAND`s — 51 covered, 535 excluded
with a stated reason, **0 unhandled**), the result renders pages
(`--headless=text` and `--headless=layout-tree` match the CMake reference byte
for byte), **the 77 vcpkg dependencies are fetched and built by Bazel with
zero network access** (findings 30–33), and **so are the 10 Rust crates and
`flapc`** — 154 crates.io crates fetched from `Cargo.lock`, built by
network-blocked cargo actions (finding 34).

**`git clone && bazel build //:ladybird` now builds the browser.** Verified by
removal, twice: with `Build/full/vcpkg_installed` gone, and then with
`Build/full/cargo` **and** `Build/full/bin/flapc` gone, a `bazel clean` build of
all six binaries renders `--headless=text` and `--headless=layout-tree`
byte-identically to the CMake reference. `Build/full` is still needed to
*regenerate* the BUILD files and to run the parity harness — a
converter-development dependency, not a build dependency. See
[Known gaps](#known-gaps) for what is still owed.

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
| `cargo_crates.bzl`, `cargo_index.bzl`, `cargo_extension.bzl` | One `http_archive` per crates.io crate (154), the name/version/sha256 index the vendor staging resolves through, and the module extension that creates the repos + the 3 pinned Rust toolchain components. **Generated** by `Meta/emit_cargo_bazel.py` **from `Cargo.lock` alone** — no cargo, no network, no CMake |
| `cargo.bzl`, `Meta/cargo_build.sh`, `Meta/cargo_binary_build.sh`, `Meta/cargo_vendor.sh` | `rust_sysroot` (the pinned 1.96.1 toolchain merged into one tree), `cargo_crate` / `cargo_binary` (offline, network-blocked build actions), and `cargo_lib` (one consumable `CcInfo` per crate: its archive + its FFI headers) |
| `cargo_ring.bzl` | The 10 `cargo_crate` + 10 `cargo_lib` targets and `flapc`, as a macro for the root package (the crate sources are at the repo root and `glob()` is package-relative). **Generated** by `Meta/emit_cargo_bazel.py` |
| `Build/full/**/BUILD.bazel` | Shims over the *reference CMake build tree*: **only generated headers now.** The Rust archive and `flapc` shims are gone (finding 34), as are the vcpkg ones (finding 33) |

The four generated files are reproducible: re-running each emitter against the
same `Build/full` reproduces them byte for byte — **on the same machine**. They
are not host-independent: a host include path can leak into a generated `copts`
(regenerating `Libraries/LibWeb/BUILD.bazel` on a machine with libdrm headers
adds `-I/usr/include/libdrm`), which is gap 3 below, not an emitter bug.

## Reproducing

To **build the browser**, no CMake build is needed — Bazel fetches and builds the
vcpkg tree and the Rust crates itself, with zero network access in either:

```sh
git clone https://github.com/LadybirdBrowser/ladybird && cd ladybird
# 1. Drop in the overlay.
cp -r .../examples/ladybird/workspace/. . && mv bazelrc.txt .bazelrc
git apply .../examples/ladybird/patches/*.patch   # generator determinism + a Qt header
                                                 # that is not self-contained (gap 7)
# 2. Build. This includes the 77 vcpkg ports (~45 min cold) and the 10 Rust
#    crates + flapc: the libraries depend on them through //Meta/vcpkg:<port> and
#    //:<crate>_lib, so there is no separate step. Build //:vcpkg_installed alone
#    if you want to time it.
bazel build //:ladybird //:WebContent //:RequestServer //:ImageDecoder //:Compositor //:WebWorker
```

To **regenerate or verify the BUILD files** you additionally need the reference
CMake build — that is a converter-development dependency, not a build dependency:

```sh
# 3. Reference CMake build: the model the emitters read, and the 1,408 generated
#    files the parity harness byte-compares against. NOT needed for vcpkg (finding
#    33) or Rust (finding 34) any more; verified with Build/full/vcpkg_installed,
#    Build/full/cargo and Build/full/bin/flapc all deleted.
cmake --preset Release -B Build/full -DENABLE_GUI_TARGETS=ON && ninja -C Build/full
# 4. Regenerate the BUILD files from it (optional — they are checked in).
python3 Meta/emit_build_bazel.py > BUILD.bazel
python3 Meta/emit_codegen_bazel.py Libraries/LibWeb > Libraries/LibWeb/codegen.bzl
python3 Meta/emit_root_codegen_bazel.py > codegen_root.bzl
python3 Meta/emit_libweb_bazel.py > Libraries/LibWeb/BUILD.bazel
# The vcpkg rules regenerate from the committed capture alone — no vcpkg, no network.
python3 Meta/emit_vcpkg_bazel.py --assets Meta/vcpkg_assets.tsv --distfiles > vcpkg_distfiles.bzl
python3 Meta/emit_vcpkg_bazel.py --assets Meta/vcpkg_assets.tsv --index     > vcpkg_index.bzl
python3 Meta/emit_vcpkg_bazel.py --assets Meta/vcpkg_assets.tsv --extension > vcpkg_extension.bzl
# The cargo rules regenerate from Cargo.lock alone — no cargo, no network, no CMake.
python3 Meta/emit_cargo_bazel.py --crates > cargo_crates.bzl
python3 Meta/emit_cargo_bazel.py --index  > cargo_index.bzl
python3 Meta/emit_cargo_bazel.py --ring   > cargo_ring.bzl
python3 Meta/emit_cargo_bazel.py --extension > cargo_extension.bzl
python3 Meta/emit_cargo_bazel.py --check .    # all four reproduce byte-for-byte
# 5. Check every generator against CMake.
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

1. **External deps: Bazel fetches AND builds all of them — vcpkg and Rust.**
   `bazel build //:vcpkg_installed` fetches all 76 distfiles as
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

   **Rust is closed too (finding 34), so the CMake build is no longer needed to
   build the browser at all.** The 260 MB prebuilt `librust_combined.a`, the
   hand-run `ar -M` that produced it and the reference build's `flapc` are gone.
   `Cargo.lock` already pins a sha256 for all 154 crates.io crates and the URL is
   a pure function of (name, version), so — unlike vcpkg — **nothing had to be
   captured**: `Meta/emit_cargo_bazel.py` emits the fetch rules from
   `Cargo.lock` alone, with no cargo, no network and no CMake, and the three
   pinned 1.96.1 toolchain components are `http_archive`s merged into one sysroot
   (no rustup, which fetches at run time). Each crate is then one sandboxed
   `block-network` action running `cargo --offline --locked` against a vendor dir
   assembled from Bazel's own fetched crates — checkable rather than merely
   claimed: deleting one crate from the vendor dir fails the action with "no
   matching package named `yuv` found" instead of downloading it.

   Consumed one target per crate (`//:<crate>_lib` → its archive + its generated
   FFI headers), one-for-one with CMake's `target_link_libraries` — **not** a
   shared `--start-group` over all ten archives, which is what I built first and
   which broke `ImageDecoder` and `RequestServer`. The shim's "the archives are
   circular" was false: measured, the cross-crate symbol edge count is **0** for
   all 10 crates in both directions, and the ~200-700 symbols any two share are
   each crate's own bundled copy of rust-std. Grouping them let ld satisfy one
   crate's std symbol from another crate's object and drag that crate's C++ FFI
   into a binary that never linked it. `ImageDecoder` now defines 28 `rust_*` FFI
   symbols where `WebContent` defines 328 — the narrowness is in the output, not
   just the BUILD file. Finding 34 has the whole autopsy.

   Verified by **removal** again: `Build/full/cargo` *and* `Build/full/bin/flapc`
   moved off the machine, `bazel clean`, all six binaries rebuilt from scratch
   (4,427 actions), and both `--headless=text` and `--headless=layout-tree` are
   byte-identical to the CMake reference — with Rust owning URL parsing, the CSS
   parser, the HTML tokenizer, regex and the text codecs, so a matching layout
   tree is a real signal. All 14 FFI headers are byte-identical to CMake's, and
   the 10 archives agree with the reference cargo build on the surface that can
   actually be linked against: **0 differences across all 331 `extern "C"` FFI
   symbols** (197 of them in `libweb_css_rust` alone). Their *internal* symbols do
   differ — rustc's `17h<hash>E` suffixes, LLVM `anon.*.llvm.<n>` names and the
   metadata hash in each object-file name are functions of the build path, so
   ~2.4k of ~50k names differ after normalizing the obvious ones. That is noise of
   the same class as the vcpkg `.dynstr` deltas in finding 32, but it is worth
   stating precisely rather than rounding to "identical": nothing here proves the
   archives are bit-identical, only that their linkable ABI is.

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

   Remaining shim debt, and it is the same shape on both sides — over-declared
   input *sets* with correct *paths*: each `vcpkg_lib` declares the whole vcpkg
   tree as its header input, so any change in the dep tree re-hashes every C++
   compile (making it per-port needs the tree split into per-port outputs, which
   vcpkg's `.list` manifests would give us — finding 24), and every
   `cargo_crate` declares the whole cargo **workspace's** sources, because cargo
   resolves the workspace whichever crate you ask for, so touching one `.rs`
   rebuilds all 11 crates. Both trade a rebuild for correctness, which is the
   right way round — under-declaring silently reuses a stale artifact. Per-crate
   source sets need the path-dependency graph read out of the manifests.
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
3. **`.bazelrc` still has host escapes:** `--action_env=CPLUS_INCLUDE_PATH=/usr/include/libdrm`
   and `-L/usr/lib/x86_64-linux-gnu`, both because libdrm and Vulkan are
   host-provided. (The `-L`/`-rpath` into `Build/full/vcpkg_installed/` are gone —
   finding 33.)
4. **The generated-header shims over `Build/full` are what is left of the CMake
   dependency.** `Build/full/**/BUILD.bazel` no longer shims any *binary* — the
   vcpkg `.so`s (finding 33) and the Rust archives + `flapc` (finding 34) are all
   Bazel-built, and `flapc` is now a real `cargo_binary` Bazel builds *and* runs
   as a genrule tool, so `interpreter_x86_64.S` depends on no cargo artifact. What
   remains is a handful of generated *headers* the emitters have not yet taught
   Bazel to generate, plus `Build/full` itself as the model the emitters read and
   the baseline the parity harness diffs against. That last one is a
   converter-development dependency, not a build dependency.
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
7. **Three upstreamable Ladybird fixes.** The first is the `sorted()` determinism
   fix in `Meta/Generators/libweb_bindings/to_idl_value.py`, filed as
   [ladybird#10899](https://github.com/LadybirdBrowser/ladybird/issues/10899) and
   fixed upstream.

   The second is a one-line `#include <UI/Qt/Tab.h>` in `UI/Qt/TabBar.h`:
   [`patches/0002-ui-qt-tabbar-self-contained-header.patch`](patches/0002-ui-qt-tabbar-self-contained-header.patch).
   `TabBar.h` calls `as<Tab>()` — a `dynamic_cast`, needing Tab's complete type —
   while only forward-declaring `Tab`, and compiles under CMake purely by ordering
   luck: AUTOMOC's unity `mocs_compilation.cpp` includes `moc_Tab.cpp` (hence
   `Tab.h`) before `moc_TabBar.cpp`. Bazel mocs each header separately, so nothing
   supplies the definition first. A latent upstream bug rather than a Bazel quirk —
   any build that changes compile order (different unity bucketing, an IWYU pass)
   hits it. Still to file upstream.

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
