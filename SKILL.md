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
  "bazel_args": ["--config=macos", "--copt=-fno-exceptions"],
  "target_map": { "some_cmake_exe": ":some_bazel_exe" },
  "exclude_targets": ["benchmark", "some_tool"],
  "include_tests": false,
  "ignore": {
    "defines": ["BORINGSSL_DISPATCH_TEST"],
    "flags": ["-fvisibility=hidden"],
    "flags_prefixes": ["-Wthread-safety"],
    "include_prefixes": ["third_party/"],
    "include_map": [
      {"from": "external/abseil-cpp+", "to": "@absl"},
      {"from": "bazel-out/k8-fastbuild/bin/external/abseil-cpp+", "to": "@absl"},
      {"from": "/opt/absl/include", "to": "@absl"}
    ]
  }
}
```

Fields:
- **`bazel_args`** — the extra args aquery must run with to mirror the real
  build (see step 4). Recorded so the comparison is reproducible; read by the
  operator, not the diff.
- **`target_map`** — `cmake_name → bazel_name` for intentionally renamed
  **executables**. Bazel targets are keyed by full label, so the value is
  usually `:name` (e.g. `"bssl": ":bssl"`). Libraries need no mapping.
- **`exclude_targets`** — CMake target names dropped entirely from the diff:
  third-party/vendored code Bazel pulls as an external module, or tooling out
  of scope. The **only** lever for `missing_tu`/`missing_target` on whole
  subtrees. Excluded targets still appear under `excluded.config_excluded`.
- **`include_tests`** (default `false`) — opt in to also diffing **test**
  targets. OFF by default because it requires BOTH models to be extracted with
  tests enabled and the **same** test scope (symmetric configure + aquery);
  turning it on against a tests-off extraction fabricates findings. When on,
  test sources are compared as their own project-wide TU-set union (like
  libraries) and a coarse test-binary count check runs. See step 4b / step 7.
- **`ignore.{defines,flags,flags_prefixes}`** — reviewer-approved flag/define
  differences. `flags`/`defines` match exact tokens; `flags_prefixes` by prefix.
- **`ignore.include_prefixes` vs `ignore.include_map`** — two ways to handle an
  include path that's spelled differently on each side (typically a dependency
  that's in-tree under CMake but an external module under Bazel). **Prefer
  `include_map`:**
  - **`include_map`** rewrites differing spellings of the same root to a
    canonical token (on both sides) and **keeps checking** — the dependency
    must still be present. Several `from` prefixes may map to one `to` token
    (collapse Bazel's `external/X` and its `bazel-out/.../bin/external/X` twin).
    Longest `from` wins.
  - **`include_prefixes`** just **deletes** the path from the comparison — a
    blind spot. Use only when there's no meaningful counterpart to map to.
  - The difference matters: if the dependency's include is genuinely missing on
    the Bazel side, the map **catches it**; ignore stays silent.
  - Note: include **search order** is currently not enforced (presence only) —
    see `FUTURE-include-order-collision-check.md`.

The `ignore` and `target_map`/`exclude_targets` lists are applied at **diff
time** to **both sides**, so you can tune them and re-diff without re-running
cmake/bazel.

## Scope (MVP — check before running)

Supported: static/shared/object libraries and executables; plain C/C++ sources,
compile flags, defines, include presence; external deps recorded as abstract
identities. Tests are opt-in (`include_tests`) at TU-set + binary-count level
(step 8).

**NOT yet supported — stop and tell the user if the project has these:**
- Custom commands / generated code (`configure_file`, protoc, `add_custom_command`)
- Per-test-binary identity alignment, and include search **order** (presence
  only) — both have planned follow-ups
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
> **Critical: aquery must be invoked the way the project is actually built.**
> A bare `bazel aquery` omits config-gated and top-level flags and will
> manufacture hundreds of false discrepancies. Mirror the real build:
> - Pass the project's `--config=<name>` (check `.bazelrc` for `build:<name>`
>   stanzas; note `--enable_platform_specific_config` auto-expands to
>   `--config=<os>`).
> - Pass any top-level `--copt`/`--cxxopt` the project's build/embedder sets
>   that are **not** in `.bazelrc` (e.g. boringssl expects `-fno-exceptions
>   -fno-rtti` to be set at the top level, not in libraries). The tool cannot
>   infer these — get them from the project's build instructions and pass them
>   through, or record genuinely-irreducible differences in `cmake2bazel.json`.
> - Use the **same platform/options** as the CMake configure in step 2, or the
>   two sides aren't comparable.

```bash
bazel aquery 'mnemonic("CppCompile|CppLink|CppArchive", //...)' \
    [--config=<name>] [--copt=... --cxxopt=...] \
    --output=jsonproto > aquery.json
