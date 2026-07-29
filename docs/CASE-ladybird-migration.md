# Case study / plan: migrating Ladybird to Bazel

Working migration of the [Ladybird](https://ladybird.org) browser engine
(CMake + vcpkg, C++23) to Bazel, driven by the any2bazel parity loop. This doc
is the durable plan and the running record of findings. Everything else — the
Bazel workspace, the emitters (`Meta/emit_*.py`), the parity harness, the
generated BUILD files — lives in the Ladybird checkout (`~/ladybird-work`), not
in this repo: it is target-specific scaffolding and machine-generated output, so
checking it in here would just be a stale copy. What belongs in any2bazel is
whatever these findings pushed back into the engine (`scripts/`) plus this
write-up.

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
   Filed and fixed upstream by sorting:
   [ladybird#10899](https://github.com/LadybirdBrowser/ladybird/issues/10899).

   I first pinned `PYTHONHASHSEED=0` on the genrules instead. That's the wrong
   fix — it freezes the symptom into the build system and leaves every other
   consumer emitting seed-dependent output. The generator should sort. The pin
   stays only as hermeticity defence-in-depth.

   The lesson worth keeping is about the *harness*, not the generator: it ran
   each generator once under an inherited seed, so "1331/1331 identical" was
   never evidence of reproducibility — a seed-dependent generator passes such a
   check most of the time. **Determinism checks that don't vary the
   nondeterminism source prove nothing.** `bazel_parity_harness.py` now takes
   `--seed N`.

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

## Plan for the rest

- **Ring 1c — BUILD generation to compile+link parity, bottom-up.** AK →
  LibCore/LibUnicode/… → LibJS/LibGfx → LibWeb → LibWebView → Services/UI →
  `Ladybird`. Per layer: generate `BUILD.bazel` from the model, aquery, diff,
  triage, fix. Mechanical and delegatable per library once the pattern (AK) is
  set; the two findings above are the shared plumbing to get right first.
- **Ring 2 (stretch) — real bzlmod deps.** Swap `cc_import` shims for BCR
  `bazel_dep`s where they exist + `rules_foreign_cc` otherwise, and build a
  find_package/vcpkg → BCR resolver adapter (the designed-but-unbuilt external
  resolver plug-in point). This is also the bulk of what stands between the
  current build and *"can Ulf build it on his machine"*: today it needs my
  `Build/full` for vcpkg `.so`s and the prebuilt Rust archive.
  Remaining after Ring 2: `rules_rust` for the 10 crates. (Qt — moc/rcc and the
  host include paths — is done, via `rules_qt`.)

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
