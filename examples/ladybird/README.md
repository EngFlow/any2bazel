# Ladybird → Bazel: migration artifacts (running browser)

Artifacts from migrating the Ladybird browser (CMake + vcpkg, C++23, ~3,000
production TUs) to Bazel with the any2bazel parity loop. See
`../../docs/CASE-ladybird-migration.md` for the full story and all 17 findings.

**Result: `bazel build //:ladybird` produces a browser that renders web pages**,
byte-identically to the CMake reference — HTML, CSS, layout, and JS. Every
process in the running browser (UI, `WebContent` renderer, `Compositor`,
`RequestServer`, `ImageDecoder`) is Bazel-built; proven by moving the reference
build's service directory away and watching the CMake binary fail to launch
while the Bazel one still renders.

These files run against a Ladybird checkout with a completed reference build in
`Build/full` (CMake File-API + `ninja` materialize all generated files there),
not against this repo.

## Proof of the gate

- **`PROOF-test-page.html`** — the test page (HTML + CSS + a script that mutates
  the DOM: `textContent = 'JS says 2+2=' + (2+2)`).
- **`PROOF-render-text.txt`** — `bazel-bin/ladybird --headless=text` output.
  Contains `JS says 2+2=4`, i.e. LibJS ran inside the Bazel-built renderer.
- **`PROOF-render-layout-tree.txt`** — `--headless=layout-tree` output: the full
  box tree with computed geometry and font metrics.

## Emitters (model → BUILD files)

- **`emit_build_bazel.py`** — the per-target emitter. Reads the CMake File-API
  model and emits one `cc_library` per production library and one `cc_binary`
  per production executable: `srcs` from the compile actions (cross-package
  generated srcs labeled to their owning package), `local_defines` for private
  defines (never `defines`, which would leak to consumers), per-target flag
  delta, `-isystem` for third-party includes, `additional_compiler_inputs` for
  `#embed` data, `alwayslink` for the Rust-FFI provider libs, `linkopts` for
  system libraries, and `deps` wired by class (internal → `//:Target`, vcpkg →
  `cc_import` shim, Rust → prebuilt-`.a` shim).
- **`emit_libweb_bazel.py`** — LibWeb lives in its own package (it owns the
  codegen), so its 1,961 TUs are emitted there: 1,273 checked-in srcs +688
  generated srcs referencing genrule outputs, with `includes=[".."]` so
  `<LibWeb/CSS/PropertyID.h>` resolves from both the source and genfiles roots.
- **`emit_codegen_bazel.py`** — parses `build.ninja` and emits a Bazel `genrule`
  per generator command: absolute paths → package-relative `$(location …)`,
  CMake `*.tmp` outputs → genrule `outs`, quoted args via `shlex`, every command
  pinned to `PYTHONHASHSEED=0` for hermetic, byte-stable output. `srcs` is the
  **union of the command line and the ninja edge's declared deps** — generators
  read inputs never passed as arguments (see finding 1 in the case doc), so
  scraping only the command line silently under-declares them.
- **`bazel_parity_harness.py`** — re-runs every `Meta/Generators/*.py`
  `CUSTOM_COMMAND` from `build.ninja` into a scratch mirror and byte-diffs the
  result against the CMake build, proving the generators are reproducible before
  wrapping them. Result: 71/71 single-generator outputs identical (the
  1,331-file bindings mega-command verified separately, all identical).
  Takes `--seed N` to set `PYTHONHASHSEED`: a single run inherits one seed and so
  cannot detect a seed-dependent generator (finding 2 matched by luck at seed 4
  and diverged at seed 1), so sweep several seeds.
- **`BUGREPORT-bindings-nondeterminism.md`** — the upstream bug report for
  finding 2: one paragraph, a copy-pasteable repro, and the fix. **Filed as
  [ladybird#10899](https://github.com/LadybirdBrowser/ladybird/issues/10899).**
- **`repro-bindings-nondeterminism.sh`** — self-contained repro for finding 2:
  `./repro-bindings-nondeterminism.sh /path/to/ladybird`. Needs only `python3`
  and a checkout — no CMake, no Bazel, no build. Runs the bindings generator over
  2 IDL files under `PYTHONHASHSEED` 0–5 and prints the struct emission order;
  exits 1 if the order varies (bug present), 0 if stable (patch applied), so it
  doubles as a CI guard. Note `to_idl_value.py` cannot be run directly — it is a
  library module with no `main()` whose imports need `Meta/` on `sys.path`.
- **`upstream-sort-dictionary-order.patch`** — the one-line upstream fix (for
  [ladybird#10899](https://github.com/LadybirdBrowser/ladybird/issues/10899)) making
  `generate_libweb_bindings.py` sort its dictionary dependency names instead of
  iterating a set. Reproduces the CMake reference 1332/1332 under every seed
  tried; candidate for an upstream PR.

## Emitted / hand-written BUILD files (snapshots)

- **`root.BUILD.bazel`** — the workspace root: `//:AK` (hand-written, the
  vertical-slice template), the emitted libraries and binaries, and the
  catch-all header libraries that model CMake's global `-I` of the whole source
  tree.
- **`LibWeb.BUILD.bazel`** + **`LibWeb.codegen.bzl`** — LibWeb's package: 27
  genrules reproducing all **1,379** generated files byte-for-byte (including
  the `generate_libweb_bindings.py` mega-genrule: 661 IDL inputs → 1,331 files)
  plus the 1,961-TU `cc_library`.
- **`vcpkg.BUILD.bazel`** — the external-dependency shim: one `cc_library`
  header tree + a `cc_import` per prebuilt `.so`/`.a` from vcpkg's output.
- **`rust.BUILD.bazel`** — the prebuilt-Rust shim. Cargo's per-crate archives
  have circular cross-crate references, so they are pre-merged into one
  `librust_combined.a` and every crate label aliases to it.
- **`UI.BUILD.bazel`** — the Qt autogen shim: moc/qrc sources, the generated
  shader headers, and the `filegroup` of `moc_*.cpp` that the moc unity file
  `#include`s (staged as `additional_compiler_inputs`).
- **`bazelrc.txt`** — the global build configuration mirroring
  `Meta/CMake/compile_options.cmake`: the tree-wide warning/feature set, include
  roots, `-O2 -g` (the reference is RelWithDebInfo, and it is a *correctness*
  input), the vcpkg `-isystem`, the link settings that make the prebuilt `.so`
  shims resolve, and `CPLUS_INCLUDE_PATH` for the Qt6 system includes Bazel
  refuses as copts.
- **`cmake2bazel.json`** — the migration config driving the diff loop
  (generated-path normalizations).

## Findings that became any2bazel engine fixes

1. **Bazel-default flag strip was asymmetric and over-eager** — split into
   noise vs tolerated prefixes so shared flags cancel and a CMake-only stronger
   variant is still caught.
2. **Link-input libraries were invisible to dep inference** — the extractor now
   records a link action's library inputs from its input depset, not just argv.
3. **Extractor OOM on large C++ targets** — depset flattening was eager and
   per-node (combinatorial on shared DAGs); now lazy, memoized, iterative.

The other 14 findings are migration mechanics — including an extractor invariant
Bazel's sandbox exposed (declared deps beat the command line: finding 1) and one
genuine upstream reproducibility bug in Ladybird, where a generator's emission
order followed Python's set iteration; fixed at the source by sorting
(finding 2, `upstream-sort-dictionary-order.patch`) rather than papered over
with a seed pin.
