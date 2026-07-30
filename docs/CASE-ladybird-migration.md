# Case study / plan: migrating Ladybird to Bazel

Working migration of the [Ladybird](https://ladybird.org) browser engine
(CMake + vcpkg, C++23) to Bazel, driven by the any2bazel parity loop. This doc
is the durable plan and the running record of findings. The workspace overlay
it produced — the emitters (`Meta/emit_*.py`), the parity harness, and the
generated BUILD files — is in [`examples/ladybird/`](../examples/ladybird/),
along with an honest inventory of what still stops it from being a
clone-and-build.

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

One structural gap remains before the shims can *point* at this target: `cc_import`
takes a file, and `vcpkg_tree` produces a declared **directory**, so rewiring the
34 `cc_import`s means either per-file outputs or a different shim shape. That is
plumbing; the dependency tree itself is now Bazel's.

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
  Bazel-fetched one; what remains is packaging it (plus splitting the over-coarse
  `:headers` target). This is the bulk of what stands between the current build
  and *"can someone else build it on their machine"*: today it needs a local
  `Build/full` for the vcpkg `.so`s and the prebuilt Rust archive.

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
  Remaining after Ring 2: `rules_rust` for the 10 crates (Ladybird's own source,
  167 crates.io deps in `Cargo.lock` → `crate_universe`). (Qt — moc/rcc and the
  host include paths — is done, via `rules_qt`, now fetched by
  `archive_override` from upstream rather than a local path.)
  The full gap list is in [`examples/ladybird/README.md`](../examples/ladybird/README.md#known-gaps).

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
byte-identical -> now 1,408/1,408 with every one of the build's 586 ninja CUSTOM_COMMANDs accounted for (findings 25-27); 32 findings, 3 of them real any2bazel engine fixes with
regression tests.

## Environment notes (this sandbox)

- Toolchain via apt (needs passwordless sudo): cmake, ninja, ccache,
  build-essential, Qt6 (`qt6-base-dev` etc., 6.10.2), plus autoconf/nasm/
  glslang/mesa GL dev libs. Rust via rustup (`~/.cargo`). Node/vcpkg bootstrap
  per `Meta/Utils/build_vcpkg.py`.
- `~/lb-env.sh` sets `LADYBIRD_SOURCE_DIR`, `VCPKG_ROOT`, CA certs.
- Disk ~126G, RAM 16G, 16 cores. vcpkg from-source (no cache hit) for
  Skia/ANGLE is the RAM risk; the binary cache made the first configure cheap.
