"""Extract a CanonicalModel from `bazel aquery --output=jsonproto`.

aquery is the Bazel-side counterpart to the CMake File API: it exposes the
actual actions the build would run -- CppCompile actions (one per TU, with the
full argv) AND CppLink/CppArchive actions (the link closure). compile commands
alone would miss the link half, so we use aquery.

Usage:
    bazel aquery 'mnemonic("CppCompile|CppLink|CppArchive", //...)' \
        --output=jsonproto > aquery.json
    python3 extract_bazel.py aquery.json <repo_root> model.bazel.json

The jsonproto shape (ActionGraphContainer):
    { "artifacts": [{id, pathFragmentId}], "actions": [{mnemonic, arguments[],
      targetId, outputIds[]}], "targets": [{id, label}], "pathFragments": [...] }
We reconstruct artifact paths from pathFragments, map actions -> targets, and
store each action's RAW ARGV as an Action. Interpretation (argv -> TUs / link
flags / inferred deps) is the differ's job (reconstruct.py); the extractor only
records the faithful action graph plus the kind/role annotations it can resolve.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional

from model import (Action, BuildSystem, CanonicalModel, Target, TargetKind,
                   TargetRole)
from serialize import dump_model

_COMPILE = {"CppCompile"}
_LINK = {"CppLink", "CppArchive"}
# Bazel Java compile. (Turbine = header/ijar compile, JavaSourceJar = packaging:
# both Bazel-specific, not real compilations -- skipped, like C++ header
# processing.) Mapped to the neutral 'JavaCompile' mnemonic the differ groups on.
_JAVAC = {"Javac"}

# Infer target kind from the link ACTION, not just the output extension.
# Mnemonic is authoritative for archives: CppArchive always produces a static
# library, whatever the archive extension (.a on Linux, .lo thin-archive on
# macOS/clang, .lib on Windows). Only for CppLink do we inspect the output to
# tell a shared library from an executable.
def _kind_from_link(mnemonic: str, path: str) -> TargetKind:
    if mnemonic == "CppArchive":
        return TargetKind.STATIC
    if path.endswith(".a") or path.endswith(".lo") or path.endswith(".lib"):
        return TargetKind.STATIC
    if (path.endswith(".so") or path.endswith(".dylib") or ".so." in path
            or path.endswith(".dll")):
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


def _label_to_name(label: str) -> str:
    """Full-label-derived name, e.g. //absl/log:flags -> 'absl/log:flags'.

    Must NOT reduce to the bare target name: deep package trees reuse names
    across packages (//absl/log:flags vs //absl/log/internal:flags), and
    collapsing to 'flags' would merge two distinct targets into one corrupt
    entry. Library comparison keys on source path (names are irrelevant there);
    executable alignment with CMake names is handled via target_map.
    """
    return label[2:] if label.startswith("//") else label


_HEADER_PROCESSING_MARKERS = ("-xc++-header", "-fsyntax-only")
_REAL_SOURCE_EXTS = (".cc", ".cpp", ".cxx", ".c", ".C")


def _is_real_compile(args) -> bool:
    """A compile action that produces an object from a real source (not a Bazel
    header self-containment check). Used only for ROLE classification here; the
    differ re-derives the same judgment when reconstructing TUs."""
    if any(m in args for m in _HEADER_PROCESSING_MARKERS):
        return False
    for i, a in enumerate(args):
        if a == "-c" and i + 1 < len(args) and args[i + 1].endswith(_REAL_SOURCE_EXTS):
            return True
    return any(a.endswith(_REAL_SOURCE_EXTS) for a in args)


def extract(aquery_path: str, repo_root: str) -> CanonicalModel:
    with open(aquery_path) as f:
        container = json.load(f)

    artifacts = _build_path_index(container)
    labels = _label_index(container)

    model = CanonicalModel(build_system=BuildSystem.BAZEL, repo_root=repo_root)
    by_target: Dict[str, Target] = {}

    def target_for(label_name: str, kind: TargetKind) -> Target:
        if label_name not in by_target:
            by_target[label_name] = Target(name=label_name, kind=kind)
        elif kind != TargetKind.UNKNOWN:
            by_target[label_name].kind = kind
        return by_target[label_name]

    # Store raw actions; interpretation (TUs, flags, deps) is the differ's job.
    for action in container.get("actions", []):
        mnem = action.get("mnemonic", "")
        name = _label_to_name(labels.get(action.get("targetId"), ""))
        args = tuple(action.get("arguments", []))
        outs = tuple(artifacts.get(o, "") for o in action.get("outputIds", []))

        if mnem in _COMPILE:
            t = target_for(name, TargetKind.UNKNOWN)
        elif mnem in _LINK:
            primary = outs[0] if outs else ""
            t = target_for(name, _kind_from_link(mnem, primary))
        elif mnem in _JAVAC:
            # a java_library produces a jar (an archive of classes) -> STATIC.
            # Record under the neutral 'JavaCompile' mnemonic the differ groups on.
            t = target_for(name, TargetKind.STATIC)
            t.actions.append(Action(mnemonic="JavaCompile", arguments=args,
                                    outputs=outs))
            continue
        else:
            continue
        t.actions.append(Action(mnemonic=mnem, arguments=args, outputs=outs))

    for t in by_target.values():
        t.role = _classify_bazel(t)
        model.add(t)
    return model


def _classify_bazel(t: Target) -> TargetRole:
    """Infer role from kind + name + presence of real compile actions. aquery
    has no UTILITY/dashboard concept, so roles here are PRODUCTION/TEST/AGGREGATE.
    A target with no real compile action (only links other libs) is AGGREGATE."""
    has_compile = any(
        (a.mnemonic in _COMPILE and _is_real_compile(a.arguments))
        or a.mnemonic == "JavaCompile"
        for a in t.actions)
    if not has_compile and t.kind != TargetKind.INTERFACE:
        return TargetRole.AGGREGATE
    if t.kind == TargetKind.EXECUTABLE and \
            t.name.endswith(("_test", "_tests", "_shim")):
        return TargetRole.TEST
    return TargetRole.PRODUCTION


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: extract_bazel.py <aquery.json> <repo_root> <out.json>")
    aq, repo_root, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
    dump_model(extract(aq, repo_root), out)
    print(f"wrote {out}")
