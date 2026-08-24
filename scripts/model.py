# Copyright 2026 EngFlow GmbH
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

"""Canonical build model — an ACTION-based IR shared by all extractors.

Each build system extracts into THIS model. The model is a faithful, dumb record
of build ACTIONS; it does NOT canonicalize. All interpretation (parsing argv into
translation units / link flags, stripping build-system noise, grouping per
language) happens in the DIFFER (see reconstruct.py + diff.py). This keeps the
model language- and build-system-neutral: the neutral floor of an action is its
raw argv (a list of strings), which every build system can produce -- Bazel
aquery emits it natively; CMake and Maven synthesize it from structured config.

Design decisions baked in here:
  * The neutral floor is RAW ARGV. Structured semantics (a target's deps, an
    action's inputs/outputs) are RESOLVED ANNOTATIONS a frontend fills when it
    knows them, and the differ infers from argv when it doesn't. This is the
    `resolved | raw | unknown` idea: argv is the raw floor (always present),
    annotations are the resolved overlay (when known). Never lossy.
  * Canonicalization is NOT here -- it is in the differ, keyed on the model's
    `build_system` tag (for noise) and each action's mnemonic (for grouping).
  * Dependency is an ABSTRACT identity (a name), not a resolved path/label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class BuildSystem(str, Enum):
    """Which build system produced a model. Drives build-system-specific noise
    stripping in the differ (Bazel sandbox paths, CMake build dirs, ...)."""
    CMAKE = "cmake"
    BAZEL = "bazel"
    MAVEN = "maven"
    NPM = "npm"
    UNKNOWN = "unknown"


class TargetKind(str, Enum):
    """Mechanical artifact type. Says nothing about the target's purpose."""
    STATIC = "static_library"
    SHARED = "shared_library"
    EXECUTABLE = "executable"
    OBJECT = "object_library"
    INTERFACE = "interface_library"  # header-only / no TUs
    UNKNOWN = "unknown"


class TargetRole(str, Enum):
    """Inferred FUNCTION of a target -- orthogonal to TargetKind.

    Classification is a heuristic guess, so UNKNOWN is a first-class outcome
    that stays visible rather than being silently dropped. The diff compares
    only roles in PARTICIPATING_ROLES (see diff.py); the rest are retained in
    the model for separate inspection.
    """
    PRODUCTION = "production"   # shippable library or binary -> diffed
    TEST = "test"              # test executable -> tracked, opt-in
    DASHBOARD = "dashboard"    # CTest/CDash UTILITY automation, no artifact
    AGGREGATE = "aggregate"    # meta target: only deps, no own sources
    CODEGEN = "codegen"        # generated-code / custom-command target
    UNKNOWN = "unknown"        # could not classify -- surfaced, not diffed


@dataclass(frozen=True)
class Dependency:
    """Resolution-agnostic dependency identity (a resolved annotation).

    `name` is the abstract library name (e.g. 'zlib', 'fmt', or a coordinate
    like 'com.google.guava:guava'). `external` marks deps from outside the
    project tree. How this maps to a target build label is the backend's job.
    """
    name: str
    external: bool = False


@dataclass
class Action:
    """One build action. The neutral floor is `arguments` (raw argv); the rest
    are resolved annotations a frontend fills when it can.

      mnemonic   action kind: CppCompile / CppLink / CppArchive / JavaCompile /
                 ... The differ groups and interprets per mnemonic.
      arguments  RAW ARGV (the faithful floor). Bazel: the literal command line.
                 CMake/Maven: synthesized from structured config (no real argv).
      inputs     declared input paths (annotation; e.g. a CMake compile group's
                 source list, or a link action's object/archive inputs). May be
                 empty -- the differ then infers sources from argv.
      outputs    declared output paths (annotation).
    """
    mnemonic: str
    arguments: Tuple[str, ...] = ()
    inputs: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ()


@dataclass
class TranslationUnit:
    """A reconstruction VIEW: one compiled source + its canonicalized flags.

    NOT extracted directly anymore -- the differ's reconstruct step derives TUs
    from compile Actions (one per source). Kept here because it's the shared
    shape the comparison layer operates on. For C/C++ a TU is one .cc; other
    languages reconstruct into their own comparable units.
    """
    source: str                      # repo-relative POSIX path
    defines: Tuple[str, ...] = ()    # canonicalized, sorted -- order never matters
    includes: Tuple[str, ...] = ()   # repo-relative, ORDER PRESERVED (search order)
    flags: Tuple[str, ...] = ()      # other copts, canonicalized
    language: str = "CXX"

    def key(self) -> str:
        return self.source


@dataclass(frozen=True)
class ConfiguredFile:
    """A CONFIGURE-TIME generated file: output of CMake `configure_file()`,
    which runs at configure time (string templating) leaving NO action-graph
    node. DISTINCT from build-time generation (add_custom_command/codegen, which
    lives in the action graph as Actions and maps to Bazel genrules). Recovered
    only from the cmake --trace; maps to a Bazel repository/workspace rule.

    Captured with output + generation inputs so equivalence can be proved by
    CONTENT (store the output PATH, not bytes; read + compared at diff time).
    """
    name: str                        # canonical output name, build-root-relative
    output_path: str                 # absolute on-disk output (read for content)
    template: Optional[str] = None   # source .in/.cmakein
    options: Tuple[str, ...] = ()    # e.g. ('@ONLY',)
    is_compile_input: bool = False   # output lands on an include/compile path


@dataclass
class Target:
    """A build target. Holds raw ACTIONS plus resolved annotations
    (kind/role/deps). Compile flags and link flags are NOT stored -- they are
    reconstructed from `actions` by the differ."""
    name: str
    kind: TargetKind
    actions: List[Action] = field(default_factory=list)
    deps: List[Dependency] = field(default_factory=list)   # link closure (annotation)
    role: "TargetRole" = None  # set in __post_init__ if left unspecified

    def __post_init__(self):
        if self.role is None:
            self.role = TargetRole.UNKNOWN


@dataclass
class CanonicalModel:
    """A whole project's extracted build, as actions. Source of truth for the
    differ, which reconstructs + canonicalizes from here."""
    build_system: BuildSystem = BuildSystem.UNKNOWN
    repo_root: str = ""              # used by reconstruct to make paths relative
    targets: Dict[str, Target] = field(default_factory=dict)
    # Project-wide CONFIGURE-TIME generated files (configure_file outputs).
    configured_files: Dict[str, "ConfiguredFile"] = field(default_factory=dict)

    def add(self, t: Target) -> None:
        self.targets[t.name] = t

    def add_configured_file(self, c: "ConfiguredFile") -> None:
        self.configured_files[c.name] = c
