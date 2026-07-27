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
execution):**

1. **Undeclared implicit input.** `generate_dom_tree.py` for
   `HTML/MediaControlsDOM` reads `HTML/MediaControls.css` at generation time via
   a `<link rel="stylesheet" href="MediaControls.css">` inside the input
   `MediaControls.html`. CMake never declared this dependency (it happened to
   work because the source tree is present in-place); Bazel's sandbox has only
   the declared `srcs`, so it failed loudly with `FileNotFoundError`. Fix: add
   `HTML/MediaControls.css` to that genrule's `srcs`. This is a *correctness*
   win — under CMake, editing `MediaControls.css` would not reliably retrigger
   the generator.

2. **Latent nondeterminism (`PYTHONHASHSEED`).** `generate_libweb_bindings.py`
   emits one dictionary's dependency-ordered structs (`AudioConfiguration` vs
   `VideoConfiguration` in `MediaCapabilities.h`) in an order that depends on
   Python set iteration — i.e. it varies with `PYTHONHASHSEED`. The CMake
   `Build/full` reference happens to match `PYTHONHASHSEED=0`; Bazel's default
   (randomized) seed produced a content-identical but reordered file (1378/1379
   parity). Fix: pin `PYTHONHASHSEED=0` on every codegen genrule — which both
   restores byte-parity *and* makes the actions hermetic and remote-cacheable.
   This is a real reproducibility bug in the upstream generator (its output is
   not seed-stable); the migration hardens it.

Remaining Ring 1b work is mechanical: run `emit_codegen_bazel.py` for the other
libraries with generators (LibJS bytecode, LibHTTP HSTS, the IPC endpoints under
LibRequests/LibWebView/Services, Compositor WebGL replayer) — same pattern, same
harness to verify.

## Plan for the rest (Rings 1c–2)

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

## Success gate

A **running browser built by Bazel** at the end. Compile+link parity per the
diff is the engineering check; a Bazel-built `Ladybird` that renders a page is
the acceptance test.

## Environment notes (this sandbox)

- Toolchain via apt (needs passwordless sudo): cmake, ninja, ccache,
  build-essential, Qt6 (`qt6-base-dev` etc., 6.10.2), plus autoconf/nasm/
  glslang/mesa GL dev libs. Rust via rustup (`~/.cargo`). Node/vcpkg bootstrap
  per `Meta/Utils/build_vcpkg.py`.
- `~/lb-env.sh` sets `LADYBIRD_SOURCE_DIR`, `VCPKG_ROOT`, CA certs.
- Disk ~126G, RAM 16G, 16 cores. vcpkg from-source (no cache hit) for
  Skia/ANGLE is the RAM risk; the binary cache made the first configure cheap.
