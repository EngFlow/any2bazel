# cmake2bazel

Migrate a C/C++ project from **CMake to Bazel** by generating `BUILD.bazel`
files and then *iterating until Bazel's actual build actions match a CMake
oracle*. The migration loop is driven by a **deterministic diff**, so each
round is cheap and reproducible — an LLM does only the creative work
(generating and fixing build files), never the mechanical comparison.

This repo is packaged as a [Claude Code skill](#skill); see [`SKILL.md`](SKILL.md)
for the agent-facing procedure. The Python core under `scripts/` is a plain
library you can also run by hand.

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

### Why these oracles

- **CMake side: File API codemodel-v2**, *not* `compile_commands.json`.
  compile_commands describes only compilation — it has no link information.
  Since a buildable binary needs compile **and** link, we use the codemodel,
  which exposes per-target type, the source→flags mapping, and link
  dependencies in one stable, documented schema.
- **Bazel side: `bazel aquery`**, which reports the real compile *and* link
  actions (with full argv), matching the codemodel's coverage.

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
  "target_map": { "some_cmake_exe": "some_bazel_exe" },
  "ignore": {
    "defines": ["BORINGSSL_DISPATCH_TEST"],
    "flags": ["-fvisibility=hidden"],
    "flags_prefixes": ["-Wthread-safety"]
  }
}
```

It's applied at **diff time** to **both sides**, so suppressions can be tuned and
re-diffed without re-running cmake/bazel. Every entry is an explicit, auditable
record of a difference a reviewer chose to accept.

## "Done" criterion

The current oracle strength is **per-TU compile-flag equivalence + link
closure**: the same sources compiled with canonically-equal flags, and the same
external link-dependency closure. There is (deliberately, for now) no
output-artifact or symbol diff and no test execution — those are candidate
future oracles.

## Scope

**Supported (MVP):**
- Static / shared / object libraries and executables
- Plain C/C++ sources, compile flags, defines, include search order
- External link deps recorded as **abstract identities** (resolution to a Bazel
  label is a pluggable concern, not hardcoded)

**Not yet supported:**
- Custom commands / generated code (`configure_file`, protoc, `add_custom_command`)
- Tests (tracked via the `test` role; not yet diffed)
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
  serialize.py      model ↔ JSON (the contract between stages)
tests/
  test_engine.py      diff/canonicalize/roles/config/TU-set behavior
  test_extractors.py  full pipeline on synthetic File-API + aquery fixtures
SKILL.md            Claude Code skill: the migrate-iterate procedure
```

## Usage (by hand)

```bash
# 1. CMake oracle
mkdir -p build/.cmake/api/v1/query
touch     build/.cmake/api/v1/query/codemodel-v2
cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
python3 scripts/extract_cmake.py build "$PWD" model.cmake.json

# 2. (generate BUILD.bazel files for your targets) ...

# 3. Bazel side
bazel aquery 'mnemonic("CppCompile|CppLink|CppArchive", //...)' \
    --output=jsonproto > aquery.json
python3 scripts/extract_bazel.py aquery.json "$PWD" model.bazel.json

# 4. Diff — "converged": true means parity reached. The optional 3rd arg is the
#    migration config (target_map + ignore lists).
python3 scripts/diff.py model.cmake.json model.bazel.json cmake2bazel.json
```

Run as a skill, Claude drives step 2 and the triage/fix loop automatically.

## Tests

```bash
python3 tests/test_engine.py
python3 tests/test_extractors.py
```

The extractor tests run against fixtures under `tests/` that mirror the
documented File API and aquery schemas. Real projects exercise schema details
the fixtures may not (fragment quoting, `external/` repo paths in aquery,
multi-configuration builds); add captured real output as fixtures as you
encounter it.

## License

TBD.
