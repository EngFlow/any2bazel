"""Extract a CanonicalModel from the CMake File API codemodel-v2 reply.

Why File API and not compile_commands.json: compile_commands has NO link
information, and the migration floor is compile+link. The codemodel gives us,
per target: type (static/shared/exe/object/interface), the source->compileGroup
mapping (defines, includes, flags per language), AND link dependencies. That's
both halves of what we need in one stable, documented schema.

Usage:
    # 1. ask cmake for the codemodel (once, before configure):
    mkdir -p <build>/.cmake/api/v1/query
    touch  <build>/.cmake/api/v1/query/codemodel-v2
    cmake -S <src> -B <build> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ...
    # 2. extract:
    python3 extract_cmake.py <build> <repo_root> model.cmake.json

The reply lands in <build>/.cmake/api/v1/reply/. We read the codemodel index,
then each per-target jsonFile.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from typing import Dict, List, Optional

from canonicalize import canonicalize_flags
from model import (CanonicalModel, Dependency, Target, TargetKind,
                   TranslationUnit)
from serialize import dump_model

# CMake target type string -> our TargetKind
_KIND = {
    "STATIC_LIBRARY": TargetKind.STATIC,
    "SHARED_LIBRARY": TargetKind.SHARED,
    "MODULE_LIBRARY": TargetKind.SHARED,
    "EXECUTABLE": TargetKind.EXECUTABLE,
    "OBJECT_LIBRARY": TargetKind.OBJECT,
    "INTERFACE_LIBRARY": TargetKind.INTERFACE,
}


def _find_codemodel(reply_dir: str) -> dict:
    """Locate and load the codemodel reply via the index file."""
    indexes = sorted(glob.glob(os.path.join(reply_dir, "index-*.json")))
    if not indexes:
        raise FileNotFoundError(
            f"no index-*.json in {reply_dir}; did you create the codemodel-v2 "
            "query file and re-run cmake?")
    with open(indexes[-1]) as f:
        index = json.load(f)
    # objects[] lists reply files by kind; find the codemodel
    for obj in index.get("objects", []):
        if obj.get("kind") == "codemodel":
            with open(os.path.join(reply_dir, obj["jsonFile"])) as f:
                return json.load(f)
    raise KeyError("codemodel object not found in File API index")


def _target_id_to_name(codemodel: dict) -> Dict[str, str]:
    """Map File-API target ids -> target names (for resolving dependencies)."""
    out: Dict[str, str] = {}
    cfg = codemodel["configurations"][0]
    for t in cfg["targets"]:
        out[t["id"]] = t["name"]
    return out


def _parse_target(tobj: dict, repo_root: str) -> Target:
    name = tobj["name"]
    kind = _KIND.get(tobj.get("type", ""), TargetKind.UNKNOWN)
    sources = [s["path"] for s in tobj.get("sources", [])]

    tus: List[TranslationUnit] = []
    for cg in tobj.get("compileGroups", []):
        lang = cg.get("language", "CXX")
        # reconstruct a raw flag list for the canonicalizer, mirroring how the
        # compiler would see it: fragments + -D defines + -I includes
        raw: List[str] = []
        for frag in cg.get("compileCommandFragments", []):
            raw.extend(_split_fragment(frag.get("fragment", "")))
        for d in cg.get("defines", []):
            raw.append("-D" + d["define"])
        for inc in cg.get("includes", []):
            raw.append("-isystem" if inc.get("isSystem") else "-I")
            raw.append(inc["path"])

        cdef, cinc, cfl = canonicalize_flags(raw, repo_root, is_bazel=False)
        for idx in cg.get("sourceIndexes", []):
            src = _rel(sources[idx], repo_root)
            tus.append(TranslationUnit(source=src, defines=cdef,
                                       includes=cinc, flags=cfl, language=lang))

    return Target(name=name, kind=kind, tus=tus)


def _attach_deps(target: Target, tobj: dict, id_to_name: Dict[str, str]) -> None:
    for dep in tobj.get("dependencies", []):
        dep_name = id_to_name.get(dep["id"])
        if dep_name:
            target.deps.append(Dependency(dep_name, external=False))
    # link fragments of role "libraries" that are NOT internal targets are
    # external deps (system libs / find_package results). Record them abstractly.
    link = tobj.get("link") or {}
    for frag in link.get("commandFragments", []):
        if frag.get("role") == "libraries":
            ext = _library_identity(frag.get("fragment", ""))
            if ext and not any(d.name == ext for d in target.deps):
                target.deps.append(Dependency(ext, external=True))


def _library_identity(fragment: str) -> Optional[str]:
    """Turn a link fragment into an abstract dep name.

    '-lz' -> 'z'; '/usr/lib/libfoo.a' -> 'foo'; '-framework Cocoa' -> 'Cocoa'.
    Resolution to a Bazel label is the resolver adapter's job, not ours.
    """
    frag = fragment.strip()
    if not frag:
        return None
    if frag.startswith("-l"):
        return frag[2:]
    if frag.startswith("-framework"):
        parts = frag.split()
        return parts[1] if len(parts) > 1 else None
    base = os.path.basename(frag)
    if base.startswith("lib") and "." in base:
        return base[3:].split(".")[0]
    return None


def _split_fragment(fragment: str) -> List[str]:
    """compileCommandFragments come pre-quoted; a simple split is adequate for
    the no-codegen MVP (no embedded spaces in flags we care about)."""
    return [tok for tok in fragment.strip().split() if tok]


def _rel(path: str, repo_root: str) -> str:
    if os.path.isabs(path):
        try:
            r = os.path.relpath(path, repo_root)
            if not r.startswith(".."):
                return r.replace(os.sep, "/")
        except ValueError:
            pass
    return path.replace(os.sep, "/")


def extract(build_dir: str, repo_root: str) -> CanonicalModel:
    reply_dir = os.path.join(build_dir, ".cmake", "api", "v1", "reply")
    codemodel = _find_codemodel(reply_dir)
    id_to_name = _target_id_to_name(codemodel)

    model = CanonicalModel()
    cfg = codemodel["configurations"][0]
    for tref in cfg["targets"]:
        with open(os.path.join(reply_dir, tref["jsonFile"])) as f:
            tobj = json.load(f)
        target = _parse_target(tobj, repo_root)
        _attach_deps(target, tobj, id_to_name)
        model.add(target)
    return model


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: extract_cmake.py <build_dir> <repo_root> <out.json>")
    build_dir, repo_root, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
    dump_model(extract(build_dir, repo_root), out)
    print(f"wrote {out}")
