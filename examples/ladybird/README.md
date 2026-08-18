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
zero network access** (findings 30–33), and **so are the 8 Rust crates and the 4
cargo binaries** — 155 crates.io crates fetched from `Cargo.lock`,
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
once.** (Two inputs come from that build rather than from Bazel; a Ladybird
developer therefore stages nothing, and the gap is real only for a Bazel-only clone
— see [how a Ladybird developer gets them](#getting-the-two-remaining-inputs--with-and-without-cmake).
The third, the HSTS table, is now pinned and fetched by Bazel.) The
end-to-end test (clone into a new directory, drop in the overlay, nothing else)
was run for the first time and failed **six separate times**; `Build/full` was
the dependency I had removed, and **five of the six had nothing to do with it**.
All six are addressed below, and the result is verified on the clone rather than
asserted: `//:vcpkg_installed` builds all 76 ports offline, all six binaries
build (2,842 actions, RC=0), and `--headless=text`/`--headless=layout-tree`
are **byte-identical to the CMake reference on all three test pages**. Rows 1–2
are still manual staging (a prefetch, `Meta/ladybird.py vcpkg`); row 3 is now
**fixed** — Bazel fetches the HSTS table from a pinned commit; #4 and #5 were
bugs and are fixed; #6 now fails loudly instead of 20 minutes later:

| # | What is missing on a fresh clone | Why the dev machine hid it |
|---|---|---|
| 1 | **`Build/vcpkg`** — a git clone of microsoft/vcpkg at `vcpkg.json`'s `builtin-baseline`, which `//Build/vcpkg:tree` globs with `allow_empty = True`. Matches *one* file (its own `BUILD.bazel`) and the build fails with `/tmp/.../root/vcpkg: No such file or directory`. Ladybird's own `Meta/ladybird.py vcpkg` creates it; the recipe above never says so | `Meta/ladybird.py vcpkg` had been run months earlier |
| 2 | **`.git` inside that checkout** — vcpkg resolves versioned ports with `git read-tree`, so it is load-bearing (120 MB), and the filegroup *excludes* it. The action is `no-sandbox`, so it reads the real path and got it anyway: an **undeclared input the build needs** | `no-sandbox` + a real checkout on disk |
| 3 | **`Build/caches/HSTSPreload/transport_security_state_static.json`** (10 MB) — an *unversioned, unpinned* network download CMake does at configure time (`Meta/CMake/hsts_preload.cmake` fetches Chromium's `main`). `codegen_root.bzl` named it as a genrule `srcs`, so Bazel failed cleanly with `missing input file` — but nothing produced it. **Fixed:** pinned downstream to a Chromium commit + sha256 ([`hsts_preload.bzl`](workspace/hsts_preload.bzl)), fetched with `http_file`, and the generated table is byte-identical to CMake's — verified with the staged file *deleted* | the CMake configure had already downloaded it |
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

### Getting the two remaining inputs — with and without CMake

Two audiences, two answers. **A Ladybird developer stages nothing:** one ordinary
`./Meta/ladybird.py build` produces both as side effects, which is exactly why they
stayed invisible here for months.

| Input | Who produces it in a normal build |
|---|---|
| the vcpkg checkout + its `.git` | `Meta/Utils/build_vcpkg.py`, called by `ladybird.py` **`build`** as well as `vcpkg`: clone, checkout `builtin-baseline`, bootstrap the tool at the tag+SHA512 in `scripts/vcpkg-tool-metadata.txt`. ~70 s |
| the four `git archive` tarballs | **vcpkg itself**, while building skia and angle |
| ~~the HSTS preload table~~ | **Bazel**, now: `@hsts_preload_json//file`, pinned in `hsts_preload.bzl` |

**Without ever running CMake — the actual answer, and it needs no CMake at all:**

```sh
python3 Meta/ladybird.py vcpkg               # 1. the checkout + .git (~70s). No CMake:
                                             #    `vcpkg` is a standalone subcommand.
python3 Meta/fetch_vcpkg_git_archives.py     # 2. the four git archives (~80s), each
                                             #    reproduced with git clone + git archive
                                             #    and VERIFIED against the committed SHA512
                                             # (the HSTS table needs no step: Bazel
                                             #  fetches it from a pinned commit)
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

### Reproducing the tree on another machine

The overlay is **not a fork**: it is a pinned upstream Ladybird commit + two patches
+ 45 Bazel files that sit alongside CMake's. [`apply_overlay.sh`](apply_overlay.sh) is
that sentence made executable, because *"copy `workspace/` over a clone"* has four
ways to be silently wrong — and every one of them was found by running it, not by
reading it:

1. **The Ladybird commit.** The generated BUILD files name ~1,961 LibWeb compile
   inputs and 665 IDL bindings *by path*, and were generated from one tree.
   **Nothing in this repo recorded which one** until the script did
   (`71fb301a`). Against a different tree the build fails on a moved file — or
   worse, silently omits a new one.
2. **The patches**, which have to be applied or the browser leaks one socket fd per
   completed HTTP request until it dies of `EMFILE` overnight
   ([`0001`](patches/0001-librequests-tear-down-request-when-body-is-delivered.patch)
   and [`0002`](patches/0002-requests-release-response-fd-on-completion.patch);
   see [`docs/UPSTREAM-ladybird-fd-leaks.md`](../../docs/UPSTREAM-ladybird-fd-leaks.md)).
   There used to be four: the `PYTHONHASHSEED` determinism fix and the
   non-self-contained `UI/Qt/TabBar.h` are both **fixed upstream** at this pin, so
   the repin from `f9e34731` deleted them. A patch directory that only ever grows is
   a patch directory nobody re-checks against upstream.
3. **The rename**: `bazelrc.txt` → `.bazelrc`. Stored under a different name so a
   `cp -r` cannot be mistaken for a working build — and a rename a human does by
   hand is a rename a human forgets.
4. **The order**, which is the one I would never have predicted and which the
   `cp -r` recipe above gets wrong. `Build/vcpkg/BUILD.bazel` makes the *directory*
   `Build/vcpkg` exist, and upstream's `Meta/Utils/build_vcpkg.py` treats "the
   directory is there" as "the checkout is there": it skips the clone and runs
   `git -C Build/vcpkg rev-parse HEAD`, which — there being no `.git` inside —
   **walks up to Ladybird's own repo** and returns *Ladybird's* HEAD. It then tries
   to check vcpkg's baseline out of the Ladybird repo:
   `fatal: unable to read tree (40f3c709…)`. So that one file must be staged
   **after** the prefetch; the script defers it and says so.

`--verify` checks a tree without changing it, and checks the things a file copy
cannot: that HEAD is the pinned commit, that all 45 files are byte-identical, that
the patches are **applied** (`git apply --check -R` succeeding is the proof — a patch
that reverse-applies cleanly is already in the tree), and that the `.sh` files kept
their **executable bit**, which is tree state a careless copy drops and which then
fails deep inside a build action rather than at setup.

Verified end to end: `apply_overlay.sh /tmp/lbfresh2` on an empty directory produced
a tree byte-identical to the one that renders (`diff -rq`, excluding prefetch
outputs), `Meta/fetch_vcpkg_git_archives.py` reproduced **4/4** archives verified
against the pinned SHA512s, and `bazel build` then built the 76 vcpkg ports offline
and linked `//:LibHTTP`.

#### One thing the overlay cannot carry: host tools

```sh
sudo apt install nasm autoconf automake libtool autoconf-archive libltdl-dev
# ...and, since the 71fb301a repin, four host packages upstream newly REQUIRES:
sudo apt install qt6-positioning-dev qt6-base-private-dev libxkbcommon-dev libglib2.0-dev
```

The second line is what a repin costs, and every one of the four was found by a
*configure or generate failure*, one at a time, because nothing derives this set:
`UI/Qt/CMakeLists.txt` turned `Positioning` from `OPTIONAL_COMPONENTS` into
`REQUIRED`, made `GuiPrivate` required on Linux (not just Apple/DirectX), and added
`pkg_check_modules(GIO REQUIRED gio-2.0 gio-unix-2.0)` for the new
`ExternalURLActivationToken`/`ExternalURLHandler` sources. `qt6-base-private-dev` and
`libxkbcommon-dev` are *transitive*: `Qt6GuiPrivate` reports itself NOT FOUND until
XKB is present, then names an `INTERFACE_INCLUDE_DIRECTORIES` path
(`/usr/include/.../QtGui/6.10.2`) that only the private-dev package ships — a
two-step failure where neither message mentions the package to install. That is the
finding-39 shape again, one layer out: the preflight covers *vcpkg's* host tools, so
Ladybird's own `find_package`/`pkg_check_modules` requirements are unchecked and cost
one failed configure each.

Not a courtesy list — the set is derived from vcpkg's own scripts into
[`Meta/vcpkg_host_tools.tsv`](workspace/Meta/vcpkg_host_tools.tsv), and
`vcpkg_build.sh` checks all of it before building anything, printing the ports that
need each missing tool and this exact line. vcpkg has **no Linux download** for
these (its `nasm` URLs are inside `if(CMAKE_HOST_WIN32)`; `vcpkg-make` demands
autotools via `find_program` + `FATAL_ERROR`), so they cannot be pinned the way the
76 distfiles are — the gap is named rather than papered over. Before the check
existed, each one cost a full ~20-minute build to discover, and the error named the
wrong place: `nasm` surfaced from inside `libvpx`, autotools from `gperf` via a
helper port that neither mentions (finding 39).

`glslangValidator` is the same class and still **not** covered: two genrules name
`/usr/bin/glslangValidator`, and `vcpkg_installed` does not ship it — `apt install
glslang-tools` for now (see [known gaps](#known-gaps)).

### The HSTS table: pinned downstream, because upstream is not ours to fix

`Meta/CMake/hsts_preload.cmake` downloads Chromium's
`net/http/transport_security_state_static.json` from **`main`** at configure time.
The generator turns it into a ~95,000-entry `constexpr Array` of domains LibHTTP
forces to HTTPS, so *the day you configured* decides a security-relevant table.
The right fix is one line in that `.cmake` file; we do not control it, so the
overlay pins it **downstream** and the upstream fetch is a bug report:

- [`hsts_preload.bzl`](workspace/hsts_preload.bzl) — a module extension declaring
  one `http_file` at an immutable commit URL with a `sha256`. `MODULE.bazel` names
  it; `codegen_root.bzl`'s `gen_HSTSPreloadData` takes `@hsts_preload_json//file`
  as `srcs` instead of a path under `Build/caches`.
- [`Meta/pin_hsts_preload.py`](workspace/Meta/pin_hsts_preload.py) — regenerates
  that file: resolves the newest commit touching the path, downloads it, and writes
  **the hash it measured**. `--expect-same-as <file>` refuses to write unless the
  pinned bytes equal a file you already have, which is how this pin was shown not
  to move the generated table.

**Verified, on the fresh clone, with the CMake-downloaded file deleted** — so the
pinned fetch is the only possible source: `bazel build //:gen_HSTSPreloadData`
produces both outputs **byte-identical to the CMake reference**
(`HSTSPreloadData.cpp`, 4,873,678 bytes), and `bazel build //:LibHTTP` compiles and
links them (RC=0, 183 actions).

Two things make this pin honest rather than convenient, and both are measurements:

- **Pin a commit, not a release tag.** A tag is a pin to a *different table*: at
  `139.0.7258.5` the file is 18.7 MB and generates **168,593** entries against this
  commit's **94,626**. Pinning a tag would have traded a hermeticity gap for a
  parity gap. The commit `main` pointed at when the reference build configured
  serves bytes identical to what CMake downloaded — checked with `cmp`.
- **Unpinned would have been silently wrong.** `http_file` *does* work with no
  `sha256` on the `main` URL (it builds, and Bazel prints the integrity it would
  have used) — but then Bazel caches the first fetch forever: with a local origin,
  changing the file upstream and rebuilding returned the **old** content in 0.3 s
  with no warning. And the table moved during this work — `service.gov.scot` left
  the list, 94,627 → 94,626 entries — so an unpinned fetch would have broken
  byte-parity for a reason unrelated to the migration.

The cost of pinning downstream only: **CMake still tracks `main`**, so a configure
newer than the pin disagrees with Bazel. That is now one pinned input versus one
unpinned one — a visible, dated disagreement with a commit sha to look at — instead
of two unpinned fetches that happened to agree. And it is closeable without touching
upstream: `download_file` is a no-op when the file already exists (verified with
`ENABLE_NETWORK_DOWNLOADS=OFF`), so copying Bazel's fetched file into
`Build/caches/HSTSPreload/` before configuring makes CMake consume the same pin.

### Three shortcuts tested and rejected, all of which look like simplifications

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
| `Meta/vcpkg_tool_assets.tsv` | vcpkg's OWN host tools (cmake, ninja) — url + sha512 + the filename vcpkg looks for. **Separate from the asset capture on purpose:** `vcpkg_find_acquire_program` probes the host first, so a tool the capturing machine already had is never downloaded and never captured (that is how ninja went unpinned; finding 38). Regenerate with `emit_vcpkg_bazel.py --capture-tools` |
| `Meta/vcpkg_host_tools.tsv` | The tools that must come **from the host**, because vcpkg has no Linux download for them at all — `nasm` (6 ports) and the autotools set. The third class of input, and the one that cannot be pinned: `vcpkg_find_acquire_program(NASM)` has URLs only inside `if(CMAKE_HOST_WIN32)`, and `vcpkg-make` demands `autoconf`/`automake`/`libtool` via bare `find_program` + `FATAL_ERROR`. So this file does not close the gap, it **names** it — and `vcpkg_build.sh` checks the whole list before building anything, so a machine missing three tools is told all three in one second instead of one per 20-minute build (finding 39). Regenerate with `emit_vcpkg_bazel.py --host-tools` |
| `Meta/vcpkg_capture_assets.sh` | Records the 76-distfile pin, by *being* vcpkg's asset cache. The one run allowed to fetch |
| `Meta/fetch_vcpkg_git_archives.py` | Produces the 4 `vcpkg_from_git` tarballs **without CMake**: takes the list from the committed pin, resolves each clone URL out of the portfiles, then `git clone` + `git -c core.autocrlf=false archive <ref>` and **verifies against the pinned SHA512**. 4/4 byte-identical |
| `Meta/vcpkg_capture_git_archives.sh` | Regenerates that pin with `vcpkg install --only-downloads` (~6 min, no compilation, no CMake) — vcpkg as the instrument, since which git externals are used is decided by feature-conditional CMake code, not by portfile text |
| `hsts_preload.bzl` | Chromium's HSTS preload table as one `http_file`, pinned to a **commit** + sha256 — the downstream pin for the one input upstream CMake fetches from `main`. **Generated** by `Meta/pin_hsts_preload.py` |
| `Meta/pin_hsts_preload.py` | Re-pins it: resolves the newest commit touching the path, downloads it, writes the hash it **measured**; `--expect-same-as` refuses to write unless the pinned bytes equal the file CMake downloaded (parity guard) |
| `apply_overlay.sh` | Reproduces the whole tree on another machine: clone at the pinned Ladybird commit, apply the two patches, copy the 45 overlay files (with the `bazelrc.txt` → `.bazelrc` rename), run the vcpkg prefetch, then stage the one file that must come after it. `--verify` checks an existing tree — commit, bytes, patches-applied, exec bits — and changes nothing |
| `bazelrc.txt` | → `.bazelrc`. Global copts/defines/linkopts mirrored from `Meta/CMake/compile_options.cmake` |
| `qt_runtime.bzl` | Qt's **runtime** half: a repo rule that stages the plugins of the SDK `@qt` itself names (read out of @qt's generated `qtconf.bzl`, so plugins and libraries cannot come from different Qts), the private libraries an SDK bundles beside Qt (derived from DT_NEEDED), a generated `qt.conf` pointing the binary at them, and the Qt >= 6.9 floor `UI/Qt/CMakeLists.txt` declares. Without it Qt `dlopen`ed the HOST's plugin into Bazel's Qt — finding 40 |
| `BUILD.bazel` | Root package: 34 libraries, the 5 executables, Qt moc/rcc genrules. **Generated** by `Meta/emit_build_bazel.py` |
| `codegen_root.bzl` | Non-LibWeb generator genrules (IPC endpoints, LibJS Bytecode/Op, HSTS table, WebGL replayer, TIFF tag tables, the two SPIR-V shader headers, and the chained `generate_interpreter_layout` → `flapc` interpreter assembly). **Generated** by `Meta/emit_root_codegen_bazel.py` |
| `Libraries/LibWeb/BUILD.bazel`, `generated_srcs.bzl` | LibWeb (~1,961 compile inputs). **Generated** by `Meta/emit_libweb_bazel.py` |
| `Libraries/LibWeb/codegen.bzl` | LibWeb's 26 generator genrules + the bindings mega-genrule (663 `.idl` → 1,340 files). **Generated** by `Meta/emit_codegen_bazel.py` |
| `Meta/emit_*.py` | The emitters. They read CMake's `build.ninja` + the File API codemodel and write the four generated files above |
| `Meta/bazel_parity_harness.py` | Buckets every ninja `CUSTOM_COMMAND` as covered / excluded-with-reason / unhandled, re-runs the covered ones and byte-compares against CMake's tree. Non-zero unhandled is a failure |
| `Meta/BUILD.bazel` | `//Meta:generators` filegroup (the generator scripts, as genrule inputs) |
| `Meta/vcpkg/BUILD.bazel` | The 41 `vcpkg_lib` targets Ladybird's libraries depend on, backed by `//:vcpkg_installed`. Hand-written and stable (one target per port); this is the whole interface between Ladybird and its dependencies |
| `cargo_crates.bzl`, `cargo_index.bzl`, `cargo_extension.bzl` | One `http_archive` per crates.io crate (155), the name/version/sha256 index the vendor staging resolves through, and the module extension that creates the repos + the 3 pinned Rust toolchain components. **Generated** by `Meta/emit_cargo_bazel.py` **from `Cargo.lock` alone** — no cargo, no network, no CMake |
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
network zero times. **Two** inputs still have to be present first (step 1a; gaps
7–8), both obtainable **without CMake**. The third, the HSTS preload table, is now
fetched by Bazel: it is pinned downstream to a Chromium commit + sha256 in
[`hsts_preload.bzl`](workspace/hsts_preload.bzl):

**One command does the whole tree**, and it is the recommended path because the
manual version below has four ways to be silently wrong (see
[Reproducing the tree](#reproducing-the-tree-on-another-machine)):

```sh
./apply_overlay.sh ~/ladybird          # clone at the pinned commit, patch, overlay,
                                       # vcpkg prefetch -- in the order that works
./apply_overlay.sh --verify ~/ladybird # check an existing tree, change nothing
```

Or by hand:

```sh
git clone https://github.com/LadybirdBrowser/ladybird && cd ladybird
git checkout 71fb301a851e4a098e863a7a67e6666599e1cab7   # the commit the generated
                                                        # BUILD files describe
# 1. Drop in the overlay.
cp -r .../examples/ladybird/workspace/. . && mv bazelrc.txt .bazelrc
git apply .../examples/ladybird/patches/*.patch   # generator determinism, a Qt header
                                                 # that is not self-contained (gap 9),
                                                 # and the per-request fd leak
# 1a. THE TWO INPUTS THE OVERLAY DOES NOT CARRY -- both are produced by ONE
#     ordinary Ladybird build. A Ladybird developer stages nothing by hand;
#     these are blockers for a Bazel-ONLY clone, and each one is a thing CMake or
#     vcpkg produces as a side effect (see finding 36):
#       (a) Build/vcpkg + its .git   <- python3 Meta/ladybird.py vcpkg  (~70s)
#       (b) the four Meta/CMake/vcpkg/git-archives/*.tar.gz
#                                    <- vcpkg writes them into
#                                       Build/vcpkg/downloads/ while building
#                                       skia and angle. Verified: those files'
#                                       SHA512s equal the committed ones in
#                                       vcpkg_git_archives.bzl, i.e. the
#                                       "hand-made" directory was a copy of
#                                       vcpkg's own cache.
#     WITHOUT CMAKE (the supported path for a Bazel-only clone):
python3 Meta/ladybird.py vcpkg            # (a) checkout + .git, ~70s, no CMake
python3 Meta/fetch_vcpkg_git_archives.py  # (b) 4/4 reproduced with git archive and
                                          #     verified against the pinned SHA512s
#     The HSTS preload table needs NOTHING now: Bazel fetches it as
#     @hsts_preload_json//file, pinned to a Chromium commit + sha256 in
#     hsts_preload.bzl (below). Ditto `ply`: the wheel is pinned and pip runs
#     with PIP_NO_INDEX (gap 8).
#     WITH CMake, if you were building Ladybird anyway, (a) and (b) fall out of:
#         ./Meta/ladybird.py build
#         cp Build/vcpkg/downloads/{angle,libyuv,skia}-*.tar.gz Meta/CMake/vcpkg/git-archives/
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

Every path below is **derived, not written down**, because two of them used to be
stated as literal `k8-fastbuild` paths and one was simply wrong: the vcpkg tree is
built in the **exec** configuration (`vcpkg_lib` pins `cfg = "exec"`), so it is
under `k8-fastbuild-exec` and `bazel-bin/vcpkg_installed` does not exist. The two
staging roots are also NOT the same directory, which is easy to get backwards:
helper binaries go in `<bindir>/libexec`, but the resource root is
`<bindir>/../share/Lagom` -- `LibWebView/Utilities.cpp`'s `find_prefix()` takes the
PARENT of the binary's directory and appends `share/Lagom`. Ask the build for all
three rather than spelling any of them out:

```sh
export XDG_RUNTIME_DIR=/tmp/xdg-lb && mkdir -p $XDG_RUNTIME_DIR && chmod 700 $XDG_RUNTIME_DIR
BIN=$(bazel info bazel-bin)                       # target config: where ladybird is
mkdir -p "$BIN/libexec"
for b in WebContent RequestServer ImageDecoder Compositor WebWorker; do cp -f bazel-bin/$b "$BIN/libexec/"; done
# cranelift-compiler is found as a sibling of the spawning binary (Ladybird's own
# lookup chain), so it needs no path baked in -- only to be there.
cp -f bazel-bin/cranelift-compiler "$BIN/libexec/"
# The resource root, assembled from the CLONE and from Bazel's own vcpkg tree.
# This line used to read `ln -sfn "$PWD/Build/full/share/Lagom" ...` -- i.e. the
# recipe for running the Bazel build pointed at CMake's build tree, a seventh
# thing a fresh clone does not have (finding 36). Everything in it is either in
# Base/res or in //:vcpkg_installed; `chmod u+w` because Bazel's outputs are
# read-only and `cp -r` preserves that.
#
# $(dirname $BIN), not $BIN: find_prefix() resolves the resource root against the
# PARENT of the directory holding the binary.
L="$(dirname "$BIN")/share/Lagom"
chmod -R u+w "$L" 2>/dev/null; rm -rf "$L"; mkdir -p "$L/ladybird/pdfjs/web"
cp -r Base/res/. "$L/"
# ASK for the tree rather than guessing its configuration directory.
V=$(bazel cquery 'deps(//:ladybird)' --output=files 2>/dev/null | grep 'vcpkg_installed$' | head -1)
P="$V/x64-linux-dynamic/share/pdfjs"
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
   `Cargo.lock` already pins a sha256 for all 155 crates.io crates and the URL is
   a pure function of (name, version), so — unlike vcpkg — **nothing had to be
   captured**: `Meta/emit_cargo_bazel.py` emits the fetch rules from
   `Cargo.lock` alone, with no cargo, no network and no CMake, and the three
   pinned 1.96.1 toolchain components are `http_archive`s merged into one sysroot
   (no rustup, which fetches at run time). Each crate is then one sandboxed
   `block-network` action running `cargo --offline --locked` against a vendor dir
   assembled from Bazel's own fetched crates — checkable rather than merely
   claimed: deleting one crate from the vendor dir fails the action with "no
   matching package named `yuv` found" instead of downloading it.

   The link/symbol measurements in the rest of this finding were taken at the
   **previous pin** (`f9e34731`), where the tree had 10 staticlib crates and 14
   FFI headers; at `71fb301a` upstream consolidated `libweb_css_rust` and
   `libweb_layout_rust` back into `libweb_rust`, so it is 8 crates and 19
   generated files. The counts below are left as measured rather than rescaled —
   a number nobody re-measured is not a measurement — and the parity harness,
   the emitter's `--report` and `tests/test_emit_cargo.py` carry the current ones.

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
   CMake's. Finding 36. **Two of its paths were also wrong, and are now derived
   rather than written down** (finding 40): the vcpkg tree is built in the *exec*
   configuration so `bazel-bin/vcpkg_installed` does not exist, and the resource
   root is `<bindir>/../share/Lagom` -- `find_prefix()` takes the PARENT of the
   binary's directory -- not `<bindir>/share/Lagom`. Both now come from
   `bazel info` / `bazel cquery --output=files`. Qt's plugins USED to belong on
   this list and no longer do: they are `data` of `//:ladybird` (`qt_runtime.bzl`),
   which is what that "real Bazel setup" looks like for one of the three things.
6. **`rules_qt` is not on the BCR.** `MODULE.bazel` uses an `archive_override`
   pointing at kklochkov/rules_qt v2.0.1's release tarball (stock upstream, no
   patches). The BCR's `rules_qt` module is Vertexwahn's unrelated `rules_qt6`.
   Qt itself *is* host-portable: `qt.local_repo` discovers the host Qt via
   `qmake -query`, so no Qt SDK is vendored. Its **plugins** are host-portable too
   now, and that took a fix rather than an observation: rules_qt wires up Qt's link
   half only, so the binary linked @qt's libraries and then `dlopen`ed the HOST's
   QPA plugin into them -- a SIGSEGV where the two Qt versions differ, and a silent
   pass where they agree. `qt_runtime.bzl` stages the plugins of the SDK @qt itself
   names, points a generated `qt.conf` at them, and enforces the Qt >= 6.9 floor
   `UI/Qt/CMakeLists.txt` declares. Finding 40.

   The same crash then came back from the *other* direction, and it was not Qt's
   doing: CMake puts `-fPIE` on every executable target, the generator copied it
   into each `cc_binary`'s `copts`, and Bazel appends those AFTER the global
   `--copt=-fPIC` — so the last flag won and the UI objects compiled `-fPIE`. That
   lets GCC reference extern data PC-relative, the linker emits an
   `R_X86_64_COPY`, and against a Qt built with `reduce_relocations` (every
   official/aqt SDK; Debian's is built without it) `QCoreApplication::self` ends up
   defined in the executable while `libQt6Core` writes its own copy: `qApp` is set
   in one place and read, still null, in another. Qt's headers `#error` on exactly
   this, but only when `__PIC__` is unset — Bazel passed both flags, so the check
   never fired. The emitter now drops `-fPIE` (`DROPPED_TARGET_FLAGS`): 39
   `R_X86_64_COPY` relocations across the six executables, now 0. Finding 41.
7. **The fresh clone needs two inputs the overlay does not carry** (step 1a
   above), and each stayed invisible for the same reason: it was already on the
   machine. `Build/vcpkg` — a microsoft/vcpkg checkout at `vcpkg.json`'s
   `builtin-baseline`, created by Ladybird's own `Meta/ladybird.py vcpkg` — is
   globbed by `//Build/vcpkg:tree` with `allow_empty = True`, so on a clone it
   matches one file and the failure names a missing `/tmp/.../root/vcpkg`. Its
   `.git` is *load-bearing* (vcpkg resolves versioned ports with `git read-tree`)
   yet the filegroup **excludes** it — an undeclared input the build reads anyway,
   because the action is `no-sandbox`. The four `vcpkg_from_git` tarballs are the
   other, reproduced by `Meta/fetch_vcpkg_git_archives.py`. Both are prefetches,
   not rules: a `git_repository` strips `.git`, and `git archive` output has no URL
   to `http_file`.

   **The third one — the HSTS table — is now closed.** It was the interesting case,
   because the unpinned fetch is *upstream's* (`hsts_preload.cmake` tracks Chromium's
   `main`) and we cannot change that. So it is pinned **downstream**: an `http_file`
   at an immutable commit + `sha256` in `hsts_preload.bzl`, regenerated by
   `Meta/pin_hsts_preload.py`, verified byte-identical to CMake's generated table
   with the staged file deleted, and the upstream unpinned fetch filed as a bug. The
   generalizable shape: **a converter cannot pin an input on the foreign build
   system's behalf, but it can pin it for itself — provided it pins the revision the
   foreign system is currently serving, and proves that with a byte comparison
   rather than a hash it invented.** Pin the wrong revision (a release tag, say) and
   the hermeticity gap becomes a parity gap.

   **And a fourth class, which no pin can close: host tools.** vcpkg has no Linux
   download for `nasm` or the autotools set — its `nasm` URLs live inside
   `if(CMAKE_HOST_WIN32)`, and `vcpkg-make` demands `autoconf`/`automake`/`libtool`
   with a bare `find_program` and a `FATAL_ERROR`. So they are *named* instead:
   `Meta/vcpkg_host_tools.tsv`, derived from vcpkg's own scripts, checked by
   `vcpkg_build.sh` before anything builds (finding 39). Every one of these was
   invisible for exactly the reason in this gap's first sentence — **it was already
   on the machine** — and each cost a full ~20-minute build to find, in an error
   naming the wrong place. `glslangValidator` is the same class and is **not** yet
   named: two genrules in `codegen_root.bzl` hardcode `/usr/bin/glslangValidator`
   and `vcpkg_installed` does not ship it, so it needs a pinned binary or a repo
   rule that fails legibly. Fixing *how you find out* is not the same as fixing the
   dependency, and this gap is only the former.

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

9. **A repin's first failure mode is a *loading-time* one, and no per-target check
   can catch it.** Ulf's build at `71fb301a` did not fail to compile anything; it
   failed to load:

   ```
   Error in glob: glob pattern 'Libraries/LibJS/BytecodeDef/**' didn't match
   anything, but allow_empty is set to False
   ```

   Upstream `a32d9c9f` ("LibJS: Derive bytecodes from Flap handlers") deleted that
   directory, and the pattern was a hardcoded string in a list literal in
   `Meta/emit_cargo_bazel.py`. Three properties compound here:

   * `allow_empty = False` is **correct** and had to stay. Its opposite is the
     reason the old `Build/full` shim packages could match nothing for weeks and
     fail 1,600 actions later — a file list that *may* be empty proves nothing.
   * A loading error has **no target to blame**, so nothing in the build graph can
     report it and no test that builds or queries a target can reach it.
   * The emitters that would have re-derived the pattern all ran green, because an
     emitter that enumerates *kinds* of thing cannot notice a kind going away:
     the same repin silently dropped `gen_Op` from `codegen_root.bzl`, and the
     parity harness reported **`0 UNHANDLED`** while two generated headers had no
     owner at all — its first-match-wins classifier bucketed the new Rust
     generator as "resource staging", because that command's ninja rule is
     `<tool> … && cmake -E copy_if_different …` and the *exclusion* pattern
     matched first. Every exclusion in a first-match classifier can silently
     capture a command it was not written for, and the count that should have
     caught it is computed after the capture.

   The fix is structural rather than a re-pin of the string: every crate directory
   an `allow_empty = False` glob names is now **derived** — `Cargo.toml`'s
   `members` closed over the manifests' `path =` dependencies (which is also how
   the derivation reaches *outside* the workspace, since `libjs_rust`
   build-depends on the `exclude`d `flapc`) — and `flapc`'s own extra inputs come
   from scanning it for `include_str!`. So a deleted crate deletes its own glob
   pattern. `tests/test_emit_cargo.py` guards it from both ends: the derivation
   drops a pattern when the directory is removed, and **no module-level constant
   in either emitter may hold a `<dir>/**` string** — the shape the bug took.

   The same consolidation (`libweb_css_rust` + `libweb_layout_rust` →
   `libweb_rust`) also moved two things worth naming: two of the crate's 19
   generated files are **`.inc`, not `.h`**, and Bazel deletes an undeclared
   `.inc` exactly as it deletes an undeclared header — so the suffix must not be
   what decides whether a generated file is declared. And `build_rust_binary()`
   takes `FEATURES` too (upstream's new `style-replay` is built
   `FEATURES style-recording` from the *same crate* as `libweb_rust`'s staticlib),
   which the parser only read on the `import_rust_crate` side: it would have built
   a different binary than CMake does and said nothing.

10. **Two upstreamable Ladybird fixes — down from four, and that is the point of a
   repin.** Two of the four are **fixed upstream** at this pin (`71fb301a`) and their
   patches are deleted, which is worth stating because a patch directory is a debt
   register that only shrinks if somebody re-reads it:

   * The `sorted()` determinism fix in `Meta/Generators/libweb_bindings/to_idl_value.py`
     — filed as [ladybird#10899](https://github.com/LadybirdBrowser/ladybird/issues/10899)
     and fixed upstream **better than my patch was**: upstream sorts inside
     `dependency_names_for`, so no caller can receive a set, whereas my patch sorted at
     the one call site I had found. Two of my four patches were the *same* bug in that
     one function, and I filed the second as distinct; it was not.
   * The one-line `#include <UI/Qt/Tab.h>` in `UI/Qt/TabBar.h`. `TabBar.h` calls
     `as<Tab>()` — a `dynamic_cast`, needing Tab's complete type — while only
     forward-declaring `Tab`, and compiled under CMake purely by ordering luck:
     AUTOMOC's unity `mocs_compilation.cpp` includes `moc_Tab.cpp` (hence `Tab.h`)
     before `moc_TabBar.cpp`. Bazel mocs each header separately, so nothing supplied
     the definition first. Upstream now includes the header.

   Both survived in `patches/` only because the old pin (`f9e34731`) predated the
   upstream fixes — a patch keeps applying long after it stops being needed, so
   "it still applies" is not evidence that it is still a bug.

   What remains is the fd leak, in two patches that are one series. The first,
   found only because the Bazel-built browser was left running overnight, is a
   resource leak rather than a build defect:
   [`patches/0001-librequests-tear-down-request-when-body-is-delivered.patch`](patches/0001-librequests-tear-down-request-when-body-is-delivered.patch).
   WebContent leaks one AF_UNIX socket fd per *completed* HTTP request — a cycle
   between the GC heap (four `GC::Root`s from `Fetching.cpp:2338`) and the
   refcount heap (`Response` holding `Requests::Request` by `RefPtr`) that neither
   collector can break — until `EMFILE` aborts three processes at once. The patch
   is **necessary but not sufficient**: it fixes the completed-request class
   (identifiable in `ss -np` by a dead peer, `* 0`), while a stalled-body class
   with a live peer survives it. Both are written up with reproductions in
   [`docs/UPSTREAM-ladybird-fd-leaks.md`](../../docs/UPSTREAM-ladybird-fd-leaks.md).

   The **second** is the other half of the completed-request class, and it only showed
   up on a tree that was not mine:
   [`patches/0002-requests-release-response-fd-on-completion.patch`](patches/0002-requests-release-response-fd-on-completion.patch).
   With `0001`'s teardown applied, Ulf still measured **97 leaked sockets/min**, all
   `peer=DEAD` and all sent by RequestServer — because dropping the callbacks unpins
   the GC cycle but does not *close the descriptor*: the response pipe is released
   only by `~Request`, so any other reference to the request retains one fd per
   completed request. `0002` closes it where the body is already proven complete — the
   next patch in the series, applied on top of `0001`; `patches/*.patch` is applied by
   glob in full, so an *alternative* to a patch must never live there.
   Measured A/B on 200 completed requests: **208 sockets (203 dead) → 6 (0 dead)**,
   with body delivery intact. "Collectable" and "closed" are different claims, and
   for an fd received over IPC only the second one is the bug.

   Diagnose a running browser with [`fd_census.py`](fd_census.py), which needs no
   patch and no particular tree — `python3 fd_census.py --all --watch 30` ranks every
   browser process by fd *growth*, so the data names the leaking process instead of
   your hypothesis naming it. Each report also states **which fd fixes the running
   binary contains** (`--build` for that alone), read out of the process's own mapped
   ELF symbols: a leak rate is uninterpretable without it, since "still leaking at
   92/min" means "the fix does not work" or "the fix was not in this build" and those
   need opposite next steps. It reports *"cannot tell"* rather than "fix absent"
   whenever absence could be explained by inlining: under LTO in a **static** build a
   small internal-only method is inlined into its only caller and leaves no symbol and
   no string behind, so `.debug_str` is read too and a "missing" verdict requires a
   symbol inlining cannot erase to be visible. This is a correction — the first version
   told Ulf `0002` was absent from a binary that contained it, and the negative control
   did not catch it because it was vulnerable to the same optimisation. A control only
   rules out "unreadable" if it cannot vanish for the same reason as what it guards.
   Since upstream has landed its own fix,
   `--verify` also accepts an equivalent fix in place of our exact bytes: a patch we
   carry only until upstream fixes it has an `.effect-grep` beside it, and verify
   falls back to asking whether the *effect* is present before reporting the patch
   missing.
