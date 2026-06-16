"""Flag canonicalization -- the core IP of the diff engine.

Raw compile commands never diff cleanly: absolute vs relative paths, flag
ordering, sandbox include prefixes, toolchain-default flags Bazel injects that
CMake never had. This module normalizes a raw flag list into the canonical
form the model stores, applying a per-flag POLICY:

  * defines  (-D)        : order-insensitive  -> sorted, deduped
  * includes (-I/-isystem/-iquote): ORDER MATTERS (search order) -> preserved,
                            paths made repo-relative
  * other flags          : split into "correctness" vs "ignorable"

The asymmetry (require CMake flags present in Bazel; tolerate extra Bazel
flags) is enforced in diff.py, not here. Here we only normalize and classify.
"""

from __future__ import annotations

import os
import posixpath
from typing import Iterable, List, Tuple

# Flags Bazel's C++ toolchain injects (or sandboxing adds) that have no CMake
# counterpart and must NOT count as discrepancies. Matched as exact tokens or
# prefixes. Extend per-toolchain; this is intentionally conservative.
BAZEL_DEFAULT_FLAG_PREFIXES = (
    "-fno-canonical-system-headers",
    "-no-canonical-prefixes",
    "-fstack-protector",
    "-Wunused-but-set-parameter",
    "-Wno-free-nonheap-object",
    "-fdiagnostics-color",
    "-iquote",            # bazel adds repo-root -iquote for its own include style
    "-frandom-seed=",     # derived from output path; pure noise for a diff
    "-D__DATE__",
    "-D__TIMESTAMP__",
    "-D__TIME__",
)

# Flags that carry no correctness meaning for a parity check on either side.
IGNORABLE_FLAG_PREFIXES = (
    "-g",            # debug info level -- build-type driven, not a source diff
    "-O",            # optimization level -- ditto
    "-fdebug-prefix-map=",
    "-ffile-prefix-map=",
    "-MD", "-MF", "-MT", "-MMD",  # dep-file generation; build-system bookkeeping
)


def _to_repo_relative(path: str, repo_root: str) -> str:
    """Normalize an include path to a repo-relative POSIX path.

    Absolute paths under repo_root become relative; paths outside repo_root are
    kept absolute (they're external/system includes -- the resolver handles those).
    Bazel sandbox prefixes (bazel-out/..., external/...) are normalized to a
    stable form so they line up with CMake's build-dir includes where possible.
    """
    p = path.strip()
    if not p:
        return p
    norm = os.path.normpath(p)
    if os.path.isabs(norm):
        try:
            rel = os.path.relpath(norm, repo_root)
        except ValueError:
            return norm.replace(os.sep, "/")
        if not rel.startswith(".."):
            return rel.replace(os.sep, "/")
        return norm.replace(os.sep, "/")
    return norm.replace(os.sep, "/")


def _matches_any(flag: str, prefixes: Iterable[str]) -> bool:
    return any(flag == p or flag.startswith(p) for p in prefixes)


def canonicalize_flags(
    raw: List[str],
    repo_root: str,
    *,
    is_bazel: bool,
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    """Return (defines, includes, other_flags) in canonical form.

    - defines: sorted, deduped, '-D' stripped to 'KEY=VAL'
    - includes: repo-relative, ORDER PRESERVED, dedup-adjacent only
    - other_flags: correctness-relevant only (ignorable + bazel-default dropped),
      sorted for stable comparison
    """
    defines: List[str] = []
    includes: List[str] = []
    other: List[str] = []

    it = iter(range(len(raw)))
    i = 0
    n = len(raw)
    while i < n:
        tok = raw[i]

        # -Dfoo / -D foo
        if tok == "-D" and i + 1 < n:
            defines.append(raw[i + 1]); i += 2; continue
        if tok.startswith("-D"):
            defines.append(tok[2:]); i += 1; continue

        # include flavors: -I, -isystem, -iquote, -idirafter (split or joined)
        if tok in ("-I", "-isystem", "-iquote", "-idirafter") and i + 1 < n:
            if not (is_bazel and _matches_any(tok, BAZEL_DEFAULT_FLAG_PREFIXES)):
                includes.append(_to_repo_relative(raw[i + 1], repo_root))
            i += 2; continue
        if tok.startswith("-I"):
            includes.append(_to_repo_relative(tok[2:], repo_root)); i += 1; continue

        # drop bazel toolchain defaults on the bazel side only
        if is_bazel and _matches_any(tok, BAZEL_DEFAULT_FLAG_PREFIXES):
            i += 1; continue
        # drop universally-ignorable flags on both sides
        if _matches_any(tok, IGNORABLE_FLAG_PREFIXES):
            i += 1; continue

        other.append(tok); i += 1

    # defines: order-insensitive
    canon_defines = tuple(sorted(set(defines)))
    # includes: order preserved, drop only consecutive dups
    canon_includes: List[str] = []
    for inc in includes:
        if not canon_includes or canon_includes[-1] != inc:
            canon_includes.append(inc)
    # other flags: order-insensitive for parity
    canon_other = tuple(sorted(set(other)))

    return canon_defines, tuple(canon_includes), canon_other
