# Case study: VSCode → Bazel, and what you do when there is no action graph

VSCode's build is bespoke: gulp streams, a custom incremental TypeScript
compiler (`gulp-tsb`), esbuild driven from JS, and a set of hand-written
transforms that run *between* the compile and the bundle. It has no
`compile_commands.json`, no CMake File API, no Maven reactor to interrogate.
There is nothing to extract.

That single fact drives everything in this document. Every other migration in
[EXPERIMENTS.md](../EXPERIMENTS.md) starts by *reading* the original build's
own description of what it does; this one has to start by *watching the build
run*.

Subject: `ulfjack/vscode@vscode-with-bazel` (7 commits on vscode 1.132.0),
Bazel 7.5.0, 2016 added lines: 4 custom rules (504 lines of Starlark), 5 Node
tool scripts + 3 shell scripts (1055 lines), 44 targets, and **zero** external
Bazel modules — no `bazel_dep` at all, only two `http_archive`s for Electron
and its node headers.

## What is verified here, and on what

Everything below was re-run from scratch on a clean checkout for this
document. Where a claim is *inferred* from reading code rather than observed,
it says so explicitly.

| | |
|---|---|
| npm reference build | `gulp compile-build-without-mangling`, 0 errors, 8.4 min |
| Bazel build | `bazel build //...` → 44 targets green; `//:core` 352 s in **one** action |
| Captured npm actions | **7722** (7720 `TsCompile` + 2 `Spawn`) |
| Model comparison | 7676 npm sources vs 7675 Bazel; **0** extension diffs |
| **Byte parity** | **7710 of 7710** common `.js` files **byte-identical**, 0 differing |
| **NLS metadata** | all 4 files byte-identical, incl. 2.3 MB `nls.metadata.json` |
| Native modules | 9 of 9 `.node` binaries built by Bazel via node-gyp |
| Runtime | Electron boots the Bazel-built tree and runs the workbench |

## 1. No action graph, so capture the build instead of reading it

The extractor for a bespoke build cannot synthesize a model; it has to
*record* one. `scripts/npm_instrument/preload.mjs` is a Node `--import`
preload that hooks the build in-process and appends one NDJSON record per
action, with no modification to the target repo:

```
NODE_OPTIONS='--import file:///…/preload.mjs' VSCODE_EMIT_BUILD_IR=…/actions.ndjson \
  npm run gulp compile-build-without-mangling
```

`NODE_OPTIONS` propagates into every child `node` process, so nested tool
invocations are instrumented from the same two environment variables — no
per-tool wrappers. Two surfaces are hooked: `child_process.{spawn,spawnSync,
execFile,execFileSync}` as a catch-all, and the TypeScript / esbuild module
exports.

**The interesting part is that hooking module exports is now hard.** The
preload's own comments record three failed approaches, and they're worth
keeping because each is the obvious one:

- mutating `module.exports` after the fact — ESM named imports from CJS
  snapshot the bindings at first import, so importers keep the originals;
- swapping `Module._cache` entries — the CJS→ESM translator snapshots named
  bindings at synthesis time;
- wrapping in a `Proxy` — the properties are non-configurable getters.

What works is an ESM loader (`loader.mjs`) that redirects the `typescript`
specifier to a shim with real ESM exports, plus appending a self-contained
wrapper to esbuild's `main.js` at *load* time. This is not incidental: **on
modern Node, in-process interception has to happen at module-resolution time,
not after import.** Any capture-based frontend for a JS build inherits that
constraint.

### The capture sees the build system using its own compiler

One source appeared on the npm side only, out of 7676: `file.ts`, 45 times.
It is not a source file. It is the synthetic in-memory filename vscode's own
build helpers hand to `ts.createSourceFile` / `getEmitOutput` —
`build/lib/nls-analysis.ts:115` (once per localized file, hence 45) and
`build/lib/monaco-api.ts:226,674`.

This is the characteristic artifact of capture-based extraction: hooking the
compiler API records *every* use of the compiler, including the build system's
own analysis passes, not just the actions that produce build outputs. A
File-API or aquery model can't contain this by construction. Worth stating as
a rule: **when the model is captured rather than declared, the build system's
own tooling shows up in it, and the extractor needs a story for that** — here,
a synthetic single-target grouping that the differ reports rather than hides.

## 2. Matching actions, not writing idiomatic Bazel

The stated aim of the rules is parity, not idiom, and the comments say so up
front — `ts_program.bzl`: "aimed at MATCHING the npm build's output surface
(not at idiomatic Bazel)". Three consequences, all deliberate:

