# Copyright 2026 EngFlow Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""JSON (de)serialization for the canonical (action-based) model.

The on-disk model JSON is the contract between the deterministic stages:
  extract_cmake.py  -> model.cmake.json   (targets as ACTIONS + annotations)
  extract_bazel.py  -> model.bazel.json
  diff.py           reads both, reconstructs views, compares
This keeps extraction and diffing as separate, independently-testable processes.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from model import (Action, BuildSystem, CanonicalModel, ConfiguredFile,
                   Dependency, Target, TargetKind, TargetRole)


def dump_model(m: CanonicalModel, path: str) -> None:
    obj: Dict[str, Any] = {
        "build_system": m.build_system.value,
        "repo_root": m.repo_root,
        "targets": {},
        "configured_files": {},
    }
    for cname, c in m.configured_files.items():
        obj["configured_files"][cname] = {
            "name": c.name,
            "output_path": c.output_path,
            "template": c.template,
            "options": list(c.options),
            "is_compile_input": c.is_compile_input,
        }
    for name, t in m.targets.items():
        obj["targets"][name] = {
            "kind": t.kind.value,
            "role": t.role.value,
            "deps": [{"name": d.name, "external": d.external} for d in t.deps],
            "actions": [
                {
                    "mnemonic": a.mnemonic,
                    "arguments": list(a.arguments),
                    "inputs": list(a.inputs),
                    "outputs": list(a.outputs),
                }
                for a in t.actions
            ],
        }
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)


def load_model(path: str) -> CanonicalModel:
    with open(path) as f:
        obj = json.load(f)
    m = CanonicalModel(
        build_system=BuildSystem(obj.get("build_system", "unknown")),
        repo_root=obj.get("repo_root", ""),
    )
    for name, t in obj["targets"].items():
        target = Target(
            name=name,
            kind=TargetKind(t["kind"]),
            role=TargetRole(t.get("role", "unknown")),
            deps=[Dependency(d["name"], d.get("external", False))
                  for d in t.get("deps", [])],
            actions=[
                Action(
                    mnemonic=a["mnemonic"],
                    arguments=tuple(a.get("arguments", [])),
                    inputs=tuple(a.get("inputs", [])),
                    outputs=tuple(a.get("outputs", [])),
                )
                for a in t.get("actions", [])
            ],
        )
        m.add(target)
    for cname, c in obj.get("configured_files", {}).items():
        m.add_configured_file(ConfiguredFile(
            name=c["name"],
            output_path=c["output_path"],
            template=c.get("template"),
            options=tuple(c.get("options", [])),
            is_compile_input=c.get("is_compile_input", False),
        ))
    return m
