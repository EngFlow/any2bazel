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
Bazel aquery jsonproto    ──extract_bazel.py──┘    ▲
                                                   │
                          cmake2bazel.json (migration decisions)
```

- **Oracle = CMake File API codemodel-v2**, not `compile_commands.json`.
  compile_commands has no link information; the codemodel gives per-target
  type, source→flags mapping, AND link deps — both halves of compile+link.
- **Bazel side = `bazel aquery`**, which exposes compile *and* link actions.
- **"Done" = per-TU compile-flag equivalence + link closure** (the current
  oracle strength). No artifact/symbol diff, no test execution yet.

The comparison is **asymmetric**: every correctness-relevant CMake flag must be
present on the Bazel side; extra Bazel flags are tolerated. This is what lets a
textually-different-but-equivalent build converge to zero errors.

## Two ideas that make the diff robust

### Roles, not just kinds
Every target is tagged with an inferred **role** (orthogonal to its mechanical
`kind`): `production`, `test`, `dashboard`, `aggregate`, `codegen`, `unknown`.
Only `production` targets participate in the parity diff (see
`PARTICIPATING_ROLES` in `diff.py`). Everything else is **kept in the model and
reported** under `excluded` in the diff output — visible for inspection, never
silently dropped. Turning tests on later is a one-line policy change, not a
re-extraction.

### Libraries compared as a TU-set; executables by identity
Library targets are **dissolved into a project-wide union of translation units
keyed by source path**, then compared. This makes target *names* and *grouping*
irrelevant for libraries: a rename (`crypto` → `crypto_internal`) or an
object-library fold-in (CMake's `fipsmodule` merged into Bazel's
`crypto_internal`) converges automatically with no mapping needed.

Executables — the real link deliverables — are still aligned by name (a missing
executable is a genuine gap). Use `target_map` in the config to align renamed
executables. Only **external** link deps (system libs) are checked; internal
dep names are noise once libraries are unioned.

## Canonicalization: hardcoded mechanics vs. configurable judgment

`canonicalize.py` normalizes raw flag lists. The split is deliberate:

**Hardcoded (universal facts — never a migration decision):**
- Compiler-driver mechanics: the compiler/wrapper path, `-c`, `-o`, source/`.o`
  positional args
- Toolchain/sysroot selection: `-isysroot`, `-arch`, `--sysroot`,
  `-mmacosx-version-min=`
- Reproducibility injections: `__DATE__`/`__TIME__`/`__TIMESTAMP__` defines,
  `-frandom-seed=`, `-ffile-compilation-dir=`
- Optimization/debug level (`-O*`, `-g*`) and dep-file bookkeeping (`-M*`)

**Configurable (judgment calls — recorded in `cmake2bazel.json`):**
- Warning-flag set differences (`-Wctad-maybe-unsupported`, …)
- Cosmetic/intentional define differences
These go in the `ignore` section so each suppression is an explicit, reviewable,
checked-in record of a decision made during migration.

**Never ignored (correctness):** `-std=*`, `-fno-exceptions`, `-fno-rtti`,
`-fvisibility=*` and the like surface as hard errors. Do not add these to
`ignore`.

## The migration config: `cmake2bazel.json`

Lives at the **migrated project's repo root**, committed alongside the BUILD
files as the durable record of migration decisions:

```json
{
  "target_map": { "some_cmake_exe": "some_bazel_exe" },
  "ignore": {
    "defines": ["BORINGSSL_DISPATCH_TEST"],
    "flags": ["-fvisibility=hidden"],
    "flags_prefixes": ["-Wthread-safety"]
  }
}
```

It's applied at **diff time** and to **both sides**, so you can tune
suppressions and re-diff without re-running cmake/bazel.

## Scope (MVP — check before running)

Supported: static/shared/object libraries and executables; plain C/C++ sources,
compile flags, defines, include search order; external deps recorded as abstract
identities.

**NOT yet supported — stop and tell the user if the project has these:**
- Custom commands / generated code (`configure_file`, protoc, `add_custom_command`)
- Tests (tracked via the `test` role, but not yet diffed)
- Packaging / install rules
- Automatic external-dependency resolution (find_package → bzlmod)

## Prerequisites
- `cmake` ≥ 3.14 (File API), `bazel`/`bazelisk`, `python3`

## Procedure

### 1. Confirm scope
Inspect `CMakeLists.txt`. If you find custom commands, codegen, or
`find_package` of non-system libs, surface them and confirm before proceeding.

### 2. Extract the CMake oracle
```bash
mkdir -p <build>/.cmake/api/v1/query
touch     <build>/.cmake/api/v1/query/codemodel-v2
cmake -S <src> -B <build> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON   # + project flags
python3 scripts/extract_cmake.py <build> <repo_root> model.cmake.json
```

### 3. Generate initial BUILD.bazel files  *(LLM step)*
Read `model.cmake.json`. For each production target emit a `cc_library` /
`cc_binary` with `srcs`, `hdrs`, `copts`, `defines`, `includes`, `deps`. Library
grouping need not match CMake (TU-set comparison is grouping-agnostic), but keep
**executable** names aligned or add a `target_map` entry. Write `MODULE.bazel`
as needed.

### 4. Extract the Bazel side
```bash
bazel aquery 'mnemonic("CppCompile|CppLink|CppArchive", //...)' \
    --output=jsonproto > aquery.json
