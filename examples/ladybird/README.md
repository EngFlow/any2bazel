# Ladybird → Bazel: the generated workspace

The Bazel workspace overlay produced by the any2bazel parity loop for
[Ladybird](https://ladybird.org) (CMake + vcpkg, C++23). The narrative — why
each of these files looks the way it does, and the 36 findings the migration
produced — is in [`docs/CASE-ladybird-migration.md`](../../docs/CASE-ladybird-migration.md).
This directory is the *artifact*: what you would drop into a Ladybird checkout.

**What it achieves today:** all six processes (`ladybird` UI, WebContent,
WebWorker, Compositor, RequestServer, ImageDecoder) are Bazel-built, all 51 code
generators run under Bazel with output **byte-identical to CMake's**
(1,408/1,408 files, checked by `Meta/bazel_parity_harness.py`, which accounts for
every one of the build's 586 ninja `CUSTOM_COMMAND`s — 51 covered, 535 excluded
with a stated reason, **0 unhandled**), the result renders pages
(`--headless=text` and `--headless=layout-tree` match the CMake reference byte
for byte), **the 77 vcpkg dependencies are fetched and built by Bazel with
zero network access** (findings 30–33), and **so are the 10 Rust crates, `flapc`
and `cranelift-compiler`** — 154 crates.io crates fetched from `Cargo.lock`,
built by network-blocked cargo actions (findings 34–35).

**Nothing in the emitted build reads CMake's build tree (`Build/full`).** Verified
by removal: with `Build/full/{Libraries,Services,UI,bin,cargo}` moved off the
machine — every Ladybird-generated header and every binary CMake produced — all six
binaries build from scratch and render `--headless=text` **and**
`--headless=layout-tree` byte-identically to the CMake reference, and `cquery` over
the closure of all six returns **0** targets under `Build/full` (down from 741).
`Build/full` is still needed to *regenerate* the BUILD files and to run the parity
harness — a converter-development dependency, not a build dependency.

**A fresh clone now builds and renders — after one ordinary CMake build has run
once.** (Three inputs come from that build rather than from Bazel; a Ladybird
developer therefore stages nothing, and the gap is real only for a Bazel-only clone
— see [how a Ladybird developer gets them](#how-a-ladybird-developer-gets-these-three-inputs).) The
end-to-end test (clone into a new directory, drop in the overlay, nothing else)
was run for the first time and failed **six separate times**; `Build/full` was
the dependency I had removed, and **five of the six had nothing to do with it**.
All six are addressed below, and the result is verified on the clone rather than
asserted: `//:vcpkg_installed` builds all 76 ports offline, all six binaries
build (2,842 actions, RC=0), and `--headless=text`/`--headless=layout-tree`
are **byte-identical to the CMake reference on all three test pages**. Rows 1–3
are still manual staging: two are fixed bugs (#4, #5), one now fails loudly
instead of 20 minutes later (#6), and the remainder are inputs the recipe must
produce or document:

| # | What is missing on a fresh clone | Why the dev machine hid it |
|---|---|---|
| 1 | **`Build/vcpkg`** — a git clone of microsoft/vcpkg at `vcpkg.json`'s `builtin-baseline`, which `//Build/vcpkg:tree` globs with `allow_empty = True`. Matches *one* file (its own `BUILD.bazel`) and the build fails with `/tmp/.../root/vcpkg: No such file or directory`. Ladybird's own `Meta/ladybird.py vcpkg` creates it; the recipe above never says so | `Meta/ladybird.py vcpkg` had been run months earlier |
| 2 | **`.git` inside that checkout** — vcpkg resolves versioned ports with `git read-tree`, so it is load-bearing (120 MB), and the filegroup *excludes* it. The action is `no-sandbox`, so it reads the real path and got it anyway: an **undeclared input the build needs** | `no-sandbox` + a real checkout on disk |
| 3 | **`Build/caches/HSTSPreload/transport_security_state_static.json`** (10 MB) — an *unversioned, unpinned* network download CMake does at configure time (`Meta/CMake/hsts_preload.cmake` fetches Chromium's `main`). `codegen_root.bzl` names it as a genrule `srcs`, so Bazel fails cleanly with `missing input file` — but there is no rule that produces it | the CMake configure had already downloaded it |
| 4 | **Two path bugs in `Meta/vcpkg_build.sh`** — the distfile index and its entries were passed **execroot-relative**, and vcpkg invokes the asset-cache script from its own cwd, so `awk`/`cp` looked in the wrong directory. Every asset lookup failed, reported as `no asset cache hits`, and `x-block-origin` then correctly refused the network. Fixed here (absolutize both) | the vcpkg checkout already had `downloads/tools/cmake-4.4.0-linux` from an earlier run, so vcpkg never *asked* the script for a tool |
| 5 | **The `angle` port runs `pip install ply`** (`x_vcpkg_get_python_packages`), which is **not** an asset-cache download and therefore not covered by the pin. A real hole in the "zero network access" claim: `vcpkg_tree` set `requires-network: "0"`, but that is a *scheduling hint*, and with `no-sandbox: "1"` **nothing enforced it** — with `use_default_shell_env = True` the action inherited `HTTP_PROXY`/`HTTPS_PROXY` and pip reached PyPI. **Fixed here:** the wheel is pinned by URL+sha256 (`vcpkg_python_packages.bzl`), staged into a find-links dir, and pip runs with `PIP_NO_INDEX` and the proxy variables unset | this sandbox exports a proxy, so pip silently succeeded through it |
| 6 | **The four `vcpkg_from_git` archives were pre-placed from a directory I had made by hand.** `vcpkg_git_archives.bzl` is generated, committed, listed in the table below — and **loaded by nothing**. The staging was `if [ -d ... ]; then cp ... 2>/dev/null \|\| true; fi`: three ways to succeed while copying nothing. Without them skia fails ~20 min in with `git fetch https://android.googlesource.com/.../piex.git … Error code: 128`, naming neither the directory nor the tarball. **Now a hard error in 4 seconds** naming both; reproducing them is a prefetch, not a rule — they are `git archive` output, so there is no URL to `http_file`, but cloning the pinned URL and `git archive`-ing the pinned ref reproduces the committed SHA512 exactly (verified on `libyuv`) | I created the directory by hand while building the pin, months before |

Rows 1–3 are inputs the recipe must produce or document (step 1a). Rows 4–6 were
bugs, and 5 is the substantive one: **"zero network access" was measured with
`x-block-origin`, which only governs vcpkg's own downloader.** A portfile that
shells out to pip, git or curl bypasses it entirely, and nothing was enforcing the
absence of a network — `requires-network: "0"` is a hint, and `no-sandbox: "1"`
means there is no namespace to enforce. `ply` is now pinned like any other
dependency (URL + sha256 → `http_file` → find-links dir → `PIP_NO_INDEX`), which
makes an unpinned package a hard error rather than a download. The four
`vcpkg_from_git` archives are still staged from a directory rather than produced by
the recipe — they are `git archive` output with no URL, so reproducing them means
cloning each pinned URL and `git archive`-ing the pinned ref (8 lines of shell, and
the committed SHA512s check it).

The lesson is the same one as finding 35, one layer out: **`Build/full` was the
dependency I went looking for, so it is the one I found.** The check that would
have caught all five is not a better `cquery` — it is doing the clone. See
[Known gaps](#known-gaps).

### Getting the three inputs — with and without CMake

Two audiences, two answers. **A Ladybird developer stages nothing:** one ordinary
`./Meta/ladybird.py build` produces all three as side effects, which is exactly why
they stayed invisible here for months.

| Input | Who produces it in a normal build |
|---|---|
| the vcpkg checkout + its `.git` | `Meta/Utils/build_vcpkg.py`, called by `ladybird.py` **`build`** as well as `vcpkg`: clone, checkout `builtin-baseline`, bootstrap the tool at the tag+SHA512 in `scripts/vcpkg-tool-metadata.txt`. ~70 s |
| `Build/caches/HSTSPreload/transport_security_state_static.json` | the CMake **configure**, via `hsts_preload.cmake` → `download_file`, gated on `ENABLE_NETWORK_DOWNLOADS` (default ON) |
| the four `git archive` tarballs | **vcpkg itself**, while building skia and angle |

**Without ever running CMake — the actual answer, and it needs no CMake at all:**

```sh
python3 Meta/ladybird.py vcpkg               # 1. the checkout + .git (~70s). No CMake:
                                             #    `vcpkg` is a standalone subcommand.
python3 Meta/fetch_vcpkg_git_archives.py     # 2. the four git archives (~80s), each
                                             #    reproduced with git clone + git archive
                                             #    and VERIFIED against the committed SHA512
curl -Lo Build/caches/HSTSPreload/transport_security_state_static.json \
    "$HSTS_URL"                              # 3. the HSTS table (see caveat below)
bazel build //:ladybird //:WebContent //:RequestServer //:ImageDecoder \
            //:Compositor //:WebWorker
```

Step 2 is `Meta/fetch_vcpkg_git_archives.py`, added here. Verified: **4/4 reproduced
from scratch and byte-identical to the pinned SHA512s.** Two things about it are worth
knowing, because both were mistakes I made first:

- **It takes the *list* from the committed pin, never from parsing portfiles.** A
  first version derived the list by scanning skia's and angle's portfiles and was
  wrong in both directions: 8 archives for skia where 4 are real, and libyuv missed
  entirely. `declare_external_from_git` only *declares*; feature- and
  platform-conditional `get_externals(${required_externals})` decides what is
  actually fetched, and libyuv's comes from its own port. **Statically deciding the
  set is unsound; statically resolving a name to a URL is fine**, and that is all the
  script does.
- **Regenerating the pin uses vcpkg as the instrument**, not a parser:
  `Meta/vcpkg_capture_git_archives.sh` runs `vcpkg install --only-downloads`, which
  executes the portfiles' fetch phase and stops — ~6 min, no compilation, no CMake.
  It produces **3 of the 4**: angle's zlib is fetched from angle's *build* phase, so
  it never appears in a downloads-only run. That asymmetry is recorded in the script
  rather than smoothed over.

Step 3 is the **one genuine hermeticity defect** and it cannot be closed here.
`hsts_preload.cmake` fetches Chromium's **`main`** — unversioned — so there is no
revision to pin to. Pinning a revision on the Bazel side alone would make Bazel's
output diverge from CMake's (the file at tag `139.0.7258.5` is 18.7 MB against the
10.5 MB `main` served when this machine configured, and the generated table differs),
trading a hermeticity gap for a **parity** gap. The fix is one line upstream: pin the
URL to a revision in `hsts_preload.cmake`, then `http_file` the same revision, and
both build systems consume one pinned input. Usefully, `download_file` is already a
no-op when the file exists (verified with `ENABLE_NETWORK_DOWNLOADS=OFF`), so a pinned
file staged by Bazel is consumed by CMake unchanged — the upstream change is additive.

Three shortcuts tested and rejected, all of which look like simplifications:

- **`--depth 1` on the vcpkg clone.** 8.7 MB instead of 121 MB, and `read-tree`
  even succeeds for some ports — then resolution fails on ffmpeg and harfbuzz with
  vcpkg's own `Try again with a full vcpkg clone`. The pinned versions' port trees
  live in **history**; that is what a version database *is*.
- **Dropping `builtin-baseline`** so no `.git` is needed at all. It resolves happily
  against the checked-out `ports/` — and **silently moves 10 dependency versions**
  (ffmpeg 7.1.1#5 → 8.1.2#3, harfbuzz 10.2.0 → 14.2.1#2, mimalloc 2.2.7 → 3.4.3,
  plus zlib, freetype, dbus, fontconfig, libedit, libwebp, cpptrace). A fix for a
  hermeticity blocker that changes ten dependency versions is not a fix.
- **A git submodule** — git's own answer to this, and it *does* work mechanically
  (a submodule's `.git` is a gitfile, and vcpkg's `read-tree` follows the
  indirection: verified). It fails for a better reason: **a submodule pins a
  checkout, and vcpkg pins history behind a baseline.** 14 of `vcpkg.json`'s 45
  `overrides` name a version that is *not* what `ports/` holds at the baseline
  (ffmpeg's pinned port is git-tree `0988005f…`; `HEAD:ports/ffmpeg` is
  `c40aaa40…`), so the bytes vcpkg builds exist at no single commit. A submodule
  would deliver precisely the insufficient state, still need the full 119 MB of
  history in `.git/modules`, sit in a `Build*/`-ignored directory vcpkg fills with
  ~3 GB of scratch (permanently-dirty submodule), and still not produce the `vcpkg`
  binary, which is bootstrapped from a tag+SHA512 in `scripts/vcpkg-tool-metadata.txt`.
  **`Build/vcpkg` is not a vendored dependency; it is a package manager's cache that
  happens to be a git checkout.** Finding 36.

A fresh full clone at the baseline resolves all **78 ports to exactly the versions
this dev checkout resolves** (`diff`, 0 differences) — so the checkout carries no
local state beyond the ref, which is what makes the prefetch step sufficient.

**This claim was false until recently, and the way it was false is the most
useful thing in this directory.** The README said it worked; it did not. Every
binary depended on **741 targets under `Build/full`**, the overlay shipped four
`BUILD.bazel` shims and *zero* headers, and `glob(..., allow_empty = True)` meant
a fresh clone got **no error at all** — just `fatal error: LibXML/Export.h: No
such file or directory` some 1,600 actions in. A glob over a foreign tree cannot
fail, so nothing ever said the tree was missing. Of the 741, **666 were merely
*shadowing* Bazel's own outputs** (the LibWeb bindings headers — Bazel generates
all 692, and the shim silently won or lost on include order); 31 were real gaps;
and closing them exposed **two genuine bugs the CMake tree had been masking** —
an FFI header collision and an entire missing Rust target. See
[Known gaps](#known-gaps) for what is still owed.

## Layout

`workspace/` mirrors the paths these files occupy inside a Ladybird checkout.

| Path | What it is |
|------|-----------|
| `MODULE.bazel` | bzlmod deps: `rules_cc`, `platforms`, `rules_shell`, `rules_qt` (fetched from upstream, see below), plus the 76 vcpkg distfile repos via a generated module extension |
| `Meta/vcpkg_assets.tsv` | **The dependency pin.** 76 `(url, sha512, dst)` rows captured from vcpkg itself via its `x-script` asset hook. Everything below is generated from this, with no vcpkg, no CMake and no network |
| `vcpkg_distfiles.bzl`, `vcpkg_index.bzl`, `vcpkg_extension.bzl`, `vcpkg_git_archives.bzl` | One `http_file` per distfile, the sha512→label index the asset script resolves through, the module extension that creates the repos, and the 4 `vcpkg_from_git` archives. **Generated** by `Meta/emit_vcpkg_bazel.py` |
| `vcpkg.bzl`, `Meta/vcpkg_build.sh` | `vcpkg_tree`: builds the whole dep tree as an ordinary Bazel action with `x-block-origin`, so it reaches the network zero times. Deliberately not a `repository_rule` |
| `Meta/vcpkg_capture_assets.sh` | Records the 76-distfile pin, by *being* vcpkg's asset cache. The one run allowed to fetch |
| `Meta/fetch_vcpkg_git_archives.py` | Produces the 4 `vcpkg_from_git` tarballs **without CMake**: takes the list from the committed pin, resolves each clone URL out of the portfiles, then `git clone` + `git -c core.autocrlf=false archive <ref>` and **verifies against the pinned SHA512**. 4/4 byte-identical |
| `Meta/vcpkg_capture_git_archives.sh` | Regenerates that pin with `vcpkg install --only-downloads` (~6 min, no compilation, no CMake) — vcpkg as the instrument, since which git externals are used is decided by feature-conditional CMake code, not by portfile text |
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
| `export_headers.bzl`, `Libraries/LibWeb/export_header.bzl` | The 15 `generate_export_header` `Export.h` files + AK's two `configure_file` headers, generated by Bazel from the same inputs CMake uses. **Generated** by `Meta/emit_export_headers_bazel.py`; `--check Build/full` byte-compares every one against CMake's |
| ~~`Build/full/**/BUILD.bazel`~~ | **Gone.** These shimmed the reference CMake build tree, and by the end they supplied *nothing*: the 709 headers they globbed were 21 Bazel generates and 688 LibWeb bindings headers Bazel also generates. Deleting them removed the *`Build/full`* dependency — not every clone-and-build blocker, as the table at the top now records |
| `Build/vcpkg/BUILD.bazel` | A filegroup over the microsoft/vcpkg checkout. **Still the same `allow_empty = True` glob over a foreign tree that finding 35 is about** — it just globs a *different* tree, so a fresh clone gets no diagnostic. Gap 7 |

The four generated files are reproducible: re-running each emitter against the
same `Build/full` reproduces them byte for byte — **on the same machine**. They
are not host-independent: a host include path can leak into a generated `copts`
(regenerating `Libraries/LibWeb/BUILD.bazel` on a machine with libdrm headers
adds `-I/usr/include/libdrm`), which is gap 3 below, not an emitter bug.

## Reproducing

To **build the browser**, no CMake build is needed — Bazel fetches and builds the
vcpkg *ports* and the Rust crates itself, and vcpkg's own downloader reaches the
network zero times. Three inputs still have to be present first (step 1a; gaps 7–8)
— two of them obtainable **without CMake**, the third (the HSTS table) unpinnable
until fixed upstream, so "no network at all" is not yet true:

```sh
git clone https://github.com/LadybirdBrowser/ladybird && cd ladybird
# 1. Drop in the overlay.
cp -r .../examples/ladybird/workspace/. . && mv bazelrc.txt .bazelrc
git apply .../examples/ladybird/patches/*.patch   # generator determinism + a Qt header
                                                 # that is not self-contained (gap 9)
# 1a. THE THREE INPUTS THE OVERLAY DOES NOT CARRY -- all three are produced by
#     ONE ordinary Ladybird build. A Ladybird developer stages nothing by hand;
#     these are blockers for a Bazel-ONLY clone, and each one is a thing CMake or
#     vcpkg produces as a side effect (see finding 36):
#       (a) Build/vcpkg + its .git   <- python3 Meta/ladybird.py vcpkg  (~70s)
#       (b) Build/caches/HSTSPreload/transport_security_state_static.json
#                                    <- the CMake CONFIGURE downloads it
#                                       (Meta/CMake/hsts_preload.cmake, gated on
#                                        ENABLE_NETWORK_DOWNLOADS, default ON)
#       (c) the four Meta/CMake/vcpkg/git-archives/*.tar.gz
#                                    <- vcpkg writes them into
#                                       Build/vcpkg/downloads/ while building
#                                       skia and angle. Verified: those files'
#                                       SHA512s equal the committed ones in
#                                       vcpkg_git_archives.bzl, i.e. the
#                                       "hand-made" directory was a copy of
#                                       vcpkg's own cache.
#     WITHOUT CMAKE (the supported path for a Bazel-only clone):
python3 Meta/ladybird.py vcpkg            # (a) checkout + .git, ~70s, no CMake
python3 Meta/fetch_vcpkg_git_archives.py  # (c) 4/4 reproduced with git archive and
                                          #     verified against the pinned SHA512s
curl -Lo Build/caches/HSTSPreload/transport_security_state_static.json \
  https://raw.githubusercontent.com/chromium/chromium/main/net/http/transport_security_state_static.json
#                                         # (b) unpinnable until fixed upstream
#     WITH CMake, if you were building Ladybird anyway, all three fall out of:
#         ./Meta/ladybird.py build
#         cp Build/vcpkg/downloads/{angle,libyuv,skia}-*.tar.gz Meta/CMake/vcpkg/git-archives/
#     `ply` needs nothing now: the wheel is pinned and pip runs with
#     PIP_NO_INDEX (gap 8).
#
# NB vcpkg's buildtrees peak around 3 GB. They go next to the declared output
#    (inside bazel-out) rather than $TMPDIR, so a small /tmp tmpfs is not a
#    problem and no flag is needed -- see the note in Meta/vcpkg_build.sh for why
#    the --action_env route is two ways wrong.
# 2. Build. This includes the 77 vcpkg ports (~45 min cold) and the 10 Rust
#    crates + flapc + cranelift-compiler: the libraries depend on them through
#    //Meta/vcpkg:<port> and //:<crate>_lib, so there is no separate step. Build
#    //:vcpkg_installed alone if you want to time it.
#    No CMake build is needed, and none is consulted -- see the removal test
#    above. That was not true before finding 35.
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
# The 15 Export.h + AK's 2 configure_file headers: emitted, and byte-compared
# against CMake's own copies. (AK/Backtrace.h is excluded on purpose -- it is a
# host PROBE, not a template, so comparing it to this machine's tree would only
# re-confirm this machine.)
python3 Meta/emit_export_headers_bazel.py        > export_headers.bzl
python3 Meta/emit_export_headers_bazel.py --libweb > Libraries/LibWeb/export_header.bzl
python3 Meta/emit_export_headers_bazel.py --check Build/full   # 17/17 identical
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
for b in WebContent RequestServer ImageDecoder Compositor WebWorker; do cp -f bazel-bin/$b "$ER/bazel-out/k8-fastbuild/libexec/"; done
# cranelift-compiler is found as a sibling of the spawning binary (Ladybird's own
# lookup chain), so it needs no path baked in -- only to be there.
cp -f bazel-bin/cranelift-compiler "$ER/bazel-out/k8-fastbuild/libexec/"
# The resource root, assembled from the CLONE and from Bazel's own vcpkg tree.
# This line used to read `ln -sfn "$PWD/Build/full/share/Lagom" ...` -- i.e. the
# recipe for running the Bazel build pointed at CMake's build tree, a seventh
# thing a fresh clone does not have (finding 36). Everything in it is either in
# Base/res or in //:vcpkg_installed; `chmod u+w` because Bazel's outputs are
# read-only and `cp -r` preserves that.
L="$ER/bazel-out/k8-fastbuild/share/Lagom"
chmod -R u+w "$L" 2>/dev/null; rm -rf "$L"; mkdir -p "$L/ladybird/pdfjs/web"
cp -r Base/res/. "$L/"
P=bazel-bin/vcpkg_installed/x64-linux-dynamic/share/pdfjs
cp -r --no-preserve=mode "$P/build" "$L/ladybird/pdfjs/"
cp -r --no-preserve=mode "$P/web/." "$L/ladybird/pdfjs/web/"
# UI/cmake/ResourceFiles.cmake stages this one file into pdfjs/web/, not pdfjs/.
mv "$L/ladybird/pdfjs/pdfjs-ladybird-transport.mjs" "$L/ladybird/pdfjs/web/"
./bazel-bin/ladybird --headless=text file:///tmp/test-page.html
```

The assembled tree is byte-identical to CMake's `Build/full/share/Lagom`
(`diff -rq`, 0 differences), and with it a fresh clone renders `--headless=text`
and `--headless=layout-tree` byte-identically to the CMake reference on all
three test pages.

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

   **Closing the header gap found two real bugs the CMake tree had been masking
   (finding 35), and this is the argument for verify-by-removal.** Both were
   *correctness* bugs in the Bazel build that a green build could not have shown,
   because a stale include path from CMake's tree was quietly supplying the right
   answer:

   * **An FFI header collision.** Eight of the ten crates emit a header literally
     named `RustFFI.h`, and four TUs `#include <RustFFI.h>` with no directory.
     CMake is unambiguous because `FFI_OUTPUT_DIR` defaults to the library's *own*
     binary dir; Bazel puts every dep's include dirs on one command line, so
     **LibRegex was compiling against LibUnicode's header** — masked only by a
     leftover `-IBuild/full/Libraries/LibRegex` that shadowed both. Removing the
     tree turned it into `'RustRegexFlags' has not been declared`. The fix needed
     *two* steps, and the first alone looked sufficient: publish the unprefixed
     dir on a separate target (`cargo_bare_include`) so it does not ride on
     `cargo_lib`, **and** depend on it through `implementation_deps`. Include dirs
     propagate along the C++ dep graph too, not just out of one rule — with a
     plain `deps` edge, LibGfx inherited LibTextCodec's dir through
     `LibGfx → LibTextCodec` and `YUVData.cpp` compiled against the wrong header
     (`'FFI' does not name a type`). `implementation_deps` is Bazel's name for
     exactly the scope CMake's `PRIVATE` include dir has, which is *why* a bare
     include is unambiguous in CMake and had to be made unambiguous here.
   * **An entire Rust target missing from the graph.** `Libraries/LibWasm` declares
     its Cranelift crate with `build_rust_binary()`, not `import_rust_crate()`, and
     the emitter parsed only the latter — so Cranelift was absent from Bazel
     *entirely*. Two host escapes were covering for it: the FFI header sat in
     `Build/full/Libraries/LibWasm` where a global `-I` reached it, and the
     compiler binary was named by an **absolute path to my machine** baked into
     `-DWASM_CRANELIFT_COMPILER_PATH=/home/ubuntu/...`, which alone would have
     broken any other checkout. It is now a `cargo_binary` that also declares its
     `cbindgen` header (byte-identical to CMake's), and the define is rewritten to
     a bare file name so Ladybird's *own* lookup chain
     (`resolve_cranelift_compiler_path`: env var → compile-time path →
     sibling-of-self) finds it in Bazel's bin dir — the dependency is declared as
     `data`, the path is not asserted.

   The generalizable part: **a shim that cannot fail cannot be trusted.** Both
   bugs were invisible while a foreign tree was on the include path, and both
   surfaced the moment it was removed. Neither would have been found by building
   harder.
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

   **No absolute host path remains in the generated BUILD files.** Two did until
   recently, and both were the same mistake in different clothing: a value that is
   correct on the machine that generated it and meaningless anywhere else, sitting
   in a checked-in file where nothing would ever contradict it.
   `-DWASM_CRANELIFT_COMPILER_PATH="/home/ubuntu/.../bin/cranelift-compiler"` was
   load-bearing (finding 35) and now resolves through Ladybird's own
   sibling-of-self lookup; `vcpkg_tree`'s `cache_dir` was merely a resumability
   affordance and is now empty, which is also the honest default (an empty cache
   *is* the genuine from-source build). Grepping the emitted output for `/home/`
   is a one-line check worth keeping in any migration — a hardcoded path that
   happens to work is the failure mode that survives every test run on the
   author's machine.
4. **The `Build/full` shims are gone; what remains is a *converter* dependency,
   not a build dependency.** `Build/full/**/BUILD.bazel` no longer exists. The
   vcpkg `.so`s (finding 33), the Rust archives + `flapc` (finding 34), the
   Cranelift compiler (finding 35), the 15 `Export.h` + AK's two `configure_file`
   headers (finding 35) and all 692 LibWeb generated headers are Bazel's own
   outputs. `Build/full` is still read by the *emitters* (it is the model they
   translate) and by the parity harness (it is the baseline they diff against) —
   which is a dependency of *regenerating* the BUILD files, not of building the
   browser from them.

   The distinction was invisible while the shims existed, which is exactly why
   they lasted: `glob(["**/*.h"], allow_empty = True)` over a tree that is not
   there yields an empty list and no diagnostic, so the build failed ~1,600
   actions later with a missing-header error naming neither the shim nor the
   tree. **The lesson generalizes past this migration:** a shim over a foreign
   build tree should fail loudly when the tree is absent, or it is indistinguishable
   from a shim that is not needed. `allow_empty = True` on a glob whose emptiness
   means "the thing you depend on is missing" converts a build error into a
   mystery.
5. **Running the UI needs manual staging** (the commands above). Ladybird's UI
   spawns its service binaries by looking next to itself, and `share/Lagom` for
   resources. A real Bazel setup would express this with `data` + runfiles;
   doing so means teaching Ladybird's process-launch path about runfiles, so it
   is a change to the target, not just to the BUILD files. **The staging block
   itself was a clone-and-build blocker until now** — its last line symlinked
   `Build/full/share/Lagom`, CMake's build tree, into place. Everything the
   resource root needs is in `Base/res` plus `//:vcpkg_installed`'s pdf.js, and
   the block above assembles it from those; the result is `diff -rq`-identical to
   CMake's. Finding 36.
6. **`rules_qt` is not on the BCR.** `MODULE.bazel` uses an `archive_override`
   pointing at kklochkov/rules_qt v2.0.1's release tarball (stock upstream, no
   patches). The BCR's `rules_qt` module is Vertexwahn's unrelated `rules_qt6`.
   Qt itself *is* host-portable: `qt.local_repo` discovers the host Qt via
   `qmake -query`, so no Qt SDK is vendored.
7. **The fresh clone needs three inputs the overlay does not carry** (step 1a
   above), and each stayed invisible for the same reason: it was already on the
   machine. `Build/vcpkg` — a microsoft/vcpkg checkout at `vcpkg.json`'s
   `builtin-baseline`, created by Ladybird's own `Meta/ladybird.py vcpkg` — is
   globbed by `//Build/vcpkg:tree` with `allow_empty = True`, so on a clone it
   matches one file and the failure names a missing `/tmp/.../root/vcpkg`. Its
   `.git` is *load-bearing* (vcpkg resolves versioned ports with `git read-tree`)
   yet the filegroup **excludes** it — an undeclared input the build reads anyway,
   because the action is `no-sandbox`. And
   `Build/caches/HSTSPreload/transport_security_state_static.json` is an
   *unversioned* download from Chromium's `main` branch that CMake does at
   configure time; `codegen_root.bzl` names it as a genrule input, so Bazel fails
   cleanly, but nothing produces it.

   The honest fix for all three is the same shape as findings 30–33: pin them and
   let Bazel fetch them. The vcpkg checkout is a `git_repository` at the
   `builtin-baseline` commit (which `vcpkg.json` already names, so the pin exists
   — it just is not wired to Bazel); the HSTS table is an `http_file`, and needs a
   hash Ladybird does not currently pin at all, because the URL tracks `main`.

8. **"Zero network access" is true of vcpkg's downloader, not of the vcpkg
   action.** The pin + `x-block-origin` is verified — a distfile missing from the
   index is a hard error, not a fetch. But `x-block-origin` only governs vcpkg's
   *own* downloads, and the `angle` overlay-port calls
   `x_vcpkg_get_python_packages`, which runs **`pip install ply`**. That is not an
   asset-cache download, so nothing pins it and `x-block-origin` never sees it.
   Nor is anything stopping it: `vcpkg_tree` sets `requires-network: "0"`, which is
   a *scheduling hint* Bazel does not enforce, and `no-sandbox: "1"` means there is
   no network namespace to enforce it in — so with `use_default_shell_env = True`
   the action inherits `HTTP_PROXY`/`HTTPS_PROXY` and pip reaches PyPI.

   It went unnoticed because this sandbox exports a proxy, so pip silently
   succeeded; a fresh clone in a network-free environment fails with
   `No matching distribution found for ply`. Two things to fix, and the second
   matters more than the first: pin `ply` as an `http_file` wheel staged into the
   port's venv, **and stop taking `requires-network: "0"` as a claim** — the
   verification has to be an environment with no route to the network, not a flag
   that says there is none. Same shape as the shim that could not fail: a control
   that is not enforced is indistinguishable from one that is not there.

9. **Three upstreamable Ladybird fixes.** The first is the `sorted()` determinism
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
