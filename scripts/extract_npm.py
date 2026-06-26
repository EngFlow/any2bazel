"""Extract a CanonicalModel from an npm/gulp/esbuild build INSTRUMENTED by
scripts/npm_instrument/preload.mjs.

There is no native action graph for an npm-driven build (no aquery, no
File-API codemodel). Like Maven, we *capture* instead of synthesize: the Node
preload hooks esbuild + child_process at runtime and writes one NDJSON record
per build action. This script reads that NDJSON and groups it into the
shared CanonicalModel.

Identity choices (subject to revision once a real-vs-real diff lights up):
  * EsbuildBundle target name = first output path (deterministic; same entry
    point produces the same output across runs).
  * EsbuildTransform target name = "<transform>" (one synthetic target for the
    project-wide per-file transpile pass; the differ groups its actions by
    sourcefile the same way Java compiles group by source-set).
  * Spawn target name = the spawned executable's basename. Roles are
    classified DASHBOARD by default; tsgo/tsc/eslint/stylelint live here.

Usage:
    # Run the instrumented build first:
    NODE_OPTIONS="--import file://$PWD/cmake2bazel/scripts/npm_instrument/preload.mjs" \\
    VSCODE_EMIT_BUILD_IR=$PWD/actions.ndjson \\
    npm run transpile-client

    python3 cmake2bazel/scripts/extract_npm.py actions.ndjson "$PWD" model.npm.json
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, Iterable, List, Optional

from model import (Action, BuildSystem, CanonicalModel, Target, TargetKind,
                   TargetRole)
from serialize import dump_model


_INTERESTING_MNEMONICS = ("EsbuildBundle", "EsbuildTransform", "Spawn")


def _make_rel(repo_root: str):
    """Return a function that rewrites any literal occurrence of `repo_root/`
    inside a string to its repo-relative form. Applied to inputs, outputs, and
    argv entries -- both bare paths and embedded `--sourcefile=/abs/...` forms
    become repo-relative so the model is portable across checkouts."""
    if not repo_root:
        return lambda s: s
    root = repo_root.rstrip("/") + "/"
    def rel(s: str) -> str:
        return s.replace(root, "") if isinstance(s, str) else s
    return rel

# Spawned commands that produce no artifact: hygiene / lint / type-check passes.
# Classified DASHBOARD so they show up under `excluded` rather than being
# diffed. Anything else spawned (notably `tsgo` when it emits) stays UNKNOWN
# until we have a concrete reason to promote it.
_DASHBOARD_SPAWN_BASENAMES = frozenset({
    "eslint", "tsec", "stylelint", "tsgo",
})


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


def _classify_bundle_role(outputs: Iterable[str]) -> TargetRole:
    """Production for normal bundle output; TEST when an output path implies
    test artifacts. Heuristic and conservative -- unknown stays UNKNOWN so it
    surfaces."""
    for o in outputs:
        op = o.replace(os.sep, "/")
        if "/test/" in op or op.endswith("/test"):
            return TargetRole.TEST
    return TargetRole.PRODUCTION


def _target_for_bundle(rec: dict) -> str:
    outs = rec.get("outputs") or []
    if outs:
        # First output is stable across runs for a given entry point.
        return outs[0]
    args = rec.get("arguments") or []
    # Fallback: the entryPoints field flattened into argv by preload.mjs.
    for a in args:
        if a.startswith("--entryPoints=") or a.startswith("--outfile=") or a.startswith("--outdir="):
            return a.split("=", 1)[1]
    return "<esbuild>"


def _spawn_target(rec: dict) -> str:
    args = rec.get("arguments") or []
    return _basename(args[0]) if args else "<spawn>"


def _spawn_role(name: str) -> TargetRole:
    return (TargetRole.DASHBOARD if name in _DASHBOARD_SPAWN_BASENAMES
            else TargetRole.UNKNOWN)


def _add_action(model: CanonicalModel, name: str, kind: TargetKind,
                role: TargetRole, action: Action) -> None:
    t = model.targets.get(name)
    if t is None:
        t = Target(name=name, kind=kind, role=role)
        model.add(t)
    t.actions.append(action)


def extract(ndjson_path: str, repo_root: str) -> CanonicalModel:
    model = CanonicalModel(build_system=BuildSystem.NPM, repo_root=repo_root)
    rel = _make_rel(repo_root)
    with open(ndjson_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Don't fail the extract on a half-written line (build may have
                # been killed mid-flush). Skip and continue.
                continue
            mnemonic = rec.get("mnemonic")
            if mnemonic not in _INTERESTING_MNEMONICS:
                continue
            action = Action(
                mnemonic=mnemonic,
                arguments=tuple(rel(a) for a in (rec.get("arguments") or [])),
                inputs=tuple(rel(p) for p in (rec.get("inputs") or [])),
                outputs=tuple(rel(p) for p in (rec.get("outputs") or [])),
            )
            if mnemonic == "EsbuildBundle":
                # Classify off the ORIGINAL outputs -- _classify_bundle_role
                # and _target_for_bundle don't care about absolute vs relative.
                name = rel(_target_for_bundle(rec))
                role = _classify_bundle_role(rec.get("outputs") or [])
                _add_action(model, name, TargetKind.UNKNOWN, role, action)
            elif mnemonic == "EsbuildTransform":
                _add_action(model, "<transform>",
                            TargetKind.UNKNOWN, TargetRole.PRODUCTION, action)
            elif mnemonic == "Spawn":
                name = _spawn_target(rec)
                _add_action(model, name, TargetKind.UNKNOWN,
                            _spawn_role(name), action)
    return model


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: extract_npm.py <actions.ndjson> <repo_root> <out.json>")
    ndjson, repo_root, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
    dump_model(extract(ndjson, repo_root), out)
    print(f"wrote {out}")
