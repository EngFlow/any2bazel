"""JSON (de)serialization for the canonical model.

The on-disk model JSON is the contract between the three deterministic stages:
  extract_cmake.py  -> model.cmake.json
  extract_bazel.py  -> model.bazel.json
  diff.py           reads both
This keeps extraction and diffing as separate, independently-testable processes.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from model import (CanonicalModel, Dependency, Target, TargetKind,
                   TargetRole, TranslationUnit)


def dump_model(m: CanonicalModel, path: str) -> None:
    obj: Dict[str, Any] = {"targets": {}}
    for name, t in m.targets.items():
        obj["targets"][name] = {
            "kind": t.kind.value,
            "role": t.role.value,
            "link_flags": list(t.link_flags),
            "deps": [{"name": d.name, "external": d.external} for d in t.deps],
            "tus": [
                {
                    "source": tu.source,
                    "defines": list(tu.defines),
                    "includes": list(tu.includes),
                    "flags": list(tu.flags),
                    "language": tu.language,
                }
                for tu in t.tus
            ],
        }
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)


def load_model(path: str) -> CanonicalModel:
    with open(path) as f:
        obj = json.load(f)
    m = CanonicalModel()
    for name, t in obj["targets"].items():
        target = Target(
            name=name,
            kind=TargetKind(t["kind"]),
            role=TargetRole(t.get("role", "unknown")),
            link_flags=tuple(t.get("link_flags", [])),
            deps=[Dependency(d["name"], d.get("external", False)) for d in t.get("deps", [])],
            tus=[
                TranslationUnit(
                    source=tu["source"],
                    defines=tuple(tu.get("defines", [])),
                    includes=tuple(tu.get("includes", [])),
                    flags=tuple(tu.get("flags", [])),
                    language=tu.get("language", "CXX"),
                )
                for tu in t.get("tus", [])
            ],
        )
        m.add(target)
    return m
