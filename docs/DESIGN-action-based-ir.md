# Design note: action-based IR (cross-language)

Status: design, grounded in real CMake/Bazel/Maven output. Not yet implemented.

## The reframe (decided)

Today the model stores **C/C++-shaped payload** inside each target: `TranslationUnit`
(one `.cc` + per-file flags), `copts`/`linkopts`, include paths, a compile-then-link
two-stage assumption. Validating against Maven showed this is **not language-neutral**
— almost none of it survives the jump to Java.

New shape:

- **The model stores ACTIONS**, and the neutral floor of an action is its **raw
  argv** (a list of strings). This is the genuine common denominator: Bazel
  aquery *is* argv; Maven is *lowered down* to a javac command line. (Earlier
  drafts gave the action a structured `classpath: [...]` field — that was Maven
  structure smuggled into the IR. The Bazel side has no such field, so the IR
  would have been Maven-shaped, not neutral. Rejected.)
- **Structured semantics are ANNOTATIONS over the argv, not replacements.** Pure
  strings lose information the differ needs and can't reliably recover — which
  argv token is a dep vs flag vs output (the C++ side *guesses* this and it's
  been a recurring bug source: the `.lo` archive, header-processing actions,
  archive-vs-flag), and coordinate identity (`-cp .../error_prone-2.50.0.jar` is
  a path; Maven *knows* it's `com.google.errorprone:...:2.50.0` scope=compile,
  unrecoverable from the path). So:
    - **argv is always present** — the faithful `raw` floor.
    - **annotations (deps with coordinate/label identity, output role, etc.) are
      filled when a frontend KNOWS them** (Maven knows its classpath elements;
      Bazel knows declared inputs/outputs, which aquery provides alongside argv),
      and **absent/inferred when it doesn't** (differ falls back to argv parsing).
  This is the doc's `resolved | raw | unknown` tri-state applied to action
  structure: argv = raw floor (always), annotation = resolved overlay (when
  known). Never lossy; degrades gracefully. The differ trusts an annotation over
  inference.
- **Canonicalization moves OUT of the model and INTO the differ.** The model is a
  dumb, faithful record of actions (argv + whatever annotations the frontend
  could resolve). The differ interprets them.
- **The differ is language/mnemonic-aware:** C++ compile actions → group per-TU and
  diff per-TU; Java compile actions → group per source-set and diff the group for
  equivalence. Same comparator framework, per-language grouping rules.