**One action for the whole program.** `ts_program` runs `tsc --project` once
and declares **one** `declare_directory` instead of ~21k individually declared
files. Measured: 352 s of critical path in a single action, no parallelism, no
per-file caching. The idiomatic Bazel answer (per-file actions, or
`ts_project` from rules_ts) would have changed the emit surface being
compared; the whole-program compile is what TypeScript exposes natively and
what gulp-tsb uses under the hood.

**Flags forced on the command line, because the original forces them in
code.** `build/lib/compilation.ts:38` sets `options.sourceMap = true`
unconditionally, overriding the tsconfig, so the rule passes `--sourceMap`
explicitly to line up. You cannot read this off the tsconfig; it only exists
in the build system's JS.

**The transform is ported, not re-implemented.** See §3.

This is the same conclusion [BAZEL-RULES.md](BAZEL-RULES.md) reaches from the
ruleset side, arrived at independently: `ts_project`'s validator rejects
attributes that disagree with the tsconfig, and an `outDir` above the package
is rejected outright, so a ruleset built around idiomatic layout cannot express
"reproduce this build's exact output surface."

## 3. A transform whose output depends on traversal order

This is the finding that generalizes furthest.

VSCode's NLS (localization) pass rewrites `localize("key", "English")` into
`localize(NNN, null)`, where `NNN` is **a global counter incremented
monotonically across all files**. Upstream knows exactly how fragile that is —
`build/lib/nls.ts:24`, their comment, their capitals:

```js
.pipe(sort()) // IMPORTANT: to ensure stable NLS metadata generation, we must sort the files because NLS messages are globally extracted and indexed across all files
```

So the *output bytes of nearly every file* depend on the *order in which the
build walked the file set*. `bazel/tools/nls_transform.mjs` (408 lines) is a
port of `build/lib/nls.ts` + `nls-analysis.ts` that reproduces gulp-sort's
comparator: sort by absolute path with `String.localeCompare`.

**Verified, both directions.** With the port as written, all four NLS outputs
are byte-identical to npm's, including the 2.3 MB `nls.metadata.json`:

```
nls.keys.json      IDENTICAL   (755,433 bytes)
nls.messages.json  IDENTICAL   (1,123,296 bytes)
nls.metadata.json  IDENTICAL   (2,302,110 bytes)
nls.messages.js    IDENTICAL   (1,123,511 bytes)
```

Then I reversed that one comparator (`b.abs.localeCompare(a.abs)`) and
rebuilt. Result: **1903 of 7710 `.js` files differ**, all four NLS files
differ — and **the build still reported success**. No type error, no failed
action, no diagnostic. A green build is not evidence about this class of bug at
all; only byte comparison against the reference is.

The lesson for any2bazel, stated generally: **a build step whose output
depends on traversal order cannot be validated by any amount of building.** It
has no flags to compare, so an action-graph diff is blind to it by
construction (the same blind spot Dolphin §4/§6 hit from the host-dependency
side). It is caught by byte-comparing outputs, and it is *found* by reading the
original build for global mutable state. Grep targets: a counter or index
incremented across files, and any explicit sort whose comment explains why it
matters.

A second instance in the same file, not yet handled on the Bazel side:
`compilation.ts:45` computes the emitted line ending by reading **its own
source file's bytes** —

```js
options.newLine = /\r\n/.test(fs.readFileSync(import.meta.filename, 'utf8')) ? 0 : 1;
```

so vscode's emitted line endings are a function of how *the build script
itself* was checked out (`core.autocrlf`). The Bazel rules don't reproduce
this; parity held here because the checkout is LF. **Inferred, not observed:**
on a CRLF checkout I'd expect every emitted file to differ. It is the purest
example of a host question the action graph cannot see.

## 4. The source tree is a build output

`bazel build //:bundles` failed on:

```
out-build/vs/base/browser/ui/codicons/codicon/codicon.css:9:10:
  ERROR: Could not resolve "./codicon.ttf"
```

`codicon.ttf` is **gitignored** (`.gitignore:11`) yet its location is **inside
`src/`**. `build/lib/compilation.ts:434-446` copies it there from
`node_modules/@vscode/codicons/dist/`, as an npm postinstall step, and the
copy is repeated as a gulp task guarded by a friendly error message telling
you to run `npm install`.

