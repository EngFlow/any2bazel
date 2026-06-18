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
canonicalize each CppCompile's argv into a TranslationUnit.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional

from canonicalize import canonicalize_flags, canonicalize_link_flags
from model import (CanonicalModel, Dependency, Target, TargetKind,
                   TargetRole, TranslationUnit)
from serialize import dump_model

_COMPILE = {"CppCompile"}
_LINK = {"CppLink", "CppArchive"}

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


_ARCHIVE_EXTS = (".a", ".lo", ".lib")


def _archive_identity(arg):
    """Abstract dep name for an archive FILE link input, or None if `arg` isn't
    one (it's a flag, object, or the output). Mirrors the CMake side's
    _library_identity: 'libcatch2_main.a' -> 'catch2_main'. Cross-build name
    differences (vs CMake's 'Catch2Main') are aligned by an explicit dep_map
    config entry at diff time, not a fuzzy match."""
    if arg.startswith("-") or not arg.endswith(_ARCHIVE_EXTS):
        return None
    base = os.path.basename(arg)
    stem = base.split(".")[0]
    if stem.startswith("lib"):
        stem = stem[3:]
    return stem or None


def _is_external_path(arg):
    """True if an archive path is an external/third-party dependency (a bzlmod
    module) rather than an in-project library. Bazel puts external repos under
    'external/<repo>' (and a 'bazel-out/.../bin/external/<repo>' mirror)."""
    return "external/" in arg


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


_HEADER_EXTS = (".h", ".hpp", ".hh", ".hxx", ".inc", ".inl")


def _is_header_processing(args: List[str]) -> bool:
    """True for Bazel header self-containment actions (the parse_headers /
    layering_check features), which compile a HEADER standalone to verify it's
    self-contained. They carry '-xc++-header' and emit a '.processed' output,
    not an object file. CMake has no equivalent, so counting them as TUs would
    show every header as a spurious extra_tu. Skip them."""
    return "-xc++-header" in args or "-fsyntax-only" in args


def _source_from_compile_args(args: List[str]) -> Optional[str]:
    """The compiled source is the argument to -c (or the lone source arg).
    Returns None for header inputs -- those are not translation units."""
    src = None
    for i, a in enumerate(args):
        if a == "-c" and i + 1 < len(args):
            src = args[i + 1]
            break
    if src is None:
        for a in args:
            if a.endswith((".cc", ".cpp", ".cxx", ".c", ".C")):
                src = a
                break
    if src is None or src.endswith(_HEADER_EXTS):
        return None
    return src


def _label_to_name(label: str) -> str:
    """Full-label-derived name, e.g. //absl/log:flags -> 'absl/log:flags'.

    Must NOT reduce to the bare target name: deep package trees reuse names
    across packages (//absl/log:flags vs //absl/log/internal:flags), and
    collapsing to 'flags' would merge two distinct targets into one corrupt
    entry. Library comparison keys on source path (names are irrelevant there);
    executable alignment with CMake names is handled via target_map.
    """
    return label[2:] if label.startswith("//") else label


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
            if _is_header_processing(args):
                continue   # header self-containment check, not a real TU
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
            t = target_for(name, _kind_from_link(mnem, primary))
            # Link deps come in two argv shapes:
            #   1. -l<name>  -> system lib (external)
            #   2. an archive FILE input (libfoo.a / .lo) -- Bazel links its deps
            #      by path, not -l. Archives under external/ (or bazel-out's
            #      external/ mirror) are external deps (bzlmod modules, e.g.
            #      catch2); archives elsewhere are internal in-project libs that
            #      the TU-set union already covers, so they're recorded internal
            #      and ignored by the external-dep check.
            for a in args:
                if a.startswith("-l"):
                    dep = a[2:]
                    if dep and not any(d.name == dep for d in t.deps):
                        t.deps.append(Dependency(dep, external=True))
                    continue
                ident = _archive_identity(a)
                if ident and not any(d.name == ident for d in t.deps):
                    t.deps.append(Dependency(ident, external=_is_external_path(a)))
            # link FLAGS (everything that isn't an input/dep/driver-mechanic).
            # CppArchive (static lib) has no meaningful link flags; only CppLink.
            if mnem == "CppLink":
                t.link_flags = canonicalize_link_flags(args, is_bazel=True)

    for t in by_target.values():
        t.role = _classify_bazel(t)
        model.add(t)
    return model


def _classify_bazel(t: Target) -> TargetRole:
    """Infer role on the Bazel side from kind + name conventions. aquery has no
    UTILITY/dashboard concept, so roles here are PRODUCTION/TEST/AGGREGATE."""
    if not t.tus and t.kind != TargetKind.INTERFACE:
        return TargetRole.AGGREGATE
    if t.kind == TargetKind.EXECUTABLE and (
            t.name.endswith(("_test", "_tests", "_shim"))):
        return TargetRole.TEST
    return TargetRole.PRODUCTION


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
