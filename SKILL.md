---
name: any2bazel
description: Migrate a project (CMake, Maven, or a VSCode-style npm/esbuild build) to Bazel by iterating until Bazel's build actions match the reference build. Use when asked to convert/port a CMake/Maven/npm project to Bazel, generate BUILD.bazel files, or verify Bazel build parity against a reference build. CMake→Bazel is the mature path; Maven and npm are newer. MVP scope — no codegen/custom commands.
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
---

# /any2bazel — migrate to Bazel via parity iteration

Migrate a project to Bazel by generating `BUILD.bazel` files, then **iterating
until Bazel's actual build actions match the reference build**. The loop is
driven by a deterministic diff, so each round is cheap and the LLM only does the
creative work (generating and fixing BUILD files), never the mechanical
comparison.

> **Formerly `cmake2bazel`.** The engine grew a language-neutral, action-based
> IR with multiple frontends. One artifact still carries the old name: the
> migration config file is still literally `cmake2bazel.json`.

## Frontends and maturity

All frontends extract into one shared **action-based model**; a language/
mnemonic-aware differ compares each against a Bazel `aquery` model.

| Frontend | Reference source | Diffs | Status |
|----------|------------------|-------|--------|
| **CMake** | File API codemodel-v2 | C/C++ compile + link parity | **Mature** — the validated path, detailed below |
| **Maven** | forked `javac` argfiles | Java source-set parity | **Early** — argv-floor only |
| **VSCode / npm** | esbuild/tsc/`child_process` instrumentation | standalone TS emit check | **Experimental** — not wired into the main loop |

**Trust and detail the CMake path.** The numbered procedure below is the
CMake→Bazel loop. The Maven and npm frontends share the model and differ but are
newer captures; see *Other frontends* at the end for how they differ.

## Core idea

Each build system is made to emit a structured description of what it *actually
builds*, normalized into one **canonical action model**, then compared:

```
CMake File API codemodel  ──extract_cmake.py──┐
                                              ├─► reconstruct.py ─► diff.py ─► worklist / converged?
Bazel aquery jsonproto    ──extract_bazel.py──┘        ▲
                                                       │
                          cmake2bazel.json (migration decisions)
```

- **CMake side = File API codemodel-v2** (not `compile_commands.json`, which
  lacks link info). **Bazel side = `bazel aquery`**. Both expose compile *and*
  link actions.
- **The model stores raw ACTIONS** (argv floor + annotations); all
  canonicalization/interpretation happens in the differ (`reconstruct.py` +
  `canonicalize.py` + `diff.py`), keyed on the model's `build_system` tag (for
  noise) and each action's mnemonic (for grouping). See
  `docs/DESIGN-action-based-ir.md`.
- **Parity has two stages**, both reported by one diff run ("steps" below = the
  numbered procedure; these are the *stages* of what's checked):
  - **Compile parity** — every TU compiled with equivalent flags/defines/
    includes (project-wide TU-set), and the external link-dependency closure
    matches.
  - **Link consistency** — every name-aligned executable/shared-lib links with
    equivalent link flags. The per-binary stage: the *artifacts* agree, not just
    the TUs.

  Done when both stages converge. The comparison is **asymmetric**: every
  correctness-relevant CMake flag must be present on the Bazel side; extra Bazel
  flags are tolerated. No artifact/symbol diff or test execution yet.

## How targets are matched

- **Roles:** each target gets a `role` (`production`, `test`, `dashboard`,
  `aggregate`, `codegen`, `unknown`). Only `production` is diffed; the rest are
  reported under `excluded` (never silently dropped). Tests are opt-in
  (`include_tests`).
- **Libraries** are compared as a project-wide **union of TUs keyed by source
  path**, so names and grouping don't matter — a rename or object-library
  fold-in converges with no mapping. Only **external** link deps are checked;
  internal dep names are noise.
- **Executables** align by **name** (a missing exe is a real gap); use
  `target_map` for renames.

## Canonicalization

`canonicalize.py` strips noise before comparing flags. Universal mechanics
(driver/wrapper paths, `-c`/`-o`, sysroot, reproducibility defines, `-O*`/`-g*`)
are dropped in code — never your concern. Judgment calls (warning-set or cosmetic
differences) go in `cmake2bazel.json`'s `ignore`. Correctness flags (`-std=*`,
`-fno-exceptions`, `-fno-rtti`, …) must never be ignored — they surface as hard
errors by design.

