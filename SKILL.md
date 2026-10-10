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

## Prerequisites

All migrations require these tools. If any are missing, stop and ask the user to
install them.

- `python3`
- `bazel` or `bazelisk`
- `curl` or `wget`
- `jq`

## Migration strategy overview

Each build system is made to emit a structured description of what it *actually
builds*, normalized into one **canonical action model**, then compared:

```
Reference build           ──extract_<frontend>.py──┐
                                                   ├─► reconstruct.py ─► diff.py ─► worklist / converged?
Bazel aquery jsonproto    ──extract_bazel.py───────┘        ▲
                                                            │
                               any2bazel.json (migration decisions)
```

- **Reference side** = the structured action description the reference build
  exposes (see [Identify reference](#identify-reference)). **Bazel side =
  `bazel aquery`**. Both expose compile *and* link actions.
- **The model stores raw ACTIONS** (argv floor + annotations); all
  canonicalization/interpretation happens in the differ (`reconstruct.py` +
  `canonicalize.py` + `diff.py`), keyed on the model's `build_system` tag (for
  noise) and each action's mnemonic (for grouping). See
  `docs/DESIGN-action-based-ir.md`.
- **Parity has two stages**, both reported by one diff run:
  - **Compile parity** — every TU compiled with equivalent flags/defines/
    includes (project-wide TU-set), and the external link-dependency closure
    matches.
  - **Link consistency** — every name-aligned executable/shared-lib links with
    equivalent link flags. The per-binary stage: the *artifacts* agree, not just
    the TUs.

  Done when both stages converge. The comparison is **asymmetric**: every
  correctness-relevant reference flag must be present on the Bazel side; extra
  Bazel flags are tolerated. No artifact/symbol diff or test execution yet.

### How targets are matched

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

### Canonicalization

`canonicalize.py` strips noise before comparing flags. Universal mechanics
(driver/wrapper paths, `-c`/`-o`, sysroot, reproducibility defines, `-O*`/`-g*`)
are dropped in code — never your concern. Judgment calls (warning-set or cosmetic
differences) go in `any2bazel.json`'s `ignore`. Correctness flags (`-std=*`,
`-fno-exceptions`, `-fno-rtti`, …) must never be ignored — they surface as hard
errors by design.

## The migration config: `any2bazel.json`

Lives at the **migrated project's repo root**, committed alongside the BUILD
files as the durable record of migration decisions. Each reference guide notes
whether its frontend reads it:

```json
{
  "bazel_args": ["--config=macos", "--copt=-fno-exceptions"],
  "target_map": { "some_reference_exe": ":some_bazel_exe" },
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
  build (see [Extract the Bazel action graph](#extract-bazel-graph)). Recorded so
  the comparison is reproducible; read by the operator, not the diff.
- **`target_map`** — `reference_name → bazel_name` for intentionally renamed
  **executables**. Bazel targets are keyed by full label, so the value is
  usually `:name` (e.g. `"bssl": ":bssl"`). Libraries need no mapping.
- **`dep_map`** — `reference_dep_name → bazel_dep_name` for an **external link
  dep** spelled differently per build (e.g. `Catch2Main` vs `catch2_main`). An
  explicit, recorded rename — not a fuzzy match — so a residual `missing_dep` is
  a genuine gap.
- **`exclude_targets`** — reference target names dropped entirely from the diff:
  third-party/vendored code Bazel pulls as an external module, or tooling out
  of scope. The **only** lever for `missing_tu`/`missing_target` on whole
  subtrees. Excluded targets still appear under `excluded.config_excluded`.
- **`include_tests`** (default `false`) — opt in to also diffing **test**
  targets. OFF by default because it requires BOTH models to be extracted with
  tests enabled and the **same** test scope (symmetric configure + aquery);
  turning it on against a tests-off extraction fabricates findings. When on,
  test sources are compared as their own project-wide TU-set union (like
  libraries) and a coarse test-binary count check runs. See
  [Diff tests](#diff-tests).
- **`ignore.{defines,flags,flags_prefixes}`** — reviewer-approved compile
  flag/define differences. `flags`/`defines` match exact tokens; `flags_prefixes`
  by prefix.
- **`ignore.{link_flags,link_flags_prefixes}`** — reviewer-approved LINK flag
  differences (per-executable link-flag diff, same asymmetric-subset policy as
  compile flags). Common case: the reference repeats compile/codegen flags on
  the link line where they're benign, while Bazel doesn't.
- **`ignore.include_map` / `ignore.include_prefixes`** — for an include root
  spelled differently per side (e.g. a dep in-tree in the reference, external
  under Bazel). **Prefer `include_map`**: it rewrites both sides' spellings to a
  canonical token and still verifies presence (several `from`s may map to one
  `to`; longest wins). `include_prefixes` just deletes the path (a blind spot) —
  use only when there's no counterpart to map to. Search **order** is not
  enforced (presence only) — see `docs/FUTURE-include-order-collision-check.md`.

The `ignore` and `target_map`/`exclude_targets` lists are applied at **diff
time** to **both sides**, so you can tune them and re-diff without re-running
the reference build or bazel.

## Migration process

> **Script paths vs. project paths.** The `scripts/…` paths below are relative
> to **this skill's own directory** (where this `SKILL.md` lives) — NOT the
> project being migrated. When running inside a target repo, invoke them by
> absolute path, e.g.
> `python3 "$SKILL_DIR/scripts/extract_bazel.py" …` where `$SKILL_DIR` is this
> skill's install location (e.g. `~/.claude/skills/any2bazel`). The artifacts
> you *produce* — `model.*.json`, `aquery.json`, `diff.json`, the generated
> `BUILD.bazel`/`MODULE.bazel`, and `any2bazel.json` — live in or beside the
> target repo.
>
> **Working directory:** run the reference build and `bazel` from the **target
> repo root** (the Bazel workspace). Only the `$SKILL_DIR/scripts/*.py` helpers
> live elsewhere.
>
> **`<repo_root>` placeholder:** the project's source/workspace root. It MUST be
> **identical** in the reference extractor (`extract_<frontend>.py`) and
> `extract_bazel.py` calls: both key translation units by their path relative
> to `<repo_root>`, so a mismatch makes every source look missing/extra and the
> diff becomes meaningless.

### Prepare for migration

#### Identify the reference build system {#identify-reference}

All frontends extract into one shared **action-based model**; a language/
mnemonic-aware differ compares each against a Bazel `aquery` model. Each
frontend has a reference guide with the context unique to that build system.
Match the marker files at the repo root against the *Detect via* column of
 the table below.
- If more than one matches (e.g. a CMake project with a `package.json` for
  tooling), ask the user which build is the reference.
- If none match, stop and offer to use the current project as a case study
  to add support for a new build system frontend.

| Frontend | Detect via | Reference source | Diffs | Status | Guide |
|----------|------------|------------------|-------|--------|-------|
| **CMake** | `CMakeLists.txt` | File API codemodel-v2 | C/C++ compile + link parity | **Mature** — the validated path | [REFERENCE-cmake.md](docs/REFERENCE-cmake.md) |
| **Maven** | `pom.xml` | forked `javac` argfiles | Java source-set parity | **Early** — argv-floor only | [REFERENCE-maven.md](docs/REFERENCE-maven.md) |
| **VSCode / npm** | `package.json` driving gulp/esbuild/tsc | esbuild/tsc/`child_process` instrumentation | standalone TS emit check | **Experimental** — not wired into the main loop | [REFERENCE-npm.md](docs/REFERENCE-npm.md) |

**Read the matching reference guide in full before continuing**, and don't
read the others. Later steps defer to it for anything specific to the
reference build.

#### Confirm scope {#confirm-scope}

Inspect the reference build for anything unsupported. Surface them and confirm
with the user before proceeding.

**NOT yet supported — warn the user if the project has these:**
- Custom commands / generated code
- Per-test-binary identity alignment, and include search **order** (presence
  only) — both have planned follow-ups
- Packaging / install rules
- Automatic external-dependency resolution

Each build system specific reference guide lists additional limits that you must
look for.

#### Extract the reference model {#extract-reference-model}

Run the reference build's extraction exactly as its reference guide describes.
Every extractor produces the same canonical model:
```bash
python3 scripts/extract_<frontend>.py <…> <repo_root> model.<frontend>.json
```
Note the platform and options the reference build was configured with; the
Bazel side must mirror them.

### Create initial BUILD.bazel files

Read `model.<frontend>.json`. For each production target emit a `cc_library` /
`cc_binary` with `srcs`, `hdrs`, `copts`, `defines`, `includes`, `deps`. Library
grouping need not match the reference (TU-set comparison is
grouping-agnostic), but keep **executable** names aligned or add a `target_map`
entry. Write `MODULE.bazel` as needed — before picking rulesets or pinning
versions, read
[docs/BAZEL-RULES.md](docs/BAZEL-RULES.md) (which rulesets have been exercised
here, why some were hand-written instead, and why a version must be resolved
rather than recalled). Put `common --check_direct_dependencies=error` in the
generated `.bazelrc` so a declared version that MVS overrides fails the build
instead of being a warning nobody reads.

### Main migration loop {#main-migration-loop}

#### Extract the Bazel action graph {#extract-bazel-graph}

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
>   through, or record genuinely-irreducible differences in `any2bazel.json`.
> - Use the **same platform/options** as the reference build in
>   [Extract the reference model](#extract-reference-model), or the two sides
>   aren't comparable.
> - **Always** call aquery with `--features=-compiler_param_file` and
>   `--features=-linker_param_file` to disable use of param files. Param files
>   are never generated during `aquery` calls, so when they're in use they'll
>   always point to files that are either stale, or do not exist at all.

```bash
bazel aquery 'mnemonic("CppCompile|ObjcCompile|CppLink|CppArchive", //...)' \
    --features=-compiler_param_file --features=-linker_param_file \
    [--config=<name>] [--copt=... --cxxopt=...] \
    --output=jsonproto > aquery.json
python3 scripts/extract_bazel.py aquery.json <repo_root> model.bazel.json
```
The mnemonics above cover C/C++. If the reference guide specifies a different
mnemonic set, use that instead.

If analysis fails, fix that first before trusting the diff. If a flag differs
only because of a build-convention gap (e.g. `-std=gnu++17` vs `-std=c++17`,
GNU-extensions on/off), that's a judgment call for `any2bazel.json`, not a
BUILD-file bug.

#### Diff against reference build {#diff}

```bash
python3 scripts/diff.py model.<frontend>.json model.bazel.json \
    <repo_root>/any2bazel.json > diff.json   # 3rd arg optional
```
`diff.json` has `converged` (⇔ zero `error` discrepancies), a `discrepancies`
worklist (each with `kind`, `severity`, `target`, `tu`, `cmake_only`,
`bazel_only`), and an `excluded` map of non-participating targets by role.
`cmake_only` is a historical name: for every frontend it means "present in the
reference, absent in Bazel".
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
`extra_target` (compiled/built on the Bazel side but not the reference) and
`extra_test_tu` — note them but they need no action unless they point to
something you didn't intend to add.

#### Triage findings {#triage}

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

#### Fix findings {#fix}

> [!IMPORTANT]
> This is the step where you may modify and create build files.

All per-iteration judgment goes into the generated `BUILD.bazel`, `MODULE.bazel`, `.bzl`,
and `any2bazel.json` (reviewed). `$SKILL_DIR/scripts/` are deterministic and must
**not** be edited per-run.

For each `error`, decide: real defect → fix the BUILD file; accepted difference
→ add to `any2bazel.json` `ignore` (only for warning/cosmetic flags, **never**
correctness flags).

| kind             | fix |
|------------------|-----|
| `missing_target` | add the missing `cc_binary`, or `target_map` a renamed exe, or `exclude_targets` if out of scope |
| `missing_tu`     | add the source to some library's `srcs`, or `exclude_targets` if it's a vendored/out-of-scope subtree |
| `defines_diff`   | add each `cmake_only` define to `defines`, or `ignore.defines` it |
| `includes_diff`  | add the missing reference include root to `includes`; for a dep whose root is spelled differently each side, `ignore.include_map` it (preferred) or `ignore.include_prefixes` it |
| `flags_diff`     | add each `cmake_only` flag to `copts`, or `ignore.flags` it |
| `link_flags_diff`| add each `cmake_only` flag to the target's `linkopts`, or `ignore.link_flags` it if benign (e.g. a compile flag the reference repeats at link) |
| `missing_dep`    | add the missing external/system dep to the target's `deps`/`linkopts`; if it's just a name spelled differently per build (`Catch2Main` vs `catch2_main`, `OpenSSL::SSL` vs `ssl`), add a `dep_map` entry. External deps are captured from both `-l` flags and archive-file inputs (e.g. `external/catch2+/libcatch2_main.a`) |
| `missing_test_tu`| (tests on) add the test source to a `cc_test`, or `exclude_targets` if out of scope |
| `test_binary_count` | (tests on, warning) differing number of test executables — investigate which side has the extra/missing binary |

Kinds specific to a reference build system are listed in its reference guide.

#### Repeat

If you edited `BUILD.bazel`/`MODULE.bazel`/`.bzl`, re-run from
[Extract the Bazel action graph](#extract-bazel-graph) (re-extract the Bazel
side); if you only edited `any2bazel.json`, re-run from
[Diff against reference build](#diff). Repeat until `converged: true`. Report
remaining `warn` items and the `excluded` roles.

### (Optional) Diff tests {#diff-tests}

Once production parity is reached, opt into test diffing:
- Re-extract **both** sides with tests enabled and the **same** scope: the
  reference build with tests enabled as its reference guide describes; aquery
  over `//...` (not a single target). Asymmetric scope fabricates findings.
- Set `"include_tests": true` in `any2bazel.json`, re-extract both models
  ([reference](#extract-reference-model) and [Bazel](#extract-bazel-graph))
  with the test-inclusive configure/aquery, then re-run the
  [main migration loop](#main-migration-loop).
- Test sources are compared as a project-wide TU-set union (grouping/naming
  agnostic); a `test_binary_count` warning flags differing numbers of test
  executables. Per-binary identity alignment is a later layer — for now,
  `missing_test_tu` tells you a test source isn't compiled on the Bazel side
  (e.g. an un-ported test binary).
- Run the main migration loop until all tests converge.

### Report results

Summarize: production targets reconciled, rounds taken, suppressions recorded in
`any2bazel.json` (with rationale), excluded roles (dashboard/codegen) for
human follow-up, and — if `include_tests` was on — test-source parity and any
test-binary count gap.

## Updating this skill

To change this skill or its scripts, read
[CONTRIBUTING.md](CONTRIBUTING.md) first. It covers testing and presubmit
requirements.