So `glob(["src/**"])` — the most innocuous line in the BUILD file — silently
depends on whether a *non-Bazel* step ran first. I only hit it because I ran
`npm ci --ignore-scripts`; with scripts enabled the file is already sitting in
the tree and the Bazel build looks self-contained. Rule: **a glob over a
directory that the original build writes into is an undeclared dependency on
the original build.** The tell is a gitignored path inside a source directory.

## 5. `node_modules` is both the toolchain and a hostile package universe

Three distinct problems, one directory.

**It is the toolchain.** `//:core` needs
`node_modules/typescript/lib/tsc.js`, and the bundles need
`build/node_modules/esbuild` — vscode has a *second* npm project under
`build/` with its own lockfile, so a single `npm ci` at the root is not
enough to build. (Missed on first attempt; the rule fails loudly, which is
correct behavior.)

**It contains a foreign Bazel package.** `node_modules/cpu-features/deps/
cpu_features/` ships its own `BUILD.bazel` *and* `WORKSPACE`, which breaks
`bazel build //...` outright:

```
error loading package 'node_modules/cpu-features/deps/cpu_features':
  cannot load '//:bazel/platforms.bzl': no such file
```

This is Dolphin §8's problem arriving from the opposite direction — there, *we*
had put build files inside vendored submodules; here a *dependency* ships them
uninvited. And the obvious fix is wrong: I measured it.
`echo node_modules > .bazelignore` makes the build fail immediately with
`typescript filegroup does not include node_modules/typescript/lib/tsc.js`,
because `.bazelignore` hides the very sources you need. The fix that works is
one line of `.bazelrc`:

```
build --deleted_packages=node_modules/cpu-features/deps/cpu_features
query --deleted_packages=node_modules/cpu-features/deps/cpu_features
```

Same conclusion as Dolphin §8, now confirmed on a second, structurally
different repo: **neutralize the stray package, don't hide the directory.**
Note the subject repo has no `.bazelrc` at all, so `bazel build //...` does not
currently work as committed; the per-target builds do.

**It is where the runtime looks.** `vscode_app` assembles only the JS tree —
by design, per its docstring — so the Bazel-built app has no `node_modules`.
`bazel run //:vscode` boots Electron, loads the bundled `main.js`, and then the
extension host dies with `Cannot find package '@vscode/spdlog'` (and
`@vscode/sqlite3`, `@vscode/deviceid`, `@xterm/headless`, `native-keymap`).
The npm-populated tree, overlaid with the 9 Bazel-built `.node` binaries, is
the supported path. The migration's boundary is therefore *the JS build*, not
the application.

## 6. Native modules: a build action that asks the host questions

`nodegyp_module` drives `node-gyp` per native package — 9 of them, all 9
building successfully against Electron's node headers. Two findings.

**A build action reaching the network.** `run_nodegyp.mjs:109-115` runs
`node-gyp` from `PATH` and, on `ENOENT`, falls back to
`npx --yes node-gyp` — which *downloads node-gyp from the npm registry inside
a Bazel action*. Observed directly: `npm error code SELF_SIGNED_CERT_IN_CHAIN
… request to https://registry.npmjs.org/node-gyp failed`. On the authoring
host `node-gyp` was on `PATH`, so the fallback never fired and the
non-hermeticity stayed invisible. This is Dolphin §6's lesson in a new place:
the build worked on exactly one machine's configuration.

**pkg-config inside the sandbox.** `native-keymap`'s `binding.gyp` runs
`${PKG_CONFIG:-pkg-config} x11 xkbfile --libs`, so the action failed until I
installed `libx11-dev` / `libxkbfile-dev` on the host. The host question that
Dolphin's CMake asked through `find_package` is asked here by a `binding.gyp`
inside a Bazel action, which is strictly worse: it is invisible to both build
systems' dependency declarations.

## 7. Two version claims that are wrong as committed

**Electron is pinned to a version `package.json` doesn't request.**
`MODULE.bazel:19` says "Version pinned to what package.json requests" and
pins **42.2.0**; `package.json` has requested **42.7.0** since commit
`8748be1f1a8` (2026-05-27), *before* the first Bazel commit (2026-07-14).
Verified by running the binary Bazel actually fetched: `v42.2.0`. The same
42.2.0 is passed as `electron_target` for all 9 native modules, so they are
compiled against an ABI the npm build does not use. A comment asserting a
version relationship is not a mechanism that maintains one — and this is the
JS analogue of Dolphin's pasted `6.10.2`.