## The migration config: `cmake2bazel.json`

Lives at the **migrated project's repo root**, committed alongside the BUILD
files as the durable record of migration decisions. The filename still carries
the old project name; it applies to the CMake→Bazel path (the only frontend that
reads it today):

```json
{
  "bazel_args": ["--config=macos", "--copt=-fno-exceptions"],
  "target_map": { "some_cmake_exe": ":some_bazel_exe" },
  "dep_map": { "Catch2Main": "catch2_main" },
  "exclude_targets": ["benchmark", "some_tool"],
  "include_tests": false,
  "ignore": {
    "defines": ["BORINGSSL_DISPATCH_TEST"],
    "flags": ["-fvisibility=hidden"],
    "flags_prefixes": ["-Wthread-safety"],
    "link_flags": ["-fno-common"],
    "link_flags_prefixes": ["-Wl,-dead_strip"],
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
- **`dep_map`** — `cmake_dep_name → bazel_dep_name` for an **external link
  dep** spelled differently per build (CMake's archive basename `Catch2Main`
  vs Bazel's `catch2_main`, or `OpenSSL::SSL` vs `ssl`). An explicit, recorded
  rename — not a fuzzy match — so a residual `missing_dep` is a genuine gap.
- **`exclude_targets`** — CMake target names dropped entirely from the diff:
  third-party/vendored code Bazel pulls as an external module, or tooling out
  of scope. The **only** lever for `missing_tu`/`missing_target` on whole
  subtrees. Excluded targets still appear under `excluded.config_excluded`.
- **`include_tests`** (default `false`) — opt in to also diffing **test**
  targets. OFF by default because it requires BOTH models to be extracted with
  tests enabled and the **same** test scope (symmetric configure + aquery);
  turning it on against a tests-off extraction fabricates findings. When on,
  test sources are compared as their own project-wide TU-set union (like
  libraries) and a coarse test-binary count check runs. See step 8.
- **`ignore.{defines,flags,flags_prefixes}`** — reviewer-approved compile
  flag/define differences. `flags`/`defines` match exact tokens; `flags_prefixes`
  by prefix.
- **`ignore.{link_flags,link_flags_prefixes}`** — reviewer-approved LINK flag
  differences (per-executable link-flag diff, same asymmetric-subset policy as
  compile flags). Common case: CMake repeats compile/codegen flags
  (`-fvisibility=hidden`, `-fno-common`) on the link line where they're benign,
  while Bazel doesn't.
- **`ignore.include_map` / `ignore.include_prefixes`** — for an include root
  spelled differently per side (a dep in-tree under CMake, external under Bazel).
  **Prefer `include_map`**: it rewrites both sides' spellings to a canonical
  token and still verifies presence (several `from`s may map to one `to`; longest
  wins). `include_prefixes` just deletes the path (a blind spot) — use only when
  there's no counterpart to map to. Search **order** is not enforced (presence
  only) — see `docs/FUTURE-include-order-collision-check.md`.

The `ignore` and `target_map`/`exclude_targets` lists are applied at **diff
time** to **both sides**, so you can tune them and re-diff without re-running
cmake/bazel.

## Scope (MVP — check before running)

Supported: static/shared/object libraries and executables. Compile-parity stage:
plain C/C++ sources, compile flags, defines, include presence; external deps as
abstract identities. Link-consistency stage: per-executable/shared-lib link
flags. Tests are opt-in (`include_tests`) and get the compile-parity stage only
(procedure step 8).

**NOT yet supported — stop and tell the user if the project has these:**
- Custom commands / generated code (`configure_file`, protoc, `add_custom_command`)
- Per-test-binary identity alignment, and include search **order** (presence
  only) — both have planned follow-ups
- Packaging / install rules
- Automatic external-dependency resolution (find_package → bzlmod)
- **Maven**: coordinate-identity deps (`group:artifact:version`, scope) and Java
  flag canonicalization — argv-floor only.
- **VSCode / npm**: not integrated into this parity loop (`diff_ts.py` is a
  standalone TS emit check only).

## Prerequisites
- `python3`; `bazel`/`bazelisk`
- CMake path: `cmake` ≥ 3.14 (File API)
- Maven path: `mvn` (forked-compile capable)
- VSCode/npm path: `node` (the instrumentation preload)

## Procedure

> **Script paths vs. project paths.** The `scripts/…` and `tests/…` paths below
> are relative to **this skill's own directory** (where this `SKILL.md` lives) —
> NOT the project being migrated. When running inside a target repo, invoke them
> by absolute path, e.g.
> `python3 "$SKILL_DIR/scripts/extract_cmake.py" …` where `$SKILL_DIR` is this
> skill's install location (e.g. `~/.claude/skills/any2bazel`). The artifacts
> you *produce* — `model.*.json`, `aquery.json`, `diff.json`, the generated
> `BUILD.bazel`/`MODULE.bazel`, and `cmake2bazel.json` — live in or beside the
> target repo.
>
> **Working directory:** run `cmake` and `bazel` from the **target repo root**
> (the Bazel workspace). Only the `$SKILL_DIR/scripts/*.py` helpers live
> elsewhere.
>
> **`<repo_root>` placeholder:** the project's source/workspace root — normally
> the same path as `<src>`. It MUST be **identical** in the `extract_cmake.py`
> and `extract_bazel.py` calls: both key translation units by their path
> relative to `<repo_root>`, so a mismatch makes every source look
> missing/extra and the diff becomes meaningless.

### 1. Confirm scope
Inspect `CMakeLists.txt`. If you find custom commands, codegen, or
`find_package` of non-system libs, surface them and confirm before proceeding.

### 2. Extract the CMake reference model
Single CMake pass emits both the File API reply and a `--trace` (the latter is
the only place `configure_file()` outputs are visible — they leave no node in
the build graph). The optional 4th arg feeds the trace to the extractor, which
records configure-time generated files in `configured_files`.
```bash
mkdir -p <build>/.cmake/api/v1/query
touch     <build>/.cmake/api/v1/query/codemodel-v2
cmake -S <src> -B <build> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    --trace-expand --trace-format=json-v1 2> <build>/trace.jsonl   # + project flags
python3 scripts/extract_cmake.py <build> <repo_root> model.cmake.json <build>/trace.jsonl
```
> Configure-time generated compile inputs (e.g. CMake's `configure_file` output
> `zconf.h`) are recorded but **not yet diffed** — the Bazel-side extraction and
> the content differ are TODO (see `docs/TODO-configure-time-generation.md`).
> This is distinct from build-time codegen (genrules), which is also unmodeled.

### 3. Generate initial BUILD.bazel files  *(LLM step)*
Read `model.cmake.json`. For each production target emit a `cc_library` /
`cc_binary` with `srcs`, `hdrs`, `copts`, `defines`, `includes`, `deps`. Library
grouping need not match CMake (TU-set comparison is grouping-agnostic), but keep
**executable** names aligned or add a `target_map` entry. Write `MODULE.bazel`
as needed — before picking rulesets or pinning versions, read
[docs/BAZEL-RULES.md](docs/BAZEL-RULES.md) (which rulesets have been exercised
here, why some were hand-written instead, and why a version must be resolved
rather than recalled). Put `common --check_direct_dependencies=error` in the
generated `.bazelrc` so a declared version that MVS overrides fails the build
instead of being a warning nobody reads.

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
Synthetic target names `<libraries>`, `<external>` (and `<tests>` when
`include_tests`) denote the unioned library-TU, external-dep, and test-TU
comparisons.

The diff reports both parity stages at once: **compile-parity** kinds
(`missing_tu`, `defines_diff`, `includes_diff`, `flags_diff`, `missing_dep`,
`missing_target`) and the **link-consistency** kind (`link_flags_diff`, one per
name-aligned executable/shared library). Fix compile parity first — link flags
are easiest to reason about once the TUs underneath agree.

`error`-severity kinds (above) block convergence and are what you fix. The diff
also emits **WARN-only** kinds that don't block `converged` — `extra_tu` /
`extra_target` (compiled/built on the Bazel side but not CMake) and
`extra_test_tu` — note them but they need no action unless they point to
something you didn't intend to add.

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
| `link_flags_diff`| add each `cmake_only` flag to the target's `linkopts`, or `ignore.link_flags` it if benign (e.g. a compile flag CMake repeats at link) |
| `missing_dep`    | add the missing external/system dep to the target's `deps`/`linkopts`; if it's just a name spelled differently per build (`Catch2Main` vs `catch2_main`, `OpenSSL::SSL` vs `ssl`), add a `dep_map` entry. External deps are captured from both `-l` flags and archive-file inputs (e.g. `external/catch2+/libcatch2_main.a`) |
| `missing_test_tu`| (tests on) add the test source to a `cc_test`, or `exclude_targets` if out of scope |
| `test_binary_count` | (tests on, warning) differing number of test executables — investigate which side has the extra/missing binary |

Then re-diff: if you edited `BUILD.bazel`/`MODULE.bazel`, re-run from step 4
(re-extract the Bazel side); if you only edited `cmake2bazel.json`, re-run from
step 5. Repeat until `converged: true`. Report remaining `warn` items and the
`excluded` roles.

### 8. (Optional) Diff tests
Once production parity is reached, opt into test diffing:
- Re-extract **both** sides with tests enabled and the **same** scope: CMake
  configured without `-D..._BUILD_TESTING=OFF`; aquery over `//...` (not a
  single target). Asymmetric scope fabricates findings.
- Set `"include_tests": true` in `cmake2bazel.json`, re-extract both models
  (steps 2 and 4) with the test-inclusive configure/aquery, then re-run the
  diff/triage/fix loop (steps 5–7).
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

## What you edit

`$SKILL_DIR/scripts/` are deterministic and must **not** be edited per-run. All
per-iteration judgment goes into the generated `BUILD.bazel`/`MODULE.bazel` and
`cmake2bazel.json` (reviewed).

## Other frontends

The Maven and VSCode/npm frontends share the action model and differ but are
newer captures — treat their output as exploratory, and don't apply the CMake
`cmake2bazel.json` machinery to them (they don't read it).

### Maven → Bazel (early)
Maven has no action graph; the reference is the **forked `javac` argument
file**. Java compiles a whole source set at once, so it's compared as a
project-wide union of `.java` sources (grouping/naming agnostic), analogous to
how libraries are compared. argv-floor only — coordinate deps aren't extracted.
```bash
mvn clean compile -Dmaven.compiler.fork=true          # writes <module>/target/*arguments
python3 scripts/extract_maven.py <module_dir> <repo_root> model.maven.json
bazel aquery 'mnemonic("Javac", //...)' --output=jsonproto > aquery.json
python3 scripts/extract_bazel.py aquery.json <repo_root> model.bazel.json
python3 scripts/diff.py model.maven.json model.bazel.json > diff.json
```
Diff kinds: `missing_java_src` / `extra_java_src` (a `.java` compiled on only
one side).

### VSCode / npm → Bazel (experimental)
An npm/gulp/esbuild build is instrumented in-process by a Node preload (hooks
esbuild, the TS language service, and `child_process`), emitting one NDJSON
record per action. `diff_ts.py` does a **standalone** TS source→emit check; it
is not part of the main diff loop.
```bash
NODE_OPTIONS="--import file://$SKILL_DIR/scripts/npm_instrument/preload.mjs" \
VSCODE_EMIT_BUILD_IR=$PWD/actions.ndjson \
    npm run <build-script>
python3 scripts/extract_npm.py actions.ndjson <repo_root> model.npm.json
python3 scripts/diff_ts.py model.npm.json model.bazel.json    # standalone check
```

## Tests

```bash
python3 tests/run_all.py    # discovers every tests/test_*.py, one exit code
```

Do not hand-list the files. This block used to name six of the eleven and chain
them with `&&`, so it stopped at the first failure and never reached the rest —
and three of the files it omitted had no `if __name__ == "__main__"` block, so
running them defined 46 tests, called none, and exited 0. `run_all.py` discovers
the files and **fails if any file contributed no tests**, which is the
`allow_empty = False` of test discovery (case study finding 35).

Extractor tests run against fixtures that mirror the documented File API,
aquery, Maven argfile, and npm-NDJSON schemas. When a real project surfaces a
schema detail the extractors mishandle (fragment quoting, `external/` repo paths
in aquery, multi-config), fix the extractor and capture that output as a new
fixture.
