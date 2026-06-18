# Build System Intermediate Representation (Build IR)

## 1. Abstract

Migrating between native C/C++ build systems (e.g., make, autotools, CMake, MSBuild, Bazel, Meson) is largely manual and error-prone. Writing a direct translator for each source/target pair is O(N^2) in the number of build systems supported, and such translators tend to produce non-idiomatic output because build systems differ in how they handle configuration-time evaluation, dependency encapsulation, and source globbing.

The **Build IR (Intermediate Representation)** reduces this to an O(N) problem by inserting a shared intermediate representation between frontends and backends: each build system needs one extractor (to IR) and one generator (from IR), rather than a translator per pair. A frontend extracts build facts into the IR; a backend generates native build files from it. Extraction runs as multiple passes across target platforms so that platform-specific differences can be recorded as diffs against a common base graph. The IR stores two kinds of data: deterministic execution facts (compilation flags, explicit file linkages, generated files) and augmented data inferred by heuristics or an LLM (API boundaries, globbing, toolchains).

---

## 2. Core Goals & Principles

### What This Architecture Achieves

* **Decouple Extraction from Generation:** A frontend extractor should know nothing about Bazel; a backend generator should know nothing about CMake. The Build IR is the strict API boundary between them.
* **Cross-Platform Support:** The generated build system must not be platform-specific. The IR stores a base graph plus per-platform overlay layers (diffs keyed by a constraint such as OS or CPU), so the backend can emit native conditional logic (e.g., Bazel `select()`).
* **Two Phases (Deterministic vs. Augmented):** 
  * *Phase 1 (extract facts):* Extract fully resolved arrays, absolute paths, and explicit commands. These are taken directly from the build and are correct by construction.
  * *Phase 2 (aggregate & augment):* Recover developer intent by applying heuristics and LLM-assisted inference to deduce boundaries, reconstruct globs, and normalize shell commands. Augmentations carry a confidence tier (see *Phase 2 Confidence Tiers*): those *recovered* from intent sources are high-confidence; those *suggested* by heuristics alone need human review.

* **Idiomatic Output:** The generated build files should be close to what a build engineer would write by hand, rather than a verbatim dump of resolved facts.
* **Faithful Transfer, Not Improvement:** The tool targets fidelity to the source's *encoded* intent, not improvement beyond it. If the source globbed, the output globs; if the source listed files explicitly, the output lists them explicitly — future-file behavior is then identical to the original, which is the most that is logically available (a migration is at best as good as the source state at conversion time). The one sanctioned improvement is transformations **provably equivalent over the observed legs** — e.g., collapsing a flag repeated across four legs into one `select()`. Transformations that alter *unobserved or future* state (e.g. promoting an explicit file list to a glob) are speculative and must not be applied silently; they require human sign-off.

### Non-Goals

* **We are not building an Execution Engine:** The Build IR tooling does not compile C++ code or execute test binaries. It *does* invoke a build system's configuration/analysis phase to extract a resolved command graph — both from the source (for extraction) and from the generated output (for validation; see *Validation*).
* **Zero-Touch Migrations for Legacy Code:** We do not expect 100% perfect, "one-click" migrations without human review. The architecture embraces a permissive "fail-safe" extraction phase, followed by a linting phase that highlights semantic violations for human correction.

---

## 3. The Build IR Data Model & Lifecycle

### The Source Evidence Model

The frontend is a **fan-in of many format-specific collectors**. It consumes as much evidence as a project exposes and normalizes it into the IR. Sources fall into three **trust tiers**, and a fact's storage is governed by the tier of the source that produced it.

* **Tier 1 — Resolved execution facts (highest trust).** What the build *actually did*: `compile_commands.json`, the Ninja/MSBuild action graph, the CMake File API reply, the `ctest` test listing. Correct by construction. These — and only these — populate the deterministic fields (Phase 1, `base_targets`, etc.).
* **Tier 2 — Intent sources (high trust, describes intent not outcome).** Why the build is shaped as it is, including things execution erased: `CMakeLists.txt` / `Makefile` text, `cmake --trace-expand` and `configure_file()` traces, the configure/build invocation command. Used to *recover* intent (globs, templating, the leg vector) during augmentation.
* **Tier 3 — Consumption sources (lowest trust, aspirational).** How the build is driven from outside: CI scripts (e.g. a CI `matrix:` block, which often *is* the leg matrix), READMEs, packaging/install manifests. Useful for discovering which legs ship and what the public surface is — but these rot, lie, and encode aspiration. An LLM may synthesize them into structured hints.

Three rules make consuming everything safe:

* **Lower tiers may only propose, never populate.** A Tier 2 or Tier 3 fact may only ever land in an *augmented* field. If a synthesized CI fact reached a deterministic field, the "Phase 1 is correct by construction" guarantee would be void.
* **The IR is the format-specificity boundary.** Collectors are plural and format-specific (Ninja parsing, MSBuild XML, CMake traces). They normalize into uniform IR evidence; **nothing downstream of the IR may know which collector a fact came from.** The exception is recorded provenance (below), kept for validation/debugging only.
* **Graceful degradation; Tier 1 is sufficient.** A project exposing only Tier 1 must still produce a *correct* (if more explicit, less idiomatic) migration. Tiers 2–3 are strictly additive — they raise idiomaticity and recover intent; they are never preconditions for correctness. The linter reports low-confidence-but-correct migrations when intent sources are absent.

#### Provenance

Every fact in the IR — deterministic or augmented — carries a lightweight `provenance` record: which source(s) produced it and at what trust tier.

```
provenance: {
  sources: [ "compile_commands.json", "CMakeLists.txt:42", ... ],
  tier: 1 | 2 | 3,
  status: "resolved" | "raw" | "unknown",   // see Failure Semantics
  reason: <string>,                          // required when status != resolved
  was_probed: <bool>,                        // value derived from a configure-time probe, not a literal (Fan-Out 4)
}
```