python3 scripts/extract_bazel.py aquery.json <repo_root> model.bazel.json
```
If analysis fails, fix that first before trusting the diff.

### 5. Diff
```bash
python3 scripts/diff.py model.cmake.json model.bazel.json \
    <repo_root>/cmake2bazel.json > diff.json   # 3rd arg optional
```
`diff.json` has `converged` (⇔ zero `error` discrepancies), a `discrepancies`
worklist (each with `kind`, `severity`, `target`, `tu`, `cmake_only`,
`bazel_only`), and an `excluded` map of non-participating targets by role.
Synthetic target names `<libraries>` and `<external>` denote the unioned
library-TU and external-dep comparisons.

### 6. Triage and fix  *(LLM step)*, then loop
For each `error`, decide: real defect → fix the BUILD file; accepted difference
→ add to `cmake2bazel.json` `ignore` (only for warning/cosmetic flags, **never**
correctness flags).

| kind             | fix |
|------------------|-----|
| `missing_target` | add the missing `cc_binary` (executables) |
| `missing_tu`     | add the source to some library's `srcs` |
| `defines_diff`   | add each `cmake_only` define to `defines`, or `ignore` it |
| `includes_diff`  | add/reorder `includes` to preserve CMake search order |
| `flags_diff`     | add each `cmake_only` flag to `copts`, or `ignore` it |
| `missing_dep`    | add the external dep to `deps` (resolve via the dep adapter) |

Re-run steps 4–5 (or just 5 if only the config changed). Repeat until
`converged: true`. Report remaining `warn` items and the `excluded` roles.

### 7. Report
Summarize: production targets reconciled, rounds taken, suppressions recorded in
`cmake2bazel.json` (with rationale), and excluded roles (dashboard/test/codegen)
for human follow-up.

## Files

| path | role | who edits |
|------|------|-----------|
| `scripts/model.py`        | canonical model + `TargetRole` | — |
| `scripts/canonicalize.py` | flag-policy normalizer (hardcoded mechanics) | — |
| `scripts/config.py`       | `cmake2bazel.json` loader (judgment calls) | — |
| `scripts/extract_cmake.py`| File API → model, role classification | — |
| `scripts/extract_bazel.py`| aquery → model, role classification | — |
| `scripts/diff.py`         | role-filtered, TU-set parity diff | — |
| `scripts/serialize.py`    | model ↔ JSON | — |
| `<repo>/cmake2bazel.json` | migration decisions (renames + ignores) | **LLM, reviewed** |
| BUILD.bazel / MODULE.bazel| generated build files | **LLM, each round** |

The `scripts/` are deterministic and must not be edited per-run. Per-iteration
judgment goes into the generated BUILD files and `cmake2bazel.json`.

## Tests

```bash
python3 tests/test_engine.py && python3 tests/test_extractors.py
```

Extractor tests run against fixtures that mirror the documented File API and
aquery schemas. When a real project surfaces a schema detail the extractors
mishandle (fragment quoting, `external/` repo paths in aquery, multi-config),
fix the extractor and capture that output as a new fixture.
