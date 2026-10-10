# Reference build: VSCode / npm

Supporting context for migrating **from** an npm/gulp/esbuild build. The
migration process itself lives in [SKILL.md](../SKILL.md); this file only
covers what is unique to npm as the reference build.

**Status: experimental** — not wired into the main parity loop.

## Detecting it

A `package.json` at the repo root whose build scripts drive gulp, esbuild, or
`tsc`.

## Scope traps

This frontend is **not integrated into the parity loop**: there is no
`diff.py`/`triage.py` pass. `diff_ts.py` is a standalone TS source→emit check
that replaces the diff and triage steps.

## Extracting the reference model

An npm/gulp/esbuild build is instrumented in-process by a Node preload (hooks
esbuild, the TS language service, and `child_process`), emitting one NDJSON
record per action.

The preload must be run from a copy **inside the target repo**
(`cp "$SKILL_DIR"/scripts/npm_instrument/*.mjs <repo>/.instr/`):
`typescript-shim.mjs` resolves `typescript` relative to its own location, so
running it from the skill directory fails with `ERR_MODULE_NOT_FOUND`.
```bash
NODE_OPTIONS="--import file://<repo>/.instr/preload.mjs" \
VSCODE_EMIT_BUILD_IR=$PWD/actions.ndjson \
    npm run <build-script>
python3 "$SKILL_DIR/scripts/extract_npm.py" actions.ndjson <repo_root> model.npm.json
```

## Comparison

```bash
python3 "$SKILL_DIR/scripts/diff_ts.py" model.npm.json model.bazel.json    # standalone check
```

Worked example, with byte-parity results and the failure modes a bespoke JS
build brings (no action graph to extract, traversal-order-dependent output,
`node_modules` as both toolchain and foreign Bazel package):
[CASE-vscode-migration.md](CASE-vscode-migration.md).