python3 scripts/extract_bazel.py aquery.json <repo_root> model.bazel.json
```
If analysis fails, fix that first before trusting the diff. If a flag differs
only because of a build-convention gap (e.g. `-std=gnu++17` vs `-std=c++17`,
GNU-extensions on/off), that's a judgment call for `cmake2bazel.json`, not a
BUILD-file bug.

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

### 6. Triage
```bash
python3 scripts/triage.py diff.json                 # grouped summary
python3 scripts/triage.py diff.json --kind flags_diff   # drill into one kind
python3 scripts/triage.py diff.json --json          # full histograms
```
A raw `diff.json` can have hundreds of per-TU entries that collapse to a few
**systematic** causes. `triage.py` groups them by `kind` and shows, per kind, a
value→frequency histogram of the `cmake_only` (actionable) and `bazel_only`
(usually tolerated) entries. **Read it this way:** a value on (nearly) every TU
is systematic — one fix (a copt, an include, a `target_map`/`ignore`/`copts`
entry) clears it in bulk; a value on one TU is local. Always triage before
hand-reading the worklist.

### 7. Fix  *(LLM step)*, then loop
For each `error`, decide: real defect → fix the BUILD file; accepted difference
→ add to `cmake2bazel.json` `ignore` (only for warning/cosmetic flags, **never**
correctness flags).

| kind             | fix |
|------------------|-----|
| `missing_target` | add the missing `cc_binary`, or `target_map` a renamed exe, or `exclude_targets` if out of scope |
| `missing_tu`     | add the source to some library's `srcs`, or `exclude_targets` if it's a vendored/out-of-scope subtree |
| `defines_diff`   | add each `cmake_only` define to `defines`, or `ignore.defines` it |
| `includes_diff`  | add the missing CMake include root to `includes`; for a dep whose root is spelled differently each side, `ignore.include_map` it (preferred) or `ignore.include_prefixes` it |
| `flags_diff`     | add each `cmake_only` flag to `copts`, or `ignore.flags` it |
| `missing_dep`    | add the external dep to `deps` (resolve via the dep adapter) |
| `missing_test_tu`| (tests on) add the test source to a `cc_test`, or `exclude_targets` if out of scope |
| `test_binary_count` | (tests on, warning) note the differing test-binary count; per-binary alignment is not yet enforced |

Re-run steps 4–6 (or just 5–6 if only the config changed). Repeat until
`converged: true`. Report remaining `warn` items and the `excluded` roles.

### 8. (Optional) Diff tests
Once production parity is reached, opt into test diffing:
- Re-extract **both** sides with tests enabled and the **same** scope: CMake
  configured without `-D..._BUILD_TESTING=OFF`; aquery over `//...` (not a
  single target). Asymmetric scope fabricates findings.
- Set `"include_tests": true` in `cmake2bazel.json` and re-run steps 5–7.
- Test sources are compared as a project-wide TU-set union (grouping/naming
  agnostic); a `test_binary_count` warning flags differing numbers of test
  executables. Per-binary identity alignment is a later layer — for now,
  `missing_test_tu` tells you a test source isn't compiled on the Bazel side
  (e.g. an un-ported test binary).

### 9. Report
Summarize: production targets reconciled, rounds taken, suppressions recorded in
`cmake2bazel.json` (with rationale), excluded roles (dashboard/codegen) for
human follow-up, and — if `include_tests` was on — test-source parity and any
test-binary count gap.

## Files

| path | role | who edits |
|------|------|-----------|
| `scripts/model.py`        | canonical model + `TargetRole` | — |
| `scripts/canonicalize.py` | flag-policy normalizer (hardcoded mechanics) | — |
| `scripts/config.py`       | `cmake2bazel.json` loader (judgment calls) | — |
| `scripts/extract_cmake.py`| File API → model, role classification | — |
| `scripts/extract_bazel.py`| aquery → model, role classification | — |
| `scripts/diff.py`         | role-filtered, TU-set parity diff | — |
| `scripts/triage.py`       | groups diff.json into a systematic-cause worklist | — |
| `scripts/serialize.py`    | model ↔ JSON | — |
| `<repo>/cmake2bazel.json` | migration decisions (renames + ignores) | **LLM, reviewed** |
| BUILD.bazel / MODULE.bazel| generated build files | **LLM, each round** |

The `scripts/` are deterministic and must not be edited per-run. Per-iteration
judgment goes into the generated BUILD files and `cmake2bazel.json`.

## Tests

```bash
python3 tests/test_engine.py && python3 tests/test_extractors.py \
    && python3 tests/test_triage.py
```

Extractor tests run against fixtures that mirror the documented File API and
aquery schemas. When a real project surfaces a schema detail the extractors
mishandle (fragment quoting, `external/` repo paths in aquery, multi-config),
fix the extractor and capture that output as a new fixture.
