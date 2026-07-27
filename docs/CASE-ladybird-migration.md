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

### Findings for the engine (real tool output, not fixture-driven)

1. **Bazel-default flag strip is asymmetric and over-eager.**
   `canonicalize.py:BAZEL_DEFAULT_FLAG_PREFIXES` strips `-fstack-protector` and
   `-fdiagnostics-color` **on the Bazel side only**. But Ladybird *explicitly*
   sets `-fstack-protector-strong` / `-fdiagnostics-color=always` on both sides,
   so the strip makes them look CMake-only. Worked around via `ignore.flags`;
   the cleaner fix is to only strip the bazel-default when CMake didn't also
   request it (symmetry-aware). Candidate engine improvement.

2. **Library-level external deps are invisible on the Bazel side.** The Bazel
   extractor reads external deps from `-l`/archive inputs on the link line, but
   Bazel defers dynamic-lib linking to the final binary — so an intermediate
   `cc_library`'s `.so` deps never appear on its own link line. CMake's
   codemodel *does* record them per target. Hence AK shows 4 residual
   `missing_dep` (fmt/simdutf/mimalloc/cpptrace) that are real deps, just not
   observable at library granularity under Bazel. They resolve at the
   final-binary link. Candidate engine improvement: read Bazel's structured
   `cc_library` dep edges (cquery/providers), not just link-line `-l` tokens,
   so the external closure is comparable per target rather than only globally.

## Plan for the rest (Rings 1–2)

- **Ring 1b — codegen byte-parity.** Wrap the generator families
  (`libweb_bindings` IDL, `generate_libweb_css_*.py`, WebGL, etc.) as genrules
  invoking the same Python; byte-diff outputs against `Build/full`. Nail this
  before compiling anything downstream of generated headers (LibWeb depends
  heavily on them).
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
