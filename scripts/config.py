"""Migration config -- the checked-in record of human decisions.

Lives at the migrated project's repo root as `cmake2bazel.json`. Unlike the
hardcoded canonicalization rules (driver mechanics, toolchain/sysroot,
reproducibility injections -- universal facts baked into canonicalize.py), this
file holds the JUDGMENT CALLS a migration must make and that deserve review:

  * target_map : intentional target renames (cmake_name -> bazel_name). Mostly
                 unnecessary now that libraries diff at TU-set level, but still
                 used to align executables.
  * ignore     : flags/defines a reviewer has decided are acceptable to differ
                 between the two builds (e.g. a warning set Bazel configures
                 differently, or a CMake-default define intentionally dropped).
                 Applied at DIFF time and to BOTH sides, so tuning them and
                 re-diffing needs no re-extraction.

Because it's a file, every suppression is an explicit, reviewable, version-
controlled line -- a durable record of why a given difference was accepted.

Example cmake2bazel.json:
    {
      "target_map": { },
      "ignore": {
        "defines": ["BORINGSSL_DISPATCH_TEST"],
        "flags":   ["-Wctad-maybe-unsupported", "-fvisibility=hidden"],
        "flags_prefixes": ["-Wthread-safety"]
      }
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Set

CONFIG_FILENAME = "cmake2bazel.json"


@dataclass
class MigrationConfig:
    target_map: Dict[str, str] = field(default_factory=dict)
    ignore_defines: Set[str] = field(default_factory=set)
    ignore_flags: Set[str] = field(default_factory=set)
    ignore_flag_prefixes: tuple = ()

    def flag_ignored(self, flag: str) -> bool:
        return (flag in self.ignore_flags
                or any(flag.startswith(p) for p in self.ignore_flag_prefixes))

    def define_ignored(self, define: str) -> bool:
        # match on full token or KEY (before '=')
        return define in self.ignore_defines or \
            define.split("=", 1)[0] in self.ignore_defines


def load(path: str) -> MigrationConfig:
    """Load config from an explicit file path, or return empty if absent."""
    if not path or not os.path.isfile(path):
        return MigrationConfig()
    with open(path) as f:
        obj = json.load(f)
    ig = obj.get("ignore", {})
    return MigrationConfig(
        target_map=obj.get("target_map", {}) or {},
        ignore_defines=set(ig.get("defines", [])),
        ignore_flags=set(ig.get("flags", [])),
        ignore_flag_prefixes=tuple(ig.get("flags_prefixes", [])),
    )


def find_and_load(repo_root: str) -> MigrationConfig:
    """Load <repo_root>/cmake2bazel.json if it exists."""
    return load(os.path.join(repo_root, CONFIG_FILENAME))