Provenance is the connective tissue of the model: it enforces the tier rule (deterministic fields must be tier 1), powers **cross-source corroboration** (the same fact seen in two sources raises confidence; a conflict is a divergence signal caught at extraction time), records extraction failure at field granularity (*Failure Semantics*), and gives the linter its confidence signal.

#### Failure Semantics

Extraction is permissive and fail-safe: a node is **never silently dropped or quietly defaulted** when extraction is incomplete. Partial-ness is recorded explicitly, at field granularity, via the `status` in `provenance`.

**Every fact field is tri-state:**

* **`resolved`** — present and interpreted. The normal case; trusted per its tier.
* **`raw`** — present but uninterpreted (e.g. a `copts` string the flag parser didn't recognize, a `raw_command` whose path didn't normalize). The literal bytes are retained and **passed through unchanged** to the generated build. This is *Faithful Transfer* applied to a parse failure: correctness is preserved, only idiomaticity is lost. `raw` and `unknown` are distinct states — `raw` has the bytes, `unknown` does not.
* **`unknown`** — absent, with a recorded `reason` (scanner didn't run, leg unavailable, etc.). 

**The inviolable rule: `unknown` ≠ empty.** Absence-of-data and known-to-be-empty must be distinguishable in the schema, and consumers must never default `unknown` to a value. `deps: []` (verified: links nothing) is a fact; `deps: unknown` (never determined) is not — defaulting it to `[]` causes silent under-linking.

**Node-level health rollup.** `health` is **not stored** — it is derived on demand from the node's fields' `provenance.status`, so it can never drift out of sync with the facts it summarizes. It aggregates those field states into the generator's per-target decision:

* **`COMPLETE`** — all fields `resolved`. Generate idiomatically.
* **`DEGRADED`** — not `COMPLETE`, but every *essential* field is present (`resolved` or `raw`). Covers `raw` fields (generate via passthrough) and `unknown` *non-essential* fields (generate without them). Idiomaticity reduced; correctness preserved.
* **`INCOMPLETE`** — an essential field is `unknown`. Cannot be safely generated; must be handed to a human.

These are total: every node is exactly one (`INCOMPLETE` if any essential field is `unknown`; else `COMPLETE` if all fields `resolved`; else `DEGRADED`).

A field is **essential** if it is a deterministic (Tier 1) field the node cannot be generated without. Essentiality is defined **per node category and type** (see *Node Categories*): `srcs` is essential for a compiled library but *not* for an `INTERFACE_LIBRARY` (header-only, legitimately source-less) or an `ALIAS` (which carries only a forward reference); a resolved `destination` is essential for an install rule; a `command` for a test. None apply across categories. Missing *augmentation* (a `suggested_glob`, a visibility guess) never affects health — it only reduces idiomaticity. This maps directly onto the deterministic-vs-augmented split.

**Leg-level health.** A leg that fails to extract entirely is marked **excluded**, never treated as a leg in which all facts are absent — otherwise the lattice would mis-attribute every platform-specific fact (it cannot distinguish "not present on Windows" from "Windows never ran"). The intersection lattice (*The Overlay Model*) operates **only over successfully extracted legs**, and reduced platform coverage is reported by the linter.

**Connection to the linter.** Health rolls straight into the linter's severity table (§6): `INCOMPLETE` → **Error** (blocks unattended migration); `raw`/`DEGRADED` fields → **Warning**/**Info** (correct via passthrough, un-idiomatic); an excluded leg → coverage **Warning**. Failure semantics add no new surfacing component — they reuse provenance and the linter.

### Root Workspace

The `root object` holds the base graph and the per-platform overlays.

* **Deterministic (Extracted Facts):**
  * `project_name`: The root project name
  * `base_targets`: The artifact nodes (test and install nodes are held analogously). The exact root structure and the node identity/keying scheme across categories are deferred to the storage/schema design; see *Node Categories* and *Node Identity and Alignment*.
  * `overlays`: The attribute and presence diffs against `base_targets`, each keyed by the leg-set (face) it applies to (e.g., the array additions/removals for `os=windows`). See *The Overlay Model*.


* **Augmented / LLM-Inferred:**
  * **Constraint Mapping:** Mapping each leg vector's raw dimension values onto organizational standard constraints (e.g., mapping the `os=windows` dimension to `@platforms//os:windows`). These constraints are what the leg-set faces are expressed in. See *The Overlay Model*.


### The Overlay Model

This section defines how per-platform differences are represented. It is the core of the cross-platform story and the rest of the data model depends on it.

#### Legs and the Leg Vector

Extraction runs once per **leg**: a single, fully resolved build of the project under one fixed configuration. Each leg is dense and non-composable — it is the literal end state of one real build, not a partial diff.

A leg is identified by its **leg vector**: a keyed assignment over a declared set of **constraint dimensions** (the dimensions are an unordered set — no dimension is privileged; see *The Intersection Lattice*). The dimension set is configuration, not hardcoded (see *Incomplete Dimensions* below). Dimensions are partitioned by which entity *owns* (fixes) them:

* **Toolchain-owned:** `os`, `cpu`, `compiler`, `compiler_family`. These are correlated — a `linux-gcc-x64` toolchain fixes all four at once. Modeling them as owned by a single toolchain atom collapses the correlated axes, so the observed configurations form a rectangular matrix over `(toolchain × invocation)` rather than a sparse one over the raw axes.
* **Invocation-owned:** `mode` (`opt`/`debug`), feature flags, and other switches passed at configure/build time.

```
LegVector = {
  toolchain: { os, cpu, compiler, compiler_family },   // toolchain-owned
  mode: "opt" | "debug",                               // invocation-owned
  features: { <flag>: <value>, ... }                   //  "
}
```

Example matrix:

```
win-msvc-x64   / Release      win-msvc-x64   / Debug
win-msvc-arm64 / Release      win-msvc-arm64 / Debug
linux-gcc-x64  / Release      linux-clang19-x64 / Release
```

#### Node Identity and Alignment

Overlays are computed **per node**: every diff is between the same target as it appears across different legs. This requires a **platform-stable join key** that is independent of anything that varies per platform.

* The join key is derived from **platform-invariant** properties: the target's declared name in the source build system (when the frontend can recover it) plus its output artifact role (`STATIC_LIBRARY`, `EXECUTABLE`, …) and normalized output path (`lib/libfoo.a` and `foo.lib` → `foo`).
* **Targets with no output artifact** — header-only / `INTERFACE` libraries, `ALIAS` targets, and other non-compiled nodes — have no output path to normalize. For these the key falls back to the **declared name combined with the normalized defining-directory path** (the source-relative directory of the declaring build file, e.g. `src/util/`), which is platform-invariant. The declared name alone is insufficient because the same name can recur in different subdirectories. An `ALIAS` additionally records the `id` of the target it forwards to, so consumers referencing the alias still align onto the real node.
* The join key **must not** be derived from `srcs`, flags, or absolute paths — these are exactly the attributes expected to diverge.

Alignment of a node across legs has three outcomes, which the model represents distinctly:

1. **Present in every leg, identical** → belongs in the base; no overlay.
2. **Present in every leg, attributes differ** → base + one or more **attribute overlays**.
3. **Present in only some legs** → a **presence overlay** (e.g., a Windows-only target). Presence is a different kind of diff from an attribute diff and is tagged as such.

#### The Intersection Lattice and Ownership Rule

Common state is not a single base plus a flat list of diffs. It is a **lattice of intersections** computed bottom-up from the dense leaf legs to the project root:

* **Leaves** are the dense legs.
* **Internal nodes** are intersections over a *group* of legs — one for every projection of the matrix (all-Windows, all-Release, all-x64, ...), not a single nested ordering. A tree that nests dimensions in one fixed order (OS > arch > mode) would fail to factor cross-cutting facts: a flag shared by all Release legs but split across OS/arch would never reach a shared node and would be duplicated. The lattice computes every projection, so it is recovered once.

**Ownership rule:** a fact belongs to the **maximal set of legs that all share it**. This set is uniquely determined by the fact ("which legs have it"), so placement is unambiguous.

A leg-set generates clean output only when it is a **face** of the constraint cube — i.e., expressible as a conjunction of constraint equalities (`os=windows ∧ mode=debug`). This yields exactly three output shapes:

| Leg-set of a fact | Output |
|---|---|
| All legs (whole cube) | Base graph, no `select()` |
| A proper face | One `select()` / `config_setting` on that conjunction |
| Not a face | Diagnostic — see below |

#### Incomplete Dimensions / Fallback

Build facts are *caused* by configuration properties (`-O2` by `mode=opt`, `/EHsc` by `compiler=msvc`). A fact therefore appears on exactly the legs whose properties triggered it. **If the constraint vocabulary names every property that build logic branches on, every fact's leg-set is a face by construction** — non-faces cannot occur.

This is an assumption about the *input*: it cannot be proven internally and holds only if the declared dimension set covers the causes of divergence. When a leg-set is **not** a face, it means one of:

1. **Incomplete dimension set** — a property causing divergence is not modeled (e.g. a flag keys on `compiler_family=gnu`, which was folded into the toolchain and never exposed as an axis). The fix is to *add a constraint axis*; this is finite and actionable. The linter reports it constructively: *"facts X, Y share leg-set {a, c}, which is not a face under the current dimensions — a `compiler_family` axis is likely missing."*
2. **Extraction artifact** — ordering, absolute-path noise, etc. Linter territory.
3. **Genuine interaction** — note that a conjunction such as `os=windows ∧ mode=debug` *is* a face and generates cleanly; the only truly non-face case is an arbitrary disjunction with no shared constraint, which reduces to case 1.

In all cases **correctness is never at risk**. The fallback is to emit the fact keyed on the exact set of leg tuples it appears in (the dense, full-tuple form). Only idiomaticity is lost, and the linter points at precisely those spots.

#### Defining the Base

The base graph is the **intersection of all legs** — only facts common to every leg. Each leg is then reconstructable as `base + overlays`, no leg is privileged, and the model is symmetric. (The trade-off: the intersection may not itself be a buildable configuration. The alternative — designating one canonical leg, e.g. `linux-gcc-x64-opt`, as the base — keeps the base buildable but makes diffs asymmetric and noisier. Intersection is chosen for clean `select()` generation.)


### Node Categories

The IR has **three distinct node kinds**, not one enum, because they are different *kinds* of thing and forcing them into one node type would leave most fields meaningless (and wrongly trip `INCOMPLETE` health for absent-but-inapplicable fields):

* **Artifact / vertex nodes** (Fan-Out 1) — build targets: executables and libraries. Most produce an output file and have `srcs`/`copts`/`deps`. Output-less vertices are the exception and exist purely for the dependency graph: `INTERFACE_LIBRARY` (header-only), `ALIAS` (1:1 forward), and `GROUP` (1:N phony aggregator — `make all_tests`); see *Node Identity and Alignment*.
* **Invocation nodes** (Fan-Out 5) — a command run, not an artifact. Two subtypes: a **test** (invocation **+ a verdict** — `WILL_FAIL`, `PASS_REGULAR_EXPRESSION`) and a **driver** (the bare invocation, no verdict — `make lint`/`format`/`docs`).
* **Install nodes** (Fan-Out 6) — post-build *placement rules*, neither an artifact nor a build step.

Note the dividing line from Fan-Out 3 actions: an **action** produces a file the build graph *consumes* (output-driven, produced-by edge); an **invocation** is a named leaf whose outputs, if any, are side-effects nobody consumes. Output-producing custom commands (`add_custom_command(OUTPUT …)`) are Fan-Out 3 actions, not invocations.

"Essential field" (for *Failure Semantics* health) is defined **per category and type** — e.g. `srcs` is essential for a compiled library, but not for an `INTERFACE_LIBRARY` or an install rule.

### Fan-Out 1: Target Nodes / Vertices

Every build **target** — an executable or library, including header-only (`INTERFACE_LIBRARY`), `ALIAS`, and phony `GROUP` aggregator targets that produce no output file — is a node. (Invocations and install rules are separate node kinds; see Fan-Out 5 and 6.)

* **Deterministic (Extracted Facts):**
  * `id`: The platform-stable join key (see *Node Identity and Alignment*). Derived only from platform-invariant properties so the same target aligns across legs.
  * `type`: `EXECUTABLE`, `STATIC_LIBRARY`, `SHARED_LIBRARY`, `OBJECT_LIBRARY`, `INTERFACE_LIBRARY` (header-only, no output artifact), `ALIAS` (forwards to another node's `id`), `GROUP` (phony aggregator — no output, no command, exists only to group `deps`; e.g. `make all_tests`)
  * `language`: Derived from file extensions by fixed rule (`.cpp` -> `CXX`)
  * `srcs` & `hdrs`: Exact, fully expanded arrays of source files (No globs)
  * `includes` & `defines`: Exact `-I` and `-D` parameters
  * `copts` & `linkopts`: Raw string arrays of compiler/linker flags

* **Essential fields** (an `unknown` here → `INCOMPLETE`; see *Failure Semantics*), by `type`:
  * All artifact types: `id`, `type`.
  * Compiled types (`EXECUTABLE`, `STATIC_LIBRARY`, `SHARED_LIBRARY`, `OBJECT_LIBRARY`): additionally `srcs`. (`language` is *not* essential — it is derived by fixed rule from `srcs`; `hdrs`, `includes`, `defines`, `copts`, `linkopts` are not essential, an empty set is a valid fact.)
  * `INTERFACE_LIBRARY`: no `srcs` requirement (header-only is legitimately source-less); `hdrs` is essential (an interface library with unknown headers cannot be generated).
  * `ALIAS`: the forwarded-to `id` is essential; it has no `srcs`/`hdrs` of its own.
  * `GROUP`: only `id` and `type` are essential — it has no `srcs`/output and relies entirely on Fan-Out 2 `deps`. Generates into a Bazel `filegroup` or a deps-only target.

* **Lifecycle Steps (attribute, not a node):**
  * `lifecycle_steps`: an ordered list of `{ phase: PRE_BUILD | PRE_LINK | POST_BUILD, command, exec_platform }` attached to the artifact — `add_custom_command(TARGET t POST_BUILD …)`: copy a DLL beside the exe, codesign, strip. These are modeled as an attribute of the artifact, **not** a free-standing node, because their identity is inseparable from `(target, phase, ordinal)`.
  * **Backend-dependent fidelity:** some target systems have **no lifecycle-hook concept** (Bazel has no post-build hook); the idiomatic mapping is to *restructure* the step into a separate action/genrule that consumes the artifact as input. When a backend cannot express a lifecycle step, that is a linter **Warning** (an unmapped concept), **never a silent drop** — mirroring the install asymmetry (Fan-Out 6). Not essential (most artifacts have none).

* **Augmented / LLM-Inferred:**
  * **Visibility:** Calculated via In-Degree Analysis (highest common directory of consumers). LLMs can assist by reading directory contexts (`internal/`, `api/`)
  * **Glob Reconstruction:** Heuristics compare explicit `srcs` lists against directory contents to suggest a human-readable glob (e.g., `suggested_glob: "*.cpp"`)
  * **Target Consolidation:** Collapsing intermediate/dummy targets (like CMake `OBJECT` libraries) into logical, unified nodes

### Fan-Out 2: Dependencies / Edges

The links between targets defining the Directed Graph

* **Deterministic (Extracted Facts):**
  * `deps`: Internal Target IDs this node links against
  * `external_links`: Raw system libraries or absolute paths to package manager caches


* **Augmented / LLM-Inferred:**
  * **Public vs. Private Scope:** Parsing header `#include` directives to deduce if a dependency is part of the public interface or a private implementation detail
  * **External Package Normalization:** Translating raw cache paths (e.g., `vcpkg_installed/x64/lib/libz.a`) into semantic package metadata (`{ package: "zlib", version: "1.3.1" }`).
    * **Version and identity must be *recovered*, not guessed.** When a Tier 2 lockfile/manifest is present (`vcpkg.json` + baseline, `conan.lock`), package name and exact version are read from it — **Recovered**, high confidence. With no manifest, identity is sniffed from the cache path alone — **Suggested**, version `unknown` (never `"latest"`, which silently breaks reproducibility by allowing an ABI different from the one the source built against).
    * **note:** Normalization deliberately replaces a source path with a reference the *target* system re-resolves from its own registry, so the generated command graph is *expected* to diverge from the source's — correct re-resolution and a wrong package mapping look identical to the comparator. Its correctness therefore rests entirely on provenance and the linter, not on validation.

### Fan-Out 3: Custom Actions & Templates / Edge Cases

Handling code generation, protobufs, and configure-time file templating.

* **Deterministic (Extracted Facts):**
  * `raw_command`: The literal shell string executed by the native build tool
  * `exec_platform`: `HOST | TARGET` — which toolchain the action runs under (see Fan-Out 4). A codegen tool like `protoc` is `HOST`; a step that runs a freshly-built target binary is `TARGET`. Defaults to `HOST`; reliably recovered only from a cross-compile leg, otherwise inferred (Suggested).
  * `outputs`: The files an action produces. This is the basis of "generated" — `is_generated` on an artifact is *derived* ("is some action's `outputs`"), not a standalone flag. Recovered from the Ninja/MSBuild action graph (Tier 1); when only a `compile_commands.json` is available, inferred heuristically (in a build dir, absent from source control, matches `*.pb.h`, …) and marked **Suggested**.
  * `template_substitutions`: Exact key-value dictionary dumped from template traces (e.g., `cmake --trace-expand`)

* **Essential fields** (an `unknown` here → `INCOMPLETE`): `raw_command` (or, for a templating action, `template_substitutions` + the template input) and `outputs` — an action whose command or whose produced files are unknown cannot be generated. `exec_platform` is never `unknown` (it has a default), so it is not essential; a low-confidence *inferred* value is a linter **Warning**, not an `INCOMPLETE`.

* **The Produced-By / Consumed-File Edge:**
  * The edge linking an action's `outputs` to the targets whose `srcs`/`hdrs`/includes reference those files. Without it a generated `foo.pb.h` is indistinguishable from a checked-in source, and the backend would emit a target referencing a file that does not exist yet, with no rule to produce it first.
  * In a fully-resolved leg the generated file already exists on disk (codegen ran before compilation), so the edge is *recovered by correlation*: an action whose `outputs` include `foo.pb.h` plus a compile command consuming `foo.pb.h`. A consumed generated file whose **producer is `unknown`** makes the consuming node `INCOMPLETE` (*Failure Semantics*) — it cannot be safely generated.
  * The ordered case (`protoc` → header → library) is a clean DAG once this edge exists; ordering is the target build system's job to topologically sort, not something the IR tracks. This is distinct from `cycle_group` (below), which resolves target-*link* cycles, not file production.

* **Templating as a First-Class Live Action:**
  * `configure_file()` and similar template steps are a pure function `(template, substitution_map) → output`, and both inputs are captured. They are modeled as a normal action (`inputs = {template, substitution_map}`, one `output`) and generated **live, never frozen**: Bazel `expand_template(...)`, or `configure_file(...)` again for CMake-to-CMake. Changing a substitution regenerates the output — *Faithful Transfer* preserved.
  * Per-leg divergence (e.g. `config.h` with `SIZEOF_LONG` 8 vs 4) localizes to the **substitution values**, not the template (which is identical across legs). Probe-derived values are resolved via the toolchain (see Fan-Out 4), so this divergence is handled by toolchain selection rather than per-target `select()`.
  * **The one residual freeze:** a true cycle in the produced-by/consumed-file graph (a template whose content depends on something built *later*) cannot be modeled as a live rule. Only then is the output captured by resolved content, and the linter raises an **Error**.

* **Augmented / LLM-Inferred:**
  * **Command Normalization:** Stripping batch/shell boilerplate, replacing hardcoded paths with hermetic toolchain variables, and swapping explicit file names for macro placeholders (e.g., `$@`)
  * **Action Grouping:** Grouping highly repetitive single-file actions into a single logical rule (e.g., batching `protoc` invocations)

#### Cycle Detection and Escape Hatches

Source build systems often tolerate cyclical dependencies (A -> B -> A) via linker groupings, but strict DAG systems (Bazel, Buck) will crash upon evaluation

* **Deterministic (Extracted Facts):** The extraction phase records the exact `deps` relationships, including the cycle.
* **Augmented / LLM-Inferred:**
  * **Graph Analysis:** An augmentation pass runs Tarjan's algorithm to detect cycles. It tags all involved targets with a shared `cycle_group` ID
  * **Backend Resolution (Escape Hatch):**
      * **Target Merging (Preferred):** The generator collapses all targets sharing a `cycle_group` into a single monolithic `cc_library` to satisfy the strict DAG
      * **Linker Delegation (Fallback):** If targets cannot be merged (e.g., precompiled archives), the generator drops target-level `deps` and relies on `linkopts = ["-Wl,--start-group", ...]` to force the host linker to resolve the cycle

### Fan-Out 4: Toolchains / Execution Environment

Toolchains abstract the underlying compiler and host environment away from individual targets. They must be modeled as global, abstract providers rather than hardcoded target properties.

* **Host vs. Target Toolchains (Execution Platforms):**
  * A cross-compile leg has **two** active toolchains: the **target** toolchain (the leg vector's `toolchain` dimension — what C++ artifacts are compiled *for*) and an **exec/host** toolchain (what build tools run *on*). Example: cross-compiling to `windows-arm64` from `linux-x64`, a `protoc` used during the build must be a Linux-x64 binary even though the shipped artifacts are Windows-ARM64.
  * Every action (Fan-Out 3) and test (Fan-Out 5) therefore carries an **`exec_platform: HOST | TARGET`** tag stating which toolchain it runs under. Without it the backend would emit a rule telling Bazel/Meson to execute a target-arch binary on the build machine — an instant crash. This maps to Bazel's exec transition, Meson's `native : true`, and CMake imported host tools.
  * **Extraction caveat (confidence-tiered):** in a *native* leg, host == target, so the distinction is **not observable** in the resolved facts — `protoc` was built with the same compiler as everything else. The tag is reliably **Recovered** only from a cross-compile leg (Tier 1, the host tool visibly uses a different toolchain) or from an intent source (Tier 2, e.g. the tool is invoked inside a custom command). A native-only extraction must **infer** it (Suggested) and the linter flags low-confidence `exec_platform` assignments — getting this wrong only surfaces once someone first cross-compiles.
  * **Default:** absent any signal, an action's `exec_platform` defaults to `HOST` (a build step runs on the build machine) and a normal test defaults to `TARGET` (it exercises the built artifact). Both are overridable by recovered evidence.

* **Tracking the Invocation Command:**
  * The extraction pipeline captures the original invocation command (e.g., `cmake -DCMAKE_CXX_COMPILER=...` or environment variables like `CC=clang`).
  * **Purpose:** This captures the *intent* of the build matrix leg, supplying the invocation-owned dimensions of its leg vector (e.g., `mode`, feature flags). The toolchain-owned dimensions (`os`, `cpu`, `compiler`) come from the resolved toolchain. Together they form the leg vector used by *The Overlay Model*.
* **Deterministic (Extracted Facts):**
  * Absolute paths to compiler executables, linker tools, and archivers, plus target architectures, scraped from `compile_commands.json` or the CMake File API.
  * Implicit system include paths and library directories extracted by directly probing the legacy compiler executable.
  * **Probe Table:** Configure-time feature-detection results (`check_symbol_exists`, `try_compile`, `CMAKE_SIZEOF_VOID_P`, …) captured per-leg. A probe result is a property of the toolchain + platform, not of any one target, so it lives here; templating actions (Fan-Out 3) reference it rather than inlining the value. Per-leg divergence (`SIZEOF_LONG` 8 vs 4) is resolved by the toolchain dimension of the leg vector — different toolchain, different probe table — so it needs no per-target `select()`. Each entry's `provenance.was_probed = true`: re-running a probe means executing the build (Non-Goal #1), so the value is captured, not recomputed. The flag lets a backend hardcode the result *with an annotation* ("`HAVE_FOO` was a `check_symbol_exists` probe, hardcoded for this toolchain") instead of emitting a silent constant, and keeps re-emitting a real check open as a future option.
* **Augmented / LLM-Inferred:**
  * **Hermetic Abstraction:** Replacing rigid absolute host paths (`/usr/bin/c++`) with abstract workspace IDs (`toolchain_id: "cxx_compiler"`) or downloaded toolchain archives, deferring actual toolchain resolution to the backend generator and organizational policy. This keeps the generated build system reproducible across developer machines.
* **Backend Generation:**
  * The IR `TOOLCHAIN` nodes are generated into idiomatic host/target configurations, such as Bazel `cc_toolchain_suite` / `cc_toolchain_config` definitions. Probe-table values are hardcoded into the toolchain definition (e.g. a central `config.h` the toolchain provides), where templating actions resolve them by toolchain selection.

### Fan-Out 5: Invocations (Tests & Drivers)

An invocation is **not an artifact** — it is the *running of* a command, usually a built executable but possibly a script or system tool. It is a separate node kind because the relationship is many-to-one: one executable can back many invocations (parameterized tests, `gtest_discover_tests`), so it cannot be folded into the artifact it runs. There are two **subtypes**:

* **`TEST`** — an invocation **plus a verdict** (a pass/fail criterion: exit code, `WILL_FAIL`, `PASS_REGULAR_EXPRESSION`). Primary Tier 1 source: the test listing (`ctest --show-only=json-v1`, and equivalents).
* **`DRIVER`** — the **bare** invocation, no verdict: `add_custom_target(name COMMAND …)` such as `make lint`/`format`/`docs`. A driver may write files as a **side-effect**, but those are explicitly *not* produced-by-edge outputs (nothing in the build graph consumes them) — which is exactly what keeps it out of Fan-Out 3 and keeps the DAG from blocking on a file no consumer waits for.

* **Deterministic (Extracted Facts):**
  * `id`: The invocation **name**, which is platform-invariant — unlike the executable path (`build/test/foo` vs `build\test\foo.exe`). This is the join key; invocations flow through *The Overlay Model* like any node (a platform-only test → presence overlay; per-leg env → attribute overlay).
  * `kind`: `TEST | DRIVER`.
  * `verdict`: The pass/fail criterion (`TEST` only; absent for a `DRIVER`). Derived from properties such as `WILL_FAIL`, `PASS_REGULAR_EXPRESSION`, else default "exit code 0".
  * `command`: The argv to run. `command[0]` is a build-tree path resolved to the producing **artifact node** by the same output-path correlation as the produced-by edge (Fan-Out 3) — but it **may not be one of our artifacts** (it can be `python`, a wrapper script, or a system tool); when it resolves to none, it is kept as a raw command, not forced to a dangling artifact reference.
  * `runtime_deps` (a.k.a. `data`): targets and generated files that must be **up-to-date and present in the runtime tree** before the invocation runs, but which it does *not* link against — a `TEST` reading a generated `test_data.json` or a fixture dir, a `TEST` that shells out to a *second* built tool, `make deploy` needing the main `EXECUTABLE` built before its AWS-CLI script runs. This is **distinct from Fan-Out 2 `deps`** (link-against): an invocation links nothing, it requires things to *exist at run time*. Maps to Bazel `data` (runfiles), not `deps`. Note `command[0]`'s producing artifact is an *implicit* member — `runtime_deps` captures the rest.
    * **Extraction confidence:** the `command[0]` artifact is recovered by output-path correlation (high). The remaining runtime deps are generally **not observable** from the test listing or `compile_commands.json` — they surface only from a source-side clause (`DEPENDS`, `FIXTURES_REQUIRED`, a fixture-dir argument; Tier 2) or not at all. When unrecoverable, this is a known blind spot: the field is left `unknown` (not empty — per *Failure Semantics*, an empty `runtime_deps` would falsely assert "needs nothing at run time") and the linter flags it.
  * `exec_platform`: `HOST | TARGET` — which toolchain the invocation runs under (see Fan-Out 4). A test that exercises the built artifact is `TARGET` (and is unrunnable on the build machine when cross-compiling — the backend must emit it as a target-platform test, not a build-time action); a host-side driver like `format` is `HOST`. Defaults to `TARGET` for a `TEST`, `HOST` for a `DRIVER`.
  * `working_dir` and `env`: The invocation's working directory and environment. **These are correctness-critical and saturated with absolute paths** (`WORKING_DIRECTORY=/abs/...`, `ENVIRONMENT=[MY_TESTS_SOME_DIR=/abs/...]`, even toolchain paths like `STANDARD_COMPILER=/usr/bin/c++`). The hermetic-abstraction machinery (Fan-Out 4) **must** apply here — an un-abstracted absolute path means the migrated invocation will not run. A path that cannot be made workspace-relative is `raw` and linter-flagged, never silently emitted.
  * `properties`: An open key→value bag (`LABELS`, `TIMEOUT`, `WILL_FAIL`, `PASS_REGULAR_EXPRESSION`, `FIXTURES_*`, ...). Recognized keys are normalized to IR fields; unrecognized keys are retained as `raw` passthrough (per *Failure Semantics*) rather than dropped.
  * The source-side `provenance` is recoverable from the listing's backtrace graph (defining file, line, and the macro/command used — e.g. a wrapper `add_test_with_..._ENV` expanding to `add_test`), which populates `provenance.sources` precisely.

* **Essential fields** (an `unknown` here → `INCOMPLETE`): `id` (the name), `kind`, and `command` (the argv — an invocation whose command is unknown cannot be run). `verdict` is essential for a `TEST` (a test with no pass criterion is meaningless) and absent for a `DRIVER`. `working_dir`, `env`, and `properties` are **not** essential — they are commonly absent (a test with no `WORKING_DIRECTORY` runs in the default cwd); an absent one is a valid fact, not a failure. `exec_platform` has a default and is not essential. `runtime_deps` is **not** essential (an invocation may genuinely need nothing), but an `unknown` `runtime_deps` is a linter finding (a missing prerequisite could make the migrated invocation fail at run time), not an `INCOMPLETE`.

* **Backend Generation:**
  * A `TEST` generates into the target system's test rule (Bazel `cc_test` referencing the artifact, with `args`/`env`/`tags`; or `add_test()`). Tags/labels map to the target system's grouping (`LABELS` -> Bazel `tags`).
  * A `DRIVER` generates into a runnable, non-test rule (Bazel `sh_binary` / a `run_binary`-style rule; or `add_custom_target()` for CMake-to-CMake) — never a `*_test` rule, since it has no verdict.

### Fan-Out 6: Install / Packaging Rules

An install rule is **neither an artifact nor a build step** — it is a post-build *placement rule*. This is the area where source systems diverge most and where faithful transfer is hardest, so it is modeled deliberately.

* **Deterministic (Extracted Facts):**
  * `source`: What is placed — an **artifact** (a built target, by `id`), or **raw files/directories** that are not build outputs at all (`install(FILES include/foo.h ...)`, docs, license, data). These are different cases and tagged distinctly.
  * `destination`: Where it goes, captured **relative to the install prefix** (`<prefix>/lib`, `bin`, `include`) — **never** the resolved absolute path. This is the hermetic-abstraction pattern again: a frozen `/usr/local/lib` would destroy relocatability. The prefix itself is a configure-time variable.
  * `component`: The grouping tag (`runtime` vs `devel`) that downstream packaging (CPack, deb/rpm `-dev` splits) depends on. Dropping it silently merges packages, so it is preserved even when the immediate backend ignores it.
  * `attributes`: Permissions, `RENAME`, RPATH fixup (install **rewrites** RPATH — a real transformation, not a copy), stripping, and SO-version symlinks (`libfoo.so → libfoo.so.1.2`). Unmodeled attributes are `raw`/linter-flagged, never dropped.

* **Essential fields** (an `unknown` here → `INCOMPLETE`): `source` (what is placed) and `destination` (the prefix-relative target — an install with unknown destination cannot be emitted). `component` and `attributes` are **not** essential — a rule with no component is valid (default/unstated), and unmodeled attributes degrade to `raw` rather than blocking.

* **Backend Generation (fidelity is backend-dependent):**
  * Unlike compile/link, install has **no universal target**. CMake-to-CMake re-emits `install(...)`; a `pkg_tar`/`rules_pkg` rule approximates it for Bazel; some backends have **no equivalent at all**. When a backend cannot map an install rule, that is a linter finding (an unmapped concept), **never a silent drop**. This asymmetry — install fidelity varies by backend in a way compile fidelity does not — is itself part of the contract.

* **Explicit Non-Goal — `install(EXPORT)` / package-config generation.** Generating `FooConfig.cmake` / `FooTargets.cmake` so downstream projects can `find_package(Foo)` is *install-time code generation* — effectively a second backend, not a target attribute. It is **out of scope for now** and recorded as a named future subsystem rather than half-modeled. An encountered `install(EXPORT)` is captured as `raw` and linter-flagged as unsupported.

---

## 4. Phase 2 Confidence Tiers

Augmentations are not uniform in trust. Each carries a confidence tier derived from its source `provenance`, and the tier dictates whether it is applied automatically or held for human sign-off.

* **Recovered (high confidence).** Derived from a Tier 2 intent source that states the answer directly. The augmenter reads the original construct rather than guessing. Applied automatically.
* **Suggested (needs review).** Inferred by heuristic when no intent source resolves the question. Emitted as a proposal the linter surfaces for human confirmation.

### Worked Example: Glob Reconstruction

Glob reconstruction shows why the tiers matter and why intent sources are load-bearing rather than cosmetic.

* **Source already globbed** (`file(GLOB ...)`, `$(wildcard ...)`) → the augmenter reads the actual glob from the Tier 2 source and reproduces it. **Recovered.** This is not reconstruction-by-guessing; it is reading the original.
* **Source listed files explicitly** → the right answer is an explicit list, and the source says so. The augmenter emits the explicit list. **Recovered** (the recovered decision is "do not glob").
* **Genuinely ambiguous** — source has an explicit list that happens to equal the directory contents, and surrounding code/comments don't reveal whether that was deliberate → better-informed inference, but still inference. **Suggested**, flagged for review.

This is why glob handling specifically cannot be auto-blessed by the validator: a glob can be correct for every file present at extraction time yet wrong about which *future* files belong, and validation is point-in-time (see *Validation*). Recovering the answer from the Tier 2 source — rather than promoting an explicit list to a glob speculatively — is what keeps this within *Faithful Transfer, Not Improvement*.

### Frontend Locality of Intent Augmentation

Augmentations that read Tier 2/3 sources (glob recovery, template detection, CI synthesis) are **frontend-specific** — they parse `CMakeLists.txt`, Ninja files, MSBuild XML, CI YAML. Per the Source Evidence Model, this format-specific work lives in the **frontend collectors**, which normalize their findings into uniform IR augmentations. The shared backend generator never parses a source build format; it consumes only normalized IR.

---

## 5. Validation

Validation is the safety net that lets Phase 2 be permissive: augmentations may be speculative because divergences are caught by comparison rather than trusted blindly. There are two passes.

### Pass 1 — Cross-Source Corroboration (intra-extraction)

Because the frontend consumes multiple sources, the same fact often appears in more than one (e.g. a generator invocation visible in both the Ninja action graph and the `CMakeLists.txt` `add_custom_command`). At extraction time:

* **Agreement** across sources raises the fact's confidence.
* **Conflict** is a divergence signal raised *before generation*, attributed via `provenance` to the disagreeing sources.

### Pass 2 — Command-Graph Comparison (post-generation)

After the backend generates the target build system, the tooling invokes the **generated** build's configuration/analysis phase to produce its resolved command graph, and diffs it against the source legs' command graphs (per leg). Divergences are reported.

The diff is **normalized, not literal**. Some facts are *expected* to diverge by design — hermetic toolchain paths (the target re-resolves `/usr/bin/c++` to its own path) and externally-normalized packages (re-resolved from the target registry). These are flagged in `provenance` and **excluded from the comparison** (or compared structurally rather than by absolute string); otherwise every hermetic path would surface as a false-positive divergence. Their correctness rests on the linter, not on Pass 2 — see Fan-Out 2 and Fan-Out 4. (Consequently this pass does *not* catch a hermetic/package reference resolving to the wrong target — that is a semantic claim only the linter can flag.)

After normalization, the pass deterministically catches everything else observable in the legs: a glob too narrow (a compiled file dropped), a glob too wide over files that exist now, or a flag/define dropped or mistranslated. Such errors are **incorrect-but-detected** — consistent with the fail-safe posture.

---

## 6. The Linter / Semantic Lint

Validation (§5) checks what is *observable* by comparing command graphs. The linter checks what is *not* observable: semantic claims the comparator cannot confirm, and augmentations that were applied speculatively. It is the component that makes the trust story legible — its single responsibility is to consume `provenance` and confidence tiers across the IR and emit a severity-ranked report. Migration is permissive by design (fail-safe extraction, speculative augmentation); the linter is where that permissiveness is surfaced for human judgment rather than hidden.

### Inputs

* The `provenance` and confidence tier (`Recovered` / `Suggested`) of every augmented fact.
* Overlay-model diagnostics (leg-sets that are not faces).
* Cross-source conflicts from Validation Pass 1.

### What It Flags

The linter consolidates duties that are otherwise scattered across the spec into one catalogue, ranked by severity:

| Severity | Meaning | Examples |
|---|---|---|
| **Error** (blocks unattended migration) | A correctness-sensitive claim that could not be recovered and validation cannot bless | External package whose **version/identity could not be recovered** from a manifest (§ Fan-Out 2); a `Suggested` mapping in a correctness-critical position; an **`INCOMPLETE` node** (essential field `unknown`; *Failure Semantics*); a **test path/env that could not be made workspace-relative** (the migrated test will not run; Fan-Out 5) |
| **Warning** (needs review) | Speculative augmentation that may alter *unobserved/future* behavior | `Suggested` glob promoted from an explicit list (§4); non-face overlay diffs requiring a missing constraint axis (*Overlay Model*); an **excluded leg** reducing platform coverage (*Failure Semantics*); an **install rule the backend cannot map** or an **`install(EXPORT)`** (unsupported concept, never silently dropped; Fan-Out 6); an **inferred `exec_platform`** on an action/test from a native-only extraction (wrong host/target split crashes a future cross-compile; Fan-Out 4); a **lifecycle step the backend cannot map** (e.g. a `POST_BUILD` hook on a Bazel target; restructure required, never silently dropped; Fan-Out 1); an **`unknown` `runtime_deps`** on an invocation (an unrecovered fixture/prerequisite may make the migrated test fail at run time; Fan-Out 5) |
| **Info** (correct, low confidence) | Migration is correct but idiomaticity is reduced for lack of intent sources | Tier-1-only project with no intent sources (*Source Evidence Model*); cross-source conflicts already resolved in favor of the higher tier; **`DEGRADED` nodes** with `raw` passthrough fields (*Failure Semantics*); **probe-derived values** (`was_probed`) hardcoded per-toolchain, which will not re-probe on new platforms (Fan-Out 4) |
