# Case study / plan: migrating Ladybird to Bazel

Working migration of the [Ladybird](https://ladybird.org) browser engine
(CMake + vcpkg, C++23) to Bazel, driven by the any2bazel parity loop. This doc
is the durable plan and the running record of findings. The workspace overlay
it produced — the emitters (`Meta/emit_*.py`), the parity harness, and the
generated BUILD files — is in [`examples/ladybird/`](../examples/ladybird/),
along with an honest inventory of what still stops it from being a
clone-and-build.

Bazel now builds the browser with **nothing** read out of CMake's build tree
(`Build/full`) — 0 targets in the closure of all six binaries, down from 741. That
was claimed prematurely and was false for most of this migration;
[finding 35](#finding-35-the-claim-was-false-and-a-glob-is-why-nobody-noticed) is
the autopsy, and it is the finding to read first if you are migrating a project of
your own — the bug was in the *shape of the verification*, not in the Bazel rules.

**`git clone && bazel build` on a fresh clone still fails, though, and finding 36
is the second autopsy.** Removing `Build/full` was the goal, so `Build/full` is
what I verified; the clone needs four *other* things nobody had asked for — a
vcpkg checkout (globbed with `allow_empty = True`, the same pattern one tree over),
its `.git`, an unpinned HSTS table, and `pip install ply` inside a vcpkg port that
`x-block-origin` never sees. **The only check that finds all of them is doing the
clone**, which is finding 36's one-line summary and took two goes to learn.

## Why Ladybird

A real, recognizable, from-scratch browser engine — a strong headline for the
tool. It is also a hard target that exercises the two biggest engine gaps at
once (build-time codegen, and external-dependency resolution), so it doubles as
a forcing function for new engine capability.

## The reference build (Ring 0 — DONE)

- Full-GUI configure (`ENABLE_GUI_TARGETS=ON`, Qt6 6.10.2 from apt, vcpkg deps
  mostly from the upstream binary cache): `Build/full`.
  - **Headless (`ENABLE_GUI_TARGETS=OFF`) is a trap:** on Linux it drops the
    entire web engine (LibWeb/LibGfx/LibMedia/LibWebView + Services + UI), not
    just Qt. You must build with GUI on to migrate the browser.
- File API codemodel → `model.cmake.full.json`: **416 targets** (298
  production, 30 dashboard, 88 codegen); 34 libraries, LibWeb dominating with
  ~1,961 compile inputs; **2,991 production compile inputs, 696 generated.**
- Reference build compiles (3,406 steps, ~32 min on 16 cores) and the
  `Ladybird` binary **runs headless and renders** HTML+CSS+JS
  (`--headless=text` on a test page executed `2+2` via LibJS). This is the
  acceptance anchor: Bazel must reproduce a running browser.

### The codegen surface (the crux)

696 generated inputs, and **665 of them are LibWeb IDL bindings** — one C++
file per Web API interface, produced by `invoke_py_idl_generator` calling
`Meta/Generators/libweb_bindings`. The rest: CSS property/enum/keyword tables
(~16, via `generate_libweb_css_*.py`), WebGL, HSTS, LibJS bytecode defs. It is
a small number of generator *families* driven by JSON/IDL inputs — not 700 ad
hoc rules. Strategy: run the **same** Python generators from Bazel genrules and
prove byte-identity against `Build/full`'s materialized output (the technique
validated on the vscode `.js` emit check).

## Vertical slice: AK (DONE — proves the loop)

AK is the foundation library (38 sources, external deps fmt/simdutf/mimalloc/
cpptrace, two configure-generated headers). Standing it up end-to-end proved the
whole loop works on real Ladybird and surfaced the reusable scaffolding:

- **`MODULE.bazel`**: bzlmod, `rules_cc` 0.2.19, `platforms` 1.0.0. (Declared
  0.2.17 for most of this migration, which was inert: MVS resolved 0.2.19 via
  `bazel_tools` on Bazel 9.2.0. Caught only on adding
  `common --check_direct_dependencies=error`, which is now in the `.bazelrc` so
  the declared and resolved versions cannot drift apart again — see
  [BAZEL-RULES](BAZEL-RULES.md).)
- **vcpkg shim**: a `BUILD.bazel` dropped into
  `Build/full/vcpkg_installed/x64-linux-dynamic/` — one `cc_library(:headers)`
  over the whole `include/` tree + `cc_import` per prebuilt `.so`. Consumes
  vcpkg output as prebuilt; no dep build-system migration (that's Ring 2).
- **Global flags belong in `.bazelrc`**, not per-target copts: Ladybird's
  `Meta/CMake/compile_options.cmake` applies one warning/feature set to every
  target, so mirror it as global `--cxxopt`s. Same for the repo-root include
  roots (`-I.`, `-ILibraries`, `-IServices`, `-IBuild/full`…) and `-DNDEBUG`
  (RelWithDebInfo).
- **Explicit source lists beat globs**: `glob(["AK/*.cpp"])` wrongly pulled in
  `DemangleWindows.cpp` (platform-gated off on Linux). Drive `srcs` from the
  model's per-target input list.
- **`includes=["."]` is rejected by Bazel** ("resolves to workspace root"). Use
  `--cxxopt=-I.` globally instead; use `includes=` only for subdirs (e.g. the
  generated-header copy root `genroot`).

Result: **118 → 4 discrepancies.** The 118 were fully systematic (every diff
×38 sources = one cause): missing `-DNDEBUG`, missing include roots, and the
global warning set — all cleared in bulk by the `.bazelrc` + config.

### Findings for the engine (real tool output, not fixture-driven) — FIXED

Both were fixed in any2bazel (this branch), with regression tests, and
re-validated against the real AK diff.

1. **Bazel-default flag strip was asymmetric and over-eager (FIXED).**
   `canonicalize.py` stripped `-fstack-protector` / `-fdiagnostics-color` on the
   Bazel side only, by prefix. Ladybird explicitly sets
   `-fstack-protector-strong` / `-fdiagnostics-color=always` on both sides, so
   (a) the prefix also ate the project's `-strong` variant and (b) stripping
   only Bazel meant the shared flag no longer cancelled, fabricating a false
   cmake-only `flags_diff` on every one of AK's 38 sources. Fix: split the
   defaults into `BAZEL_NOISE_FLAG_PREFIXES` (pure noise, still stripped) and
   `BAZEL_TOLERATED_FLAG_PREFIXES` (kept through canonicalization so shared
   flags cancel; a CMake-only stronger variant still correctly errors; the
   tolerated default is filtered only from the cosmetic `bazel_only` display in
   `diff.py`). Re-validated: AK's `flags_diff` errors vanished WITHOUT any
   `ignore.flags` workaround. Tests: `test_shared_tolerated_default_flag_*`,
   `test_cmake_stronger_variant_than_bazel_default_is_caught`,
   `test_bazel_only_tolerated_default_filtered_from_display`.

2. **Link-input libraries were invisible to dep inference (FIXED, with a
   caveat).** The Bazel extractor recorded only link-action `arguments` +
   `outputs`, so dep inference saw only `-l`/archive tokens on argv. But Bazel
   feeds many link deps to the linker as depset INPUTS by path (statically
   linked `.a`, external solibs), never as `-l` flags. Fix: the extractor now
   records a link action's library inputs (`.a`/`.so`/versioned-solib) from its
   input depset, and `reconstruct.py` infers deps from them (new
   `_lib_identity`, handling `libfmt.so.12.2.0` -> `fmt`). Tests:
   `test_bazel_link_input_libs_inferred_as_deps`,
   `test_bazel_link_input_static_archive_inferred_as_dep`.
   **Caveat (by design, not a bug):** for an intermediate SHARED library like
   AK, Bazel puts no deps on its link line or in its link depset at all — it
   genuinely defers dynamic linking to the final binary. So AK-in-isolation
   still shows 4 residual `missing_dep` (fmt/simdutf/mimalloc/cpptrace); those
   are unobservable at that granularity and resolve at the project-wide external
   closure when the final `Ladybird`/`WebContent` binaries link. The fix makes
   the static-link and final-binary cases correct; the intermediate-solib case
   is inherently a project-wide-only check.

## Ring 1b — codegen byte-parity (DONE: all 46 generators)

**Result: 1379/1379 LibWeb generated files byte-identical to the CMake build,
produced by Bazel genrules invoking the same Python generators.**

**How Ladybird codegen actually works (the good news).** The entire generated
surface is *Python scripts* — there are **no compiled host-tool generators**
(no IPC-compiler binary, no bindings-compiler binary; even IPC endpoints come
from `Meta/Generators/generate_ipc_definitions.py`). Extracting every
`CUSTOM_COMMAND` from `Build/full/build.ninja` that touches `Meta/Generators/`
yields **46 generator commands** across the tree, plus one mega-command:
`generate_libweb_bindings.py -o Bindings <661 IDL files>` produces **1331
files** in a single invocation (not 665 per-file runs, as the "665 bindings"
framing suggested). Each generator command is a clean
`python3 <script> -h out.h -c out.cpp -j in.json` shape, trivially a genrule.

**Tooling built (committed to any2bazel):**
- `Meta/bazel_parity_harness.py` — re-runs every generator command from
  `build.ninja` into a scratch mirror and byte-diffs each output against
  `Build/full`. Proves the generators are reproducible before we wrap them.
  Sweeps `--seed` and enumerates directory-output generators, so the bindings
  mega-command is actually covered: 1,402 files compared, identical at every seed.
- `Meta/emit_codegen_bazel.py` — parses `build.ninja`, rewrites each generator
  command as a Bazel `genrule` (absolute source paths → package-relative
  `$(location …)`; CMake `*.tmp` outputs → genrule `outs`; quoted args via
  `shlex`), and emits the bindings mega-rule. Output: `Libraries/LibWeb/
  codegen.bzl` (27 genrules) + `Meta/BUILD.bazel` (`//Meta:generators`
  filegroup staging the whole `Meta/Generators` + `Meta/Utils` Python tree,
  since generators do `sys.path.append` then `import Generators.*` / `Utils.*`).

**Two parity findings surfaced by Bazel sandboxing (the payoff of hermetic
execution — one an extractor bug it caught, one a latent upstream
reproducibility bug it caught and we fixed at the source):**

1. **Off-command-line input dropped by the extractor (our bug, not CMake's).**
   `generate_dom_tree.py` reads `HTML/MediaControls.css` by following a `<link>`
   out of its input `MediaControls.html`, so the `.css` never appears on the
   generator's command line. Our first `emit_codegen_bazel.py` built `srcs` by
   scraping the command line, dropped it, and the sandbox failed with
   `FileNotFoundError`. Upstream CMake gets this right (it's in the generator's
   `DEPENDS`; `touch` the `.css` and ninja re-runs) — nothing to patch there.

   The invariant: *the build graph's declared dependency list is authoritative,
   the command line is not.* `srcs` = union(command line, edge `DEPENDS`), since a
   tool may read inputs it was never handed. Matters for 3 of 46 generator edges
   here. Scraping command lines is the tempting shortcut for a CMake→Bazel
   extractor and this is where it breaks.

2. **Latent nondeterminism in the generator.**
   `generate_libweb_bindings.py` emitted `MediaCapabilities.h`'s dictionary
   structs in an order that varied with `PYTHONHASHSEED`, because
   `dictionaries_in_dependency_order()` iterated a *set* of dependency names.
   Filed as
   [ladybird#10899](https://github.com/LadybirdBrowser/ladybird/issues/10899)
   and **fixed upstream** in `1df71518` — by sorting inside
   `dependency_names_for()` (return type becomes `List[str]`), which is a better
   place than my local patch's call site: the function can no longer hand a set
   to *any* caller. My patch is dropped in favour of theirs.

   I first pinned `PYTHONHASHSEED=0` on the genrules instead. That's the wrong
   fix — it freezes the symptom into the build system and leaves every other
   consumer emitting seed-dependent output. The generator should sort. With the
   upstream fix in, I verified the pin is no longer load-bearing (removed it from
   all 46 genrules: build green, and Bazel's 1,332 binding files still
   byte-identical to CMake's), then put it back purely as defence-in-depth.

   **The harness lesson, twice over.** First: it ran each generator once under an
   inherited seed, so "identical" was never evidence of reproducibility — a
   seed-dependent generator passes such a check most of the time. Determinism
   checks that don't vary the nondeterminism source prove nothing, so
   `bazel_parity_harness.py` took a `--seed N`. Second, and worse: when upstream's
   fix landed I re-ran the seed sweep to confirm it, and got 71/71 identical at
   eight seeds *with the fix reverted*. The sweep was checking the wrong files.
   `outputs_of()` scraped destinations out of CMake's `copy_if_different <tmp>
   <dest>`, but the bindings mega-command writes a whole output *directory*
   (`-o Bindings`) with no such copy — so it contributed **zero** comparisons and
   the run still reported success. The one generator with a known determinism bug
   was the one generator the determinism harness never looked at. Fixed by
   enumerating output directories when no `copy_if_different` is found: coverage
   goes 71 → 1,402 files, it reproduces the `MediaCapabilities.h` diff on the
   unfixed generator, and passes at every seed on the fixed one. A green check
   whose *coverage* you haven't verified is indistinguishable from no check;
   "71 files" should have looked absurd next to "1,379 generated files" long
   before upstream forced me to look.

### Ring 1b tail — the other 19 generators (DONE)

The tree has 46 Python-generator commands; the 27 above are LibWeb's. The other
19 all land in the **root** Bazel package (16 IPC endpoints under
`Services/*` + `LibWebView`/`LibRequests`/`LibImageDecoderClient`, LibJS's
`Bytecode/Op`, LibHTTP's HSTS table, the Compositor WebGL replayer), emitted by
`Meta/emit_root_codegen_bazel.py` into `codegen_root.bzl`. **23 output files,
23/23 byte-identical to CMake.** All 46 generators are now Bazel-run.

Three things this surfaced that the LibWeb emitter didn't have to handle:

- **CMake's `cd` has nothing to do with where output lands.**
  `WebContentClientEndpoint.h` is generated with
  `cd Build/full/Libraries/LibWebView` but written to `Services/WebContent/`.
  Output paths in `build.ninja` are relative to that `cd`, so they must be
  resolved against it and then rebased onto the package — not read literally.
- **Subpackage sources need labels.** The WebGL replayer reads
  `Libraries/LibWeb/WebGL/GLFunctions.json`, which is in a *different* Bazel
  package, so the root package must reference it as
  `//Libraries/LibWeb:WebGL/GLFunctions.json`. Bazel rejected the flat path
  outright, which is the sort of thing a path-scraping emitter gets wrong
  silently under CMake.
- **Generating the files isn't enough — the include roots have to move too.**
  Consumers include them as `<WebContent/WebContentClientEndpoint.h>` (CMake's
  `-IServices`), so without genfiles header roots the compile keeps silently
  resolving to CMake's copy under `Build/full` and the genrules are dead weight.
  The emitter now also emits `generated_{libraries,services}_headers`
  `cc_library`s (`includes = ["Libraries"|"Services"]`), listed *before* the
  `Build/full` roots.

**Verified by removal, not by inspection:** with all 23 CMake-generated files
moved out of `Build/full`, all six binaries still build and the browser still
renders `--headless=text` and `--headless=layout-tree` byte-identically to the
reference. That is the test that distinguishes "Bazel generates this" from
"Bazel happens to find CMake's copy."

Of the 739 headers the `Build/full` globs supply, **708 are now also produced by
Bazel**; the remaining 31 are Rust FFI headers and CMake's
`generate_export_header` output (Ring 2 / rules_rust territory).

## Ring 1c — per-library BUILD generation (in progress, pattern proven)

The 42 production libraries form a clean **13-layer dependency DAG** (L0 `AK`
→ … → L9 `LibWeb` → L11 `LibWebView` → L12 `webcontentservice`). Ring 1c walks
it bottom-up, generating a `cc_library` per lib from the reference model and
diffing to compile+link parity.

**Emitter (`Meta/emit_build_bazel.py`, in the Ladybird tree).** Reads
`model.cmake.full.json` and emits one `cc_library` per production lib: `srcs`
from the compile actions, `local_defines` for the target's private
`<Name>_EXPORTS`, a per-target gendir `-I` copt, and `deps` wired by class —
internal → `//:Target`, vcpkg externals → the shim package, prebuilt Rust
crates → a cargo-`.a` shim. Global copts/defines live in `.bazelrc`.

**Proven across the representative cases** (built + diffed to zero
compile-parity discrepancies):
- **Plain lib** — `LibDiff` (AK-only dep).
- **vcpkg-dep lib** — `LibCrypto` (needs `crypto`, `tommath` `.so` shims).
- **Rust + generated-header lib** — `LibUnicode`: depends on the prebuilt
  `liblibunicode_rust.a` **and** a cargo-generated `LibUnicode/RustFFI.h`.

**Findings this surfaced:**

3. **`cc_library.defines` leak (real correctness fix).** AK's private defines
   (`AK_EXPORTS`, `AK_HAS_CPPTRACE=1`, `FMT_SHARED`) were emitted as `defines`,
   which **propagate to every consumer** in Bazel — so LibCrypto/LibDiff TUs
   got them, which CMake keeps `PRIVATE` to AK. The diff caught it as
   `defines_diff` (bazel_only) on every downstream TU. Fix: per-target
   `<Name>_EXPORTS`-style defines are `local_defines` (non-propagating); the 7
   genuinely-global defines (`USE_VULKAN=1`, `_FORTIFY_SOURCE=3`, …) moved to
   `.bazelrc`. Cleared all `defines_diff`.

4. **Generated/prebuilt headers need a Bazel target, not just `-I`.** CMake's
   global `-IBuild/full/Libraries` made `<LibUnicode/RustFFI.h>` resolve because
   the file sat in-tree; Bazel's sandbox has only declared inputs, so the same
   `-I` copt isn't enough — the header must be a `hdrs` of some target. Modeled
   the cargo-`cxx` FFI byproducts as a `//Build/full/Libraries:rust_ffi_headers`
   `cc_library` (`includes=["."]`) that Rust-dependent libs depend on. (Ring 1b
   LibWeb codegen headers are already proper genrule outputs.)

**Rust decision (Ring 1c uses prebuilt, Ring 2 builds from source).** Seven core
libs (LibUnicode/URL/TextCodec/Regex/Gfx/JS) plus LibWeb depend on Rust crates
that cargo compiles to static `.a` archives. For compile+link and
running-browser parity we `cc_import` the reference build's `.a` (a
`//Build/full/cargo/.../release` shim package), the same deferred-linking
philosophy as the vcpkg `.so` shims. Building them hermetically via `rules_rust`
is Ring 2.

**Status: the whole L0–L8 C++ stack builds and links under Bazel** — 32
libraries (~660 TUs) including the JS engine (`LibJS`), `LibGfx`, `LibWasm`,
`LibTLS`, `LibUnicode`. Project-wide diff over the built stack is down to **3
discrepancies, all on one generated assembly file** (`interpreter_x86_64.S`) —
i.e. the entire C++ library stack is at compile-parity. Only LibWeb (its own
package, Ring 1b codegen) + the services/UI + Rust-from-source remain.

**Additional findings surfaced walking the ladder:**

5. **`-isystem` vs `-I` for third-party headers.** CMake passes vcpkg include
   roots with `-isystem`, which suppresses `-Werror` inside third-party headers
   (openssl `tls1.h` `cast-qual`, skia `SkTemplates.h` `attributes`). Emitting
   them as `-I` (or via `cc_library.includes`, which Bazel spells inconsistently)
   makes those warnings fatal. Fix: global vcpkg `-isystem` in `.bazelrc` + the
   emitter uses `-isystem` for any per-target `vcpkg_installed` include.

6. **Optimization level is a correctness input.** The reference build is
   RelWithDebInfo (`-O2`); Bazel defaults to `-O0`. `LibWasm`'s musttail
   interpreter loop only compiles clean at `-O2` (`-Werror=maybe-musttail-local-
   addr` fires when the tail call can't be optimized). Pinned `-O2 -g` in
   `.bazelrc` — needed for faithful parity and codegen agreement anyway.

7. **C++23 `#embed` data are compiler inputs.** `LibJS` `#embed`s
   `JavaScriptImplementations/*.js`; the sandbox must stage them. Emit them as
   the consuming target's `additional_compiler_inputs` (detected by scanning
   sources for `#embed`).

8. **`cc_library.defines` vs `local_defines`** (already noted as finding 3) and
   **string-valued defines** (`WASM_CRANELIFT_COMPILER_PATH="…"`) need embedded
   quotes escaped for Bazel (`=\\"…\\"`).

9. **The whole source tree as an implicit header root.** CMake's global
   `-ILibraries -IServices -I.` lets any TU `#include <LibGfx/Palette.h>` with no
   dep edge; the source tree is simply present. Bazel sandboxes to declared
   inputs, so this is modeled as three catch-all header libraries every target
   depends on: `//:all_source_headers` (all in-tree `.h`),
   `//Build/full/Libraries:generated_lib_headers` (CMake `generate_export_header`
   `Export.h` + other materialized headers), and
   `//Build/full/Services:generated_service_headers` (IPC endpoints). Faithful to
   how CMake actually compiles; a stricter per-dep header model is future work.

### LibWeb builds + links (the big one — DONE)

**`//Libraries/LibWeb:LibWeb` builds and links** — 1,781 compile actions
producing `libLibWeb.so` (55 MB) — with **zero** `defines_diff`/`flags_diff`/
`includes_diff` across all 1,273 checked-in TUs vs the CMake reference. LibWeb
lives in its **own package** (`Libraries/LibWeb/BUILD.bazel`) because it owns
the Ring 1b codegen (`codegen.bzl` genrules): its 1,961 TUs split into 1,273
checked-in srcs + 688 generated srcs that reference the genrule outputs by
package-relative label (Bazel resolves a source-looking label to the same-package
genrule output). Emitter: `Meta/emit_libweb_bazel.py` (mirrors the
per-lib emitter but rebases every path to the package and pulls generated
src/hdr lists from `generated_srcs.bzl`).

**Findings this surfaced:**

10. **Generated headers need a genfiles include root; source/genrule name
    collisions must be de-duped.** Generated headers are included as
    `<LibWeb/CSS/PropertyID.h>`, but under Bazel they land in
    `bazel-bin/Libraries/LibWeb/…`, not the source tree. `includes=[".."]` on
    the LibWeb library puts **both** `Libraries` (source root) and
    `bazel-bin/Libraries` (genfiles root) on the header search path so
    `<LibWeb/…>` resolves for checked-in and generated headers alike. Four
    headers (`HTML/AttributeNames.h`, `HTML/TagNames.h`, `SVG/AttributeNames.h`,
    `SVG/TagNames.h`) exist as **both** a checked-in file and a genrule output;
    the `hdrs` glob then matched the on-disk copy while the label resolved to the
    genrule output → "label duplicated in hdrs". Fix: `glob(["**/*.h"],
    exclude = LIBWEB_GENERATED_HDRS) + LIBWEB_GENERATED_HDRS` (generated wins).
    The remaining 689 "missing_tu" in the LibWeb diff are purely the
    `bazel-out/…/bin/` vs `Build/full/` genfile-path prefix, not real gaps
    (add a genfiles-prefix normalization to `cmake2bazel.json` to silence).

11. **Extractor OOM on large targets (real tool bug — FIXED).** LibWeb's single
    link action pulls a `depSetOfFiles` DAG of ~4k depsets over a ~13k-artifact
    closure, heavily shared across nodes. `extract_bazel._build_depset_index`
    eagerly materialized a *flattened leaf list per node* (`list.extend` up the
    DAG), which is combinatorial and OOM-killed the extractor (exit 137) on any
    real C++ target — the diff loop simply couldn't run on LibWeb. Fix:
    `_DepsetResolver` flattens **lazily**, only for the handful of link /
    TsProgram actions that actually need a closure, with an iterative
    visited-set DAG walk (shared subgraphs expanded once) and per-root
    `frozenset` memoization. Extraction of LibWeb dropped from OOM to seconds.
    Regression test: `test_bazel_shared_depset_dag_does_not_blow_up` (a wide
    shared-`mid` diamond that would explode under the old per-node expansion).

### Services + renderer executables build, link, and RUN (DONE)

The full renderer/service process set — `LibWebView`, `LibDevTools`, the five
service static libs (`webcontentservice`/`requestserverservice`/
`imagedecoderservice`/`compositorservice`/`webworkerservice`), and the five
service **executables** (`WebContent`, `Compositor`, `WebWorker`,
`RequestServer`, `ImageDecoder`) — all build and link under Bazel. Each
executable is a real PIE ELF that runs (`--help` works). `WebContent` is the
web renderer process; this is the headless browser's whole backend.

The emitter now emits `cc_binary` for `role==production && kind==executable`
targets (system libs → `linkopts`, exe→exe deps dropped as runtime-spawn, not
link edges). Cross-package generated srcs are labeled to their owning package
(`//Libraries/LibWeb:WebGL/GLFunctions.cpp`, `//Build/full/Services:…`).

**Findings this surfaced (link-time — the payoff of actually linking binaries):**

12. **Transitive `DT_NEEDED` of the prebuilt `.so` shims.** The vcpkg `cc_import`
    shims are the top-level `.so` only; their own siblings (avif→libyuv,
    cpptrace→libdwarf) aren't link inputs, so ld fails with undefined refs when
    it can't follow `DT_NEEDED`. Fixed globally in `.bazelrc`:
    `-LBuild/.../lib` + `-Wl,-rpath-link,…` (ld follows NEEDED at link),
    `-Wl,--allow-shlib-undefined` (a shared lib's own deps resolve at *its*
    load, not at this link), and `-Wl,-rpath,…` (the binary finds them at run
    time — matches CMake's install-rpath to the vcpkg lib dir).

13. **Prebuilt Rust archives are a link-cycle + duplicate-symbol minefield.**
    Cargo emits one `.a` per crate with **circular cross-crate references**
    (`liburl_rust` ↔ `libregex_rust`) — GNU ld's single pass can't resolve that
    across separate archives. Fix: pre-merge all crate `.a` into one
    `librust_combined.a` (`ar -M`) and `alias` every crate label to it, so the
    whole rust closure links as one unit. Two more: (a) the Rust FFI is *also*
    circular with C++ — `libregex_rust` calls LibUnicode's `extern "C"`
    exports (`unicode_simple_case_fold`) and LibUnicode calls back into
    `libunicode_rust`; in the reference these are separate `.so`s that resolve
    at runtime, but as static archives one pass can't satisfy the cycle. Fix:
    `alwayslink=True` on the FFI-provider libs (`LibUnicode`, `LibWeb`) so all
    their `extern "C"` symbols are present before the rust archive references
    them. (b) Each crate archive bundles an identical copy of the rust
    allocator shim (`__rust_realloc`, `__rust_alloc_zeroed`); merging them into
    one link collides. Fix: `-Wl,--allow-multiple-definition` (the copies are
    byte-identical; the reference sidesteps this only by per-crate `.so`
    isolation). All three are prebuilt-archive artifacts that Ring 2
    (rules_rust from source, one target per crate) dissolves.

14. **System libraries a *library* pulls must propagate as `linkopts`.**
    LibMedia needs `libpulse` (a `/usr/lib` system `.so`, no vcpkg shim);
    `pulse` was an UNKNOWN external. Added to `SYSTEM_LIBS`, and the emitter now
    puts a lib's system deps in its own `linkopts` (which propagate to the final
    binary — matches CMake's interface/private system-dep flow), not only on
    executables. Also: `woff2dec` (static) references `woff2common` symbols, so
    the shim declares `woff2dec`'s `deps=[":woff2common"]`.

## 🏁 THE GATE: a Bazel-built browser that renders (MET)

`bazel build //:ladybird` produces a 100 MB PIE ELF that **renders web pages**:

```
$ bazel-bin/ladybird --headless=text /tmp/test-page.html
Hello from Bazel

JS says 2+2=4
```

HTML parsed, CSS applied, layout run, and **LibJS executed the page's script**
(`document.getElementById('out').textContent = 'JS says 2+2=' + (2+2)`).
`--headless=layout-tree` dumps the full box tree with computed geometry and font
metrics. Output is
**byte-identical to the CMake reference build's**.

**Proof it is really the Bazel build, end to end.** Ladybird is multi-process:
the UI binary spawns `WebContent` (renderer), `Compositor`, `RequestServer`,
`ImageDecoder`. Moving the reference build's entire service directory out of the
tree (`mv Build/full/libexec …`) and re-running:

- CMake `Build/full/bin/Ladybird` → **fails**: `Could not launch any of
  [ …/libexec/RequestServer, …/bin/RequestServer, ./RequestServer ]`.
- Bazel `bazel-bin/ladybird` → **renders**, spawning its own Bazel-built
  siblings from `bazel-bin/`.

So every process in the running browser — UI, renderer, compositor, network,
image decoder — is Bazel-built.

**What the `ladybird` UI binary needed (last-mile findings):**

15. **Qt's moc unity file needs its includes staged as compiler inputs.** CMake
    AUTOMOC generates `mocs_compilation.cpp`, which is a unity file that
    `#include`s each `EWIEGA46WW/moc_*.cpp`. Only the unity file is a `srcs`
    entry, so Bazel's sandbox lacked the 12 included `.cpp` — they are neither
    headers nor separately-compiled sources. Fix: a `filegroup` of the
    `moc_*.cpp` wired as the binary's `additional_compiler_inputs` (the same
    lever finding 7 needed for `#embed` data). *Superseded:* moc is now run by
    Bazel via `rules_qt` and the unity file is gone — see the `rules_qt`
    section. Generated shader headers
    (`WebContentViewLinux{Frag,Vert}Shader.h`, included by bare name) became
    `hdrs` of the UI package with `includes=["Qt"]`.

16. **Bazel rejects absolute include paths outside the execution root — even as
    `-isystem`, even globally.** `find_package(Qt6)` yields
    `-I/usr/include/x86_64-linux-gnu/qt6/...` and
    `-I/usr/lib/.../mkspecs/linux-g++`. As per-target `copts` Bazel errors with
    "references a path outside of the execution root"; moving them to global
    `.bazelrc` `--cxxopt`s fails identically (the check is on the flag, not the
    target). The working lever is the compiler's own env:
    `build --action_env=CPLUS_INCLUDE_PATH=<qt dirs>`, which Bazel passes
    through unvalidated. The emitter now *skips* absolute include roots in
    per-target copts and they are declared once in `.bazelrc`. (This is the
    general shape of the answer for any `find_package` system dependency, and a
    concrete argument for Ring 2's hermetic-toolchain/BCR direction. *Mostly
    superseded:* Qt now comes from `rules_qt`, leaving only `libdrm` here.)

17. **Catch-all header libraries make every edit a world rebuild.** Because
    every target depends on `//:all_source_headers`, adding `UI/**/*.h` to its
    glob (needed for `<UI/Qt/Application.h>`) invalidated all ~2,700 actions —
    twice, once more for the UI autogen header target. Faithful to CMake's
    global `-I`, but it costs incrementality; a stricter per-dep header model is
    the fix (noted in finding 9 as future work, now with a measured cost).

Rust stays prebuilt-`.a` until Ring 2.

## Hermeticity cleanup: dropping the global `-IBuild/full` include roots

The `.bazelrc` carried `--cxxopt=-IBuild/full{,/Libraries,/Services}` on *every*
compile, added early to mirror CMake's build-dir include. It was a hole of
exactly the kind finding 1 is about, but repo-wide: with those roots visible, any
TU could `#include` any CMake-generated header **without declaring a dep on it**,
so Bazel could not catch a missing edge — the sandbox was being handed the whole
reference build's header tree.

It turned out to be entirely unnecessary. Removed, and the build is unaffected:
LibWeb's 2,573 actions compile, `//:ladybird` plus all 5 service binaries link,
and both `--headless=text` and `--headless=layout-tree` are **byte-identical to
the CMake reference**. Generated headers now resolve only through declared deps
(the per-package genrule outputs, plus the header-root `cc_library`s under
`Build/full/*/BUILD.bazel` — which *are* declared).

Worth noting how weak the earlier evidence was: aquery showed both
`bazel-out/.../Bindings/MediaCapabilities.h` and
`Build/full/.../Bindings/MediaCapabilities.h` as inputs to the same compile. The
parity result was still real — the compile command line puts Bazel's genfiles
root *before* `Build/full`, so Bazel's headers won — but it held by include-order
luck rather than by construction. Now it holds by construction.

Also deduplicated two repeated `--linkopt`s left over from iterating.

**Still not portable to another machine**, which is a separate problem from this
one: the build consumes 224 vcpkg `.so`s, a 260 MB prebuilt `librust_combined.a`,
and (at the time) 13 Qt `moc` outputs from `Build/full`. Qt is since solved (see
the `rules_qt` section below); the rest is Ring 2 (+ rules_rust).
(`-march=native` is *not* part of that problem: upstream CMake does the same by
default, so mirroring it is correct parity.)

## Qt: from CMake's prebuilt autogen to `rules_qt` (DONE)

Findings 15 and 16 above were the two ugliest shims in the build: 12 `moc_*.cpp`
plus `qrc_ladybird.cpp` copied out of CMake's `ladybird_autogen/`, and host Qt
headers smuggled past Bazel's absolute-path check via
`--action_env=CPLUS_INCLUDE_PATH`. Both are gone, replaced by
[`kklochkov/rules_qt`](https://github.com/kklochkov/rules_qt): `qt_cc_moc` over
the 11 `Q_OBJECT` headers and `qt_qrc`/`qt_cc_rcc` for `ladybird.qrc`. The unity
`mocs_compilation.cpp` and its `additional_compiler_inputs` filegroup are
deleted, `CPLUS_INCLUDE_PATH` is down to `libdrm`, and Qt's libraries now reach
the link through declared deps (`@qt//:QtCore` etc., resolved via Bazel's
`_solib`) instead of `-lQt6Core` plus a global `-L`. Verified as finding 1
teaches: with CMake's `ladybird_autogen/` moved aside the build is still green,
and all **11 moc bodies are byte-identical** to CMake's.

I evaluated two rule sets, and the difference is instructive:

18. **The Qt SDK should be discovered, not pinned.** Vertexwahn's `rules_qt6`
    (the `rules_qt` module *on the BCR*) downloads a fixed qt.io build — 6.8.3 in
    0.0.7, while Ladybird's CMake requires ≥ 6.9, so it needed a new module entry
    (URLs + SHAs) before it would even be a legal Qt for this project. It also
    drags in a second Qt alongside the distro one, which then wants
    `QT_PLUGIN_PATH`/`QT_QPA_PLATFORM_PLUGIN_PATH` set by hand at runtime.
    kklochkov's `qt.local_repo` instead runs `qmake -query` on an installed Qt
    and generates the repo from the answer, so `moc`, the headers, and the `.so`s
    are guaranteed to be one consistent SDK — here the same 6.10.2 CMake uses.
    Nothing to pin, nothing to keep in sync, no runtime env fixups. (Its
    `remote_repo` covers the hermetic build-from-source case.) That is the right
    factoring for a `find_package`-shaped dependency, and it is what the
    `find_package` → Bazel adapter I owe Ring 2 should imitate.

19. **moc's output depends on the header's whole `#include` closure — so the rule
    must own the include paths.** moc emits metatype includes only for types
    whose definition it *has seen*: staging just the one `Q_OBJECT` header
    silently drops e.g. `#include <QtGui/qtextcursor.h>` from four outputs. Not a
    build error — wrong output that still compiles, caught only by the byte-diff
    against CMake. With genrules I had to hand-maintain a `moc_input_headers`
    filegroup plus matching `-I` flags (and a Qt-side `include_files` filegroup,
    since `tools = [moc]` stages the binary but no headers). kklochkov's rule
    reads the include dirs straight off the Qt toolchain's `CcInfo`
    `compilation_context` — including `external_includes`, which its comments
    call out as an easy field to miss — and stages the headers itself. Same
    byte-identical result with **zero** include flags in my BUILD file: the rule
    knows what moc needs better than its callers do.

20. **CMake's moc unity file was hiding a real bug in Ladybird.** Compiling each
    `moc_*.cpp` separately breaks one TU: `TabBar.h` calls `as<Tab>()` (a
    `dynamic_cast`) inline but only forward-declares `Tab`. It only ever compiled
    because `mocs_compilation.cpp` `#include`s `moc_Tab.cpp` *before*
    `moc_TabBar.cpp`. Unity builds hide incomplete-type bugs behind file
    ordering. My first fix was moc's `-b` flag to prepend the include — but that
    is papering over it in the build system, and a well-designed rule (correctly)
    offers no such knob. The actual fix is one line in `TabBar.h`:
    `#include <UI/Qt/Tab.h>`. Upstreamable, and it makes the header
    self-contained for every build system.

21. **Bazel-visible flags are only correct relative to the dependency you got.**
    Against qt.io's Qt, our faithfully-mirrored CMake `-fPIE` produced a
    `SIGSEGV` inside `QGuiApplication::screenAdded` during `QApplication`
    construction — before any Ladybird code ran, and only in GUI mode, since
    `--headless` never constructs a `QApplication`. Cause: those builds set
    `reduce_relocations`, which requires `-fPIC` consumers (Vertexwahn's
    `qt_cc_binary` hardcodes `-fPIC` for exactly this). Against the distro Qt
    that `local_repo` discovers, `-fPIE` is correct again — verified both ways.
    So this was never a Ladybird flag bug; it was a property of one Qt binary.
    (I also briefly suspected the dual ICU — qt.io's Qt bundles ICU 73, vcpkg
    gives Ladybird 78 — and was wrong: ICU version-suffixes its symbols
    (`u_strlen_73`), so both coexist fine.)

The remaining host dependency in this area is small: `libdrm` (still via
`CPLUS_INCLUDE_PATH`) and the glslang-generated shader headers, still copied
from `Build/full`.

## Finding 22: `glob()` cannot match generated files — and hid a stale-output dep

The bindings mega-genrule (661 `.idl` in, 1,331 files out) declared
`srcs = native.glob(["**/*.idl"])`, on the reasoning that the generator follows
`includes`/partial interfaces between `.idl` files, so the whole closure has to
be in the sandbox. Two of its inputs are themselves *generated*
(`CSS/GeneratedCSS{StyleProperties,NumericFactoryMethods}.idl`).

`glob()` matches **source files only** — it never sees a genrule's outputs. So
those two inputs were not declared at all. The rule nonetheless built for days,
because stale copies of CMake's output happened to be sitting in the source
tree, where the glob *did* find them. Moving those two files aside is what
exposed it (verify by removal, again). A checkout that had never run the CMake
build would have failed.

Two lessons, both about the emitters rather than about Ladybird:

- **A glob in a generated BUILD file is a smell.** The emitters know each rule's
  exact inputs — they read ninja's `DEPENDS` list. Emitting a wildcard instead
  throws that information away and replaces a declared edge with "whatever is on
  disk". Fixed: the mega-rule now lists its 659 source `.idl` plus the 2
  generated ones as explicit labels. This is also strictly *tighter* than the
  glob was — `HTML/PageSwapEvent.idl` is in the tree but not in CMake's
  dependency closure, and the glob was feeding it in.
- **Hand-appending to a generated file guarantees drift.** This rule had been
  written by hand into `codegen.bzl`, which the emitter *skips* — so re-running
  the emitter would have silently deleted it. It is now emitted by
  `bindings_rule()` in `Meta/emit_codegen_bazel.py`, which derives the outs from
  the ninja edge's output list (the generator takes an output *directory*,
  `-o Bindings`, so there is nothing to scrape from argv) and the srcs from its
  `DEPENDS` closure. Verified: the emitted rule is identical to the hand-written
  one in outs and in argument order, the full parity harness still reports
  1,402/1,402 byte-identical, and all 5 binaries build and render with the stale
  `.idl` copies removed.

Note this is the same shape as finding 3's lesson about coverage: the mega-rule
contributes zero *comparisons* to the parity harness (its output goes to a
directory, so the harness's per-file `copy_if_different` check does not apply),
so a green harness never spoke to this rule's inputs at all.

## Finding 23: where the dependencies actually come from — and why not the BCR

Before designing Ring 2 I had to answer a question I had been fudging: the 44
external libraries the browser links — are they system packages, checked-in
source, or prebuilt vendor binaries? **None of the above.** vcpkg compiles them
from upstream source, on this machine: `vcpkg.json` pins 47 ports with exact
version overrides against a pinned vcpkg baseline commit, and vcpkg downloads 85
upstream tarballs (810 MB) and builds them (3.0 GB of buildtrees, 9,814 object
files). Bazel then `cc_import`s the *output*. So Bazel is consuming the product
of a from-source build it does not control.

(I should also correct a number I had been repeating: "224 vcpkg `.so`s" was a
file count of `lib/`, counting symlinks and `.a`s. It is 87 real `.so` + 97 `.a`,
and what the linked binaries actually pull in is **44 distinct libraries**. The
~39 other shared objects in `ldd` output — glibc, libstdc++, X11/EGL/GLX/Vulkan,
glib, dbus, pulse, systemd — are host-provided, as they are under upstream CMake
too; that is not migration debt. Qt's 4 are host-provided via `rules_qt`.)

### Why not the BCR

My first instinct was to resolve these against the Bazel Central Registry, and
that was a category error, not merely a matter of taste. **The BCR would
re-source the dependencies**: different upstream versions, different patch sets,
different feature configurations than the ones Ladybird pins. That silently
destroys the baseline the whole migration is built on — if the bits differ, every
diff measured afterwards is meaningless. This project already contains the
counterexample: the ICU 73-vs-78 near-miss in the Qt work. Ladybird's curl is
`brotli+non-http+http2+http3+openssl+websockets+zstd`; the BCR's curl is whatever
its maintainer packaged. Concretely, of the 44: ~24 have BCR modules, ~20 do not
— including **skia**, the largest and most feature-specific. And Ladybird carries
**8 of its own overlay-ports** (angle, cpptrace, highway, libtommath, pdfjs,
simdutf, vulkan, wuffs) — patched recipes that exist nowhere upstream. A resolver
that "finds the BCR equivalent" would be optimizing for idiomatic Bazel over
identical bits, which is backwards for a tool whose value proposition is the
parity loop.

### The shape that works: Bazel fetches, vcpkg builds

Keep vcpkg as the *recipe* (it encodes the patches, configure flags, and feature
sets) and give Bazel ownership of fetching, hashing, and sandboxing:

- **`http_file` per distfile in `MODULE.bazel`.** vcpkg portfiles carry `SHA512`
  (2,710 of 2,856 ports); hex → base64 is exactly Bazel's SRI `integrity`.
  Verified: zlib's portfile hash, dropped into `http_file`, fetches a file
  **byte-identical to vcpkg's own `downloads/` copy**. No hand-rehashing, no
  trust downgrade.
- **`--x-asset-sources="clear;x-script,<tool> {url} {sha512} {dst};x-block-origin"`.**
  `x-script` routes every vcpkg fetch to a script that resolves it from
  Bazel-provided files *by hash*; `x-block-origin` makes vcpkg hard-fail rather
  than fall back to the network. It is an `x-` (experimental) feature, which is
  the main risk in this design.
- **Git-sourced externals need pre-staging, not a network hole.** Asset caching
  only covers `vcpkg_download_distfile`; `vcpkg_from_git` shells out to `git
  fetch`, which no asset source can intercept. This looked like it might sink the
  design. It does not: `vcpkg_from_git` uses `DOWNLOADS/${PORT}-${REF}.tar.gz` if
  it already exists, so git-sourced deps can be pre-placed as archives and `git`
  is never invoked.

**Validated on skia** — deliberately the hardest of the 44: not on the BCR,
GN-built rather than CMake, 12 patches, a specific feature set, and 10 of its own
git externals. Result: `libskia.so` **byte-identical to the reference build**
(10,145,257 bytes, `cmp` clean, 2,442 exported symbols matching), built with
**zero network access** — 15 distfiles served from a pre-fetched index by SHA512,
no origin fallback. Two of its externals needed pre-staging; Ladybird's feature
set needs only 3 of the 10 (`VCPKG_BUILD_TYPE=release` prunes spirv-tools and
spirv-headers; direct3d/dng prune the rest).

### Four things testing caught that reasoning would not have

1. **`x-block-origin` is a total boundary, including vcpkg's own tools.** It
   intercepted vcpkg provisioning cmake 4.4.0, patchelf, gn, meson and pkgconf
   for itself. That is the strongest evidence it is a real hermeticity boundary —
   and it turns the toolchain vcpkg silently provisions into a declarable input,
   the same hidden-input class as findings 1 and 22.
2. **Hashes must come from the baseline-resolved portfile, not `ports/` tip.**
   For **8 of the 47 pins the pin is older than the tip of `ports/`** (35 match,
   2 have no port dir). `ports/zlib` describes 1.3.2 while Ladybird wants 1.3.1,
   and using the tip's hash fails with exactly the checksum mismatch Bazel
   reports. The hash must be read out of the historical portfile the baseline
   resolves to, via the `versions/` DB `git-tree` (`git cat-file` in the vcpkg
   checkout). Mechanical, but the difference between working and silently wrong.
   Relatedly, `vcpkg install` must be passed Ladybird's full 45-entry `overrides`
   list — bare `vcpkg install <port>` ignores it and resolves the wrong version.
3. **The vcpkg build's reproducibility depends on a CMake-generated file.** On
   the first full run, 15 of 21 libraries differed from the reference — same
   exported symbols, same `.text`/`.data`/`.bss`, but 5 `LOAD` segments instead
   of 3. Cause: Ladybird's CMake configure writes
   `Build/full/build-vcpkg-variables.cmake` containing
   `set(ENV{LDFLAGS} -Wl,-z,noseparate-code)` (a patchelf/binutils-2.43.50
   workaround), which the custom triplets `include()`. So a *cross-system* input
   — generated by the CMake step — is load-bearing for byte-parity of the vcpkg
   step, and the Bazel rule must reproduce it explicitly rather than inherit it.
   skia was byte-identical anyway because GN passes its own link flags, which is
   precisely why validating on one library is not enough. I had been about to
   write this delta off as "timestamps"; it was not. Supplying the file took the
   tree from 6/21 to 14/21 byte-identical.
4. **Feature selections on *transitive* deps are part of the pinned config.** The
   remaining 7 differences were my own test-harness error, and an instructive one:
   I had hand-written a manifest declaring only `skia` with its feature list,
   which loses the feature selections Ladybird declares on transitive deps —
   `libpng[apng]` most visibly, whose absence removes 20 exported symbols
   (`png_get_acTL`, the APNG API). The manifest must be Ladybird's dependency
   list *verbatim*, not a hand-derived subset: "same versions" is not enough,
   it has to be the same feature closure. A resolver that reconstructs the
   manifest instead of copying it will reintroduce exactly this class of bug.

### Result on the full tree

With Ladybird's manifest verbatim (47 deps, 45 overrides), its overlay-ports and
overlay-triplets, the generated LDFLAGS file, and `--binarysource=clear` to force
a genuine from-source build: the whole dependency tree builds with **zero network
access** (55 distfiles served from the pre-fetched index by SHA512, no origin
fallback), and across **all 83 shared libraries there are no content differences
against the reference build**: 30 byte-identical, 53 differing only in `.dynstr`
zero-padding, with `.text`/`.rodata`/`.data`/`.data.rel.ro`/`.dynsym`/`.gnu.hash`/
`.rela.dyn` hashing identical in every one.

The padding is diagnosed, not hand-waved, and it is an artifact of my test
harness rather than of the design: the triplet sets `VCPKG_FIXUP_ELF_RPATH`, so
the raw linker output embeds the *absolute buildtree path* as RUNPATH and patchelf
later shrinks it to `$ORIGIN`, leaving slack proportional to the original string.
My buildtree path is 8 characters shorter than the reference's, and the affected
files are exactly 8 bytes smaller — and only libraries that link a sibling (an
extra `NEEDED` entry) are affected, which is why e.g. `libbrotlicommon` is
byte-identical while `libbrotlidec` is not. Under Bazel the buildtree path is
fixed by the sandbox, so this becomes stable; it is also a reminder that
`VCPKG_FIXUP_ELF_RPATH` makes vcpkg output *build-path-dependent*, which is worth
knowing before trusting a remote cache.

## Finding 24: swapping the dep tree out from under Bazel — the load-bearing test

Finding 23 compared my Bazel-fetched, vcpkg-built tree against the reference
*by inspection* (83 `.so`s, section hashes). Inspection answers "do these look
the same"; it does not answer "can the browser actually be built from mine". So:
verify by removal. I moved the reference tree
(`Build/full/vcpkg_installed` → `~/vcpkg_installed.REFERENCE-ASIDE`) and put my
tree in its place. With the reference gone, a green build and a correct render
can only come from my tree.

**Result: green.** 2,801 actions, `RC=0`, and the rebuilt `ladybird` renders —
`--headless=text` *and* `--headless=layout-tree` remain **byte-identical to the
CMake reference build's output** (48 and 1,564 bytes). `ldd` confirms 44 vcpkg
`.so`s resolving out of the swapped-in tree. The dependency-provisioning design
of finding 23 is therefore not just plausible, it is sufficient: Ladybird builds
and runs against a dependency tree assembled entirely from Bazel-fetched
distfiles with `x-block-origin` and zero network access.

**What the swap taught that the `.so` diff could not.** Replacing the tree
invalidated ~2,500 **compile** actions, not just the links. I would have missed
this entirely by diffing only `.so`s, and my first read — "the headers aren't
bit-identical" — was wrong in an interesting way. The common set is
*perfectly* identical: all **3,580** headers present in both trees hash the same.
The reference tree has **5 extra files** the mine does not, all under
`include/pkgconf/libpkgconf/`, and nothing in Ladybird includes them.

The cause is not a build difference but a *triplet* difference. vcpkg installs
build-time tooling ports (`pkgconf`, `gn`, `gperf`, `aclocal`, the `vcpkg-cmake`
/`vcpkg-make`/`vcpkg-tool-*` helper ports) into the **host** triplet. My run had
`x64-linux` and `x64-linux-dynamic` as separate output dirs, so the tooling
landed in the host dir; the reference build put host and target in the same dir,
so its `x64-linux-dynamic/include` also carries `pkgconf`'s headers. Pure
spill-over from a shared install root — the target-relevant content matches
exactly.

But the invalidation is the real finding, and it is a **modelling** bug in my
overlay, not a reproducibility one: the vcpkg shim declares one
`cc_library(name = "headers", hdrs = glob(["include/**"]))` — the whole 3,585-file
tree as a single target that *every* compile depends on. So any change anywhere
in the tree, including five unused `pkgconf` headers, re-hashes the input of
every C++ compile in the project. That is correct-but-coarse: it is why a
swap that changed nothing Ladybird reads still cost a 45-minute rebuild, and it
is exactly the granularity that will make a remote cache useless (any dep bump
invalidates everything). It also rhymes with finding 22 — a `glob()` in the
dependency shim is again the smell. Fixing it means per-port header targets
(`:png16_headers`, `:skia_headers`, …) with `includes=[]` scoped per port,
matching what each library actually consumes. Noted as debt, not fixed here:
the coarse target is what preserves the `-isystem`-equivalence to CMake that
finding 23's parity baseline rests on, so it should be split *after* the host
`-isystem` escape in `.bazelrc` is replaced by real per-port `includes`.

**Housekeeping trap, worth recording.** The overlay `BUILD.bazel` lives *inside*
`vcpkg_installed/x64-linux-dynamic/`, so swapping the tree deletes it and Bazel
fails with `no such package`. The committed copy under
`examples/ladybird/workspace/` is the source of truth; copy it in *before*
building. A dependency shim that lives inside the directory it describes is
fragile by construction — under a real `repository_rule` the BUILD file is
generated outside the fetched tree, which is the right shape.

## Finding 25: "1,402/1,402 generated files identical" had a blind spot — the filter

Asked the plain question "how do we get rid of the CMake build?", I enumerated
what is still consumed out of `Build/full` instead of recalling it, and found a
coverage hole in my own headline number. Four generators were never Bazel-ified
*and* never appeared in the parity harness, so they could not show up as
failures — the count was 1,402/1,402 of the files it looked at.

Both the emitter (`emit_root_codegen_bazel.py`) and the harness
(`bazel_parity_harness.py`) select ninja `CUSTOM_COMMAND`s by the same
substring: `'Meta/Generators/' in cmd`. One filter, applied twice, so the
emitter and its checker share a blind spot exactly — the checker cannot catch
what the emitter drops, which is the worst possible failure mode for a
generate-then-verify pipeline. Classifying all 740 `CUSTOM_COMMAND`s by tool:
46 are harness-covered, and the ones the filter hides are

| Hidden generator | Output | Why the filter missed it |
|---|---|---|
| `Libraries/LibGfx/TIFFGenerator.py` | `TIFFMetadata.h` + `TIFFTagHandler.cpp` | generator lives in `Libraries/`, not `Meta/Generators/` |
| `glslangValidator` | 2 `WebContentViewLinux*Shader.h` | not a Python generator at all |
| `Build/full/bin/flapc` | `interpreter_x86_64.S` | a tool **Ladybird builds itself**, then runs |
| `Build/full/bin/generate_interpreter_layout` | `Interpreter/layout.conf` | ditto — and it feeds `flapc`, so two chained self-built tools |
| 21 × `cmake -E copy_if_different` | `share/Lagom/**` (fonts, icons, themes, about-pages, pdf.js) | resource staging, not codegen |

`TIFFTagHandler.cpp` is the sharpest one: it is a *generated source file* that
Bazel currently **compiles straight out of CMake's build tree** via
`exports_files` in a `Build/full` shim, so the migration silently depends on
CMake having produced a `.cpp`, not merely a header. The two self-built tools
(`flapc`, `generate_interpreter_layout`) are the most interesting, because they
are the case Bazel handles natively and better than CMake — a host tool built in
the same graph that produces sources — and they need a real `cc_binary` +
genrule pair rather than a script genrule.

The lesson generalises past this repo, and it is the sharper form of "check a
green check's coverage" (finding 20): when the generator and its verifier share
a selector, the verifier's denominator is the generator's assumption, and a
number like 1,402/1,402 measures agreement rather than completeness. The fix is
for the harness to enumerate *all* `CUSTOM_COMMAND`s and explicitly account for
each one — covered, deliberately excluded (cpack/ctest/lint), or **unhandled** —
so a missing generator is a visible non-zero count instead of an absence.

## Finding 26: the exec configuration is a correctness boundary CMake does not have

Closing finding 25's gap meant Bazel had to *run a tool it builds*
(`generate_interpreter_layout`, which prints struct offsets that `flapc` bakes
into the LibJS interpreter assembly). That immediately hit something CMake has no
equivalent of: a tool run by a genrule is built in the **exec configuration**, a
separate flag namespace that `--cxxopt`/`--copt`/`--linkopt` do not reach. With
none of them applied, `<AK/kmalloc.h>` did not even resolve — there was no `-I.`.

Every global flag therefore has to be repeated as `--host_cxxopt`/`--host_copt`/
`--host_linkopt`, plus `--host_compilation_mode=fastbuild`, because the exec
config defaults to `opt`, whose `-D_FORTIFY_SOURCE=1` collides with Ladybird's
`=3` under `-Werror`. The duplicated list is ugly (README gap 2; the right fix is
a shared `.bzl` list or a real toolchain, not more `--host_*` lines) — but the
*split* is the point, and here it is a correctness requirement rather than
bookkeeping: the tool emits struct offsets and sizes that get compiled into
assembly used by target code, so if the tool sees different defines or ABI flags
than its consumer, the result is silent memory corruption at run time, not a
build error.

This is a case where Bazel is straightforwardly *better* than the CMake it
replaces, and it is worth stating plainly because most of this case study is the
opposite direction. CMake compiles host tools and target libraries from one flat
variable set: convenient, and it means a host/target skew is *unrepresentable* in
the model and therefore invisible right up until someone cross-compiles. Bazel
forces the two namespaces apart, which is what makes the duplication visible at
all. Making the mirror explicit is the fix; the exec/target distinction is the
feature.

Two smaller results from the same work:

- **`flapc` is a Rust crate, not a C++ program.** I had assumed C++ when I wrote
  the task up; `Libraries/LibJS/Flap` is 51 `.rs` files with a crates.io
  dependency, so building it needs `rules_rust` + crate universe — the deferred
  Rust ring, not a new debt class. The right split was to separate the chain:
  `generate_interpreter_layout` **is** a genuine Bazel `cc_binary` built and run
  in-graph, while `flapc` is consumed as the reference cargo binary declared as a
  genrule `tools` input. Either way `interpreter_x86_64.S` is now **Bazel's own
  output**, byte-identical to CMake's, instead of a checked-in artifact compiled
  out of CMake's tree.
- **No generated *source* is shimmed out of `Build/full` any more.** The
  `exports_files` entries for `TIFFTagHandler.cpp`, `Op.cpp`,
  `HSTSPreloadData.cpp`, `interpreter_x86_64.S` and `WebGLCommandReplayer.cpp`
  are gone, as is the `qt_autogen_headers` glob. Each removal was verified by
  deleting the shim and confirming the build stayed green, not by assuming.

### Finding 27: a second nondeterminism in the same function, found by the accounting

The new bucket accounting immediately earned its keep by exposing a *second*
hash-order bug in the very function I had already patched once (the `sorted()`
fix filed as ladybird#10899). `dictionaries_in_dependency_order` iterates a
**set** of dependency names, so two dictionaries that do not depend on each other
— `AudioConfiguration` and `VideoConfiguration`, both reached from
`MediaConfiguration` — are emitted in hash order, and `MediaCapabilities.h`
varies with `PYTHONHASHSEED`. Reproduced directly: seeds 0/2/7 give one order,
seeds 1/42 the other. It is clean on the seed CMake happened to use, so a single
run could never have caught it, and it sat behind a green 1,402/1,402 for weeks.

The general lesson: **a topological sort constrains dependency-before-dependent
and nothing else.** Sibling order among independent nodes is unconstrained, so
any topological sort over an unordered container is a latent nondeterminism, and
one `sorted()` fix in a function does not make that function deterministic.
Fixed in `examples/ladybird/patches/`, verified across seeds.

Also worth recording, because it is the same class of bug as the finding-25
filter: the harness's own new `bin/flapc` pattern was initially **unanchored**,
so it matched the *cargo command that builds flapc* (which names `bin/flapc` as a
copy destination), ran a full cargo build as if it were a generator, and reported
`NO_OUTS`. A "which tool is this?" test has to match the tool being *invoked*
(start of command, or after `&&`), not merely mentioned — and note it was the new
per-command reporting that surfaced it, where the old count would have absorbed
it silently.

### Verification of this work (done in my own tree, not the subagent's)

Independently re-verified rather than taken on report, in a checkout at a
*different* Ladybird revision than the one the work was done against:

- All four emitters reproduce their committed output byte-for-byte from my
  `Build/full` — except one line, which is the interesting part: my regenerated
  `LibWeb/BUILD.bazel` adds `-I/usr/include/libdrm` where the committed file has
  none. So the emitters are faithful but their output is **host-dependent**
  (libdrm's include path leaks into a generated `copts`), which is the same
  host-escape debt as README gap 3 rather than an emitter bug — but it does mean
  "the generated files are reproducible" holds only *per machine*.
- All six newly generated files byte-identical to CMake's (`cmp`).
- `bazel build` of all five binaries green (2,849 actions), and `--headless=text`
  / `--headless=layout-tree` byte-identical to `Build/full/bin/Ladybird` on a page
  exercising grid/flex/tables/entities/JS.
- The harness: 586 `CUSTOM_COMMAND`s, 51 covered, 535 excluded, **0 unhandled**,
  1,408/1,408 identical. And I checked the *teeth*, not just the green: deleting
  the TIFF entry from `COVERED` makes it appear as `UNHANDLED` and exits 1 —
  the exact failure that used to be silent.
- One dead shim the subagent left behind (`exports_files` for
  `WebGLCommandReplayer.cpp`, no longer referenced) removed after confirming by
  removal that `//:Compositor` stays green.

## Finding 28: portfiles are programs, so predict nothing — instrument vcpkg

Ring 2 part 1 is Bazel owning the *fetch*: one `http_file` per upstream distfile,
`integrity` lifted from vcpkg's own published SHA512. The obvious implementation
is to parse `portfile.cmake`. I wrote that, and it plateaus at **54 of 81
distfiles** — not because of missing regex cases, but because portfiles are CMake
**programs**, not manifests. `curl` derives `${curl_version}` from the version;
`angle` carries its own `${ANGLE_COMMIT}`; `libpsl` computes a `${short_hash}`;
`vcpkg-tool-gn` assembles `${download_urls}` per platform. Resolving those needs
a CMake interpreter — which is to say, it needs to *be* vcpkg.

So the authoritative input is a **capture**, not a parse. `x-asset-sources`'s
`x-script` hook hands a script the fully-expanded `{url} {sha512} {dst}` for every
single download — exactly the tuple an `http_file` needs. `Meta/vcpkg_capture_assets.sh`
records them by acting as vcpkg's asset cache. This is the same
instrument-don't-predict tactic as the npm extractor (`scripts/npm_instrument`),
for the same underlying reason: **a build script's inputs are only knowable by
running it.** Validated against the known-good download set: 29/29 captured
hashes are a strict subset of ground truth, with zero filename mismatches once the
`.<pid>.part` suffix vcpkg downloads through is stripped. The capture run is the
one run allowed to touch the network; its output *is* the pin, and every later
build is hermetic against it.

That is the design lesson. The process lesson is sharper, and it is the same one
as finding 25 wearing different clothes.

### Four bugs, all of them absences

Each was found by diffing my emitter's output against **what vcpkg actually
downloaded**, never by reading my own output — which looked plausible at every
intermediate stage.

1. **Emitting the manifest's 45 `overrides` covered 39 of 81 distfiles.** vcpkg
   downloads for the whole transitive **closure** (77 ports): zstd, libtiff,
   openh264, opus, theora, ogg, vorbis, libvpx, libyuv, lcms, ngtcp2/nghttp3,
   xz, libidn2, libunistring, icu, plus vcpkg's own provisioned tooling (cmake,
   ninja, meson, gn, patchelf, pkgconf, gperf, automake). Versions now come from
   the closure via `vcpkg depend-info` — vcpkg's own resolver, deliberately not a
   reimplementation — with `baseline.json` supplying transitive versions.
2. **vcpkg has four version keys, and I read two.** Date-versioned ports use
   `version-date`; reading only `version`/`version-semver` resolved **nothing**
   for `egl-registry`, `opengl-registry`, `libedit` and all eight `vcpkg-*`
   tooling ports.
3. **URLs and filenames interpolate `${VERSION}`.** Taken literally they produce
   filenames containing the characters `${VERSION}`, matching no real download.
4. **`vcpkg_from_gitlab` and `vcpkg_from_sourceforge` were unhandled entirely** —
   4 and 3 uses respectively (counted, not assumed).

Not one of these produced a *wrong value*. Every single one produced an
**absence**: an unresolved port simply vanishes from the output, and the emitter
still prints a confident list. "39 distfiles" reads exactly as well as "81" if you
never compare against the truth. So the emitter now reports unresolved ports and
unexpanded variables as explicit non-zero counts, exits non-zero on either, and
**refuses** to fall back to overrides-only when `depend-info` fails rather than
silently undercounting. A generator that only reports what it found cannot report
what it missed — which is why the third thing I build after an emitter and its
verifier is a comparison against ground truth that neither of them produced.

### The vcpkg build action, and what "resumable" actually means

Part 2 is `vcpkg.bzl`'s `vcpkg_tree` rule plus `Meta/vcpkg_build.sh`. The
placement argument is above (finding 28 / the Ring 2 plan): fetching at module
level, building as an ordinary action. One detail worth keeping: the sha512→file
index is **written by Bazel** and passed as a declared input, so the mapping the
asset-cache script resolves through is part of the action's inputs rather than
ambient state on the machine.

The operational lesson came from breaking it. A sandbox restart killed the
capture run at 27/78 ports after ~50 minutes, and my first "resumable" retry
simply kept the whole scratch root — which failed worse: ports that were
mid-install when the process died had left files on disk that were **not** in
vcpkg's status database, so the retry died with `File exists` on libpng and
libwebp. The distinction that matters:

- `downloads/` **is** resumable: content-addressed, and each file is either
  complete or absent. Keeping it is the entire benefit of resuming.
- the install tree and `buildtrees/` are **not**: they carry
  partially-applied state with no transaction around it.

So: keep `dl/`, discard `out/` and `bt/`. With a warm downloads directory the
restart reached 7/78 in forty seconds. Generalised: when resuming a long foreign
build, keep only the parts that are content-addressed, and re-derive everything
that is a mutation of shared state. "Just don't delete anything" is not a resume
strategy, it is a corruption strategy — and an append-only log plus a
content-addressed cache is the shape that survives being killed at an arbitrary
point.

### Finding 29: `x-script` gets one URL, but portfiles list mirrors

A live failure worth recording, because it is a property of the asset-cache
interface rather than of my script. `gperf`'s portfile lists **two** URLs
(`ftpmirror.gnu.org` then `ftp.gnu.org`), and several GNU ports do. But
`x-asset-sources`'s `x-script` hook is invoked with **one** `{url}` per attempt.
So when `ftpmirror.gnu.org` started returning 502 (it did, for ~13 minutes, and
wedged the capture run mid-build), my recorder failed, and vcpkg did the only
thing it could: fell back to the authoritative source — which is exactly what
`x-block-origin` forbids in a real build. In other words the multi-mirror
redundancy that portfiles encode **does not reach the asset-cache script**, so a
single flaky mirror is enough to break a nominally mirror-redundant fetch.

Two consequences for the design:

- The **capture** must not depend on one mirror being up. It now records the
  tuple unconditionally (the SHA512 is mirror-independent — that is the whole
  point of content addressing) and retries known GNU mirrors before failing.
- For **builds** this is a non-issue, and pleasantly so: once the distfiles are
  `http_file`s, mirror flakiness is Bazel's problem, and `http_file` takes a
  *list* of `urls`. So Bazel's fetching model is strictly better here than the
  asset-cache hook it replaces — which is a small argument for the whole
  direction of this ring: move fetching to the layer that models it properly.

Also worth noting on process: the wedge was invisible from the outside. The run
looked alive (a process existed, the log had recent writes) while making no
progress for 13 minutes, because retrying a 502 forever *is* activity. Liveness
is not progress, and a poll that only asks "is it running?" cannot tell the
difference — the thing to watch was the ports counter, not the process.

## Finding 30: the capture completed, and 4 of its 79 rules were phantoms

The capture from finding 28 finally ran to completion: **76 distfiles**, against a
ground-truth download set of 81. Two cheap wins first, then the interesting part.

`vcpkg install --only-downloads` exists. A capture wants the *fetches*, not the
45-minute build, and without that flag the run takes ~50 minutes and each
interruption costs everything (which is how the first two attempts died). With it
the same 76 distfiles land in **1.9 minutes**. Fifty minutes of build were being
paid for to observe a two-minute fetch — worth checking `--help` before building
a resume protocol around a runtime you never had to accept.

And the 5-file shortfall is not a shortfall. Diffed against ground truth it is
exactly: **4 `vcpkg_from_git` archives**, which bypass asset caching entirely (the
finding-23 note), and **`parsetab.py`** — PLY's generated parse table, *written by*
angle's build into `downloads/`, never fetched at all. 76 + 4 + 1 = 81, with
nothing unexplained. So `downloads/` is not a download directory; it is a
download directory *plus a scratch dir*, and treating everything in it as an input
would have pinned a build byproduct as a dependency.

### The bug: one distfile, two names, two rules

Emitting from the capture produced **83 `http_file` rules for 79 distinct files**.
Four distfiles appeared twice, under names like `giflib-6.1.3.tar.gz` *and*
`giflib-6-fb1d6319.1.3.tar.gz`. The duplicates are not harmless noise: each
phantom rule fetches a real URL and then fails its own `integrity` check, so the
generated `MODULE.bazel` is broken on a clean machine — and broken in the worst
place, module resolution, before any of it can be debugged.

The cause is a chain of three things, none of which I would have predicted:

1. My recorder used `curl -o "$dst"`, which **creates the destination before it
   knows the transfer will fail**. When finding 29's dead mirror 404'd, a 0-byte
   file was left in `downloads/`.
2. vcpkg's `vcpkg_download_distfile` never overwrites an existing file with the
   wrong hash. It splices the expected SHA512's first 8 hex chars in before the
   extension and retries *there* (`string(SUBSTRING "${arg_SHA512}" 0 8 hash)`).
   So one dead mirror **renamed four unrelated distfiles** for the rest of the run.
3. My reader keyed its map by **filename**, so the two names became two entries.

Note where the tag lands: CMake's "extension" is everything from the *first* dot,
so it goes after `giflib-6` — mid-version, not before `.tar.gz`. A plausible fix
("strip the tag before the extension") would not have worked, and the naive
`${name%.[0-9]*}` in my bash recorder silently truncated `giflib-6.1.3.tar.gz` to
`giflib-6.1` when the name arrived *without* a `.part` suffix. Two bugs in one
line of shell.

Three corrections, each at the right layer:

- **The recorder records `{dst}` raw.** It cannot correctly normalise the name —
  undoing the hash-tag splice *requires the hash* — and a lossy record cannot be
  repaired after the fact. Interpretation moved to
  `emit_vcpkg_bazel.canonical_filename()`, where it is unit-tested against both
  manglings (`tests/test_emit_vcpkg.py`, 10 tests). **Capture cheaply and dumbly;
  interpret where you can test.**
- **The capture leaves no partial file.** `rm -f "$dst"` on failure, so a fetch
  failure cannot rename a later success.
- **The distfile map is keyed by SHA512, not filename.** This is the real lesson:
  the artifact it feeds is a `sha512 → label` index, so the hash was *always* the
  identity and the filename was always incidental metadata. The bug is what
  happens when a map is keyed by the field that reads most naturally to a human
  rather than the one the system actually uses. The repo names now carry a hash
  suffix too, for the same reason — `downloads/` is flat and vcpkg disambiguates
  colliding basenames itself, and a duplicate repo name in `MODULE.bazel` is
  *silent*: the second definition just loses.

### And the static parse was still hiding one

While checking the git archives I compared the emitter's *prediction* against the
observed set: it reported **2**, the truth is **4**. `skia` reaches ten of its
externals through its own `declare_external_from_git` wrapper, and angle's are
behind `${URL}`/`${REF}` — invisible to a regex for precisely the finding-28
reason. So these are now discovered the same way as everything else here: by
**difference against what landed on disk**, with the static prediction printed
next to the observation so a shortfall is loud. (`git archive` output is
byte-identical across two independent runs and two vcpkg roots, so pinning its
SHA512 is sound.)

The union of "static parse" and "capture" is also gone. It looked like the safe
choice and was not: every static-only row turned out to be a **Windows-only**
fetch (`libiconv`, `pthreads`, `dirent`, each behind
`if(VCPKG_TARGET_IS_WINDOWS)`), so unioning added three downloads this build never
makes — three more URLs that can rot — while covering *none* of the five files the
capture genuinely misses. When one source is an instrument and the other is a
guess, "use both" is not conservative; it is just the guess with extra steps.

## Finding 31: Bazel builds the dependency tree, and it is the same tree

The end of Ring 2's main line. `bazel build //:vcpkg_installed` now builds all 77
vcpkg ports from the 76 Bazel-fetched distfiles with `x-block-origin`, reaching
the network **zero times**, and the result was checked against CMake's reference
tree rather than admired:

- **5,018 files, and the file lists are identical.** Not one path only in Bazel's
  tree, not one only in the reference (the sole difference is the shim
  `BUILD.bazel` I added to the reference myself).
- **4,740 of 5,018 files are byte-identical**, including **every** header,
  `.cmake` and `.pc` file — 0 differences in anything a compiler consumes as text.
- Of the 278 that differ: **77 are `.spdx.json`** SBOMs, differing in a random
  UUID and a timestamp, and **179 are binaries** whose *exported symbol tables are
  byte-identical, all 179 of them*. The remaining 22 are `.list` manifests and
  similar bookkeeping.
- The binary differences are the embedded absolute build path, and I confirmed the
  mechanism rather than assuming it: `libfontconfig`'s `.dynstr` is exactly 74
  bytes larger, holding the one differing string (`bazel-out/...` vs
  `Build/release/...`), which shifts every subsequent offset and accounts for all
  471 differing `.text` bytes. `.rodata` is byte-identical there; in ffmpeg the
  same cause appears in `.rodata` instead, as `__FILE__` strings from buildtrees.
  Both trees produce 3 LOAD segments, so finding 24's `build-vcpkg-variables.cmake`
  LDFLAGS input is correctly in play.

Then the actual test, which is removal, not inspection (finding 24's rule): move
the reference tree out of the build path entirely, put Bazel's output in its place,
rebuild. All five binaries relink, `--headless=text` and `--headless=layout-tree`
are **byte-identical** to the CMake reference, and JavaScript runs. The control
that makes this mean anything: the swapped-in `libSDL3` carries 58 `PULSEAUDIO_*`
symbols the reference build does not have, so "which tree is loaded" is decidable
from the artifact rather than from my belief about it — and it is Bazel's.

One structural gap remained before the shims could *point* at this target:
`cc_import` takes a file, and `vcpkg_tree` produces a declared **directory**.
Finding 33 resolves it — by dropping `cc_import` rather than feeding it.

## Finding 32: the byte-diff found a host leak that byte-parity would have hidden

The 179-binary ABI check turned up one library where the *local* symbols differed
in a way paths could not explain: Bazel's `libSDL3.so` has 89 extra local symbols,
72 of them PulseAudio, and a `PULSEAUDIO_*` driver the reference build lacks
entirely (`DISKAUDIO`, `DUMMYAUDIO` in the reference; those plus `PULSEAUDIO` in
Bazel's). The exported ABI is identical — 1,272/1,272 — so nothing downstream
notices, which is exactly what makes it worth writing down.

The cause is not Bazel. `sdl3`'s portfile does not mention pulse at all; SDL's own
CMake sniffs the host for `libpulse-dev` and silently compiles in a driver if it
finds one. And the timestamps settle it: SDL3 was built at 15:56, `libpulse-dev`
landed on this machine at 16:25, twenty-nine minutes later. **The reference tree
and the Bazel tree were built from identical inputs on the same machine and are
not the same tree, because the host changed underneath them.** Two things follow.

First, this is a defect in the *reference*, not in Bazel's build — the CMake tree
is the one that is stale, and no amount of comparing Bazel against it would have
revealed that. It only showed up because the comparison was
symbol-by-symbol against ground truth rather than "does it differ, y/n": a
same-size, same-section binary with an identical export table looked like just
another path diff.

Second, it is a concrete instance of the argument for the on-mission direction.
Byte-parity against a vcpkg-built tree is a *baseline*, not a *goal*: the baseline
is itself a function of whatever `-dev` packages the host happened to have, so
chasing it exactly would mean pinning the host, which is the thing the migration is
trying to eliminate. libjpeg-turbo and libvpx show the harmless version of the same
class (27 and 38 assembler `FILE` symbols each, balanced on both sides, pure
paths). SDL3 shows the harmful version. Building the deps with Bazel's own
toolchain and declared sysroot is what actually closes it; wrapping `vcpkg install`
inherits the host sniffing along with the recipe. (Inert here — Ladybird uses SDL3
for gamepad input, not audio — but "inert today" is a property of the consumer, not
of the build.)

## Finding 33: `cc_import` wants a file, so stop importing and describe the link

The tree was buildable and unusable. `vcpkg_tree` declares a **directory** output
(one action, 5,018 files, names not knowable at analysis time); `cc_import` takes
a **file** per attribute. So all 34 shims kept reading the CMake reference tree,
and `//:vcpkg_installed` was a target you could build but not consume — the
migration's last host escape survived precisely because the two Bazel concepts
did not typecheck against each other.

The two obvious ways out are both bad. Declaring ~40 per-file outputs means
hand-maintaining a list of `.so` names *and their version suffixes*
(`libavcodec.so.61.19.101`) that changes on every dep bump, i.e. re-encoding
vcpkg's output in Starlark. Unpacking the directory into a shim package doubles
the bytes and adds a copy of a 400 MB tree to every build.

The third way is to stop modelling the deps as *imported files* and model them as
what the linker actually consumes: **a search path**. `vcpkg_lib` returns a
`CcInfo` directly —

- compilation context: the tree artifact as a header input, plus
  `-isystem <tree>/<triplet>/include` (and `include/skia` etc. for the ports whose
  headers are not at the root);
- linking context: `-L<tree>/lib -l<port>`, with **the tree as
  `additional_inputs`**.

`additional_inputs` is the load-bearing part: it makes the directory a declared
input of every link that transitively depends on the port, so the sandbox
contains it and `-L` resolves. Nothing is enumerated, nothing is copied, and the
dep bump story is "rebuild the tree".

It is also *more* correct than `cc_import` was, in a way I did not anticipate.
`cc_import` stages a `shared_library` into `_solib_k8` under its **file** name
(`libfmt.so`) while the dynamic loader asks for the **SONAME** (`libfmt.so.12`),
so runfiles alone never satisfied a binary Bazel *runs* during the build — which
is why the old setup needed a `runtime_libs` filegroup plus a relative `-rpath`
in `.bazelrc` to paper over it. A search path into the real tree has every
version symlink already in place, because vcpkg put them there.

**The configuration trap.** A 45-minute action reachable from both the target and
the exec configuration is built **twice**, for byte-identical output — vcpkg picks
its own triplet and never sees Bazel's flags, so the tree is not
configuration-dependent, but its output *path* is. Worse, a genrule running an
exec-config tool cannot find the target-config copy at the path baked into its
rpath. Pinning `vcpkg_lib`'s tree attr to `cfg = "exec"` in *both* configurations
gives one tree both link against, and a one-attribute `vcpkg_tree_for_exec`
transition hands genrules the same copy. Sound only because host == target here;
a real cross-compile has to plumb the triplet through anyway.

Two things fell out that are worth more than the plumbing. The vcpkg include root
was a **global** `-isystem` in `.bazelrc`, so every TU could include any
third-party header without declaring the dep — the same undeclared-input hole
finding 1 caught for generated headers, hiding in the flags file rather than in a
BUILD rule. Now `include/skia` arrives *with* the `//Meta/vcpkg:skia` edge, so
including `<skia/...>` without the dep fails to compile. And the old shim
`BUILD.bazel` lived **inside** `Build/full/vcpkg_installed/`, i.e. inside the very
directory it described: swapping the tree deleted the package that consumed it.
The interface to the deps now lives at `//Meta/vcpkg`, outside anything
replaceable.

Verified by removal, since inspection has been wrong before: the CMake vcpkg tree
moved off the machine, `bazel clean`, 4,390 actions from scratch, all six binaries
relinked, `--headless=layout-tree` byte-identical to the reference. `ldd` resolves
46 libraries from Bazel's output tree and 0 from `Build/full`; the finding-32
control (58 PulseAudio symbols = Bazel's SDL3, 0 = CMake's) confirms which tree is
loaded. The four `.bazelrc` escapes are deleted rather than relocated. vcpkg is no
longer a reason you need a CMake build first — Rust is the only one left.

## Finding 34: cargo and Bazel disagree about the unit, and every bug was that

Rust was the last host escape: 10 crates consumed as a **260 MB prebuilt
`librust_combined.a`** copied out of `Build/full/cargo/`, merged by a hand-run
`ar -M` that the README told you to type, and `flapc` taken from the reference
build. It is closed now — 154 crates.io crates fetched by Bazel from
`Cargo.lock`, 10 archives and `flapc` built by sandboxed, network-blocked
actions, `Build/full/cargo` and `Build/full/bin/flapc` moved off the machine,
`bazel clean`, all six binaries rebuilt (4,427 actions) and
`--headless=text`/`--headless=layout-tree` byte-identical to the CMake reference.

The plumbing is finding 33's three layers again (fetch at module level, build as
an action, consume as `CcInfo`), and the fetch layer is *easier* than vcpkg's:
`Cargo.lock` already carries a sha256 for every registry crate and crates.io's
URL is a pure function of (name, version), so there is **nothing to capture** —
no instrumented run, no network, no CMake, just a parse. The interesting part is
not the plumbing. It is that every single bug in this ring came from one
mismatch, and it is not a mismatch about Rust:

> **cargo's unit of work is the workspace; Bazel's is the package.** cargo
> resolves all 11 crates together, writes their generated headers into one shared
> directory, and treats "which file came from which crate" as an implementation
> detail. Bazel demands the opposite: per-target inputs, per-target declared
> outputs, and deletion of anything undeclared.

Each bug is that seam, and each one presented as an *absence* that Bazel's
undeclared-output deletion turned into a visible failure:

- **The headers collide.** Six crates each emit a file literally named
  `RustFFI.h` into the same `$FFI_OUTPUT_DIR`. So "which crate's copy survives"
  is a real question, and for one build the answer was wrong: `liburl_rust`
  shipped `libregex_rust`'s header, and LibURL compiled against the wrong ABI.
  The fix is to stop treating the shared scratch dir as the source of truth and
  resolve each header from the owning crate's own
  `build/<crate>-*/root-output` — which is exactly what
  `Meta/CMake/sync_rust_ffi_header.cmake` does, i.e. upstream hit this too and
  papered it with a copy step rather than naming it.
- **CMake's own header declaration is incomplete.** `libweb_rust` declares one
  FFI header; its build scripts write two, because its dependency
  `libweb_html_tokenizer` writes `HTMLTokenizerRustFFI.h` into the same shared
  dir and three LibWeb TUs include it. CMake never notices — nothing declares it
  and nothing deletes it. Bazel deletes it, and the compile fails. So the
  declared list has to be the *union*, and the emitter has to say when its two
  sources disagree rather than silently pick one.
- **Bazel packages cut across the cargo workspace.** Four crates live under
  `Libraries/LibWeb/`, which is its own Bazel package, and `glob()` is
  package-relative — so the root package, which owns the ring, cannot see their
  sources at all. Hence a `//Libraries/LibWeb:rust_crate_srcs` filegroup on that
  side of the boundary. The alternative (making the root package own those files)
  means deleting the LibWeb package.
- **`rustc` finds its sysroot through `argv[0]`.** Merging the three fetched
  toolchain components with `cp -al` copies Bazel's *symlinks into its fetch
  cache*, so `bin/rustc` resolves back into the cache, looks for
  `lib/rustlib/<triple>` there, and reports "can't find crate for `std` … the
  target may not be installed" — a message that sends you hunting for a missing
  component that is in fact right there. `cp -alL` makes it a real file, and the
  sysroot is the merged tree.
- **cargo validates every target in a manifest while parsing it**, so `flapc`
  needs its `benches/` and `tests/` directories declared as inputs even though
  nothing builds them.

### The bug I introduced by believing a comment

The shim I was replacing said the archives have "circular cross-crate symbol
references (`liburl_rust` → `libunicode_rust`, and the regex crate is bundled
into several)", which is why the 260 MB `ar -M` merge existed. I kept the premise
and only moved it into the graph: all 10 archives in **one linker input** bracketed
by `-Wl,--start-group/--end-group`, so ld re-scans until nothing new resolves.
That retires the `ar` step, it links `ladybird` and `WebContent`, and it is wrong.

`ImageDecoder` and `RequestServer` failed with hundreds of undefined
`rust_sfd_*`, `script_gdi_*`, `eval_gdi_*` — symbols defined in
`Libraries/LibJS/RustIntegration.cpp`, in a binary that deliberately does not link
LibJS. Inside a `--start-group`, ld may satisfy `libgfx_rust`'s std symbol from a
member object of `libjs_rust.a`, which drags that object in, which then wants
`libjs_rust`'s C++ side. The group did not resolve a cycle; it manufactured
dependencies between units that had none.

Because the premise was false, and I only found that out by measuring instead of
reading: take each archive's undefined symbols, subtract the ones **its own
archive also defines**, and ask which other crate supplies the rest.

| | cross-crate symbol edges | what's actually left over |
|---|---|---|
| all 10 crates, both directions | **0** | 176–274 libc/libgcc/pthread symbols each |

Every one of the 200–700 symbols any two archives "share" is defined in *both* of
them, because each Rust staticlib bundles its own copy of rust-std,
compiler-builtins and alloc. That is symmetric by construction — which is exactly
what a dependency is not. Ladybird's Rust crates never call each other; they call
**C++**, and C++ calls them.

So the right shape is one target per crate carrying that crate's archive and that
crate's headers, and the dep edge in `BUILD.bazel` is one-for-one with CMake's
`target_link_libraries`. Which is where the answer was the whole time: CMake links
`libgfx_rust.a` into LibGfx and nothing else, and I had a byte-parity harness
pointed at that build. I trusted a prose comment over a build I could query.

The narrowness is now visible in the output, not just the BUILD files:
`ImageDecoder` defines **28** `rust_*` FFI symbols where `WebContent` defines
**328**. Under the group, `ImageDecoder` did not link at all.

**The transferable lesson.** A wrapped foreign build system hands you two things:
a recipe, and an *explanation* of the recipe. Finding 23 said take the recipe
verbatim and never re-derive it — that was about the manifest. This is the dual:
the explanation is not part of the recipe, and it is not evidence. "The archives
are circular" was load-bearing in a hand-written shim, survived into a doc, and
was still wrong. Bazel is unusually good at punishing that, because a shared
`--start-group` and ten separate inputs both *link* on the target you happen to be
testing, and only differ on the target that needs less. Then check the *narrow*
direction: a superset always links; only the subset proves the edges are real.

### What is left

Both remaining debts are over-declaration, and they are honest:

- Every crate declares the **whole workspace's** sources, because cargo resolves
  the workspace whichever crate you ask for. Touch one `.rs` and 11 crates
  rebuild. Under-declaring would silently reuse a stale archive, so the trade is
  the right way round — but per-crate source sets need the path-dependency graph
  read out of the manifests, which is real work not done here.
- Each `vcpkg_lib` still declares the whole vcpkg tree as its header input
  (finding 33's leftover). Same shape: paths are per-port, the input *set* is not.

Nothing in the emitted build reads `Build/full` any more. It is still needed to
*regenerate* the BUILD files and to run the parity harness — a converter-development
dependency, not a build dependency, and a different claim from the one this ring
closed. (It is also a different claim from "a fresh clone builds", which is still
false for unrelated reasons — finding 36.)

**That claim was still false when I wrote it, and finding 35 is the autopsy.** I
had closed the two *binary* dependencies (vcpkg, Rust) and concluded the tree was
free; it was not. Every binary still depended on **741 targets under `Build/full`**
for *generated headers*, and I had not checked, because the thing that would have
told me — a build failure — could not happen: the shims globbed a foreign tree
with `allow_empty = True`. Read finding 35 before believing any "verified by
removal" in this document, including the ones above: removal is only a test of
what you actually remove.

## Finding 35: the claim was false, and a glob is why nobody noticed

Ulf asked where things stood on checking out Ladybird and building it with Bazel.
I said it worked — findings 33 and 34 had closed vcpkg and Rust, the two *binary*
dependencies on CMake's tree, and the README said so in bold. Then I checked,
which I should have done before saying it.

It did not work. Every one of the six binaries depended on **741 targets under
`Build/full`**, CMake's build tree, and the overlay in `examples/ladybird/`
shipped four `BUILD.bazel` files and **zero headers**. A fresh clone got no error
from that — it built for roughly 1,600 actions and then died on `fatal error:
LibXML/Export.h: No such file or directory`, a message that names neither the shim
nor the missing tree.

**The mechanism is the finding.** The shims were

```python
cc_library(
    name = "generated_lib_headers",
    hdrs = glob(["**/*.h"], allow_empty = True),
    includes = ["."],
)
```

over a directory that only exists after a CMake build. `allow_empty = True` turns
"the tree you depend on is absent" into an empty list and no diagnostic. A glob
over a foreign tree **cannot fail**, so a shim that is broken and a shim that is
unnecessary are indistinguishable — and I had been reading a green build as
evidence for the second.

That generalizes past this migration, and it is the transferable lesson: **if the
emptiness of a glob means "a thing I depend on is missing", `allow_empty = True`
converts a build error into a mystery.** Either let it fail, or don't pretend the
input is optional.

### 741 → 31: most of them were not gaps, they were shadows

Counting the true gaps needed care, and my first count was nonsense — "741
targets, 76 covered", which is impossible. The bug was **matching headers by
basename**: every `Export.h` matched every other `Export.h`, so coverage looked
enormous. Comparing exact logical paths gave the real picture:

| | count | what it was |
|---|---|---|
| LibWeb bindings headers | 666 | **Bazel already generates all 692.** The shim was *shadowing* Bazel's own outputs, silently winning or losing on include order |
| `generate_export_header` `Export.h` | 15 | real gap |
| Rust FFI headers (`RustFFI.h`, `CraneliftFFI.h`) | 15 | real gap |
| Qt `moc_predefs.h` | 1 | not a gap at all — Bazel runs moc itself via `rules_qt`; nothing referenced it |
| AK `configure_file` headers | 2 | already closed earlier in the session |

So 666 of the 741 were pure duplication, which I proved by deleting them: the
build stayed green and recompiled 2,635 actions from Bazel's own headers. The
honest gap was **31 files**, and the reason a 90%-redundant shim survived is the
same `allow_empty` — nothing ever compared the two sets.

### The 15 `Export.h`: derive the template, don't copy the output

`Meta/emit_export_headers_bazel.py` emits them, and two decisions in it matter.

All 15 normalize to **one** template; the per-library tokens are derived the way
`Meta/CMake/targets.cmake` derives them (api = `upper(lib - "Lib") + "_API"`,
prefix = `upper(lib)`, exports = `lib + "_EXPORTS"`), so adding a library needs no
edit. All 17 emitted artifacts (15 + AK's two) are **byte-identical to CMake's**,
checked by `--check Build/full`, and that check earned its keep twice: once on a
`#cmakedefine` regex (the templates put the `#` at column 0 with the indent
*after* it — `#    cmakedefine01 FOO`, which two regex attempts got wrong), and
once on a **1-byte** difference a `render()`-level check could never see — a
heredoc adds a trailing newline, so the emitted shell needed one stripped from the
body. Only byte-comparing the *built artifact* catches that.

`AK/Backtrace.h` is deliberately **not** in the parity check, and that is the
interesting one. It is not a template — CMake writes it from
`find_package(Backtrace)`, i.e. from a *question about the host*. So the genrule
**compiles a probe** rather than baking my machine's answer. Comparing the result
against my own tree would only re-confirm my own tree, which is why it is
excluded: test the variable, not the value.

`Libraries/LibWeb` needed its own emitter mode, for a reason worth stating because
Bazel is right and I was wrong: include dirs cannot escape a package, so LibWeb's
`Export.h` must be an output *of the LibWeb package*. `includes = ["../.."]` is
rejected ("resolves to the workspace root"). It lands at `genroot/LibWeb/Export.h`
with `includes = ["genroot"]`.

### Bug 1: eight crates ship a `RustFFI.h`, and LibRegex had the wrong one

Removing the shim exposed a **real, pre-existing correctness bug**. Eight of the
ten Rust crates emit a header named literally `RustFFI.h`, and four TUs include it
with no directory (`#include <RustFFI.h>`). CMake is unambiguous because
`FFI_OUTPUT_DIR` defaults to the consuming library's *own* binary dir. Bazel puts
every dep's include dirs on one command line, and my rules published every
crate's unprefixed `ffi/` dir to every consumer — so **LibRegex was compiling
against LibUnicode's header**, and the only thing hiding it was a leftover
`-IBuild/full/Libraries/LibRegex` that shadowed both. Delete the tree and it
becomes `'RustRegexFlags' has not been declared`.

The fix needed **two** steps, and the first one alone looked sufficient — which is
the part I would otherwise have shipped:

1. **Publish the unprefixed dir on its own target** (`cargo_bare_include`), not on
   `cargo_lib`. Necessary because `CcInfo`'s `system_includes` propagate
   transitively: folded into `cargo_lib`, LibGfx inherited LibRegex's *and*
   LibTextCodec's bare dirs and failed with `'FFI' does not name a type`.
   (`cc_common.create_compilation_context` has no settable "local includes";
   I tried.)
2. **Depend on it through `implementation_deps`.** Step 1 stops the dir leaking out
   of `cargo_lib`; it does *not* stop it leaking out of **LibTextCodec**. Include
   dirs propagate along the C++ dep graph too, so `LibGfx → LibTextCodec` handed
   LibGfx someone else's `RustFFI.h` anyway, and `YUVData.cpp` failed identically.

`implementation_deps` is Bazel's name for exactly the scope CMake's
`target_include_directories(... PRIVATE)` has — which is *why* a bare include is
unambiguous in CMake, and what had to be reproduced rather than approximated. The
three crates that need it are **derived by scanning the source** for a
directory-less include, not hardcoded, and the build emitter now **imports** that
derived set instead of keeping its own copy beside it (it had one; a hand-kept copy
of a derived set is finding 23 in miniature).

### Bug 2: an entire Rust target was missing, behind two host escapes

`//:WebContent` and four others then failed with one error:
`CraneliftBridge.cpp:13:10: fatal error: CraneliftFFI.h: No such file or directory`.

Cranelift — Ladybird's AOT WebAssembly compiler, `ENABLE_CRANELIFT_JIT=ON` — was
absent from the Bazel graph **entirely**. `Libraries/LibWasm/CMakeLists.txt` was
not in the emitter's `CMAKELISTS`, and it declares its crate with
`build_rust_binary()`, which the parser did not handle at all. Two host escapes had
been covering for it:

- the FFI header sat in `Build/full/Libraries/LibWasm`, which a global `-I` reached;
- the compiler binary was named by **an absolute path to my machine**, baked into
  `-DWASM_CRANELIFT_COMPILER_PATH="/home/ubuntu/ladybird-work/Build/full/bin/cranelift-compiler"`.

The second one alone would have broken every other checkout on earth, and it was
in a checked-in generated file. Nothing was going to tell me: it is a *define*, so
it compiles fine and the lookup only fails at run time, on a code path that needs a
WebAssembly page big enough to trigger AOT compilation.

A `build_rust_binary` crate is a genuinely different shape, not a variant spelling,
and the rules now say so: there is **no archive to link** (`cargo rustc --bin` is
the whole build, and what C++ consumes is the *executable*, spawned at run time),
but it **still emits a cbindgen header** its caller includes. So `cargo_binary`
declares FFI headers like `cargo_crate` does, `cargo_lib` yields a headers-only
`CcInfo` when the archive is absent, and the crate is consumed through the same two
labels a staticlib crate is.

For the binary itself, the fix is *not* to point the define at `bazel-bin` — that
is the same escape with a nicer prefix. Ladybird already resolves the compiler
through a chain (`resolve_cranelift_compiler_path`: `$LADYBIRD_CRANELIFT_COMPILER`
→ compile-time path → **sibling-of-self**), and Bazel puts every root-package
output in one bin directory, so link 3 finds it with no path baked in at all. The
define becomes the bare filename and the binary is attached as `data`, so it is
genuinely *there* in the runfiles of everything that links LibWasm. **The
dependency is declared; the path is not asserted.**

### What actually verified it

Not "the build is green" — that was the state I started from. The header is
byte-identical to CMake's; then `Build/full/{Libraries,Services,UI,bin,cargo}` was
moved off the machine and the three shim packages **deleted**, leaving zero
Ladybird-generated headers under `Build/full`; all six binaries rebuilt from
scratch (2,704 actions); and both `--headless=text` and `--headless=layout-tree`
came out byte-identical to the CMake reference on three pages, two of which execute
WebAssembly. `cquery` over the closure of all six binaries now returns **0** targets
under `Build/full`.

One control worth recording, because it is the kind of check that keeps an
enthusiastic conclusion honest: I instrumented `cranelift-compiler` with a wrapper
to prove the browser was invoking it, and **it never was** — even on a
300-function module. Before concluding the wiring was broken I ran the same probe
against the *CMake reference build*, which also never invoked it. Compilation is
submitted to a thread pool and the page finishes first; the Bazel build matches the
reference exactly. Without the reference-side control I would have "fixed" a
non-bug.

Two smaller repairs fell out of the same pass, both drift the emitter rule exists
to prevent: `emit_libweb_bazel.py` did not emit LibWeb's export-header block, so
regenerating would have silently truncated it; and six of the seven per-target
`-IBuild/full` copts existed only because CMake spells a target's own binary dir
relative to itself (`Build/full/Services/WebContent/../..`) and the emitter
compared paths as **strings** against its global-roots set. `os.path.normpath`
before the comparison removed all six.

### The same bug, a third time, in this repo's own tests

Having learned that a check which cannot fail is indistinguishable from one that
is not needed, I applied it to the test suite itself — by counting, not by reading
the exit code. `for f in tests/test_*.py; do python3 "$f"; done` exited 0 for all
ten files, but three of them (`test_emit_cargo.py`, `test_emit_vcpkg.py`,
`test_vcpkg_plumbing.py`) had **no `if __name__ == "__main__"` runner at all**:
Python imported the module, defined 46 `def test_` functions, called none of them,
and exited 0. There is no pytest in this sandbox, so nothing else was calling them
either. Exactly the shape of `allow_empty = True` — a green result carrying no
information.

With runners added, 6 of those 46 failed, and every one was a *true* report about
the finding-35 changes rather than a stale assertion: the ring emitter had grown a
third parameter (the binary crates), `cargo_lib` had gained an
`archive == None` branch for a `--bin` crate that has no archive, the shared
FFI-header lookup had moved into `cargo_vendor.sh` so both drivers use one copy,
and one test read `Build/full/Libraries/BUILD.bazel` — a file whose *deletion* was
the fix. That last one is the pleasing part: the test failed because the thing it
asserted about no longer exists, so it became the stronger assertion that the path
**must not** exist.

The repair is not "add a runner to each file", though that is what was missing: a
per-file runner is precisely the thing nobody notices the absence of, so doing only
that leaves the next silent file undetected. What was missing was **one entry point
that knows how many test files exist and how many tests each contributed**, so a
file going quiet is a failure and not just a smaller number nobody was counting.
`tests/run_all.py` discovers `tests/test_*.py`, and fails the run if any file
defines no tests or will not import — the `allow_empty = False` of test discovery.
Its guards are tested by triggering them, because a guard never seen to fire is
back where this started. The suite is 135 tests over 11 files, 0.3s, one exit code.

The general lesson, which outlives this repo's test layout: **the unit that has to
be accounted for is the CONTAINER, not the item.** Counting passing tests cannot
detect a missing file; counting matched files cannot detect an empty glob;
counting green actions cannot detect a header supplied by a shim. Each time, the
fix was to make the enclosing thing declare how many children it should have.

## Finding 36: I removed the dependency I was looking for, so that is the one I found

Ulf asked, again: *can I clone and build with Bazel?* Finding 35 had just closed the
741 `Build/full` header dependencies, `cquery` returned 0, and the README said yes.
This time I did not answer from the README. I ran `git clone` into an empty
directory, dropped the overlay in, and typed the command.

**It failed. Six times, for six different reasons, and five of them have nothing to
do with `Build/full`.**

| What was missing | The error a cloner gets | Why my machine hid it |
|---|---|---|
| `Build/vcpkg` — a microsoft/vcpkg checkout at `vcpkg.json`'s `builtin-baseline` | `/tmp/.../root/vcpkg: No such file or directory` | `Meta/ladybird.py vcpkg` had been run months earlier |
| its `.git` (120 MB), which vcpkg needs for `git read-tree` to resolve versioned ports — and which the filegroup **excludes** | `fatal: not a git repository: '.git'` … `while checking out port sqlite3` | the action is `no-sandbox`, so it read the real checkout regardless of what was declared |
| `Build/caches/HSTSPreload/transport_security_state_static.json`, an **unversioned** download CMake does at configure time from Chromium's `main` | `missing input file '//:Build/caches/...'` | CMake's configure had already fetched it. **Now fixed**: pinned downstream to a commit + sha256 (`hsts_preload.bzl`) and fetched by Bazel |
| two path bugs in `Meta/vcpkg_build.sh`: the distfile index *and its entries* were execroot-relative, and vcpkg runs the asset script from its own cwd | `awk: cannot open ...`, then `cp: cannot stat ...`, surfacing as `no asset cache hits` and `x-block-origin blocks trying the authoritative source` | the checkout already had `downloads/tools/cmake-4.4.0-linux`, so vcpkg never *asked* the script for a tool |
| `ply`, which the `angle` port installs with **`pip install ply`** | `No matching distribution found for ply` | this sandbox exports `HTTP_PROXY`, and the action inherits it |
| the four `vcpkg_from_git` archives, staged from a directory I had made **by hand** — `vcpkg_git_archives.bzl` is generated, committed, documented, and **loaded by nothing** | 20 minutes in: `git fetch https://android.googlesource.com/.../piex.git … Error code: 128` | the directory existed on my disk from when I built the pin |

### Three of them are the same bug as finding 35, in three different syntaxes

`//Build/vcpkg:tree` is `glob(["**"], allow_empty = True)` over a directory a fresh
clone does not have. It matches exactly one file — *its own `BUILD.bazel`* — reports
nothing, and the build dies later somewhere else. That is the finding-35 pattern
verbatim; I had deleted the three `Build/full` shims and left a fourth shim, over a
different tree, in place. **I had been looking for `Build/full`, so `Build/full` is
what I removed.**

The git-archive staging is the same idea written in bash, and it is worth quoting
because it packs three independent ways to succeed while doing nothing into four
lines:

```bash
if [ -d "$SRC/Meta/CMake/vcpkg/git-archives" ]; then                     # skips
    cp "$SRC/Meta/CMake/vcpkg/git-archives/"*.tar.gz "$ROOT/downloads/" \
        2>/dev/null || true                                              # hides, forgives
fi
```

A directory test that skips silently, a redirect that swallows the error, and a
`|| true` that forgives the exit code. The four tarballs it was supposed to place
only ever existed because *I* had made that directory while capturing the pin.
`vcpkg_git_archives.bzl` — generated, checked in, listed in the README's file table
— is loaded by **nothing**; it is documentation wearing a `.bzl` extension. Without
those files skia gets 20 minutes in and dies fetching `piex.git` from
android.googlesource.com, naming neither the directory nor the tarball nor the port
that pinned it. It now fails in four seconds saying exactly what is missing and
where it comes from.

The `.git` case is worse than a missing input, because it is a *lie in the
declaration*: the filegroup explicitly excludes `.git/**`, the port resolution
genuinely requires it, and the build works anyway because `no-sandbox: "1"` lets the
action read the real path instead of the declared inputs. An excluded input that the
action reads is strictly worse than an undeclared one — the exclusion looks like a
decision.

### The fifth is a hole in a claim I had "verified"

"The 77 vcpkg ports are built with **zero network access**" was measured with
`x-block-origin`, and that measurement is sound as far as it goes: remove a distfile
from the index and the build hard-fails instead of fetching. But `x-block-origin`
governs **vcpkg's own downloader** and nothing else. The `angle` overlay-port calls
`x_vcpkg_get_python_packages`, which runs `pip install ply` — not a distfile, not an
asset, never seen by the pin.

And nothing stopped it, because I had written the enforcement as a *label*:

```python
execution_requirements = {"local": "1", "no-sandbox": "1", "requires-network": "0"}
```

`requires-network: "0"` is a scheduling hint. It does not build a network namespace,
and `no-sandbox: "1"` guarantees there is none to build. With
`use_default_shell_env = True` the action inherits this sandbox's `HTTP_PROXY`, so
pip quietly succeeded for months. **A control that is not enforced is
indistinguishable from a control that is not there** — the same sentence as finding
35's glob, applied to an `execution_requirements` key instead of a `glob()`
argument.

The fix needs no patch to the portfile, because pip has supported offline switches:
the wheel is pinned by URL and sha256 (`vcpkg_python_packages.bzl` → `http_file`),
declared as an input of the vcpkg action, staged into a find-links directory, and pip
runs with `PIP_NO_INDEX=1` and the proxy variables unset. An unpinned package is then
`No matching distribution found` — an error, not a download, which is precisely the
property `x-block-origin` gives the other 76. Two details are worth keeping: the URL
is `files.pythonhosted.org`'s content-addressed path (immutable for a version, unlike
`pip install ply`, which resolves against whatever PyPI serves today), and the wheel
had to be added to `use_repo` *and* to the emitter's `--use-repo` output — it is the
one vcpkg input no instrument can capture, so it is also the one a regeneration can
silently drop.

The other half of the fix is not code: **verify in an environment with no route to
the network.** A flag asserting there is no network is exactly the kind of evidence
this migration keeps getting wrong.

### What generalizes

Three times now the same shape: a `glob` that cannot fail, test files that ran no
tests, and an `execution_requirements` key that enforces nothing. Each one was
*green*, and green was the problem. What is worth taking from finding 36 specifically
is narrower and more uncomfortable:

**Verification finds what it is aimed at.** "Verified by removal" is the strongest
check in this document, and it is still only a test of *what you remove*. I removed
`Build/full` because `Build/full` was the thing I had been arguing about; a clone
needs `Build/vcpkg`, a `.git`, an HSTS table and a Python package, and no amount of
rigor about `Build/full` was ever going to mention them. The check that finds all
five is not a better `cquery` or a wider removal — it is **the actual user's first
command, run the way the user runs it, in a directory that has never seen this
project.** That is one line of shell, it costs ten minutes, and I should have run it
before the first time I said yes.

### Then the seventh: the recipe for running it pointed at CMake's build tree

With all six fixed, the clone builds — `//:vcpkg_installed` offline (76 ports,
`pip is offline; 1 pinned wheel(s)`), then all six binaries, 2,842 actions, RC=0 —
and then the *documented run command* fails with `Runtime error: mkdir: Permission
denied (errno=13)`, which is what Ladybird says when it cannot find its resource
root. The README's staging block ended with:

```sh
ln -sfn "$PWD/Build/full/share/Lagom" "$ER/bazel-out/k8-fastbuild/share"
```

**`Build/full`.** Six findings about a fresh clone not having CMake's build tree,
and the last line of the recipe symlinks CMake's build tree. It had never been read
as an instruction, only as something that already worked on my machine — the same
mechanism as all six, one layer further out, in prose rather than in a `glob`. The
resource tree needs no CMake at all: it is `Base/res` (in the clone) plus pdf.js from
`//:vcpkg_installed` (`share/pdfjs/{build,web}`, with
`pdfjs-ladybird-transport.mjs` moved into `web/`, which is where
`UI/cmake/ResourceFiles.cmake` puts it). Assembled that way it is `diff -rq`-identical
to `Build/full/share/Lagom`, and the fresh clone's binaries then render
`--headless=text` and `--headless=layout-tree` **byte-identically to the CMake
reference on all three test pages**. Two smaller notes worth keeping: Bazel's outputs
are read-only, so `cp -r` propagates that and the second staging run fails with
`Permission denied` (`cp --no-preserve=mode`); and the six render RCs were 1 for a
*single* reason — a missing resource root — which is a reminder that a nonzero exit
from a browser is one bit and says nothing about which of six pages failed.

So the honest state is: **a fresh clone builds and renders, with three inputs staged
by hand** (`Build/vcpkg` + its `.git`, the unpinned HSTS table, the four
`git archive` tarballs). That is a different sentence from "clone and build", and the
difference is exactly what rows 1–3 of the README table say.

### What closing the last three actually takes — and why my first answer was wrong

Ulf's follow-up was the right one: *so what's needed to make it work?* My first
answer was "a custom `repository_rule` that clones vcpkg at the baseline — it closes
two of the three blockers." I had done real work to support it: I confirmed
`git_repository`/`new_git_repository` **strip `.git`** (so the built-in rule cannot
deliver the one property this dependency needs), that a custom rule shelling out to
`git clone` keeps it, and that `glob(["**"])` then carries the `.git` files as
declared inputs. All true, and all beside the point.

**Ladybird already ships the thing I was proposing to write.** `Meta/ladybird.py
vcpkg` — 45 lines in `Meta/Utils/build_vcpkg.py` — clones microsoft/vcpkg, checks out
`vcpkg.json`'s `builtin-baseline`, and bootstraps the tool at a tag+SHA512 the
checkout itself pins. I ran it from an empty directory: **75 seconds, RC=0, `.git`
present at the right commit, `vcpkg` binary built.** The README's step 1a has been
telling cloners to run it the whole time. So blockers 1 and 2 are not missing
machinery; they are a **prefetch step that the recipe must run in the right order**,
and the honest fix is a `MODULE.bazel`-adjacent note plus a check that fails clearly
when it has not been run — not a repo rule reimplementing a script the project
maintains.

Two lessons, and the second is the uncomfortable one. First: **a repo rule that
re-implements the foreign project's own bootstrap script is a fork of it.** It would
drift the moment Ladybird bumps its baseline or its tool metadata, and the drift
would look like a Bazel bug. Second: **I answered a "what's needed" question by
designing, not by reading the project.** Nothing in the environment stopped me from
opening `Meta/Utils/build_vcpkg.py` before proposing to duplicate it — the same
failure mode as answering from the README instead of doing the clone, one level up:
the fix I imagine is more available to me than the fix that exists.

**The four `git archive` tarballs (row 6) collapse the same way.** No repo rule
needed: clone the pinned URL, `git archive <ref>`. I reproduced `libyuv`'s tarball
that way and its SHA512 matched `vcpkg_git_archives.bzl`'s committed value **exactly**
— which is the useful part, because it means the committed hashes are *checkable* and
the reproduction is verifiable in one line rather than trusted. Eight lines of shell,
against a `repository_rule` I would have had to design, test and maintain.

**Then Ulf asked the question that dissolved even the eight lines: "how does a
Ladybird developer get the correct stuff on disk?"** The answer is that they do
nothing, because **one ordinary `./Meta/ladybird.py build` produces all three
inputs.** `build` calls `build_vcpkg()` itself (not just the `vcpkg` subcommand), so
the checkout and its `.git` appear; the CMake *configure* downloads the HSTS table via
`hsts_preload.cmake`, gated on `ENABLE_NETWORK_DOWNLOADS`, **default ON**; and vcpkg
writes the four `git archive` tarballs into `Build/vcpkg/downloads/` while building
skia and angle. I checked that last one against my own tree: the SHA512s of
`Build/vcpkg/downloads/{angle,libyuv,skia}-*.tar.gz` **equal the committed values** in
`vcpkg_git_archives.bzl`. So "the directory I made by hand months ago" was a copy of
vcpkg's own download cache — I had not built a pin, I had copied one, and then
forgotten which.

That reframes the finding-36 table — but it also let me answer the wrong question.
"Run the normal build first" is fine for a Ladybird developer and **useless for the
case the whole exercise is about**: Bazel without CMake. Ulf had to ask a third time
before I built it.

**Without CMake, two of the three are now closed, and the third is a one-line
upstream fix.**

`Meta/ladybird.py vcpkg` is a standalone subcommand — no configure, no CMake — so the
checkout and its `.git` cost ~70 s. The four `vcpkg_from_git` tarballs are what
needed building, and the shape of the solution is the interesting part, because I got
it wrong twice on the way:

1. **A static parse of the portfiles is unsound, and wrong in both directions at
   once.** My first script scanned skia's and angle's portfiles for
   `declare_external_from_git` / `checkout_in_path` and produced **8** archives for
   skia where 4 are real, while **missing libyuv entirely**. Both errors have one
   cause: `declare_external_from_git` only *declares*, and
   `get_externals(${required_externals})` picks from that under feature and platform
   `if()`s — the set is decided by CMake evaluation, not by the text — while libyuv's
   archive comes from the libyuv *port* calling `vcpkg_from_git` directly, which a
   scan of skia+angle cannot see. This is finding 30's lesson recurring: **portfiles
   are programs, so do not re-derive what they compute.**
2. **So take the list from the pin and use vcpkg as the instrument for regenerating
   it.** `vcpkg install --only-downloads` runs the portfiles' fetch phase and stops:
   ~6 minutes, no compilation, no CMake, and vcpkg_from_git produces its tarballs at
   the refs the real resolution picks. Same tactic as the 76-distfile asset capture
   — instrument the foreign build system rather than predicting it.
3. **`Meta/fetch_vcpkg_git_archives.py` then reproduces each pinned tarball with
   `git clone` + `git -c core.autocrlf=false archive <ref>`** (byte-for-byte what
   `vcpkg_from_git.cmake` runs internally) **and verifies it against the committed
   SHA512.** Result: **4/4 reproduced from scratch, byte-identical to the pin.** The
   hashes came from vcpkg; git reproducing them is the proof the two agree, so the
   pin is checked rather than trusted. Static resolution survives only where it is
   sound: mapping an already-known archive *name* to a clone URL.

One asymmetry is recorded rather than smoothed over: `--only-downloads` yields **3 of
the 4**, because angle's zlib is fetched from angle's *build* phase via
`checkout_in_path`, not its fetch phase. The script says so; "--only-downloads gets
them all" would have been the comfortable, false version.

That left **exactly one** genuine hermeticity defect: the HSTS table, fetched from
Chromium's unversioned `main`. It is now closed too — pinned *downstream*, to the
commit `main` is serving rather than to a release tag, which is the part I got wrong
first; see below. A useful detail found while checking how invasive the upstream fix
would be, and which also makes the downstream pin shareable: CMake's `download_file`
is a no-op when the file already exists (verified with `ENABLE_NETWORK_DOWNLOADS=OFF`),
so the Bazel-fetched pinned file, copied into `Build/caches/HSTSPreload/` before
configuring, is consumed by CMake unchanged.

My four successive answers to "what is needed" were: a `repository_rule`, a prefetch
script, a `cp`, and finally a script plus a capture instrument. The middle two were
smaller because I kept reading further into what the project already does — but the
last one is *bigger* than the `cp`, and that is the actual lesson. **"It falls out of
the existing build" was a true sentence that dissolved the question instead of
answering it.** For the audience that has CMake it is the right answer; for the
audience this migration exists to serve it is a non-answer, and I gave it because it
let me stop working.

**The HSTS table (row 3) is the one whose unpinned fetch is not ours to fix — which
turns out not to mean we cannot pin it.** `Meta/CMake/hsts_preload.cmake` fetches
`raw.githubusercontent.com/chromium/chromium/**main**/net/http/transport_security_state_static.json`
— an unversioned ref, at CMake *configure* time. My first answer was "pin it
upstream, until then stage it", and it was wrong in the way that matters: **it made
someone else's repo a prerequisite for our hermeticity.** A converter usually cannot
change the project it converts, so an answer that requires an upstream patch is an
answer that never ships.

The correction, and it is a general shape worth stating: **a converter cannot pin an
input on the foreign build system's behalf, but it can pin it for itself — provided
it pins the revision the foreign system is currently *serving*, and proves that with
a byte comparison rather than a hash it invented.** What made me think otherwise was
picking the wrong revision. I tested a Chromium *release tag* (`139.0.7258.5`): 18.7
MB against `main`'s 10.5 MB, **168,593** generated entries against **94,626** — so
pinning *that* really would have traded a hermeticity gap for a parity gap. But the
commit `main` pointed at when this machine configured serves bytes identical to what
CMake downloaded (`cmp`, 10,521,748 bytes), and pinning **that** costs no parity at
all. A tag is a pin to a *different table*; a commit is a pin to *this* one. The
distinction is the whole finding, and I had generalized "pinning breaks parity" from a
single badly chosen pin.

So the overlay now pins downstream: `hsts_preload.bzl` (an `http_file` at that commit
+ sha256, consumed by `gen_HSTSPreloadData` as `@hsts_preload_json//file`) and
`Meta/pin_hsts_preload.py`, which re-pins by *measuring* — it downloads the file and
writes the hash it computed, and `--expect-same-as` refuses to write a pin whose bytes
differ from the file the other build system already has. Verified on the fresh clone
with the CMake-downloaded file **deleted** — so the pin is the only possible source:
`HSTSPreloadData.h`/`.cpp` byte-identical to the CMake reference, and `//:LibHTTP`
compiles and links them (RC=0). The residual cost is stated rather than hidden: CMake still
tracks `main`, so a configure newer than the pin disagrees with Bazel — one pinned
input against one unpinned one, a dated disagreement with a sha to look at, instead of
two unpinned fetches that happened to agree. The upstream one-liner is filed as a bug,
not depended upon.

### Can we just `http_file` the HSTS table? Measured, all four combinations

Asked directly, so I ran it rather than reasoned about it. `http_file` has two knobs
that matter — pinned ref or `main`, `sha256` or none — and Bazel behaves differently
in all four:

| URL ref | `sha256` | what Bazel does | what you get |
|---|---|---|---|
| `main` | none | **fetches, builds, and prints** `DEBUG: … a canonical reproducible form can be obtained by modifying arguments integrity = "sha256-ObT9…"` | works; unpinned |
| `main` | given | fails the moment upstream moves: `Checksum was 5d5df26… but wanted 000…` | a build that breaks on Chromium's commit rate |
| pinned tag | given | fetches; 18.7 MB | hermetic, **and not what CMake built** |
| any | none + plain `http://` | refuses outright: `No URLs left after removing plain http URLs due to missing checksum` | — |

So the answer to "can we?" is **yes, mechanically** — row 1 builds today. Two measured
facts decide whether we should.

**First: unpinned means Bazel caches whatever it saw first, forever.** With a local
HTTP server as the origin I fetched `VERSION-ONE`, changed the file upstream to
`VERSION-TWO`, rebuilt: `-> VERSION-ONE`, in 0.3 s, no refetch, no warning. Same with
a `file://` origin. That is the right behaviour for a *pinned* input and the worst
possible behaviour for an unpinned one: **two developers who first built on different
days build different browsers and neither can tell.** The output is a
94,000-entry `constexpr Array` of domains that get forced to HTTPS — a silent
difference in security behaviour, not in a log line.

**Second, and this is the number that settles it:** the table moved *while I was
working on this*. The file this machine's CMake configure fetched from `main` and the
one `http_file` fetched from `main` today differ by one entry:

```
< static constexpr Array<HSTSPreloadEntry, 94627> s_hsts_preload_entries { {
> static constexpr Array<HSTSPreloadEntry, 94626> s_hsts_preload_entries { {
-     HSTSPreloadEntry { "service.gov.scot"sv, true },
```

One domain left Chromium's preload list, so `HSTSPreloadData.cpp` is 53 bytes shorter,
and the byte-parity claim this whole document rests on would have failed for a reason
that has nothing to do with the migration. And the *pinned* tag is not a way out
either: `139.0.7258.5` yields **168,593** entries against today's **94,626** — the
list was pruned hard in between, so pinning unilaterally on the Bazel side doesn't
drift, it just diverges by 74,000 entries.

So row 1 is out (an unpinned `http_file` is the only input in the overlay whose
staleness would be invisible), row 3's *tag* is out (it diverges by 74,000 entries),
and what is left is row 3 with a **commit**: `3d75766` — the newest commit touching
the path, i.e. what `main` serves — whose bytes are identical to CMake's download.
That is the pin that shipped. The rule I was reaching for and got backwards on the
first pass: **pin what the other build system is serving today, and prove it with
`cmp`; do not pin what looks canonical.** A release tag looks like the responsible
choice and is the one that breaks parity.

Two shortcuts I tested and rejected, both of which look like simplifications and one
of which I would have shipped:

- **`--depth 1` on the vcpkg clone.** 8.7 MB instead of 121 MB, and `read-tree` even
  succeeds for some ports — then resolution fails on ffmpeg and harfbuzz with
  `failed to unpack tree object` and vcpkg's own advice, `Try again with a full vcpkg
  clone`. The pinned versions' port trees live in **history**, not at the baseline
  commit; that is what a version database is.
- **Dropping `builtin-baseline`** so `.git` is not needed at all. vcpkg then resolves
  against the checked-out `ports/` and needs no `read-tree` — it "works". It also
  **silently moves 10 dependencies**: ffmpeg 7.1.1#5 → 8.1.2#3, harfbuzz 10.2.0 →
  14.2.1#2, mimalloc 2.2.7 → 3.4.3, plus zlib, freetype, dbus, fontconfig, libedit,
  libwebp and cpptrace. A "fix" for a hermeticity blocker that changes ten dependency
  versions is the same class of error as everything else in this document, dressed as
  simplification.

The positive control for all of it: a **fresh** full clone at the baseline, driven by
the same manifest, resolves all **78 ports to exactly the versions this dev checkout
resolves** (`diff`, 0 differences). The checkout carries no local state beyond the
ref — which is precisely why a prefetch step is sufficient and a rule is not needed.

### Why not a git submodule?

The obvious question, since a submodule is git's own answer to "vendor another repo at
a pinned commit" and it would give the cloner `Build/vcpkg` with a working `.git` from
`git clone --recurse-submodules`. It **does** work mechanically — I checked, because
the `.git` here is load-bearing and a submodule's `.git` is not a directory but a
*gitfile* (`gitdir: ../../.git/modules/Build/vcpkg`). vcpkg's
`git --git-dir .git read-tree <tree>` follows that indirection fine: `READ-TREE OK`.
So "submodules break vcpkg" is not the reason.

The reason is that **a submodule pins the wrong thing.** A submodule pins one commit
and gives you its *checkout*; vcpkg's manifest pins a baseline commit and then reads
**history behind it**. Ladybird's `vcpkg.json` carries 45 `overrides`, and **14 of
them name a version that is not what `ports/` contains at the baseline**:

| pinned in `vcpkg.json` | what `ports/` holds at the baseline |
|---|---|
| ffmpeg 7.1.1#5 | 8.1.2#3 |
| harfbuzz 10.2.0 | 14.2.1#2 |
| mimalloc 2.2.7 | 3.4.3 |
| qtbase 6.10.0#1 | 6.11.1#1 |
| freetype 2.13.3 | 2.14.3 |
| simdutf 9.0.0 | 8.2.0 *(older than the pin)* |
| …plus zlib, dbus, fontconfig, libedit, libwebp, libtommath, cpptrace, angle | |

Concretely: ffmpeg 7.1.1#5's port is git-tree `0988005f…`, while
`HEAD:ports/ffmpeg` at the baseline is `c40aaa40…`. The bytes vcpkg builds are
**not in the working tree at any single commit** — they are extracted from the object
database by `read-tree`, per port, per pinned version. That is what the version
database *is*, and it is why `--depth 1` fails with vcpkg's own `Try again with a full
vcpkg clone`: shallow gives you the tree, and the tree is not the pin.

So a submodule would deliver exactly the state that is *insufficient* — the baseline
checkout — while still requiring the full history behind it to be present, and it
would add costs of its own: the same 119 MB in `.git/modules` (no saving), plus
`Build/vcpkg` is inside a `Build*/`-ignored path that vcpkg fills with ~3 GB of
`downloads/`, `installed/`, `buildtrees/` scratch, so every cloner's `git status`
would show the submodule dirty forever (needing `ignore = dirty` in `.gitmodules` to
paper over it), and `git submodule update` would fight vcpkg for who owns the
directory. And a submodule still would not produce the `vcpkg` **binary** — that is
not tracked in the repo; it is bootstrapped from a tag+SHA512 pinned in
`scripts/vcpkg-tool-metadata.txt`. `Meta/ladybird.py vcpkg` does that too.

The generalizable point, and the reason this is worth a section rather than a
footnote: **`Build/vcpkg` is not a vendored dependency, it is a package manager's
cache directory that happens to be a git checkout.** Every instinct that treats it as
"a pinned copy of another repo" — submodule, `git_repository`, `http_archive` of a
tarball, `--depth 1` — pins the checkout and loses the history, and the history is the
dependency. Getting this right is what `builtin-baseline` + `overrides` means, and it
is why the answer to "how do we get it" keeps coming back to *run the project's own
bootstrap*.

## Finding 37: the overlay was not reproducible, and the thing that proved it was a `cp`

Asked to get the tree onto another machine, I reached for the obvious answer — publish
the branch, `cp -r workspace/. ladybird/` — and then ran it on an empty directory
instead of describing it. Four things were wrong, and only the first was one I could
have found by reading.

**Nothing recorded which Ladybird commit the overlay describes.** The generated BUILD
files name ~1,961 LibWeb compile inputs and 665 IDL bindings *by path*; they were
generated from exactly one upstream tree. That tree's sha appeared nowhere — not in
the README, not in `cmake2bazel.json`, not in a comment. Every parity claim in this
document is relative to a commit the document never named. This is the same class as
the `--depth 1` and release-tag mistakes: **a pin that is not written down is not a
pin**, and the reason it survived so long is that my working copy *was* the pin.

**The interesting one: the overlay and upstream's bootstrap fight over `Build/vcpkg`.**
`Build/vcpkg/BUILD.bazel` is an overlay file, so a `cp -r` creates the *directory*
`Build/vcpkg`. Upstream's `Meta/Utils/build_vcpkg.py` then does:

```python
if not vcpkg_checkout.is_dir():
    git clone …
else:
    bootstrapped = git -C Build/vcpkg rev-parse HEAD
```

The directory exists, so it takes the `else`, and `git -C Build/vcpkg rev-parse HEAD`
— with no `.git` inside — **walks up to Ladybird's own repository** and cheerfully
returns *Ladybird's* HEAD. It then tries to check vcpkg's baseline out of the Ladybird
repo: `fatal: unable to read tree (40f3c709…)`. Two correct programs, one wrong
composition: upstream infers "cloned" from `is_dir()`, and the overlay's job is to put
a file in that directory. **`git`'s upward search for `.git` is what turns a missing
directory into a wrong answer instead of an error** — the same property that makes
`git -C` convenient makes it unsafe as an existence check. The fix is ordering
(prefetch, *then* stage that one file), and `apply_overlay.sh` defers it and explains
why at the point of deferral.

The other two were mundane and would have cost someone an afternoon: `bazelrc.txt`
must be renamed to `.bazelrc` (stored under a different name precisely so a `cp -r`
cannot be mistaken for a working build — and then the recipe relies on a human
remembering the rename), and the two upstream patches must be *applied*, not merely
shipped.

So the deliverable is a script, and the part worth keeping is `--verify`, which checks
what a file copy cannot: HEAD is the pinned commit, all 44 files are byte-identical,
the patches are applied (`git apply --check -R` succeeding is the proof — a patch that
reverse-applies cleanly is already in the tree), and the `.sh` files still have their
executable bit. That last check exists because of an earlier bug of exactly this shape:
scripts committed `100644` while my dev tree had them `+x` by hand, so only a fresh
clone failed, and only at action time. **The general rule this migration keeps
rediscovering: my working tree carries state git does not, and the only way to find it
is to reconstruct the tree somewhere else and diff.** `apply_overlay.sh /tmp/lbfresh2`
now produces a tree byte-identical to the one that renders, which is the first time
that sentence has been checked rather than assumed.

## Finding 38: the pin recorded what my machine lacked, not what the build needs

Ulf's clone failed where mine never could:

```
vcpkg_build: distfile MISSING FROM INDEX:
    .../ninja-build/ninja/releases/download/v1.13.2/ninja-linux.zip
error: there were no asset cache hits, and x-block-origin blocks trying the
       authoritative source
```

The 76-distfile pin came from *instrumenting vcpkg's own downloader* (finding 28) —
the strongest evidence available, and the thing I have leaned on hardest in this
document, because the capture cannot invent a URL and cannot miss one vcpkg asked
for. It has one blind spot, and it is not in the instrument, it is in the
**subject**: `vcpkg_find_acquire_program` probes the host *before* downloading. My
machine has `/usr/bin/ninja` at exactly 1.13.2 — the version vcpkg wants — so vcpkg
never asked for ninja, so the capture never saw it, so the pin never had it. Nothing
was broken. The observation was faithful; it was an observation *of my machine*.

The reason this went unnoticed is the reason it is worth a finding: **cmake is in the
pin, and only by luck.** The host cmake is 4.2.3 against the required 4.4.0, so vcpkg
*did* download that one — and its presence made the whole class look covered. One
member of a category being present by accident is what a partial pin looks like from
the inside.

Two general shapes, both of which I had already written down in weaker forms:

- **An instrument that records what a program *did* cannot pin what the program
  *would do elsewhere*, when the program's behaviour depends on the machine.** The
  capture is exact about vcpkg's requests and silent about vcpkg's *decisions*.
  Finding 36 said "a green check has to be compared against something it did not
  produce"; the comparand here is vcpkg's own tool metadata,
  `scripts/vcpkg-tools.json`, versioned inside the checkout at the baseline, carrying
  url + sha512 + archive name for every tool on every platform. That is a **pin**
  rather than an observation, so it is complete regardless of what is installed
  anywhere.
- **Host-tool discovery is a hermeticity boundary that looks like a convenience.**
  Every `find_program`/`find_package` is a place where the build's inputs depend on
  the machine, and a capture-based pin will silently record the *complement* of
  whatever is installed. The pin's contents should not be a function of one
  machine's `/usr/bin`.

The fix that first suggested itself — derive the tools from `vcpkg-tools.json` at emit
time — was wrong, and **two existing tests caught it before Ulf could**: the emitter's
central promise is that *the committed pin alone regenerates every Bazel file with no
vcpkg checkout, no CMake and no network*, and reading vcpkg's metadata during emit
quietly made a vcpkg checkout a requirement again. So the derivation is a separate,
deliberate step (`--capture-tools`) whose output is committed as
`Meta/vcpkg_tool_assets.tsv`, exactly like the asset capture; emitting unions it in
and, *when* a checkout happens to be present, cross-checks it and warns if the
committed pin has gone stale. That is the same division as everywhere else here:
regenerating a pin may use the world, consuming one may not.

Scoping, stated because a reviewer should not have to infer it:
`vcpkg-tools.json` also pins dotnet, node, powershell-core, azcopy, gsutil, coscli and
nuget — ~400 MB for tools no port in this closure invokes (only the unrelated
`vbs-enclave-tooling-codegen` does). `BUILD_TOOLS` is `("cmake", "ninja")`, the two
`scripts/detect_compiler` needs before any port builds at all, and the emitter
*reports* the ones it skipped rather than hiding the decision.

Verified: the ninja `http_file` fetches and integrity-checks (sha512 confirmed against
upstream independently of Ulf's error text), `bazel query 'deps(//:vcpkg_installed, 1)'`
now lists **78** distfiles including ninja, and the index the asset script reads
resolves that hash to the Bazel-fetched file — the exact lookup that failed on his
machine. Suite 182/182, six new tests, one of which is the regression test for the
bug report itself.

## Finding 39: the pin fixed one class; the next two failures were a different class

Finding 38's fix was correct and did not survive contact. Ulf pulled it, rebuilt, and
got — twenty minutes in, from inside `libvpx`:

```
CMake Error at scripts/cmake/vcpkg_find_acquire_program.cmake:201 (message):
  Could not find nasm.  Please install it via your package manager:
```

Installed nasm, rebuilt, twenty more minutes, from `gperf`:

```
CMake Error at .../share/vcpkg-make/vcpkg_make.cmake:108 (message):
  gperf currently requires the following programs from the system package
  manager:

      autoconf autoconf-archive automake libtoolize
```

Same blind spot as finding 38 — *the capturing machine had the tool* — but a
different **class**, and I had assumed the class was closed. Finding 38's fix reads
vcpkg's `scripts/vcpkg-tools.json` and pins url+sha512 for the tools vcpkg fetches
for itself. That works only for tools vcpkg *can* fetch. On Linux:

```cmake
set(program_name nasm)
set(apt_package_name "nasm")
if(CMAKE_HOST_WIN32)
    set(download_urls "https://www.nasm.us/.../nasm-3.01-win64.zip" ...)
endif()
```

Three URLs and a sha512 — **all inside the Windows branch**. On Linux there is
nothing to pin. vcpkg probes the host, does not find it, and stops. Six ports in
this closure need it (`dav1d`, `ffmpeg`, `libjpeg-turbo`, `libvpx`, `openh264`,
`openssl`). So the honest statement is not "the pin is incomplete" but **"this is
the boundary of the port"** — and the thing that was actually broken was not the
hermeticity, it was *how you find out*.

**Three classes of input, not two.** Ring 2 had a two-box model — distfiles Bazel
fetches, and vcpkg's own tools (finding 38's pin). The third box is tools that can
only come from the host, and it needs different treatment because there is no URL
to put in it. Naming a gap is not fixing it; but an unnamed gap costs 20 minutes
per member to discover, one at a time, in an error that points at the wrong place.

**And it has two mechanisms, which is why my first attempt missed half of it.**
I generalised from `nasm`, shipped a scan of `vcpkg_find_acquire_program` call
sites, and that would not have caught the autotools failure at all: `vcpkg-make`
never calls `vcpkg_find_acquire_program`. It calls bare
`find_program(AUTORECONF NAMES autoreconf)` and raises `FATAL_ERROR` with an apt
line. Worse, it does so from a **helper port**, so the error names `gperf` while
the requirement lives in a file `gperf` does not mention. A scan built from one
example is a scan calibrated to one example.

So the derivation covers both, and the list is *derived* from vcpkg's own scripts
(`emit_vcpkg_bazel.py --host-tools` -> committed `Meta/vcpkg_host_tools.tsv`),
never hand-written: a baseline bump that adds a requirement is a
regenerate-and-review, not a rediscovery.

**Most of the work was suppressing false positives, and that is the finding.** A
preflight that demands packages you do not need is one the third person deletes.
Four separate ways the naive scan cried wolf, each needing a real distinction:

- **`CLANG`.** Both call sites are behind `if(... STREQUAL "MSVC")`. Reading call
  sites without evaluating their guards demands a 2 GB toolchain on every Linux
  machine.
- **`openssl`'s `NASM` and `CLANG`.** In `ports/openssl/windows/portfile.cmake` —
  guarded by nothing in the file, only by the `include()` in its parent. The
  platform split is at the **path** level, so the path has to be read.
- **`else()` after a negated test.** `dav1d` is `if(NOT VCPKG_TARGET_IS_WINDOWS)
  ... else()` — that `else` *is* the Windows branch. Treating every `else()` as
  reachable imports the Windows-only `GASPREPROCESSOR`.
- **`angle`'s `mesa-common-dev`.** A `message(WARNING)`. The portfile *also* has an
  unrelated `FATAL_ERROR` about architectures, so a file-level "does it contain
  both a FATAL_ERROR and an apt line" check staples them together. **Advice is not
  a requirement**; the check anchors on the text of the `FATAL_ERROR` itself.

Two entries can only be *named*, not verified: `autoconf-archive` ships m4 macros
and `libltdl-dev` ships headers, so there is no binary to probe. They are reported
as unverifiable rather than assumed satisfied — finding 35's rule again, in a third
place: a check that cannot fail must not look like a check that passed.

Verified negatively, which finding 38 could not be (`sudo` hangs in this sandbox,
so I could not hide `/usr/bin/ninja`). Here the probe is `command -v`, so a
restricted `$PATH` *is* a machine without the tools:

```
vcpkg_build: MISSING host tool: libtoolize|glibtoolize (apt: libtool)
vcpkg_build:     needed by: vcpkg-make
vcpkg_build: MISSING host tool: nasm (apt: nasm)
vcpkg_build:     needed by: dav1d,ffmpeg,libjpeg-turbo,libvpx,openh264
vcpkg_build:     sudo apt install libtool nasm autoconf-archive libltdl-dev
```

Both, from one run, in one second, with the ports that need each and one pasteable
line — against the two failures that cost Ulf 40 minutes to learn two package
names. Confirmed in a real `bazel build //:vcpkg_installed`: the TSV is a declared
input (`aquery` shows `Meta/vcpkg_host_tools.tsv`), and the action ran the
preflight and proceeded into the build. Suite 193/193, 11 new tests — one per false
positive above, because each was a real bug in my own derivation.

**The retrospective bit.** The environment notes at the bottom of this document
have said "plus autoconf/nasm/glslang/mesa GL dev libs" since the beginning. The
requirement was *documented and never checked* — so it was invisible to everyone
who did not read the bottom of a 2,300-line file, which is everyone. Prose in a
case study is not a preflight. `glslangValidator` is the same shape and is still
open: two genrules in `codegen_root.bzl` name `/usr/bin/glslangValidator`, and
`vcpkg_installed` does not ship it.

## Finding 40: nine failures, one bug — a host requirement upstream checks and my overlay inherited silently

Ulf's Ubuntu 24.04 machine ran the Bazel-built Ladybird headless and got a SIGSEGV
from the GUI:

```
ladybird(...)
QApplicationPrivate::init()
QXcbConnection::initializeScreens(bool)
QXcbConnection::handleScreenAdded(...)
--> SIGSEGV in libQt6Core
```

and offered a deal: *"if you can figure out how to make it work, then I won't
upgrade."* That machine is the most valuable thing in this migration. It has found
**eight** real defects (findings 37, 38, 39 and the gaps between them) that this
sandbox is structurally incapable of finding: gcc 15.2, Qt 6.10.2, ICU 78,
`nasm`/`perl`/`glslang`/`libdrm` all present, every requirement silently satisfied.
A machine that *disagrees* with mine is a test oracle, not an inconvenience, and the
only way to keep it is to stop breaking it.

### The bug

The binary links Qt from the Bazel repo and loads Qt's **plugins** from wherever
`libQt6Core`'s baked-in prefix points — on his box, the *distro* Qt's plugin
directory. Two different Qt builds in one process.

Qt does not link its QPA platform plugin; `QApplication`'s constructor `dlopen`s it.
Where it looks is decided inside `libQt6Core`: `qt.conf` next to the executable, then
`QT_PLUGIN_PATH`, then the prefix compiled into the library. Qt 6.9.2's `qt_prfxpath`
is **empty** (`strings lib/libQt6Core.so.6.9.2`), so the prefix falls back to *the
directory of the executable* — and a Bazel binary's directory has no `platforms/`, so
the search falls through to the compiled-in system path and Qt loads
`/usr/lib/x86_64-linux-gnu/qt6/plugins/platforms/libqxcb.so` into a process whose
`libQt6Core` came from `bazel-bin/_solib_k8/...rules_qt++qt+qt...`.

rules_qt is not at fault: it wires up Qt's *link* half faithfully (one SDK, discovered
by `qmake -query`; headers, libs and moc all from it). Nothing wired up the *runtime*
half, because on the machine where the overlay was written there was nothing to
notice.

**Which way the skew points decides which failure you get.** Both halves reproduced
here, with a real X server:

| plugin vs. linked libs | Qt's version gate | outcome |
|---|---|---|
| plugin **older** | rejects it: `factoryloader: Ignoring QPA plugin due to mismatching Qt versions 395520 394240` | `no Qt platform plugin could be initialized`, clean abort |
| plugin **newer or equal-minor** | **passes** | plugin calls into an ABI it was not built against → SIGSEGV in `initializeScreens` → `handleScreenAdded` |

(Those integers are Qt versions: `(v>>16, (v>>8)&255, v&255)`, so 395520 = 6.9.0 and
394240 = 6.4.0.) The gate is the cruel part: it catches the harmless direction and
waves through the one that corrupts memory.

And on **my** box it passes. `QT_DEBUG_PLUGINS=1` on the Bazel-built binary here
scanned `/usr/lib/x86_64-linux-gnu/qt6/plugins/platforms` and loaded the **distro**
`libqxcb.so` — the exact same wrong lookup — while `libQt6Core.so.6.10.2` came from
Bazel's solib dir. Both are 6.10.2, so the ABI happens to match. **The bug was
present in every green GUI run this project has ever reported.**

Then the strongest evidence available: pointing `qt.local_repo` at the aqt **6.9.2**
SDK on this machine and rebuilding reproduced **his backtrace, frame for frame** —
`Ladybird::Application::create_platform_event_loop` → `QApplicationPrivate::init` →
`QXcbConnection::initializeScreens` → `handleScreenAdded` → a fault in libQt6Core at
`mov 0x8(%rdi),%rbx` with `rdi = 0`. Not a machine I cannot see any more.

### The exoneration that mattered

My first hypothesis was two ICUs: the binary needs `libicu*.so.78` (vcpkg) and aqt's
`libQt6Core` needs `libicu*.so.73`, both in one process, and `ld` even warns *"may
conflict"*. Wrong — worth recording because it is the kind of theory that sounds too
good to check. I linked ICU 78 **and** aqt's ICU 73 into one process with
`-Wl,--no-as-needed` and constructed a `QApplication`: `screens=1 qt=6.9.2`. ICU
coexists fine; two sonames are two libraries. (The fixed build now does exactly this
on purpose: `objdump -p` shows `libicu*.so.73` *and* `libicu*.so.78` in one binary,
and it runs.) Any earlier note here blaming "two ICUs" is superseded: it was never an
ICU problem, it was this same plugin/library provenance skew seen through a different
symptom.

### The fix: the Qt edge becomes self-contained, like `vcpkg_lib`

`qt_runtime.bzl`, and the shape is deliberately the one Ring 2 arrived at for vcpkg —
*the dependency arrives with the dep edge*:

1. **`qt_plugins`** (repository rule) reads **@qt's own generated `qtconf.bzl`** for
   `QT_INSTALL_PLUGINS` and symlinks every plugin the SDK ships into a repo, one
   `filegroup` per plugin type. Reading @qt's file rather than a path of my own is the
   whole point: the plugins cannot come from a different Qt than the libraries,
   because both names come from one `qmake -query`. Every type, not the four Ladybird
   needs today — a hand-picked list drifts, and a missing input method or file dialog
   is a defect nobody notices for a month.
2. **`qt_plugin_tree`** re-declares them as outputs of the package that holds the
   binary, so they land at `bazel-bin/plugins/<type>/*.so` — and, as `data` of
   `//:ladybird`, in the runfiles tree too.
3. **`qt_conf`** writes the file that redirects the search:

   ```
   [Paths]
   Prefix = .
   Plugins = plugins
   ```

   Setting `Prefix` **replaces** the compiled-in prefix, so `/usr` is not outranked,
   it is *never scanned* — `QT_DEBUG_PLUGINS=1` on the fixed build shows zero scans
   of any `/usr` directory. `Prefix = .` is what makes one file correct in both
   layouts, because Qt resolves it against the directory of the executable via
   `/proc/self/exe`, so `bazel-bin/ladybird` and the runfiles tree's symlink to it
   both land on the staged tree.
4. **`runtime_libs`** carries the private libraries an SDK bundles beside Qt.

That fourth piece was the hard one, and it is where the loader stopped being
intuition and became a measurement. aqt's `libQt6Core` needs `libicui18n.so.73`,
which exists only inside the SDK, so the binary died in `ld.so` before `main()` and
`LD_LIBRARY_PATH=<sdk>/lib` was the workaround — a workaround a human has to remember
is a bug that has been rounded down to a habit. **No rpath on the binary can fix
it**, for two reasons I only believed after reducing them to three generated `.so`
files:

- `libQt6Core` finds its own ICU through `RUNPATH $ORIGIN`, and `$ORIGIN` is the
  directory the loader **opened the object by** — Bazel's solib dir, not the SDK. (`ldd`
  on that very symlink resolves ICU happily, because `ldd`'s `$ORIGIN` is the
  realpath's directory. That near-miss is what made this look like a path problem.)
- Adding the SDK dir to *our* rpath does not help either: `DT_RUNPATH` is consulted
  only for an object's own direct dependencies, and while `DT_RPATH` **is** inherited
  by transitive loads, an intermediate object that has a `DT_RUNPATH` of its own
  blocks the inherited `DT_RPATH` entirely. `libQt6Core` has one. All four
  combinations measured before believing it.

So the fix is not a search path at all: make the SDK's private libraries real link
inputs, so **Bazel** stages them and the binary's own runpath — the one glibc will
consult, because they are now direct dependencies — resolves them. The list is
derived, not written down: DT_NEEDED of the SDK's Qt modules ∩ the non-Qt `.so` files
beside them. For aqt 6.9.2 that is exactly `libicui18n/libicuuc/libicudata.so.73`;
for a distro Qt it is empty and the target degenerates to nothing.

Five things I got wrong on the way, each caught by a probe rather than by reasoning:

- **The staged plugins must be SYMLINKS, not copies.** A plugin needs Qt libraries
  the binary does not link (`libqxcb.so` → `libQt6XcbQpa.so.6`), and aqt's plugins
  carry `RUNPATH $ORIGIN/../../lib`, resolved from the object's real path. Copying
  breaks exactly that; the distro's plugins have no `RUNPATH` at all, so a *copied*
  distro plugin resolves `libQt6XcbQpa.so.6` from `/usr` — reintroducing the bug
  through the fix.
- **`plugins/` is a substring of `qt_plugins/`.** My path-stripping matched inside the
  *repository name* and staged everything one directory too deep
  (`bazel-bin/plugins/plugins/...`), which built cleanly and pointed `qt.conf` at an
  empty tree. Found by looking at the output, not by the build failing.
- **"Non-Qt libraries beside libQt6Core" is the whole system on a distro Qt.** Its lib
  dir *is* `/usr/lib/x86_64-linux-gnu`, so the first version of the derivation
  proposed linking 56 `cc_import`s including `ld-linux` into the binary. It is a
  system directory, so it "worked" — which is precisely the kind of accident this
  finding is about. The discriminator is whether the Qt lib dir is itself a default
  loader directory.
- **An embedded `:/qt/etc/qt.conf` Qt resource does not work.** Tempting, since we
  already run `rcc`, and Qt 6.9.2 does look for that path — but the resource is
  registered by a static initialiser in the binary and every variant I built ignored
  it, while a file on disk worked first try.
- **`--no-as-needed` is load-bearing.** The binary references no ICU 73 symbol (it has
  its own ICU 78), so the linker drops the `DT_NEEDED` as unused and the staging
  silently stops working.

### The class, which is the finding

Every one of the nine failures Ulf's machine has produced is the same sentence:

| # | symptom on his box | the host requirement | who declares it |
|---|---|---|---|
| 1 | `vcpkg: No such file or directory` | the `Build/vcpkg` clone | `Meta/ladybird.py vcpkg` |
| 2 | `fatal: unable to read tree` | that clone's `.git`, at a baseline | vcpkg's baseline resolution |
| 3 | HSTS table differs | Chromium's JSON, *unpinned* | `Meta/CMake/hsts_preload.cmake` |
| 4 | `glslangValidator: No such file` | `glslang-tools` | nothing — my genrule hardcodes `/usr/bin` |
| 5 | `ninja-linux.zip MISSING FROM INDEX` | `ninja` ≥ 1.13.2 | `vcpkg_find_acquire_program` (probes, then downloads) |
| 6 | `Could not find nasm` | `nasm` | `vcpkg_find_acquire_program` (Linux: no URL to pin) |
| 7 | `gperf requires autoconf autoconf-archive automake libtoolize` | autotools | `vcpkg-make`'s bare `find_program` |
| 8 | libdrm headers not found | `libdrm-dev` | CMake `pkg_check_modules(... REQUIRED)` |
| 9 | **SIGSEGV in `initializeScreens`** | **Qt ≥ 6.9, and its plugins** | **`find_package(Qt6 6.9 REQUIRED)`** — upstream checks it; my overlay did not |

Not nine bugs. One bug, nine times: **a host requirement that upstream declares and
checks, which the Bazel overlay inherited without inheriting the check.** CMake's
`find_package`, `pkg_check_modules(REQUIRED)` and `vcpkg_find_acquire_program` are not
ceremony — they are the *preflight*, and translating a build system means translating
its preflight, not only its compile lines. Nine times I translated the commands and
dropped the assertion; nine times the machine that disagreed with mine was the one
that told me.

So `qt_plugins` also carries the floor. `UI/Qt/CMakeLists.txt` says
`find_package(Qt6 6.9 REQUIRED COMPONENTS Core Widgets)`; the repo rule now fails at
fetch time with the version it found, the prefix it found it in, the package to
install, and a warning not to mix SDKs — the finding-39 mechanism (derive the
requirement from the source of truth, check it where it is needed, name the fix in the
error) extended from vcpkg's driver to the overlay's own build. `glslangValidator`
(#4) is the last member of the table with no check at all.

### One more wrong path, found by walking the recipe

Verifying the GUI meant following the README's own staging recipe, which promptly
failed with `UNEXPECTED ERROR: stat: No such file or directory at
UI/Qt/WebContentView.cpp:1047` — the theme `.ini`. Two of its paths were wrong, both
in the same way as this finding: written down instead of derived. The vcpkg tree it
copies from is built in the **exec** configuration, so `bazel-bin/vcpkg_installed`
does not exist at all (`k8-fastbuild-exec` is the directory); and the resource root
is not `<bindir>/share/Lagom` but `<bindir>/../share/Lagom`, because
`LibWebView/Utilities.cpp`'s `find_prefix()` takes the **parent** of the binary's
directory. Both are now `bazel info` / `bazel cquery --output=files` invocations,
which answer for whatever configuration you are on. Prose describing an output path
is a pin with no checker (gap 5, closed).

**Verified by removal**, which is the only kind of verification this document
accepts. With `/usr/lib/x86_64-linux-gnu/qt6/plugins` hidden behind an empty tmpfs and
**no `LD_LIBRARY_PATH`**: the aqt build loads `libqxcb.so` from the aqt SDK (previously:
the distro's), zero `/usr` directories are scanned, and against the host Qt the GUI
opens its window on Xvfb and stays up. Headless still renders the reference page.
Passing because the host happened to agree is how all nine of these got here.

## Finding 41: the plugin fix was right and the crash stayed — `-fPIE` gave `qApp` a copy relocation

Finding 40 fixed the plugin path, Ulf updated his tree, and the GUI segfaulted in
`QXcbConnection::initializeScreens` **again**, with the same backtrace. That is the
most useful shape a bug can have: it proves the previous fix was necessary, not
sufficient, and it means the earlier reasoning had a passenger.

What the new evidence ruled out, before any theory:

- `QT_DEBUG_PLUGINS=1` showed the scan hitting `bazel-out/.../bin/plugins/platforms`
  and every plugin resolving to *his own* SDK's realpath. `/usr/lib/x86_64-linux-gnu/qt6`
  was never touched. Finding 40's fix was working exactly as documented.
- `info sharedlibrary` showed **one** `libQt6Core` in the process, and the `Qt 6.9.2
  (x86_64-little_endian-lp64 ... GCC 10.3.1)` build strings of the Bazel-staged copy and
  the SDK copy were byte-identical. Not a version mix, not two Qts.
- The **offscreen** QPA plugin crashed identically. Whatever this was, it was not the
  platform plugin, not xcb, not the X server, not a screen enumeration quirk.

### The bug

At the fault, `rdi = 0` and `si_addr = 0x8`, on `mov 0x8(%rdi),%rbx` — inside
`doActivate` (`libQt6Core` + 0x1e89ce, just past
`QObjectPrivate::ConnectionData::deleteOrphaned`), reached from
`QWindowSystemInterface::handleScreenAdded` → `QGuiApplication::screenAdded`. Qt was
emitting a signal from a **null `qApp`**, in the middle of `QApplication`'s own
constructor, which had just set it.

`readelf -rW` says why, in three lines:

| object | relocation against `_ZN16QCoreApplication4selfE` |
|---|---|
| aqt `libQt6Core.so.6.9.2` | **none** — accessed PC-relative, its own BSS |
| aqt `libQt6Gui.so.6.9.2` | `R_X86_64_GLOB_DAT` — read through the GOT |
| `bazel-bin/ladybird` | **`R_X86_64_COPY`** — definition materialised in the exe |

A copy relocation moves the *definition* of an extern data symbol into the
executable's BSS and repoints every GOT slot at it. So `QApplication`'s constructor
made Core write `self` in **Core's** BSS, while Gui read the **executable's** BSS,
which was still zero. The first emit through it dies. His distro Qt 6.4.2 *does*
carry a `GLOB_DAT` for `self` in Core, so both halves agree there and the bug cannot
appear — which is why this only ever showed up against an official SDK.

The cause is one flag. CMake puts `-fPIE` on every executable target
(`CMAKE_POSITION_INDEPENDENT_CODE` + an exe), the capture recorded it faithfully, and
`emit_build_bazel.py` copied it into `copts` on all seven generated `cc_binary`
targets. Bazel appends per-target copts **after** the `.bazelrc`'s `--copt=-fPIC`,
and for GCC the last of the pair wins: the UI/Qt objects compiled `-fPIE` while every
library around them compiled `-fPIC`. Under `-fPIE` GCC may reference extern data
directly instead of through the GOT, and the linker turns that into the copy
relocation. Qt is built with `reduce_relocations` (in `mkspecs/qconfig.pri`;
Debian's is not), which is what makes Core's side of the disagreement PC-relative.

**Qt diagnoses this and the diagnosis could not fire.**
`qcompilerdetection.h` has `#error "-fPIE is not sufficient if Qt was configured with
-DFEATURE_reduce_relocations=ON ... Compile your code with -fPIC and without -fPIE"`
— guarded by `#if ... || defined(__PIC__)`. Bazel passed **both** flags, so `__PIC__`
was defined at preprocess time and the guard never triggered. The build was clean and
the binary was broken: a compiler-authored checker, defeated by flag order.

### Reduced, then fixed

Eight lines, no Ladybird, no Bazel — `QGuiApplication` plus one `printf`, against the
same SDK:

```
g++ -fPIE -pie  ... -> R_X86_64_COPY for self -> SIGSEGV (identical stack)
g++ -fPIC -pie  ... -> GOT, no COPY reloc    -> "instance=0x… screens=1"
```

`-Wl,-z,nocopyreloc` is **not** an alternative: it converts the same defect into
`Symbol _ZN16QCoreApplication4selfE causes overflow in R_X86_64_PC32`. The fix is to
stop asking for `-fPIE`, which is also not a divergence from CMake's semantics —
Bazel compiles a `cc_binary`'s objects PIC and links `-pie`, which is what
`POSITION_INDEPENDENT_CODE` was asking for. So the flag is dropped in the generator
(`DROPPED_TARGET_FLAGS`), not in the generated file, because `BUILD.bazel` is output
and a hand-edit survives exactly one regeneration.

**Verified by removal**, on both Qts and all six executables: `R_X86_64_COPY` count
39 → **0** (including `qApp`, and incidentally `stdout`, `QString::_empty` and 20-odd
`staticMetaObject`s, every one of them the same latent hazard). Against the aqt 6.9.2
SDK the GUI now starts where it previously segfaulted under *both* the xcb and the
offscreen plugin; against the distro Qt, unchanged. Guarded by
`tests/test_pie_copy_relocation.py` (5 tests), which asserts the generated file is
`-fPIE`-free, that the drop lives in the emitter and is consulted, that the reason is
written at the drop site, and that global `--copt=-fPIC` — the thing that makes
dropping `-fPIE` correct — is still in `bazelrc.txt`.

**What this cost, and the lesson.** Two rounds on Ulf's machine for one crash, because
after finding 40 I read "SIGSEGV in `initializeScreens`" as "the plugin bug, again"
instead of as an unexplained fault. The plugin evidence was *checkable in one command*
(`QT_DEBUG_PLUGINS=1`) and I asked for it only on the second pass. A backtrace that
survives a fix is not the same bug; the frames say where a process died, never why.
It also makes finding 40's own "verified by removal" honest about its scope: hiding
the host plugin directory proved the plugins were right, and proved nothing at all
about the objects that linked them. Two Qts in one process was a real bug. One Qt with
two copies of one pointer was the next one down.

## Plan for the rest

- **Ring 1c — BUILD generation to compile+link parity, bottom-up.** AK →
  LibCore/LibUnicode/… → LibJS/LibGfx → LibWeb → LibWebView → Services/UI →
  `Ladybird`. Per layer: generate `BUILD.bazel` from the model, aquery, diff,
  triage, fix. Mechanical and delegatable per library once the pattern (AK) is
  set; the two findings above are the shared plumbing to get right first.
- **Ring 2 — Bazel-driven vcpkg (design settled and validated end to end, see
  findings 23 and 24).** `http_file` per distfile in `MODULE.bazel` + `vcpkg
  install` wrapped so that Bazel owns fetching and vcpkg is only the build
  recipe. *Not* BCR — see finding 23 for why re-sourcing the deps would destroy
  the parity baseline this migration is built on. Finding 24 closed the loop by
  swapping the reference tree out and building the browser against the
  Bazel-fetched one; finding 33 then packaged it, so the build no longer needs a
  local `Build/full` for the vcpkg `.so`s at all. What stands between this and
  *"can someone else build it on their machine"* is now Rust alone: the prebuilt
  crate archive and `flapc`.

  **Where the vcpkg invocation goes — decided.** Not a `repository_rule`, which
  is what I first said reflexively. Repository rules run at load time,
  single-threaded, unsandboxed, with no remote execution, no remote cache and no
  fine-grained invalidation; a 45-minute C++ build does not belong there. Split
  it by what each layer is good at: **fetching** stays at module level
  (`http_file` per distfile — that is precisely bzlmod's job, and it is what
  pins the hashes and shares the download cache), while **building** becomes an
  ordinary build action, so it is sandboxed, cacheable and remotable.

  **And wrapping vcpkg is explicitly a stepping stone, not the destination.**
  The on-mission answer for a *converter* is to consume vcpkg's resolution
  rather than its build: `vcpkg depend-info --format=list` already emits the
  full resolved graph with feature selections (47 ports), and every installed
  port ships a `.list` manifest enumerating its outputs (6,093 entries, 327
  libs, 3,791 headers across the tree) — enough to emit one Bazel target per
  port and build the deps with Bazel's own toolchain. That is the real target
  state; shelling out to the foreign build system is the thing any2bazel exists
  to replace. The reason to wrap first is sequencing, not preference: it gets a
  clone-and-build now, and nothing about it is permanent.

  Worth being explicit about what that later replacement costs, because it is a
  cost to *my verification method* rather than to any user: rebuilding the deps
  with Bazel's toolchain changes their flags and therefore their bytes, which
  destroys the byte-parity baseline findings 23/24 rest on. That is acceptable —
  Ladybird's CI does not care whether `libpng` is byte-identical, it cares that
  the browser renders, and finding 24 showed the render comparison is the
  stronger check anyway. Byte-parity was the right *ratchet* while building the
  migration; it is not the definition of done.
  Nothing remains after Ring 2: the 10 Rust crates and `flapc` are Bazel-built
  too (finding 34), from `Cargo.lock`'s own pins — and *not* via `rules_rust` /
  `crate_universe`, which was the plan here: cargo is the recipe worth keeping
  (the workspace resolution, the build scripts that run cbindgen), so it gets the
  vcpkg treatment rather than a reimplementation. (Qt — moc/rcc and the host
  include paths — is done, via `rules_qt`, now fetched by `archive_override` from
  upstream rather than a local path.)
  The full gap list is in [`examples/ladybird/README.md`](../examples/ladybird/README.md#known-gaps).

## Success gate — MET

A **running browser built by Bazel**. Compile+link parity per the diff is the
engineering check; a Bazel-built `Ladybird` that renders a page is the
acceptance test. Both are met: see "🏁 THE GATE" above — `bazel build
//:ladybird` renders HTML+CSS+JS byte-identically to the CMake reference, with
every process (UI, WebContent, Compositor, RequestServer, ImageDecoder)
Bazel-built, proven by removing the reference services and watching the CMake
binary fail while the Bazel one renders.

And with findings 33, 34 **and 35**, Bazel fetches and builds the 77 vcpkg ports,
the 10 Rust crates, `flapc`, `cranelift-compiler` and every generated header itself,
with vcpkg's own downloader reaching the network zero times. It is **not** yet a
clone-and-build: finding 36 lists the four inputs a fresh clone still lacks and the
one vcpkg port that bypasses the pin with `pip`; findings 38 and 39 add the two
classes of *host tool* that a capture on one machine structurally cannot see.
`Build/full` is now only the *converter's* input — the model the emitters read and
the baseline the parity harness diffs — not the build's, and this time that is
measured: **0 targets under `Build/full` in the `cquery` closure of all six
binaries** (down from 741), the three shim packages deleted outright, and all six
binaries rebuilt from scratch with CMake's generated tree moved off the machine,
rendering `--headless=text` *and* `--headless=layout-tree` byte-identically on
three pages including two that execute WebAssembly.

Findings 33 and 34 alone were **not** enough, and the gap between "I closed the
binary dependencies" and "the build does not read the tree" is the most
instructive part of this migration — see finding 35.

**Scoreboard:** 43 `cc_library` + 6 `cc_binary` targets; ~4,400 Bazel actions
from scratch (2,700 C++ plus the vcpkg and Rust rings);
LibWeb alone 1,961 TUs (1,273 checked-in + 688 generated) with **zero**
define/flag/include discrepancies vs CMake; 1,379/1,379 generated files
byte-identical -> now 1,408/1,408 with every one of the build's 586 ninja CUSTOM_COMMANDs accounted for (findings 25-27); 77 vcpkg ports and 154 crates.io crates fetched and built by Bazel; **0 targets under `Build/full` in the closure of all six binaries (down from 741) and 0 absolute host paths in the emitted BUILD files** (finding 35); a fresh clone still needs 4 inputs the overlay does not carry and one port reaches PyPI via pip (finding 36 — found by actually cloning), and the host tools vcpkg cannot download are
now named and preflighted rather than discovered one 20-minute build at a time
(findings 38, 39); the Qt runtime half -- the plugins Qt `dlopen`s -- wired to the
same SDK as the libraries (finding 40) and the `-fPIE` copy relocation that broke
`qApp` against any `reduce_relocations` Qt removed (finding 41, 39 -> 0
`R_X86_64_COPY`); 41 findings, 3 of them real any2bazel engine fixes with
regression tests.

**One number in this scoreboard was wrong for most of the migration, and it is
worth ending on.** "Clone and build" was claimed after the two *binary*
dependencies were closed, and the 741 remaining *header* dependencies went uncounted
because the shims that supplied them could not fail. Every "verified by removal" in
this document is only as strong as what was actually removed — which is the argument
for removal as a method, not against it: it is the only check here that ever found
this class of bug, and each time I widened what I removed, it found another.

## Environment notes (this sandbox)

- Toolchain via apt (needs passwordless sudo): cmake, ninja, ccache,
  build-essential, Qt6 (`qt6-base-dev` etc., 6.10.2), plus autoconf/nasm/
  glslang/mesa GL dev libs. **The vcpkg half of that list is no longer prose:**
  `Meta/vcpkg_host_tools.tsv` is derived from vcpkg's own scripts and checked
  before the build starts (finding 39). This line said "autoconf/nasm" for
  months while both failures below were waiting to happen — documenting a
  requirement is not checking it. Rust via rustup (`~/.cargo`). Node/vcpkg bootstrap
  per `Meta/Utils/build_vcpkg.py`.
- `~/lb-env.sh` sets `LADYBIRD_SOURCE_DIR`, `VCPKG_ROOT`, CA certs.
- Disk ~126G, RAM 16G, 16 cores. vcpkg from-source (no cache hit) for
  Skia/ANGLE is the RAM risk; the binary cache made the first configure cheap.