**Both launcher scripts compute the repo root one level too high.**
`copy-native-modules.sh:15` and `run-vscode-from-bazel.sh:16` both do
`ROOT=$(cd "$(dirname "$0")"/../../.. && pwd)` from `bazel/tools/`, which is
two levels down — so `ROOT` lands *above* the checkout and the script dies with
`ERROR: The 'info' command is only supported from within a workspace`. Both
files have been at `bazel/tools/` since they were added (`bf33cc55256`), so
they never worked as committed. The mechanism does work: with the path
corrected, all 9 `.node` files copy into place.

## 8. What this migration establishes, and what it doesn't

**Establishes.** A bespoke JS build *can* be reproduced byte-for-byte by
Bazel: 7710/7710 `.js` identical plus byte-identical NLS metadata, with 4
custom rules and no rulesets. The parity is not superficial — it survives a
global-counter transform, a forced-`sourceMap` override, and a 2.3 MB
generated metadata file.

**Doesn't.** Scope is the core TS compile, the 23 bundles, 9 native modules
and a runnable app tree. Not covered: extensions
(`build/gulpfile.extensions.ts`), tests, web/REH/CLI variants, packaging,
mangling (`compile-build-with-mangling` fails upstream at this commit,
independently of Bazel). A migration supports conclusions only about the parts
it actually migrated, so the findings here are about *the JS build pipeline*;
nothing here says what extensions or packaging would cost.

The rationale for design decisions lives **only in code comments**; all 7
commit messages are bare one-liners ("Add bazel rules", "Fix rule locations").
Every intent claim above is sourced to a comment and marked as such, and every
number is reproduced by the commands in the appendix rather than taken from a
comment.

## Appendix: reproducing these findings

Prerequisites that are themselves findings: node **24.18.0** (per `.nvmrc`;
22.x fails `npm ci` outright), `npm ci` at *both* the root and `build/`,
`node-gyp` on `PATH`, and `libx11-dev libxkbfile-dev libsecret-1-dev
libkrb5-dev` for the native modules.

```bash
# §4  the gitignored file inside src/ that the npm build writes
git check-ignore -v src/vs/base/browser/ui/codicons/codicon/codicon.ttf
grep -n codiconDest build/lib/compilation.ts

# §5  the foreign Bazel package inside node_modules
find node_modules \( -name BUILD.bazel -o -name WORKSPACE \) | head
echo node_modules > .bazelignore && bazel build //:core   # fails: no tsc.js
rm .bazelignore                                            # use --deleted_packages

# §1  capture the npm build's action graph (there is no aquery here)
cp ../any2bazel/scripts/npm_instrument/*.mjs .instr/
NODE_OPTIONS="--import file://$PWD/.instr/preload.mjs" \
VSCODE_EMIT_BUILD_IR=$PWD/actions.ndjson \
  npm run gulp compile-build-without-mangling
wc -l actions.ndjson                     # 7722 actions

# §1  the build system's own compiler use shows up in the capture
grep -c '"file.ts"' actions.ndjson       # 45; see nls-analysis.ts:115

# model-level comparison
python3 ../any2bazel/scripts/extract_npm.py actions.ndjson "$PWD" model.npm.json
bazel aquery 'mnemonic("TsProgram", //:core)' --output=jsonproto > aquery.json
python3 ../any2bazel/scripts/extract_bazel.py aquery.json "$PWD" model.bazel.json
python3 ../any2bazel/scripts/diff_ts.py model.npm.json model.bazel.json

# byte parity: 7710/7710 identical
python3 - <<'EOF'
import os, hashlib
def m(root):
    d = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.endswith('.js'):
                p = os.path.join(dp, f)
                d[os.path.relpath(p, root)] = hashlib.sha256(open(p, 'rb').read()).hexdigest()
    return d
a, b = m('out-build'), m('bazel-bin/out-build')
c = set(a) & set(b)
print('common', len(c), 'identical', sum(1 for k in c if a[k] == b[k]))
EOF

# §3  NLS byte parity, then prove the sort order is load-bearing
for f in nls.keys.json nls.messages.json nls.metadata.json nls.messages.js; do
  cmp -s out-build/$f bazel-bin/out-build/$f && echo "$f IDENTICAL" || echo "$f DIFFERS"
done
# reverse the comparator at nls_transform.mjs:338, rebuild //:core, re-run the
# byte check: 1903/7710 .js differ and the build still reports success.

# §7  the Electron version actually fetched vs. requested
find ~/.cache/bazel -name electron -type f -executable | head -1 | xargs -I{} {} --version
python3 -c "import json;print(json.load(open('package.json'))['devDependencies']['electron'])"
```
