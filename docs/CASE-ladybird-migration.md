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
