# Case study / plan: migrating Ladybird to Bazel

Working migration of the [Ladybird](https://ladybird.org) browser engine
(CMake + vcpkg, C++23) to Bazel, driven by the any2bazel parity loop. This doc
is the durable plan and the running record of findings; the Bazel workspace
itself lives in the Ladybird checkout (`~/ladybird-work`), not in this repo.

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

- **`MODULE.bazel`**: bzlmod, `rules_cc` 0.2.17, `platforms` 1.0.0.
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

## Ring 1b — codegen byte-parity (DONE for LibWeb)

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
  (Result: 71/71 single-generator outputs + 1331/1331 bindings identical.)
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
   `generate_dom_tree.py` for `HTML/MediaControlsDOM` reads
   `HTML/MediaControls.css` at generation time, by following a `<link
   rel="stylesheet" href="MediaControls.css">` inside its declared input
   `MediaControls.html`. So the `.css` is a real input that **never appears on
   the generator's command line**. Our first `emit_codegen_bazel.py` derived
   `srcs` purely by scraping the command line, so it dropped the `.css`, and
   Bazel's sandbox failed loudly with `FileNotFoundError`.

   **Upstream CMake gets this right** — it lists the file explicitly in the
   generator's `dependencies`/`DEPENDS`
   (`Meta/CMake/libweb_generators.cmake`, in the `MediaControlsDOM.cpp`
   `invoke_py_generator` call), which is visible in the ninja edge and verified
   by `touch Libraries/LibWeb/HTML/MediaControls.css && ninja -n …
   MediaControlsDOM.cpp` → the generator re-runs. Nothing to patch upstream.

   The **real** lesson is an extractor invariant: *the build graph's declared
   dependency list is authoritative, the command line is not*. `srcs` must be
   the union of (command-line paths) and (the edge's `DEPENDS`), because a tool
   may read inputs it was never handed as arguments. `emit_codegen_bazel.py`
   now parses each ninja edge's dep list and folds in any in-package input the
   command line didn't mention. Tree-wide that rule matters for **3 of 46**
   generator edges — `MediaControlsDOM` plus the two ImageDecoder IPC endpoints
   (whose `.ipc` input reaches the command line only via a `../..`-relative
   path, i.e. the same class of mismatch). Scraping command lines is the
   tempting shortcut for any CMake→Bazel extractor and this is exactly where it
   breaks.

2. **Latent nondeterminism in the generator — fixed by sorting.**
   `generate_libweb_bindings.py` emitted the dependency-ordered dictionary
   structs of `MediaCapabilities.h` (`AudioConfiguration` vs
   `VideoConfiguration`) in an order that varied with `PYTHONHASHSEED`: the
   topological sort in `dictionaries_in_dependency_order()`
   (`Meta/Generators/libweb_bindings/to_idl_value.py`) iterated a **set** of
   dependency names, and `dependency_names_for()` /
   `GenerationContext.dictionary_type_names()` build that set as a comprehension
   — so DFS visit order, and hence emission order, followed Python's per-process
   string hash.

   My first response was to pin `PYTHONHASHSEED=0` on every codegen genrule.
   That restores byte-parity, but it's the wrong fix: it freezes the symptom into
   the build system and leaves the generator producing seed-dependent output for
   everyone else (CMake, `ninja`, anyone running the script by hand). **The
   generator should sort.** One line, upstream:

   ```python
   # Meta/Generators/libweb_bindings/to_idl_value.py, in dictionaries_in_dependency_order()
   -        for dependency_name in dependency_names_for(dictionary):
   +        # Sorted: dependency_names_for() returns a set, so iterating it directly
   +        # makes the emitted order depend on PYTHONHASHSEED.
   +        for dependency_name in sorted(dependency_names_for(dictionary)):
   ```

   Sorting the names (not the `Dictionary` objects) keeps the topological
   ordering correct — it only makes the tie-break among independent dependencies
   deterministic — and it is stable regardless of seed. Verified: the patched
   generator reproduces the CMake reference **1332/1332 files byte-identical
   under seeds 0, 1, 2, 7 and 12345**, whereas the unpatched generator diverges
   on `MediaCapabilities.h` at seed 1 while matching at seed 4. Patch:
   `examples/ladybird/upstream-sort-dictionary-order.patch`. **Filed upstream as
   [LadybirdBrowser/ladybird#10899](https://github.com/LadybirdBrowser/ladybird/issues/10899)** (this one is a genuine upstream bug,
   unlike finding 1).

   **Why this hid for so long, and the harness lesson:** the parity harness
   originally ran each generator exactly once, inheriting whatever seed the
   process had. A seed-dependent generator passes such a check most of the time —
   note seed 4 matches by luck — so "1331/1331 identical" was never evidence of
   reproducibility. The harness now takes `--seed N` and is swept over several
   seeds (46 commands / 71 outputs identical at seeds 1 and 3). *Determinism
   checks that don't vary the nondeterminism source prove nothing.* The
   `PYTHONHASHSEED=0` pin stays on the genrules, but now as defence-in-depth for
   hermeticity/remote-caching rather than as the thing holding parity up.

Remaining Ring 1b work is mechanical: run `emit_codegen_bazel.py` for the other
libraries with generators (LibJS bytecode, LibHTTP HSTS, the IPC endpoints under
LibRequests/LibWebView/Services, Compositor WebGL replayer) — same pattern, same
harness to verify.

## Ring 1c — per-library BUILD generation (in progress, pattern proven)

The 42 production libraries form a clean **13-layer dependency DAG** (L0 `AK`
→ … → L9 `LibWeb` → L11 `LibWebView` → L12 `webcontentservice`). Ring 1c walks
it bottom-up, generating a `cc_library` per lib from the reference model and
diffing to compile+link parity.

**Emitter (`examples/ladybird/emit_build_bazel.py`).** Reads
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
genrule output). Emitter: `examples/ladybird/emit_libweb_bazel.py` (mirrors the
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
$ bazel-bin/ladybird --headless=text examples/ladybird/PROOF-test-page.html
Hello from Bazel

JS says 2+2=4
```

HTML parsed, CSS applied, layout run, and **LibJS executed the page's script**
(`document.getElementById('out').textContent = 'JS says 2+2=' + (2+2)`).
`--headless=layout-tree` dumps the full box tree with computed geometry and font
metrics (`examples/ladybird/PROOF-render-layout-tree.txt`). Output is
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
    lever finding 7 needed for `#embed` data). Generated shader headers
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
    concrete argument for Ring 2's hermetic-toolchain/BCR direction.)

17. **Catch-all header libraries make every edit a world rebuild.** Because
    every target depends on `//:all_source_headers`, adding `UI/**/*.h` to its
    glob (needed for `<UI/Qt/Application.h>`) invalidated all ~2,700 actions —
    twice, once more for the UI autogen header target. Faithful to CMake's
    global `-I`, but it costs incrementality; a stricter per-dep header model is
    the fix (noted in finding 9 as future work, now with a measured cost).

Rust stays prebuilt-`.a` until Ring 2.

## Plan for the rest

- **Ring 1b (remaining libs).** Apply the proven emitter/harness to the
  non-LibWeb generator commands (LibJS/LibHTTP/IPC/Compositor). Same shape.
- **Ring 1c — BUILD generation to compile+link parity, bottom-up.** AK →
  LibCore/LibUnicode/… → LibJS/LibGfx → LibWeb → LibWebView → Services/UI →
  `Ladybird`. Per layer: generate `BUILD.bazel` from the model, aquery, diff,
  triage, fix. Mechanical and delegatable per library once the pattern (AK) is
  set; the two findings above are the shared plumbing to get right first.
- **Ring 2 (stretch) — real bzlmod deps.** Swap `cc_import` shims for BCR
  `bazel_dep`s where they exist + `rules_foreign_cc` otherwise, and build a
  find_package/vcpkg → BCR resolver adapter (the designed-but-unbuilt external
  resolver plug-in point).

## Success gate — MET

A **running browser built by Bazel**. Compile+link parity per the diff is the
engineering check; a Bazel-built `Ladybird` that renders a page is the
acceptance test. Both are met: see "🏁 THE GATE" above — `bazel build
//:ladybird` renders HTML+CSS+JS byte-identically to the CMake reference, with
every process (UI, WebContent, Compositor, RequestServer, ImageDecoder)
Bazel-built, proven by removing the reference services and watching the CMake
binary fail while the Bazel one renders.

**Scoreboard:** 43 `cc_library` + 6 `cc_binary` targets; ~2,700 Bazel actions;
LibWeb alone 1,961 TUs (1,273 checked-in + 688 generated) with **zero**
define/flag/include discrepancies vs CMake; 1,379/1,379 generated files
byte-identical; 17 findings, 3 of them real any2bazel engine fixes with
regression tests.

## Environment notes (this sandbox)

- Toolchain via apt (needs passwordless sudo): cmake, ninja, ccache,
  build-essential, Qt6 (`qt6-base-dev` etc., 6.10.2), plus autoconf/nasm/
  glslang/mesa GL dev libs. Rust via rustup (`~/.cargo`). Node/vcpkg bootstrap
  per `Meta/Utils/build_vcpkg.py`.
- `~/lb-env.sh` sets `LADYBIRD_SOURCE_DIR`, `VCPKG_ROOT`, CA certs.
- Disk ~126G, RAM 16G, 16 cores. vcpkg from-source (no cache hit) for
  Skia/ANGLE is the RAM risk; the binary cache made the first configure cheap.
