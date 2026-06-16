"""Extract a CanonicalModel from `bazel aquery --output=jsonproto`.

aquery is the Bazel-side oracle that matches the CMake File API: it exposes
the actual actions the build would run -- CppCompile actions (one per TU, with
the full argv) AND CppLink/CppArchive actions (the link closure). compile
commands alone would miss the link half, so we use aquery.

Usage:
    bazel aquery 'mnemonic("CppCompile|CppLink|CppArchive", //...)' \
        --output=jsonproto > aquery.json
    python3 extract_bazel.py aquery.json <repo_root> model.bazel.json

The jsonproto shape (ActionGraphContainer):
    { "artifacts": [{id, pathFragmentId}], "actions": [{mnemonic, arguments[],
      targetId, outputIds[]}], "targets": [{id, label}], "pathFragments": [...] }
We reconstruct artifact paths from pathFragments, map actions -> targets, and
canonicalize each CppCompile's argv into a TranslationUnit.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional

from canonicalize import canonicalize_flags
from model import (CanonicalModel, Dependency, Target, TargetKind,
                   TranslationUnit)
from serialize import dump_model

_COMPILE = {"CppCompile"}
_LINK = {"CppLink", "CppArchive"}

# map output extension -> target kind (aquery doesn't label rule kind directly
# in the action graph, so we infer from the primary output of the link action)
def _kind_from_output(path: str) -> TargetKind:
    if path.endswith(".a"):
        return TargetKind.STATIC
    if path.endswith(".so") or path.endswith(".dylib") or ".so." in path:
        return TargetKind.SHARED
    return TargetKind.EXECUTABLE


def _build_path_index(container: dict) -> Dict[int, str]:
    """Reconstruct full paths from the pathFragments linked list."""
    frags = {pf["id"]: pf for pf in container.get("pathFragments", [])}

    cache: Dict[int, str] = {}

    def resolve(fid: int) -> str:
        if fid in cache:
            return cache[fid]
        pf = frags[fid]
        parent = pf.get("parentId")
        label = pf.get("label", "")
        path = os.path.join(resolve(parent), label) if parent else label
        cache[fid] = path
        return path

    artifacts: Dict[int, str] = {}
    for a in container.get("artifacts", []):
        artifacts[a["id"]] = resolve(a["pathFragmentId"])
    return artifacts


def _label_index(container: dict) -> Dict[int, str]:
    return {t["id"]: t["label"] for t in container.get("targets", [])}


def _source_from_compile_args(args: List[str]) -> Optional[str]:
    """The compiled source is the argument to -c (or the lone .cc/.cpp arg)."""
    for i, a in enumerate(args):
        if a == "-c" and i + 1 < len(args):
            return args[i + 1]
    for a in args:
        if a.endswith((".cc", ".cpp", ".cxx", ".c", ".C")):
            return a
    return None


def _label_to_name(label: str) -> str:
    """//foo/bar:baz -> baz ; keep it simple, names are matched against CMake."""
    return label.split(":")[-1] if ":" in label else label.rstrip("/").split("/")[-1]


def extract(aquery_path: str, repo_root: str) -> CanonicalModel:
    with open(aquery_path) as f:
        container = json.load(f)

    artifacts = _build_path_index(container)
    labels = _label_index(container)

    # group actions by target
    model = CanonicalModel()
    by_target: Dict[str, Target] = {}

    def target_for(label_name: str, kind: TargetKind) -> Target:
        if label_name not in by_target:
            by_target[label_name] = Target(name=label_name, kind=kind)
        elif kind != TargetKind.UNKNOWN:
            by_target[label_name].kind = kind
        return by_target[label_name]

    for action in container.get("actions", []):
        mnem = action.get("mnemonic", "")
        label = labels.get(action.get("targetId"), "")
        name = _label_to_name(label)
        args = action.get("arguments", [])

        if mnem in _COMPILE:
            src = _source_from_compile_args(args)
            if not src:
                continue
            cdef, cinc, cfl = canonicalize_flags(args, repo_root, is_bazel=True)
            t = target_for(name, TargetKind.UNKNOWN)
            t.tus.append(TranslationUnit(
                source=_rel(src, repo_root), defines=cdef,
                includes=cinc, flags=cfl))

        elif mnem in _LINK:
            outs = [artifacts.get(o, "") for o in action.get("outputIds", [])]
            primary = outs[0] if outs else ""
            t = target_for(name, _kind_from_output(primary))
            # link inputs that are other targets' archives -> deps; system libs
            # appear as -l flags in the argv
            for a in args:
                if a.startswith("-l"):
                    dep = a[2:]
                    if dep and not any(d.name == dep for d in t.deps):
                        t.deps.append(Dependency(dep, external=True))

    for t in by_target.values():
        model.add(t)
    return model


def _rel(path: str, repo_root: str) -> str:
    # bazel paths are already workspace-relative-ish (bazel-out/..., or the
    # source path); strip a leading repo_root if present, normalize separators.
    p = path.replace(os.sep, "/")
    rr = repo_root.replace(os.sep, "/").rstrip("/")
    if p.startswith(rr + "/"):
        p = p[len(rr) + 1:]
    return p


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: extract_bazel.py <aquery.json> <repo_root> <out.json>")
    aq, repo_root, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
    dump_model(extract(aq, repo_root), out)
    print(f"wrote {out}")
