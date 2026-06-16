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
    # Include paths to drop from the search-order comparison -- typically
    # third-party/vendored include roots that resolve differently because the
    # dep is in-tree under CMake but an external module under Bazel.
    ignore_include_prefixes: tuple = ()
    # CMake target names to drop entirely from the diff: third-party/vendored
    # code Bazel pulls as an external module, or tooling out of migration scope.
    # Unlike `ignore` (flags/defines), this is the only lever for missing_tu /
    # missing_target on whole subtrees. Each entry is a reviewable decision.
    exclude_targets: Set[str] = field(default_factory=set)
    # The bazel aquery invocation this migration is compared against, recorded
    # so the comparison is reproducible. Read by the skill/operator, not by the
    # diff itself (the diff consumes the already-extracted model).
    bazel_args: tuple = ()

    def flag_ignored(self, flag: str) -> bool:
        return (flag in self.ignore_flags
                or any(flag.startswith(p) for p in self.ignore_flag_prefixes))

    def define_ignored(self, define: str) -> bool:
        # match on full token or KEY (before '=')
        return define in self.ignore_defines or \
            define.split("=", 1)[0] in self.ignore_defines

    def include_ignored(self, include: str) -> bool:
        return any(include.startswith(p) for p in self.ignore_include_prefixes)

    def target_excluded(self, name: str) -> bool:
        return name in self.exclude_targets


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
        ignore_include_prefixes=tuple(ig.get("include_prefixes", [])),
        exclude_targets=set(obj.get("exclude_targets", [])),
        bazel_args=tuple(obj.get("bazel_args", [])),
    )


def find_and_load(repo_root: str) -> MigrationConfig:
    """Load <repo_root>/cmake2bazel.json if it exists."""
    return load(os.path.join(repo_root, CONFIG_FILENAME))
