# any2bazel

Migrate a project to **Bazel** — from **CMake**, **Maven**, or a **VSCode-style
npm/esbuild/gulp** build — by extracting what each build system *actually
compiles and links* into one **canonical action model**, then *iterating until
Bazel's build actions match the reference build*. The migration loop is driven
by a **deterministic diff**, so each round is cheap and reproducible — an LLM
does only the creative work (generating and fixing build files), never the
mechanical comparison.

> **Formerly `cmake2bazel`.** The engine started as a CMake→Bazel tool and grew
> a language-neutral, action-based IR with multiple frontends; hence the rename.
> One artifact still carries the old name: the migration config file is still
> literally `cmake2bazel.json` (see [The migration config](#the-migration-config-cmake2bazeljson)).

This repo is packaged as a [Claude Code skill](#skill); see [`SKILL.md`](SKILL.md)
for the agent-facing procedure. The Python core under `scripts/` is a plain
library you can also run by hand.

> **Developed for [Claude Code](https://claude.com/claude-code) using Opus 4.8.**
> It may work with other agents, but this is untested.

## Frontend maturity

The engine has a shared action-based IR and a language/mnemonic-aware differ.
The frontends that feed it are at very different maturity levels:

| Frontend | Source of truth | Diffs against Bazel | Status |
|----------|-----------------|--------------------|--------|
| **CMake** | File API codemodel-v2 (+ `--trace` for `configure_file`) | C/C++ compile + link parity, two stages | **Mature** — the validated path |
| **Maven** | forked `javac` argument files | Java source-set (`CompileGroup`) parity | **Early** — argv-floor only, no coordinate deps yet |
| **VSCode / npm** | Node preload instrumenting esbuild / tsc / `child_process` | `<tscompile>` TS emit check (`diff_ts.py`) | **Experimental** — not wired into the main diff loop |

The **CMake → Bazel** path is the one to trust and the one the rest of this
document details. Maven and VSCode/npm share the same model and differ
framework but are newer captures; treat their output as exploratory.

> Note: the **generation** step (writing `BUILD.bazel` from a source-only
> project) is the least-exercised part of the procedure — the engine has been
> validated primarily against projects that already had Bazel files. Expect the
> first from-scratch conversion to surface rough edges.

## How it works

Every build system is made to emit a structured description of what it
*actually builds*. Each is normalized into one **canonical action model**, and
the models are compared:

```
CMake File API codemodel   ──extract_cmake.py───┐
Maven forked javac argfiles ──extract_maven.py──┤
npm/esbuild instrumentation ──extract_npm.py────┼─► reconstruct.py ─► diff.py ─► worklist / converged?
                                                │        ▲
Bazel aquery jsonproto     ──extract_bazel.py───┘        │
                                       cmake2bazel.json (migration decisions)
```

### The action-based IR

The canonical model (`model.py`) is a faithful, **dumb record of build
ACTIONS** — it does *not* canonicalize. The neutral floor of an action is its
**raw argv** (a list of strings), the genuine common denominator across build
systems: Bazel `aquery` emits argv natively; CMake, Maven, and the npm
instrumentation synthesize or capture it from their own config.

- **argv is always present** — the faithful `raw` floor.
- **Structured semantics are annotations over the argv**, filled by a frontend
  when it knows them (declared inputs/outputs, resolved deps) and **inferred
  from argv by the differ** when it doesn't. Never lossy; degrades gracefully.
- **Canonicalization lives in the differ, not the model.** `reconstruct.py`
  interprets raw actions into comparable views (per-file translation units for
  C/C++, per-source-set `CompileGroup`s for Java, link flags, deps), keyed on
  the model's `build_system` tag (for noise stripping) and each action's
  **mnemonic** (for grouping). `canonicalize.py` is the pure flag-policy layer
  it calls.

This is the "more complexity in the differ, neutral model" trade documented in
[`docs/DESIGN-action-based-ir.md`](docs/DESIGN-action-based-ir.md). The
same comparator framework applies per-language grouping rules: C/C++ compiles
per file (one TU each); Java compiles a whole source set at once, so its
comparable unit is the `(sources, flags)` group.

### Why these data sources

- **CMake side: File API codemodel-v2**, *not* `compile_commands.json`.
  compile_commands describes only compilation — it has no link information.
  Since a buildable binary needs compile **and** link, we use the codemodel,
  which exposes per-target type, the source→flags mapping, and link
  dependencies in one stable, documented schema.
- **Maven side: the forked `javac` argument file.** Maven has no action graph
  like aquery. Run `mvn clean compile -Dmaven.compiler.fork=true` and the
  maven-compiler-plugin writes the exact javac argv it launches to
  `<module>/target/…JavacCompiler…arguments`. This is the **real** command line,
  captured not synthesized.
- **VSCode / npm side: runtime instrumentation.** An npm/gulp/esbuild build has
  no action graph either. `scripts/npm_instrument/preload.mjs` hooks esbuild,
  the TypeScript language service, and `child_process` in-process (via
  `NODE_OPTIONS`) and writes one NDJSON record per build action. No target-repo
  modification needed.
- **Bazel side: `bazel aquery`**, which reports the real compile *and* link
  actions (with full argv), matching the reference's coverage.

> **aquery must mirror the real build.** A bare `bazel aquery //...` omits
> config-gated flags (`--config`/`.bazelrc`) and top-level copts the embedder
> sets, fabricating false discrepancies. Run it with the same
> platform/`--config`/copts the project actually builds with, matching the
> reference configure. The tool cannot infer top-level flags (e.g. boringssl
> sets `-fno-exceptions -fno-rtti` at the top level, not in libraries) — pass
> them through or record the difference in `cmake2bazel.json`.

### The comparison is asymmetric

The diff requires every **correctness-relevant reference flag** to be present on
the Bazel side. Flags that appear *only* on the Bazel side are tolerated. This
asymmetry is the whole point: it lets a build that is *textually different but
semantically equivalent* converge to zero errors. Without it, every translation
unit would show a false diff and the loop would never finish.

### Roles: what gets diffed

Every target is tagged with an inferred **role** (`production`, `test`,
`dashboard`, `aggregate`, `codegen`, `unknown`) — orthogonal to its mechanical
`kind`. Only `production` targets participate in the parity diff
(`PARTICIPATING_ROLES` in `diff.py`); the rest are **kept in the model and
reported** under `excluded`, so a skipped dashboard or test target is an
explicit line item, never a silent omission. This is why a CMake project's 32
CTest/CDash dashboard targets don't generate 32 false discrepancies — and why
flipping tests into the diff later is a one-line change, not a re-extraction.

### Libraries diffed as a TU-set; executables by identity

Library targets are dissolved into a **project-wide union of translation units
keyed by source path**, then compared. Target *names* and *grouping* therefore
don't matter for libraries: a rename (`crypto` → `crypto_internal`) or an
object-library fold-in (CMake's `fipsmodule` merged into Bazel's
`crypto_internal`) converges with no mapping. Executables — the actual link
deliverables — stay aligned by name; a `target_map` handles intentional
executable renames. Only **external** link deps are compared; internal dep names
are noise once libraries are unioned.

Java sources compile as a source set rather than per file, so the Maven frontend
compares them the same way libraries are: a **project-wide union of `.java`
sources**, grouping- and naming-agnostic (Maven's 2 compile groups vs Bazel's 1
is irrelevant — the source set is what must match).

Source **presence** is judged across *all* roles, not within one: a source is
only "missing" if it's compiled **nowhere** on the other side. This matters
because the same source can be grouped differently per build system — e.g. a
test helper that CMake compiles straight into a test executable while Bazel puts
it in a `testonly` `cc_library`. Such a source is present on both sides (just
under different roles) and must not be falsely reported missing; only its
*flags* are compared, in whatever union it lands in.

### Two stages: compile parity, then link consistency

The TU-set comparison above is the **compile-parity stage**: it proves the same
translation units are compiled with equivalent flags, plus the project-wide
external-dependency closure. But equivalent TUs don't guarantee equivalent
*binaries* — the link step has its own flags (`-pthread`, `-rdynamic`,
`-Wl,--gc-sections`, …). So the **link-consistency stage** compares, per
name-aligned executable and shared library, the link flags each side passes
(asymmetric subset; toolchain link noise stripped). A single diff run reports
both stages; fix compile parity first, since link flags are easiest to reason
about once the TUs underneath agree.

### Canonicalization: hardcoded mechanics vs. configurable judgment

`canonicalize.py` normalizes raw flag lists, splitting noise from signal along a
deliberate line:

| category | examples | handling |
|----------|----------|----------|
| **driver mechanics** | compiler/wrapper path, `-c`, `-o`, `.o`/source args | **hardcoded** — stripped at extraction |
| **toolchain/sysroot** | `-isysroot`, `-arch`, `--sysroot`, `-mmacosx-version-min=` | **hardcoded** — environment-specific |
| **reproducibility** | `__DATE__`/`__TIME__`/`__TIMESTAMP__`, `-frandom-seed=` | **hardcoded** — universal Bazel facts |
| **opt/debug, dep-files** | `-O*`, `-g*`, `-M*` | **hardcoded** — ignorable both sides |
| **warning sets, cosmetic defines** | `-Wctad-maybe-unsupported`, project defines | **configurable** — `cmake2bazel.json` `ignore` |
| **correctness** | `-std=*`, `-fno-exceptions`, `-fno-rtti`, `-fvisibility=*` | **never ignored** — hard errors |
| **defines** | `-D…` | order-insensitive → sorted, deduped |
| **includes** | `-I`, `-isystem`, `-iquote` | **order preserved** (search order), repo-relative |

Hardcoded rules are universal facts baked into the code. Configurable
suppressions are *judgment calls* and live in a checked-in file (below), so each
one is reviewable. (Java flag canonicalization is deliberately deferred — the
Maven path keeps javac flags raw until a real Java-vs-Java diff shows what noise
to strip.)

## The migration config: `cmake2bazel.json`

Lives at the **migrated project's repo root**, committed alongside the BUILD
files as the durable record of migration decisions. The filename still carries
the old project name (`CONFIG_FILENAME` in `config.py`); it applies to the
CMake→Bazel path, which is the only frontend that reads it today.

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
    "include_map": [{"from": "external/abseil-cpp+", "to": "@absl"},
                    {"from": "/opt/absl/include", "to": "@absl"}]
  }
}
```

- **`bazel_args`** — extra args aquery must run with to mirror the real build
  (recorded for reproducibility; read by the operator, not the diff).
- **`target_map`** — `cmake_name → bazel_name` for renamed **executables**
  (Bazel targets are keyed by full label, so values look like `:name`).
  Libraries need no mapping — they're compared by source-path TU-set.
- **`dep_map`** — `cmake_dep_name → bazel_dep_name` for an external link dep
  spelled differently per build (`Catch2Main` ↔ `catch2_main`, `OpenSSL::SSL` ↔
  `ssl`). Explicit and recorded, not a fuzzy match.
- **`exclude_targets`** — CMake targets dropped from the diff (vendored
  third-party Bazel pulls externally, or out-of-scope tooling). The only lever
  for `missing_tu`/`missing_target` on whole subtrees; excluded targets are
  still reported under `excluded.config_excluded`.
- **`include_tests`** (default `false`) — opt in to diffing test targets too.
  Requires both models extracted with tests enabled at the **same scope**
  (symmetric configure + `//...` aquery). Test sources are compared as a
  project-wide TU-set union; a `test_binary_count` warning flags differing
  numbers of test executables. Per-binary identity alignment is a later layer.
- **`ignore.{defines,flags,flags_prefixes}`** — reviewer-approved compile
  flag/define differences (`flags_prefixes` matches by prefix).
- **`ignore.{link_flags,link_flags_prefixes}`** — reviewer-approved LINK flag
  differences (per-executable, same asymmetric-subset policy). Typical case:
  CMake repeats compile/codegen flags on the link line where they're benign and
  Bazel doesn't.
- **`ignore.include_map` vs `ignore.include_prefixes`** — for an include root
  spelled differently on each side (a dep that's in-tree under CMake, external
  under Bazel). **`include_map`** rewrites both sides' spellings to a canonical
  token and keeps verifying the dep is present (several `from` prefixes can
  collapse to one `to` token; longest `from` wins). **Prefer it** over
  **`include_prefixes`**, which merely deletes the path — a blind spot. If the
  dep's include is genuinely missing on the Bazel side, the map catches it;
  ignore does not. (Include search **order** is not currently enforced — see
  `docs/FUTURE-include-order-collision-check.md`.)

Applied at **diff time** to **both sides**, so suppressions can be tuned and
re-diffed without re-running the extraction. Every entry is an explicit,
auditable record of a difference a reviewer chose to accept.

## "Done" criterion

Parity is reached in **two stages**, both reported by a single diff run:

- **Compile parity** — every translation unit compiles with canonically-equal
  flags/defines/includes (project-wide TU-set), and the project-wide external
  link-dependency closure matches. For Java, the source-set union matches.
- **Link consistency** — each name-aligned executable / shared library links
  with equivalent link flags (asymmetric subset, toolchain link noise
  stripped). This confirms the build *artifacts* agree, not just their TUs.

A migration is done when both stages converge. There is (deliberately, for now)
no output-artifact or symbol diff and no test execution — those are candidate
future parity checks.

Test targets (opt-in) get the compile-parity stage: their sources are checked
for compile parity. The link-consistency stage runs for production executables/
shared libs — tests align by source-set union rather than binary identity, so
per-test-binary link consistency is a natural future increment rather than part
of today's "done".

## Scope

**Supported (MVP):**
- **CMake → Bazel** (mature): static / shared / object libraries and
  executables; plain C/C++ sources, compile flags, defines, include presence;
  external link deps recorded as **abstract identities** (resolution to a Bazel
  label is a pluggable concern, not hardcoded); tests opt-in via `include_tests`.
- **Maven → Bazel** (early): a module's forked `javac` compile, compared as a
  Java source-set union against Bazel's `Javac` actions. argv-floor only —
  coordinate-identity deps (`group:artifact:version`, scope) are not yet
  extracted or diffed.
- **VSCode / npm → Bazel** (experimental): esbuild/tsc/`child_process`
  instrumentation into the action model; `diff_ts.py` does a standalone TS
  source→emit check, not integrated with the main C++/Java diff loop.

**Not yet supported:**
- Coordinate-identity Java deps and Java flag canonicalization (Maven path)
- npm/TS diff integration into the main loop (VSCode path)
- Custom commands / build-time generated code (`configure_file` outputs are
  captured but not yet diffed — see `docs/TODO-configure-time-generation.md`;
  protoc / `add_custom_command` are unmodeled)
- Per-test-binary identity alignment (tests compare at TU-set level for now)
- Include search **order** (presence only — see
  `docs/FUTURE-include-order-collision-check.md`)
- Packaging / install rules
- Automatic external-dependency resolution (find_package → bzlmod /
  rules_foreign_cc). External deps surface as a discrepancy class; the model is
  designed so a resolver *adapter* can be plugged in per project.

## Layout

```
scripts/
  model.py             canonical ACTION model: targets, Actions (raw argv + annotations), roles, deps
  canonicalize.py      flag normalizer — hardcoded mechanics  ← core IP
  reconstruct.py       differ's smart layer: raw Actions → comparable views (TUs / CompileGroups / link flags / deps)
  config.py            cmake2bazel.json loader — configurable judgment calls
  extract_cmake.py     CMake File API codemodel → model.json (+ role classify)
  extract_configure.py cmake --trace → configure_file outputs (configure-time gen)
  extract_maven.py     Maven forked javac argfiles → model.json (JavaCompile actions)
  extract_npm.py       npm instrumentation NDJSON → model.json (Esbuild/Ts/Spawn actions)
  npm_instrument/      Node preload + esbuild/typescript shims that emit the NDJSON
  extract_bazel.py     bazel aquery jsonproto → model.json (+ role classify)
  diff.py              role-filtered, TU-set parity diff → worklist + converged
  diff_ts.py           standalone TS source→emit diff (npm vs bazel <tscompile>) — not yet integrated
  triage.py            groups diff.json into a systematic-cause worklist
  serialize.py         model ↔ JSON (the contract between stages)
tests/
  run_all.py           discovers and runs every test_*.py; fails on a silent file
  test_run_all.py      the runner's own guards, triggered rather than asserted
  test_engine.py       diff/canonicalize/roles/config/TU-set behavior
  test_extractors.py   full pipeline on synthetic File-API + aquery fixtures
  test_maven.py        Maven frontend + Java branch of reconstruct
  test_extract_npm.py  npm frontend (NDJSON → action IR) + role classification
  test_triage.py       triage grouping/histogram/cap behavior
  test_configure.py    configure_file trace extraction
  test_diff_ts.py      the standalone TS source→emit diff
  test_emit_cargo.py   the Ladybird Rust ring emitter (crates/index/ring/binaries)
  test_emit_vcpkg.py   the Ladybird vcpkg pin emitter (versions db → http_file)
  test_vcpkg_plumbing.py  what the generated vcpkg BUILD/bzl files must not say
docs/
  DESIGN-action-based-ir.md    the action-based IR reframe (grounded in CMake/Bazel/Maven)
  proposal-doc.md              the broader Build IR spec (frontends → IR → backends)
  TODO-configure-time-generation.md
  FUTURE-include-order-collision-check.md
SKILL.md               Claude Code skill: the migrate-iterate procedure
```

## Usage (by hand)

### CMake → Bazel (the mature path)

```bash
# 1. CMake reference model
mkdir -p build/.cmake/api/v1/query
touch     build/.cmake/api/v1/query/codemodel-v2
cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
python3 scripts/extract_cmake.py build "$PWD" model.cmake.json

# 2. (generate BUILD.bazel files for your targets) ...

# 3. Bazel side — MUST mirror the real build (see warning above): pass the
#    project's --config / top-level copts, matching the CMake configure.
bazel aquery 'mnemonic("CppCompile|CppLink|CppArchive", //...)' \
    [--config=<name>] [--copt=...] --output=jsonproto > aquery.json
python3 scripts/extract_bazel.py aquery.json "$PWD" model.bazel.json

# 4. Diff — "converged": true means parity reached. Optional 3rd arg is the
#    migration config.
python3 scripts/diff.py model.cmake.json model.bazel.json cmake2bazel.json > diff.json

# 5. Triage — group the worklist by systematic cause before fixing.
python3 scripts/triage.py diff.json
```

### Maven → Bazel (early)

```bash
# Reference: forked javac argfiles (writes <module>/target/*arguments)
mvn clean compile -Dmaven.compiler.fork=true
python3 scripts/extract_maven.py <module_dir> "$PWD" model.maven.json

# Bazel side: aquery over Javac actions (mapped to the neutral JavaCompile mnemonic)
bazel aquery 'mnemonic("Javac", //...)' --output=jsonproto > aquery.json
python3 scripts/extract_bazel.py aquery.json "$PWD" model.bazel.json
python3 scripts/diff.py model.maven.json model.bazel.json > diff.json
```

### VSCode / npm → Bazel (experimental)

```bash
# Instrument the real build in-process, capturing one NDJSON record per action.
NODE_OPTIONS="--import file://$PWD/scripts/npm_instrument/preload.mjs" \
VSCODE_EMIT_BUILD_IR=$PWD/actions.ndjson \
    npm run <build-script>
python3 scripts/extract_npm.py actions.ndjson "$PWD" model.npm.json

# Standalone TS source→emit check against a Bazel model (not the main loop).
python3 scripts/diff_ts.py model.npm.json model.bazel.json
```

Run as a skill, Claude drives the generation and triage/fix loop automatically.

## Tests

```bash
python3 tests/run_all.py          # the whole suite, one exit code (~0.3s)
python3 tests/run_all.py -v cargo # verbose, filtered by file or test name
```

`run_all.py` **discovers** `tests/test_*.py` rather than reading a list, and it
fails the run if a test *file* contributed nothing — no tests defined, or a module
that would not import. Both of those are the reason it exists: this README used to
list six commands for ten files, and three of those files had no
`if __name__ == "__main__"` block at all, so running them imported the module,
defined 46 test functions, called none of them, and exited 0. There is no pytest
here, so nothing else called them either.

That is the same bug as `glob(..., allow_empty = True)` over a directory that is
not there (case study finding 35): **a check that cannot fail is indistinguishable
from one that is not needed.** A per-file runner is what was missing, but a
per-file runner is also exactly what nobody notices the absence of — so the fix is
one entry point that knows how many files there are and how many tests each
contributed. Its three guards are themselves tested, by triggering them
(`test_run_all.py`).

Each file also still runs standalone (`python3 tests/test_engine.py`) for a quick
single-file loop. As the suite grows the next step is a `py_test` per file under
`bazel test //...`, so the caching and parallelism come for free and a commit gate
runs it without anyone remembering to — this repo has no `MODULE.bazel` yet.

The extractor tests run against fixtures under `tests/` that mirror the
documented File API, aquery, Maven argfile, and npm-NDJSON schemas. Real projects
exercise schema details the fixtures may not (fragment quoting, `external/` repo
paths in aquery, multi-configuration builds); add captured real output as
fixtures as you encounter it.

## License

TBD.
