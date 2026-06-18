"""Reconstruct comparable views from raw Actions -- the differ's smart layer.

The model is a faithful record of build Actions (raw argv + annotations). This
module interprets those actions into the shape the comparison operates on:
  * compile actions  -> TranslationUnits (source + canonicalized flags)
  * link actions     -> link_flags (canonicalized)
  * deps             -> resolved annotation if the frontend knew them (CMake),
                        otherwise INFERRED from link argv (Bazel only has argv)

All build-system-specific argv interpretation lives here, keyed on the model's
`build_system` tag (noise stripping) and each action's mnemonic (grouping). This
is the "more complexity in the differ" the action-based IR trades for a neutral
model. canonicalize.py (pure flag policy) is imported, not duplicated.

A TargetView is what diff.py compares -- the action model must re-derive exactly
the TU/link/dep results the old per-TU extractors produced (regression anchor).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from canonicalize import canonicalize_flags, canonicalize_link_flags
from model import (Action, BuildSystem, CanonicalModel, Dependency, Target,
                   TargetKind, TranslationUnit)

_COMPILE_MNEMONICS = {"CppCompile"}
_LINK_MNEMONICS = {"CppLink", "CppArchive"}

_HEADER_EXTS = (".h", ".hpp", ".hh", ".hxx", ".inc", ".inl")
_SOURCE_EXTS = (".cc", ".cpp", ".cxx", ".c", ".C")
_ARCHIVE_EXTS = (".a", ".lo", ".lib")


@dataclass
class TargetView:
    """Reconstructed, comparison-ready view of a Target: TUs + link flags + the
    (resolved or inferred) external/internal dep set. diff.py operates on these,
    not on raw actions."""
    name: str
    kind: TargetKind
    role: object                       # TargetRole (avoid import churn)
    tus: List[TranslationUnit] = field(default_factory=list)
    link_flags: Tuple[str, ...] = ()
    deps: List[Dependency] = field(default_factory=list)

    def tu_map(self):
        return {tu.key(): tu for tu in self.tus}


# ---- argv interpretation (was split across the two extractors) -------------

def _is_header_processing(args) -> bool:
    """Bazel header self-containment actions (parse_headers / layering_check):
    compile a HEADER standalone (`-xc++-header`, `.processed` output), not a TU.
    CMake has no equivalent; counting them would show every header as extra_tu."""
    return "-xc++-header" in args or "-fsyntax-only" in args


def _source_from_compile_args(args) -> Optional[str]:
    """The compiled source is the arg to -c (or the lone source arg). Returns
    None for header inputs -- not translation units."""
    src = None
    for i, a in enumerate(args):
        if a == "-c" and i + 1 < len(args):
            src = args[i + 1]
            break
    if src is None:
        for a in args:
            if a.endswith(_SOURCE_EXTS):
                src = a
                break
    if src is None or src.endswith(_HEADER_EXTS):
        return None
    return src


def _rel(path: str, repo_root: str) -> str:
    """Make a source path repo-relative, POSIX-normalized. Handles both an
    absolute path under repo_root (CMake) and an already-workspace-relative path
    (Bazel bazel-out/..., or a source path). Absolute paths outside repo_root and
    relative paths are returned normalized but unchanged."""
    if repo_root and os.path.isabs(path):
        try:
            r = os.path.relpath(path, repo_root)
            if not r.startswith(".."):
                return r.replace(os.sep, "/")
        except ValueError:
            pass
    p = path.replace(os.sep, "/")
    rr = repo_root.replace(os.sep, "/").rstrip("/")
    if rr and p.startswith(rr + "/"):
        p = p[len(rr) + 1:]
    return p


def _archive_identity(arg) -> Optional[str]:
    """Abstract dep name for an archive FILE link input: 'libcatch2_main.a' ->
    'catch2_main'. None if `arg` is a flag/object/output, not an archive."""
    if arg.startswith("-") or not arg.endswith(_ARCHIVE_EXTS):
        return None
    stem = os.path.basename(arg).split(".")[0]
    if stem.startswith("lib"):
        stem = stem[3:]
    return stem or None


def _is_external_path(arg) -> bool:
    """An external/third-party (bzlmod) archive vs an in-project library:
    Bazel puts external repos under 'external/<repo>'."""
    return "external/" in arg


def _infer_deps_from_link(actions, existing) -> List[Dependency]:
    """Infer link deps from link-action argv (Bazel only has argv). -l<name> ->
    external system lib; archive file inputs -> external if under external/, else
    internal. Dedup against names already present."""
    deps: List[Dependency] = list(existing)
    have = {d.name for d in deps}
    for act in actions:
        if act.mnemonic not in _LINK_MNEMONICS:
            continue
        for a in act.arguments:
            if a.startswith("-l"):
                name = a[2:]
                if name and name not in have:
                    deps.append(Dependency(name, external=True)); have.add(name)
                continue
            ident = _archive_identity(a)
            if ident and ident not in have:
                deps.append(Dependency(ident, external=_is_external_path(a)))
                have.add(ident)
    return deps


# ---- the reconstruct entry points ------------------------------------------

def reconstruct_target(t: Target, build_system: BuildSystem,
                       repo_root: str) -> TargetView:
    is_bazel = build_system == BuildSystem.BAZEL
    view = TargetView(name=t.name, kind=t.kind, role=t.role)

    for act in t.actions:
        if act.mnemonic in _COMPILE_MNEMONICS:
            if _is_header_processing(act.arguments):
                continue
            src = _source_from_compile_args(act.arguments)
            if not src:
                continue
            cdef, cinc, cfl = canonicalize_flags(
                list(act.arguments), repo_root, is_bazel=is_bazel)
            view.tus.append(TranslationUnit(
                source=_rel(src, repo_root), defines=cdef,
                includes=cinc, flags=cfl))
        elif act.mnemonic in _LINK_MNEMONICS:
            # CppArchive (static lib) has no meaningful link flags; only CppLink.
            if act.mnemonic == "CppLink":
                view.link_flags = canonicalize_link_flags(
                    list(act.arguments), is_bazel=is_bazel)

    # deps: trust the frontend's resolved annotation (CMake); else infer from
    # link argv (Bazel).
    if t.deps:
        view.deps = list(t.deps)
    else:
        view.deps = _infer_deps_from_link(t.actions, t.deps)
    return view


def reconstruct(model: CanonicalModel) -> dict:
    """Reconstruct every target into a TargetView, keyed by target name."""
    return {name: reconstruct_target(t, model.build_system, model.repo_root)
            for name, t in model.targets.items()}
