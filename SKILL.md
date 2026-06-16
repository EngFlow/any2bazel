---
name: cmake2bazel
description: Migrate a C/C++ project from CMake to Bazel by iterating until Bazel's build matches a CMake oracle. Use when asked to convert/port a CMake project to Bazel, generate BUILD.bazel files from CMakeLists, or verify Bazel build parity against CMake. MVP scope — no codegen/custom commands.
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
---

# /cmake2bazel — CMake → Bazel migration via parity iteration

Migrate a C/C++ project from CMake to Bazel by generating `BUILD.bazel` files,
then **iterating until Bazel's actual build actions match a CMake oracle**. The
loop is driven by a deterministic diff, so each round is cheap and the LLM only
does the creative work (generating and fixing BUILD files), never the
mechanical comparison.

## Core idea

Both build systems are made to emit a structured description of what they
*actually build*, normalized into one **canonical model**, then compared:

```
CMake File API codemodel  ──extract_cmake.py──┐
                                              ├─► diff.py ─► worklist / converged?
Bazel aquery jsonproto    ──extract_bazel.py──┘
```

- **Oracle = CMake File API codemodel-v2**, not `compile_commands.json`.
  compile_commands has no link information; the codemodel gives per-target
  type, source→flags mapping, AND link deps — both halves of compile+link.
- **Bazel side = `bazel aquery`**, which exposes compile *and* link actions.
- **"Done" = per-TU compile-flag equivalence + link closure** (the current
  oracle strength). No artifact/symbol diff, no test execution yet.

The comparison is **asymmetric**: every correctness-relevant CMake flag must be
present on the Bazel side; extra Bazel flags (toolchain defaults, sandbox
include prefixes) are tolerated and stripped before comparing. This is what
lets a textually-different-but-equivalent build converge to zero errors.

## Scope (MVP — check before running)

Supported:
- Static/shared/object libraries and executables
- Plain `.c`/`.cpp`/`.h` sources, compile flags, defines, include search order
- Internal target link deps; external deps recorded as abstract identities

**NOT yet supported — stop and tell the user if the project has these:**
- Custom commands / generated code (`configure_file`, protoc, `add_custom_command`)
- Tests (deferred to a later phase)
- Packaging / install rules
- Automatic external-dependency resolution (find_package → bzlmod mapping is
  recorded as a discrepancy class, not auto-wired)

## Prerequisites

- `cmake` ≥ 3.14 (File API), `bazel`/`bazelisk`, `python3`
- A configured CMake build, or the ability to configure one

## Procedure

### 1. Confirm scope
Inspect `CMakeLists.txt`. If you find custom commands, codegen, or
`find_package` of non-system libs, surface them and confirm with the user
before proceeding — the loop cannot converge on unsupported constructs.

### 2. Extract the CMake oracle
Request the File API codemodel, then configure:
```bash
mkdir -p <build>/.cmake/api/v1/query
touch     <build>/.cmake/api/v1/query/codemodel-v2
cmake -S <src> -B <build> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON   # + project flags
python3 scripts/extract_cmake.py <build> <repo_root> model.cmake.json
```

### 3. Generate initial BUILD.bazel files  *(LLM step)*
Read `model.cmake.json`. For each target, emit a `cc_library` / `cc_binary`
with `srcs`, `hdrs`, `copts`, `defines`, `includes`, and `deps`. Mirror the
CMake target graph; keep names matching the CMake target names so the diff
lines targets up. Write a `MODULE.bazel` / `WORKSPACE` as needed.

### 4. Extract the Bazel side
```bash
bazel aquery 'mnemonic("CppCompile|CppLink|CppArchive", //...)' \
    --output=jsonproto > aquery.json
python3 scripts/extract_bazel.py aquery.json <repo_root> model.bazel.json
```
If the build doesn't even analyze, fix the analysis error first (missing
`deps`, bad load) before trusting the diff.

### 5. Diff
```bash
python3 scripts/diff.py model.cmake.json model.bazel.json > diff.json
```
`diff.json` has `converged` (true ⇔ zero `error`-severity discrepancies) and a
`discrepancies` worklist. Each item has a `kind`, `severity`, `target`, `tu`,
and `cmake_only` (what to ADD on the Bazel side) / `bazel_only` (usually fine).

### 6. Fix  *(LLM step)*, then loop
For each `error` discrepancy, edit the relevant `BUILD.bazel`:

| kind             | fix |
|------------------|-----|
| `missing_target` | add the `cc_*` rule |
| `missing_tu`     | add the source to `srcs` |
| `defines_diff`   | add each `cmake_only` define to `defines` |
| `includes_diff`  | add/reorder `includes` to preserve CMake search order |
| `flags_diff`     | add each `cmake_only` flag to `copts` |
| `missing_dep`    | add the dep label to `deps` (resolve external via adapter) |

Re-run steps 4–5. Repeat until `converged: true` for a full round. Report the
remaining `warn`-severity items (e.g. extra Bazel targets/defines) as notes —
they don't block convergence.

### 7. Report
Summarize: targets migrated, rounds taken, and any discrepancy classes that
fell outside MVP scope (codegen, external deps) for human follow-up.

## Files

| path | role | who edits |
|------|------|-----------|
| `scripts/model.py`        | canonical model schema | — |
| `scripts/canonicalize.py` | flag-policy normalizer (the core IP) | — |
| `scripts/extract_cmake.py`| File API → model | — |
| `scripts/extract_bazel.py`| aquery → model | — |
| `scripts/diff.py`         | asymmetric parity diff → worklist | — |
| `scripts/serialize.py`    | model ↔ JSON | — |
| BUILD.bazel / MODULE.bazel| generated build files | **LLM, each round** |

The `scripts/` are deterministic and must not be edited per-run. All
per-iteration judgment goes into the generated BUILD files.

## Tests

```bash
python3 tests/test_engine.py && python3 tests/test_extractors.py
```

Extractor tests run against fixtures that mirror the documented File API and
aquery schemas. When a real project surfaces a schema detail the extractors
mishandle (fragment quoting, `external/` repo paths in aquery, multi-config),
fix the extractor and capture that output as a new fixture.
