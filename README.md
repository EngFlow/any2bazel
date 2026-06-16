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
Bazel aquery jsonproto    ──extract_bazel.py──┘
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
the Bazel side. Flags that appear *only* on the Bazel side — toolchain
defaults like `-fno-canonical-system-headers`, sandbox `-iquote` prefixes,
`-frandom-seed`, optimization/debug levels — are stripped before comparing and
do **not** count as discrepancies.

This asymmetry is the whole point: it lets a build that is *textually
different but semantically equivalent* converge to zero errors. Without it,
every translation unit would show a false diff and the loop would never finish.

### Flag policy (the core IP)

`canonicalize.py` normalizes each raw flag list with a per-flag policy:

| flag class            | policy |
|-----------------------|--------|
| defines (`-D`)        | order-insensitive → sorted, deduped |
| includes (`-I`, `-isystem`, `-iquote`) | **order preserved** (search order), paths made repo-relative |
| optimization/debug (`-O`, `-g`), dep-file (`-MD`…) | ignorable on both sides |
| Bazel toolchain defaults | stripped on the Bazel side only |
| everything else       | correctness-relevant → compared as a subset |

## "Done" criterion

The current oracle strength is **per-TU compile-flag equivalence + link
closure**: the same sources compiled with canonically-equal flags, and the same
link dependency closure. There is (deliberately, for now) no output-artifact or
symbol diff and no test execution — those are candidate future oracles.

## Scope

**Supported (MVP):**
- Static / shared / object libraries and executables
- Plain C/C++ sources, compile flags, defines, include search order
- Internal target link deps; external deps recorded as **abstract identities**
  (resolution to a Bazel label is a pluggable concern, not hardcoded)

**Not yet supported:**
- Custom commands / generated code (`configure_file`, protoc, `add_custom_command`)
- Tests (planned as a second phase, after compile+link parity)
- Packaging / install rules
- Automatic external-dependency resolution (find_package → bzlmod /
  rules_foreign_cc). External deps are surfaced as a discrepancy class; the
  model is designed so a resolver *adapter* can be plugged in per project.

## Layout

```
scripts/
  model.py          canonical model: targets, TUs, resolution-agnostic deps
  canonicalize.py   flag-policy normalizer  ← core IP
  extract_cmake.py  CMake File API codemodel → model.json
  extract_bazel.py  bazel aquery jsonproto  → model.json
  diff.py           asymmetric parity diff → worklist + converged flag
  serialize.py      model ↔ JSON (the contract between stages)
tests/
  test_engine.py      diff/canonicalize behavior (convergence, asymmetry, order)
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

# 4. Diff — exits with a worklist; "converged": true means parity reached
python3 scripts/diff.py model.cmake.json model.bazel.json
```

Run as a skill, Claude drives steps 2 and the fix loop automatically.

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