- **Two normalization axes, keyed differently:**
  1. *Build-system noise* (`bazel-out/` paths, `cc_wrapper.sh`, `-frandom-seed`) —
     keyed on a **build-system tag** stored on the model.
  2. *Action grouping/interpretation* (compile→TU vs compile→source-set) — keyed on
     **action mnemonic / language**, NOT build system (a C++ compile from CMake and
     from Bazel group identically — why today's canonicalizer is symmetric).

## Real data backing this

### C/C++ (already have): Bazel `CppCompile`/`CppLink`/`CppArchive`, CMake codemodel
Per-file compile actions with full argv; link actions. Already action-native on the
Bazel side.

### Java/Maven (guava core, real `mvn -X compile`):
One compile action per module/source-set. argv floor + annotations:
```
mnemonic: JavaCompile
arguments: [javac, -d, target/classes,
            -cp, <m2>/error_prone-2.50.0.jar:<m2>/jspecify-1.0.0.jar:...,   # classpath IS in argv
            -Xplugin:ErrorProne ..., -J--add-exports=jdk.compiler/...=ALL-UNNAMED,
            -Xlint:-removal,-options, -sourcepath, -XDignore.symbol.file,
            <source root files...>]
inputs:  [src/main/java/**, <classpath jars>]
outputs: [target/classes]
# annotations (resolved because Maven knows them):
deps: [ com.google.errorprone:error_prone_annotations:2.50.0 (compile),
        org.jspecify:jspecify:1.0.0 (compile), ... ]   # coordinate identity + scope
```
The classpath lives **in the argv** as `-cp ...` (same place Bazel keeps it), and
the coordinate identities are the **resolved dep annotation** — recovered from
the POM, not re-parsed from the m2 path. A raw-Bazel frontend that only has the
`-cp` path would leave `deps` unknown and the differ would parse the argv. Deps
are coordinates with scope (versioned, transitive, scoped), NOT `-l`/archive
paths — the richest cross-build dep identity we've seen.

## Fracture list (what breaks, what's reusable)

| IR element | C/C++ | Java/Maven | Verdict |
|---|---|---|---|
| target graph + dep edges + roles | ✓ | ✓ | **reusable spine** |
| `TranslationUnit` (per-file + flags) | per `.cc` | whole source root | **action, not TU** |
| compile-then-link two stages | yes | no (compile → jar) | **action mnemonics, not stages** |
| `Dependency(name, external)` | `-l`/archive | coordinate+version+scope | **needs richer dep model** |
| `copts`/`defines`/`includes` | core | mostly N/A; `javacopts` instead | **per-language args, in differ** |
| `configured_files` (configure_file) | yes | no | C/C++-specific (keep) |
| build-time codegen | (unmodeled) | annotation processors | both need it eventually |

## Implications / open questions

- **The dep model is the biggest single change.** Coordinates (group:artifact:version,
  scope, transitivity) vs. our flat `Dependency(name, external)`. Maven → Bazel
  `rules_jvm_external` `@maven//:...` is the symmetric target.
- **`packaging=bundle`** (guava uses maven-bundle-plugin/OSGi) — even "plain" Maven
  jars aren't always plain. Action synthesis must not assume `jar`.
- **Maven has no action graph** like aquery — the compile argv is either
  *captured* (the real javac command line — `mvn -X`, or a compiler fork that
  echoes argv) or *synthesized* (reconstructed from effective-POM + compiler
  plugin config). **OPEN — decide at first real extraction (next concrete
  step):** try both on guava, pick whichever is reliably obtainable; capture is
  faithful but Maven doesn't surface it as cleanly as aquery, synthesis is
  easier but risks diverging from what Maven actually runs. Either way it's
  messier Tier-1 than codemodel/aquery; expect noise.
- **Validation target**: Maven → Bazel JVM (`java_library` + `rules_jvm_external`),
  diffed against Bazel's `Javac` aquery actions — symmetric with the CMake→Bazel story.

## Validation vs. mapping (two directions, we only have one)

A distinction worth keeping explicit. There are two different operations on IRs:

- **Validation (have it):** extract both sides → canonicalize both to a common
  normal form → compare. Both IRs flow *inward* to "meet in the middle." It is
  deliberately **lossy** — it discards everything that doesn't affect
  equivalence (paths, flag order, noise). This is what the differ does.
- **Mapping / generation (don't have it):** take source IR and *produce* a
  target build. Opposite direction, and **additive** — it must invent idiom,
  grouping, visibility, structure the source never encoded. You **cannot run
  the differ backwards** to get this: canonicalization threw that information
  away on purpose. Validation-canonicalization and source→target mapping are
  not inverses.

**Today the LLM is the mapping.** It reads source IR and freehand-writes BUILD
files; the differ verifies. A sound "generate-and-verify" architecture, but the
*translation* is entirely the LLM's; the deterministic machinery is only the
verifier.

**What the differ gives a future mapping:** its canonicalization implicitly
defines the equivalence relation — what "the same build" means. Any mapping
(deterministic or LLM) must land inside that relation to pass. So the differ is
the *specification* a mapping must satisfy; building it first is the right order.

**If we ever build a deterministic mapping path:** the likely shape is
**progressive lowering** (MLIR-style), e.g. `source IR → generic → bazel-like →
BUILD`, NOT a single universal IR. Multiple dialects + lowering passes, not one
representation expressive enough for everything (that's where universal-IR
efforts die). The `bazel-like → BUILD` step (syntactic emission) is the cleanest
to separate; `source → generic` is the hard, likely-still-LLM-assisted step.
Corollary: the more of the mapping is deterministic, the smaller the differ's
job — it shrinks to verifying only the heuristic/augmented parts (globbing,
idiom, intent). It never disappears, because idiom recovery is inherently a
guess. (This is the doc's deterministic-vs-augmented split, seen from the
generation side.)

Not needed for the current work (action IR + differ = validation). Recorded so
we don't conflate "can compare two builds" with "can translate one into the
other."

## Migration risk

This is a substantial restructure: `model.py`, both C/C++ extractors, and the differ
all change; the canonicalizer relocates into the differ. Do it incrementally and keep
the existing C/C++ tests green as the regression anchor — the action model must
re-derive today's TU-level diff results exactly.
