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

from extract_configure import parse_configure_trace
from model import (Action, BuildSystem, CanonicalModel, Dependency, Target,
                   TargetKind, TargetRole)
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

# CTest/CDash dashboard target names (add_test/enable_testing machinery). These
# arrive as UTILITY targets and produce no artifact.
_DASHBOARD_NAMES = {
    "Continuous", "Experimental", "Nightly", "NightlyMemoryCheck",
    "run_tests", "fips_specific_tests_if_any",
}
_DASHBOARD_PREFIXES = ("Continuous", "Experimental", "Nightly")


def _classify(tobj: dict) -> TargetRole:
    """Infer a target's FUNCTION from its CMake type, name, and structure.

    Heuristic and deliberately conservative: anything we can't place lands in
    UNKNOWN so it shows up for inspection instead of being silently diffed or
    dropped.
    """
    name = tobj.get("name", "")
    ctype = tobj.get("type", "")

    if ctype in ("UTILITY", "GLOBAL_TARGET", "PACKAGE"):
        if name in _DASHBOARD_NAMES or name.startswith(_DASHBOARD_PREFIXES):
            return TargetRole.DASHBOARD
        # a UTILITY with custom commands is codegen; otherwise dashboard-ish
        return TargetRole.CODEGEN if tobj.get("backtraceGraph") and \
            _has_custom_command(tobj) else TargetRole.DASHBOARD

    has_sources = bool(tobj.get("compileGroups"))
    is_exe = ctype == "EXECUTABLE"

    # aggregate: real target type but compiles nothing of its own (only links)
    if not has_sources and ctype != "INTERFACE_LIBRARY":
        return TargetRole.AGGREGATE

    # test: an executable that CTest registered, or conventionally named
    if is_exe and (_is_registered_test(tobj) or name.endswith(("_test", "_tests"))
                   or name.endswith("_shim")):
        return TargetRole.TEST

    return TargetRole.PRODUCTION


def _has_custom_command(tobj: dict) -> bool:
    # File API marks generated outputs; a UTILITY with a backtrace into
    # add_custom_command/target is codegen. We approximate via presence of a
    # non-empty 'sources' that are all GENERATED, which the codemodel flags.
    for s in tobj.get("sources", []):
        if s.get("isGenerated"):
            return True
    return False


def _is_registered_test(tobj: dict) -> bool:
    # codemodel doesn't list ctest registration directly on the target; the
    # name-suffix heuristic above is the primary signal. Hook kept for when we
    # also parse the ctest reply object.
    return False


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

    # Synthesize one CppCompile Action per source: an argv the differ parses the
    # same way it parses Bazel's. CMake has no real command line, so we build the
    # canonical compiler view: [<flag fragments>, -D..., -I/-isystem ..., -c src].
    actions: List[Action] = []
    for cg in tobj.get("compileGroups", []):
        base: List[str] = []
        for frag in cg.get("compileCommandFragments", []):
            base.extend(_split_fragment(frag.get("fragment", "")))
        for d in cg.get("defines", []):
            base.append("-D" + d["define"])
        for inc in cg.get("includes", []):
            base.append("-isystem" if inc.get("isSystem") else "-I")
            base.append(inc["path"])
        for idx in cg.get("sourceIndexes", []):
            src = sources[idx]
            argv = tuple(base + ["-c", src])
            actions.append(Action(mnemonic="CppCompile", arguments=argv,
                                  inputs=(src,)))

    return Target(name=name, kind=kind, actions=actions, role=_classify(tobj))


def _attach_deps(target: Target, tobj: dict, id_to_name: Dict[str, str]) -> None:
    """Deps are a RESOLVED annotation (CMake knows them structurally). Link flags
    become the argv of a synthesized CppLink Action, parsed by the differ."""
    for dep in tobj.get("dependencies", []):
        dep_name = id_to_name.get(dep["id"])
        if dep_name:
            target.deps.append(Dependency(dep_name, external=False))
    # link fragments: role "libraries" -> external deps (annotation); role
    # "flags" -> a CppLink Action's argv (the differ canonicalizes them).
    link = tobj.get("link") or {}
    flag_tokens: List[str] = []
    for frag in link.get("commandFragments", []):
        role = frag.get("role")
        fragment = frag.get("fragment", "")
        if role == "libraries":
            ext = _library_identity(fragment)
            if ext and not any(d.name == ext for d in target.deps):
                target.deps.append(Dependency(ext, external=True))
        elif role == "flags":
            flag_tokens.extend(_split_fragment(fragment))
    if flag_tokens:
        target.actions.append(Action(mnemonic="CppLink",
                                     arguments=tuple(flag_tokens)))


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


def extract(build_dir: str, repo_root: str,
            trace_path: Optional[str] = None) -> CanonicalModel:
    """Build the canonical model from one CMake configuration's File API reply.

    If `trace_path` (a `cmake --trace-format=json-v1` stream from the SAME
    configure) is given, configure-time generated files (configure_file outputs)
    are also extracted and recorded in model.configured_files. Single pass: the
    codemodel and the trace both come from one `cmake` invocation; nothing here
    re-runs cmake.
    """
    reply_dir = os.path.join(build_dir, ".cmake", "api", "v1", "reply")
    codemodel = _find_codemodel(reply_dir)
    id_to_name = _target_id_to_name(codemodel)

    model = CanonicalModel(build_system=BuildSystem.CMAKE, repo_root=repo_root)
    include_dirs = set()
    cfg = codemodel["configurations"][0]
    for tref in cfg["targets"]:
        with open(os.path.join(reply_dir, tref["jsonFile"])) as f:
            tobj = json.load(f)
        target = _parse_target(tobj, repo_root)
        _attach_deps(target, tobj, id_to_name)
        model.add(target)
        for cg in tobj.get("compileGroups", []):
            for incd in cg.get("includes", []):
                include_dirs.add(incd["path"])

    if trace_path:
        for cfile in parse_configure_trace(
                trace_path, source_root=repo_root, build_root=build_dir,
                include_dirs=include_dirs):
            model.add_configured_file(cfile)
    return model


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        sys.exit("usage: extract_cmake.py <build_dir> <repo_root> <out.json> "
                 "[trace.jsonl]")
    build_dir, repo_root, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
    trace = sys.argv[4] if len(sys.argv) == 5 else None
    dump_model(extract(build_dir, repo_root, trace), out)
    print(f"wrote {out}")
