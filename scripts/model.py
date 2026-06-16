"""Canonical build model shared by both extractors.

Both the CMake File-API extractor and the Bazel aquery extractor normalize
into THIS model. The diff operates only on this model, never on raw command
strings. The LLM never touches this file's logic -- extraction and diffing are
deterministic so each migrate-iterate round is cheap and reproducible.

Design decisions baked in here (from the design discussion):
  * A dependency is recorded as an ABSTRACT identity (a name), not a resolved
    path. Resolution (find_package / vcpkg / bzlmod / rules_foreign_cc / ...)
    is pluggable and lives outside this model. This keeps the model agnostic
    to how either side found the lib.
  * Flag comparison is ASYMMETRIC: we require every correctness-relevant CMake
    flag to be present on the Bazel side. Extra Bazel flags (toolchain
    defaults, sandbox include prefixes) are NOT discrepancies.
  * "Done" for a target = per-TU compile-flag equivalence + link closure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple


class TargetKind(str, Enum):
    STATIC = "static_library"
    SHARED = "shared_library"
    EXECUTABLE = "executable"
    OBJECT = "object_library"
    INTERFACE = "interface_library"  # header-only / no TUs
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Dependency:
    """Resolution-agnostic dependency identity.

    `name` is the abstract library name (e.g. 'zlib', 'fmt'). `external` marks
    deps that came from outside the project tree (find_package / system / fetched).
    How this maps to a Bazel label is the resolver adapter's job, not ours.
    """
    name: str
    external: bool = False


@dataclass
class TranslationUnit:
    """One compiled source file with its canonicalized compile flags."""
    source: str                      # repo-relative POSIX path
    defines: Tuple[str, ...] = ()    # canonicalized, sorted -- order never matters
    includes: Tuple[str, ...] = ()   # repo-relative, ORDER PRESERVED (search order)
    flags: Tuple[str, ...] = ()      # other copts, canonicalized
    language: str = "CXX"

    def key(self) -> str:
        return self.source


@dataclass
class Target:
    name: str
    kind: TargetKind
    tus: List[TranslationUnit] = field(default_factory=list)
    deps: List[Dependency] = field(default_factory=list)   # link closure (direct)
    link_flags: Tuple[str, ...] = ()

    def tu_map(self) -> Dict[str, TranslationUnit]:
        return {tu.key(): tu for tu in self.tus}


@dataclass
class CanonicalModel:
    """The whole project, normalized. Source of truth for the diff."""
    targets: Dict[str, Target] = field(default_factory=dict)

    def add(self, t: Target) -> None:
        self.targets[t.name] = t
