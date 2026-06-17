# cmake2bazel

Migrate a C/C++ project from **CMake to Bazel** by generating `BUILD.bazel`
files and then *iterating until Bazel's actual build actions match the CMake
reference build*. The migration loop is driven by a **deterministic diff**, so
each round is cheap and reproducible — an LLM does only the creative work
(generating and fixing build files), never the mechanical comparison.

This repo is packaged as a [Claude Code skill](#skill); see [`SKILL.md`](SKILL.md)
for the agent-facing procedure. The Python core under `scripts/` is a plain
library you can also run by hand.

## Installing the skill

The skill must be discoverable for a Claude Code process to invoke `/cmake2bazel`.
Two ways:

**A. Install for auto-discovery** (any session sees `/cmake2bazel`). The skill
directory needs to contain `SKILL.md`; symlink the whole repo into the user
skills dir:

```bash
ln -s "$PWD" ~/.claude/skills/cmake2bazel
# or copy if you prefer a snapshot:  cp -R "$PWD" ~/.claude/skills/cmake2bazel
```

Restart/begin a Claude Code session in any project; `/cmake2bazel` is then
available. The skill's `scripts/` are referenced by absolute path from the
install location.

**B. Point a session at the repo** (no install). Start Claude Code and tell it:

> Read `~/GitHub/cmake2bazel/SKILL.md` and follow it to migrate this project.

This works for a one-off but isn't auto-discovered as a `/`-command.

> Note: the **generation** step (writing `BUILD.bazel` from a CMake-only
> project) is the least-exercised part of the procedure — the engine has been
> validated against projects that already had Bazel files. Expect the first
> from-scratch conversion to surface rough edges in step 3.

## How it works

Both build systems are made to emit a structured description of what they
*actually compile and link*. Each is normalized into one **canonical model**,
and the models are compared:

```
CMake File API codemodel  ──extract_cmake.py──┐
                                              ├─► diff.py ─► worklist / converged?
Bazel aquery jsonproto    ──extract_bazel.py──┘    ▲
                                                   │
                          cmake2bazel.json (migration decisions)
```

### Why these data sources

- **CMake side: File API codemodel-v2**, *not* `compile_commands.json`.
  compile_commands describes only compilation — it has no link information.
  Since a buildable binary needs compile **and** link, we use the codemodel,
  which exposes per-target type, the source→flags mapping, and link
  dependencies in one stable, documented schema.
- **Bazel side: `bazel aquery`**, which reports the real compile *and* link
  actions (with full argv), matching the codemodel's coverage.

> **aquery must mirror the real build.** A bare `bazel aquery //...` omits
> config-gated flags (`--config`/`.bazelrc`) and top-level copts the embedder
> sets, fabricating false discrepancies. Run it with the same
> platform/`--config`/copts the project actually builds with, matching the
> CMake configure. The tool cannot infer top-level flags (e.g. boringssl sets
> `-fno-exceptions -fno-rtti` at the top level, not in libraries) — pass them
> through or record the difference in `cmake2bazel.json`.

### The comparison is asymmetric

The diff requires every **correctness-relevant CMake flag** to be present on
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

Source **presence** is judged across *all* roles, not within one: a `.cc` is
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
one is reviewable.

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
  `FUTURE-include-order-collision-check.md`.)

Applied at **diff time** to **both sides**, so suppressions can be tuned and
re-diffed without re-running cmake/bazel. Every entry is an explicit, auditable
record of a difference a reviewer chose to accept.

## "Done" criterion

Parity is reached in **two stages**, both reported by a single diff run:

- **Compile parity** — every translation unit compiles with canonically-equal
  flags/defines/includes (project-wide TU-set), and the project-wide external
  link-dependency closure matches.
- **Link consistency** — each name-aligned executable / shared library links
  with equivalent link flags (asymmetric subset, toolchain link noise
  stripped). This confirms the build *artifacts* agree, not just their TUs.

A migration is done when both stages converge. There is (deliberately, for now) no
output-artifact or symbol diff and no test execution — those are candidate
future parity checks.

Test targets (opt-in) get the compile-parity stage: their sources are checked
for compile parity. The link-consistency stage runs for production executables/
shared libs — tests align by source-set union rather than binary identity, so
per-test-binary link consistency is a natural future increment rather than part
of today's "done".

## Scope

**Supported (MVP):**
- Static / shared / object libraries and executables
- Plain C/C++ sources, compile flags, defines, include presence
- External link deps recorded as **abstract identities** (resolution to a Bazel
  label is a pluggable concern, not hardcoded)
- Tests — opt-in via `include_tests`, compared as a TU-set union + a coarse
  test-binary count check

**Not yet supported:**
- Custom commands / generated code (`configure_file`, protoc, `add_custom_command`)
- Per-test-binary identity alignment (tests compare at TU-set level for now)
- Include search **order** (presence only — see
  `FUTURE-include-order-collision-check.md`)
- Packaging / install rules
- Automatic external-dependency resolution (find_package → bzlmod /
  rules_foreign_cc). External deps surface as a discrepancy class; the model is
  designed so a resolver *adapter* can be plugged in per project.

## Layout

```
scripts/
  model.py          canonical model: targets, TUs, TargetRole, deps
  canonicalize.py   flag normalizer — hardcoded mechanics  ← core IP
  config.py         cmake2bazel.json loader — configurable judgment calls
  extract_cmake.py  CMake File API codemodel → model.json (+ role classify)
  extract_bazel.py  bazel aquery jsonproto  → model.json (+ role classify)
  diff.py           role-filtered, TU-set parity diff → worklist + converged
  triage.py         groups diff.json into a systematic-cause worklist
  serialize.py      model ↔ JSON (the contract between stages)
tests/
  test_engine.py      diff/canonicalize/roles/config/TU-set behavior
  test_extractors.py  full pipeline on synthetic File-API + aquery fixtures
  test_triage.py      triage grouping/histogram/cap behavior
SKILL.md            Claude Code skill: the migrate-iterate procedure
```

## Usage (by hand)

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

Run as a skill, Claude drives step 2 and the triage/fix loop automatically.

## Tests

```bash
python3 tests/test_engine.py
python3 tests/test_extractors.py
python3 tests/test_triage.py
```

The extractor tests run against fixtures under `tests/` that mirror the
documented File API and aquery schemas. Real projects exercise schema details
the fixtures may not (fragment quoting, `external/` repo paths in aquery,
multi-configuration builds); add captured real output as fixtures as you
encounter it.

## License

TBD.
