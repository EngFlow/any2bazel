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

"""Migration config -- the checked-in record of human decisions.

Lives at the migrated project's repo root as `any2bazel.json`. Unlike the
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

  * toolchain_includes : include roots the BAZEL TOOLCHAIN supplies without
                 them appearing in aquery's argv (a compiler wrapper's runtime
                 -idirafter, the sysroot's builtin dirs). Not a suppression:
                 the reference is REQUIRED to carry each one, and only the
                 Bazel side is excused. See MigrationConfig.toolchain_includes.

Because it's a file, every suppression is an explicit, reviewable, version-
controlled line -- a durable record of why a given difference was accepted.

Example any2bazel.json:
    {
      "target_map": { },
      "toolchain_includes": ["/opt/sdk/staging/usr/include"],
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
import posixpath
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

CONFIG_FILENAME = "any2bazel.json"


@dataclass
class MigrationConfig:
    target_map: Dict[str, str] = field(default_factory=dict)
    # Explicit external-dependency name map, cmake_dep_name -> bazel_dep_name,
    # for deps spelled differently per build (CMake's archive basename
    # 'Catch2Main' vs Bazel's 'catch2_main', or 'OpenSSL::SSL' vs 'ssl'). Like
    # target_map but for the link-closure comparison: a reviewed, recorded
    # decision rather than a fuzzy match, so a residual missing_dep is a real
    # gap. Applied to the CMake side before comparing.
    dep_map: Dict[str, str] = field(default_factory=dict)
    ignore_defines: Set[str] = field(default_factory=set)
    ignore_flags: Set[str] = field(default_factory=set)
    ignore_flag_prefixes: tuple = ()
    # Reviewer-approved LINK flag differences (per-executable link-flag diff).
    # Same shape as ignore_flags/flag_prefixes but for the link step.
    ignore_link_flags: Set[str] = field(default_factory=set)
    ignore_link_flag_prefixes: tuple = ()
    # Include paths to drop from the search-order comparison -- typically
    # third-party/vendored include roots that resolve differently because the
    # dep is in-tree under CMake but an external module under Bazel.
    ignore_include_prefixes: tuple = ()
    # Include-path PREFIX REWRITES, applied to both sides before comparing:
    # a list of (from_prefix, to_token) pairs. Unlike ignore_include_prefixes
    # (which deletes the path, a blind spot), a map canonicalizes the differing
    # spellings of the SAME dependency to one token so the search-order check is
    # still performed. Several from-prefixes may map to one token (e.g. Bazel's
    # 'external/absl+' and 'bazel-out/.../external/absl+' twin both -> '@absl').
    # Longest from-prefix wins. Applied before ignore_include_prefixes.
    include_map: tuple = ()  # tuple of (from_prefix, to_token)
    # Include roots the BAZEL TOOLCHAIN provides IMPLICITLY -- a compiler
    # wrapper that adds `-idirafter <sdk>/usr/include` when it runs, the
    # sysroot's builtin dirs (cxx_builtin_include_directories) -- which the
    # reference build spells out explicitly on every TU (e.g. in
    # CMAKE_C_FLAGS) but which `bazel aquery` can never show, because they are
    # added at EXECUTION time. The asymmetric lever for that:
    #   * CMake side: each root is REQUIRED on every TU. Its absence is an
    #     includes_diff error (the config asserts a fact about the reference;
    #     if the reference stops carrying the root the assertion is stale, or
    #     the reference regressed, and either way someone must look).
    #   * Bazel side: the root counts as present; it is dropped from the
    #     comparison instead of being demanded of the argv.
    # This is the opposite of ignore_include_prefixes, which deletes the root
    # from BOTH sides and so never notices the reference losing it.
    # Matching is by DIRECTORY IDENTITY (normalized path equality), not by
    # prefix: a root beneath a toolchain root -- `<sdk>/usr/include/json-c`
    # under `<sdk>/usr/include` -- is a different search directory with its
    # own meaning and is compared on its own merits (mapped by include_map if
    # spelled differently per side, else required verbatim). A prefix match
    # would swallow it exactly the way include_prefixes does. Populate from the
    # toolchain, not from memory: scripts/probe_toolchain.py prints the
    # search list the (wrapped) compiler actually uses. A plain host build
    # whose reference passes no such roots needs none.
    toolchain_includes: tuple = ()
    # Source-path PREFIX REWRITES for TRANSLATION-UNIT keys, applied to both
    # sides before the TU-set comparison -- the compile-source analogue of
    # include_map. Same (from_prefix, to_token) shape, longest-prefix-wins.
    # Its purpose is GENERATED-source grouping asymmetries: e.g. CMake's AUTOMOC
    # bundles every moc_*.cpp into one mocs_compilation.cpp TU, while Bazel
    # compiles each moc_*.cpp separately. Mapping both spellings to one token
    # (the CMake bundle path AND the Bazel moc/ dir -> "@moc") reconciles them:
    # the pooled TU maps collapse to the shared token, so presence matches and
    # the representative TU's flags are still compared (extra Bazel defines stay
    # benign WARN under the asymmetric-subset rule). Only ever use for codegen
    # whose per-TU flags are uniform; never to paper over a real missing source.
    # Unlike include_map, the token REPLACES the whole path (the remainder is
    # dropped) so N generated files collapse to 1 -- see map_source().
    source_map: tuple = ()  # tuple of (from_prefix, to_token)
    # CMake target names to drop entirely from the diff: third-party/vendored
    # code Bazel pulls as an external module, or tooling out of migration scope.
    # Unlike `ignore` (flags/defines), this is the only lever for missing_tu /
    # missing_target on whole subtrees. Each entry is a reviewable decision.
    exclude_targets: Set[str] = field(default_factory=set)
    # The bazel aquery invocation this migration is compared against, recorded
    # so the comparison is reproducible. Read by the skill/operator, not by the
    # diff itself (the diff consumes the already-extracted model).
    bazel_args: tuple = ()
    # Opt-in: also diff TEST targets (role=test). OFF by default because test
    # diffing requires BOTH models to be extracted with tests enabled and the
    # SAME test scope (symmetric configure + aquery); turning it on against a
    # tests-off extraction would fabricate findings. When on, test sources are
    # compared as their own project-wide TU-set union (like libraries), plus a
    # test-binary existence/count check. See PARTICIPATING_ROLES in diff.py.
    include_tests: bool = False

    def __post_init__(self):
        # Normalize the declared toolchain roots once, so the identity test
        # in toolchain_root() is plain equality against canonical spellings.
        self.toolchain_includes = tuple(
            r for r in (_norm_root(t) for t in self.toolchain_includes) if r)

    def flag_ignored(self, flag: str) -> bool:
        return (flag in self.ignore_flags
                or any(flag.startswith(p) for p in self.ignore_flag_prefixes))

    def link_flag_ignored(self, flag: str) -> bool:
        return (flag in self.ignore_link_flags
                or any(flag.startswith(p) for p in self.ignore_link_flag_prefixes))

    def define_ignored(self, define: str) -> bool:
        # match on full token or KEY (before '=')
        return define in self.ignore_defines or \
            define.split("=", 1)[0] in self.ignore_defines

    def map_include(self, include: str) -> str:
        """Apply include_map prefix rewrites (longest from-prefix wins). Returns
        the include unchanged if no rule matches."""
        best = None
        for frm, to in self.include_map:
            if include.startswith(frm) and (best is None or len(frm) > len(best[0])):
                best = (frm, to)
        if best is None:
            return include
        frm, to = best
        return to + include[len(frm):]

    def map_source(self, source: str) -> str:
        """Apply source_map prefix rewrites to a TU key (longest from-prefix
        wins). Returns the source unchanged if no rule matches.

        NOTE the deliberate difference from map_include: the token REPLACES the
        whole path -- the remainder after the prefix is DROPPED, not appended.
        That makes the map COLLAPSING, which is exactly what the generated-code
        grouping case needs: a whole directory of Bazel moc_*.cpp files and the
        single CMake mocs_compilation.cpp bundle both become the one token, so
        the two sides' TU sets reconcile. (An appending map could never collapse
        N files to 1.)"""
        best = None
        for frm, to in self.source_map:
            if source.startswith(frm) and (best is None or len(frm) > len(best[0])):
                best = (frm, to)
        return best[1] if best else source

    def include_ignored(self, include: str) -> bool:
        return any(include.startswith(p) for p in self.ignore_include_prefixes)

    def toolchain_root(self, include: str) -> Optional[str]:
        """The declared toolchain root that `include` IS (the same directory,
        after path normalization), or None. Deliberately not a prefix match --
        see the toolchain_includes field: a subdirectory of a toolchain root is
        its own search directory and must be compared, not excused."""
        norm = _norm_root(include)
        return norm if norm in self.toolchain_includes else None

    def target_excluded(self, name: str) -> bool:
        return name in self.exclude_targets


def _norm_root(path: str) -> str:
    """Canonical spelling of an include directory for identity comparison:
    POSIX separators, `.`/`..` segments collapsed, no trailing slash. The same
    normalization canonicalize.py applies to the models' include paths, so a
    config entry and a model root that name one directory compare equal."""
    p = (path or "").strip().replace(os.sep, "/")
    if not p:
        return ""
    return posixpath.normpath(p)


def load(path: str) -> MigrationConfig:
    """Load config from an explicit file path, or return empty if absent."""
    if not path or not os.path.isfile(path):
        return MigrationConfig()
    with open(path) as f:
        obj = json.load(f)
    ig = obj.get("ignore", {})
    return MigrationConfig(
        target_map=obj.get("target_map", {}) or {},
        dep_map=obj.get("dep_map", {}) or {},
        ignore_defines=set(ig.get("defines", [])),
        ignore_flags=set(ig.get("flags", [])),
        ignore_flag_prefixes=tuple(ig.get("flags_prefixes", [])),
        ignore_link_flags=set(ig.get("link_flags", [])),
        ignore_link_flag_prefixes=tuple(ig.get("link_flags_prefixes", [])),
        ignore_include_prefixes=tuple(ig.get("include_prefixes", [])),
        include_map=tuple(
            (e["from"], e["to"]) for e in ig.get("include_map", [])),
        source_map=tuple(
            (e["from"], e["to"]) for e in ig.get("source_map", [])),
        toolchain_includes=tuple(obj.get("toolchain_includes", []) or ()),
        exclude_targets=set(obj.get("exclude_targets", [])),
        bazel_args=tuple(obj.get("bazel_args", [])),
        include_tests=bool(obj.get("include_tests", False)),
    )


def find_and_load(repo_root: str) -> MigrationConfig:
    """Load <repo_root>/any2bazel.json if it exists."""
    return load(os.path.join(repo_root, CONFIG_FILENAME))
