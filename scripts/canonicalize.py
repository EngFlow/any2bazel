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
import re
from typing import Iterable, List, Tuple

# Flags Bazel's C++ toolchain injects that are PURE NOISE: they never have a
# CMake counterpart and never collide with a real project flag, so they are
# dropped from the Bazel side at canonicalization. Matched as exact tokens or
# prefixes. Extend per-toolchain; intentionally conservative.
BAZEL_NOISE_FLAG_PREFIXES = (
    "-fno-canonical-system-headers",
    "-no-canonical-prefixes",
    "-frandom-seed=",     # derived from output path; pure noise for a diff
    "-D__DATE__",
    "-D__TIMESTAMP__",
    "-D__TIME__",
)

# Flags Bazel's toolchain injects by default that a project MIGHT ALSO set
# explicitly. These are NOT stripped at canonicalization -- doing so is unsafe
# for two reasons:
#   1. Prefix greediness: stripping "-fstack-protector" also eats a project's
#      "-fstack-protector-strong", so a flag CMake and Bazel BOTH set no longer
#      cancels and fabricates a false cmake-only discrepancy.
#   2. Asymmetry direction: stripping a flag only from the Bazel side can only
#      ever CREATE false cmake-only errors (it shrinks the Bazel set), never
#      hide a real one.
# Instead they are kept through canonicalization so shared flags cancel in the
# subtraction, and filtered only from the cosmetic `bazel_only` DISPLAY in
# diff.py (they land in the tolerated bazel-only bucket regardless). If CMake
# sets a variant Bazel lacks (e.g. CMake -fstack-protector-strong, Bazel only
# the default -fstack-protector), that correctly surfaces as a real diff.
BAZEL_TOLERATED_FLAG_PREFIXES = (
    "-fstack-protector",
    "-fdiagnostics-color",
    "-Wunused-but-set-parameter",
    "-Wno-free-nonheap-object",
    "-fno-omit-frame-pointer",
)

# Back-compat alias: the union is what "bazel default" used to mean. Kept so any
# external caller referencing the old name still works; canonicalization now
# only strips the NOISE subset (see canonicalize_flags).
BAZEL_DEFAULT_FLAG_PREFIXES = BAZEL_NOISE_FLAG_PREFIXES + BAZEL_TOLERATED_FLAG_PREFIXES

# Flags that carry no correctness meaning for a parity check on either side.
IGNORABLE_FLAG_PREFIXES = (
    "-g",            # debug info level -- build-type driven, not a source diff
    "-O",            # optimization level -- ditto
    "-fdebug-prefix-map=",
    "-ffile-prefix-map=",
    "-ffile-compilation-dir=",     # reproducibility; output-path derived
    "-MD", "-MF", "-MT", "-MMD",  # dep-file generation; build-system bookkeeping
    # --- toolchain / sysroot selection: environment-specific, not a migration
    # decision. These differ per machine/SDK and must never be a discrepancy. ---
    "-mmacosx-version-min=",
)

# Driver MECHANICS, hardcoded: how the compiler is invoked, not what it does.
# These are universal facts (a -o names an output; -c means compile-only), never
# migration decisions, so they're stripped at canonicalization, not configured.
# Flags that consume the FOLLOWING token (drop both):
_DRIVER_PAIR_FLAGS = {"-o", "-c", "-isysroot", "--sysroot", "-arch", "-target",
                      "-gcc-toolchain", "-x", "--serialize-diagnostics"}

# Defines the Bazel toolchain injects for reproducible builds (arrive as
# KEY="..." after -D splitting). Matched by KEY before '='. Hardcoded fact.
_DROP_DEFINE_KEYS = {"__DATE__", "__TIME__", "__TIMESTAMP__"}

# --- LINK-flag noise (hardcoded mechanics, dropped at canonicalization) -------
# Linker flags the Bazel toolchain / platform injects that have no migration
# meaning. Same philosophy as BAZEL_DEFAULT_FLAG_PREFIXES but for the link step.
# Conservative; extend per toolchain. Reviewer-judgment link flags go in the
# config's ignore.link_flags instead.
BAZEL_DEFAULT_LINK_PREFIXES = (
    "-Wl,-oso_prefix",          # macOS: strips build-dir prefix from debug map
    "-Wl,-S",                   # strip debug; build-type driven
    "-headerpad_max_install_names",  # macOS install-name padding
    "-fobjc-link-runtime",      # macOS objc runtime; toolchain default
    "-no-canonical-prefixes",
    "-mmacosx-version-min=",    # sysroot/version selection (also in compile)
    "-fexperimental-",
    "-Wl,-install_name",
    "-Wl,-rpath,__BAZEL",       # bazel sandbox rpaths
)


def _is_bazel_solib_rpath(tok: str) -> bool:
    """Bazel's own rpaths, into its `_solib_<cpu>` directories (spelled
    `-Xlinker -rpath -Xlinker $ORIGIN/.../_solib_k8/...`, which fold_xlinker
    turns into one `-Wl,-rpath,<dir>`). Only those are toolchain noise; a
    project's own rpath (-Wl,-rpath,$ORIGIN/../lib, -Wl,-rpath=/opt/lib,
    -Wl,-rpath-link,...) is kept and compared, even one whose path happens to
    contain `_solib_` elsewhere."""
    return _BAZEL_SOLIB_RPATH.match(tok) is not None


_BAZEL_SOLIB_RPATH = re.compile(
    r"-Wl,-rpath,(\$ORIGIN|\$EXEC_ORIGIN|@loader_path)(/.*)?/_solib_[^/]*(/|$)")

# Link flags with no correctness meaning on either side (build-type / debug).
IGNORABLE_LINK_PREFIXES = (
    "-g",
    "-O",
    "-fdebug-prefix-map=",
    "-ffile-prefix-map=",
)

# Link-driver flags that consume the FOLLOWING token (drop both).
_LINK_PAIR_FLAGS = {"-o", "-isysroot", "--sysroot", "-arch", "-target"}


def _is_link_input(tok: str) -> bool:
    """A link INPUT (object, archive, or lib reference) rather than a link FLAG.
    These are the deps/objects being linked, compared separately as the link
    closure -- not link flags. -l<name> and -L<path> are lib search refs; .o/.a/
    .lo/.obj/.dylib/.so are concrete inputs; bare positional tokens are the
    linker/wrapper path or output."""
    if tok.startswith(("-l", "-L")):
        return True
    if tok.startswith("-"):
        return False
    return (tok.endswith((".o", ".obj", ".a", ".lo", ".lib", ".so", ".dylib",
                          ".sh", ".framework"))
            or ".so." in tok
            or ("/" in tok and not tok.startswith("/")))  # relative paths/objs


def _is_driver_token(tok: str) -> bool:
    """A bare positional argv token on the Bazel side: the compiler path, a
    wrapper script, the source file, or the .o output. Compile flags always
    start with '-' (or '/' on Windows, handled separately), so any other bare
    token is driver mechanics to drop. CMake File-API fragments never contain
    these, so this only affects the Bazel side."""
    if tok.startswith("-"):
        return False
    return (tok.endswith((".sh", ".o", ".obj", ".cc", ".cpp", ".cxx", ".c", ".C",
                          ".m", ".mm"))
            or "/" in tok and not tok.startswith("/"))  # relative exec/source paths


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


def _macro_name(define: str) -> str:
    """The macro a define names: the identifier before any `(` (a
    function-like macro, `F(x)=x`) or `=` (a value)."""
    ends = [i for i in (define.find("("), define.find("=")) if i >= 0]
    return define[:min(ends)] if ends else define


def _undefine(defines: List[str], undefined: List[str], name: str) -> None:
    """Apply `-U name` as the compiler does: it cancels every `name` define
    that came before it. `name` is also recorded in `undefined`, the names
    whose LAST word is a -U: those are emitted as `-Uname` flags and still
    compared, because the -U may target a compiler-predefined macro (e.g.
    -U_FORTIFY_SOURCE). A later -D on `name` clears it (see _redefine), so
    whether a -U survives never depends on what came before it."""
    defines[:] = [d for d in defines if _macro_name(d) != name]
    if name not in undefined:
        undefined.append(name)


def _redefine(defines: List[str], undefined: List[str], d: str) -> None:
    """Apply `-D d` as the compiler does: it replaces every earlier definition
    of the same macro name (so each side ends with the last word per name,
    whether or not a -U came between), and a -U on that name is no longer the
    last word."""
    key = _macro_name(d)
    defines[:] = [x for x in defines if _macro_name(x) != key]
    if key in undefined:
        undefined.remove(key)


def _add_define(defines: List[str], d: str) -> None:
    """Append a define unless it's a hardcoded reproducibility injection
    (__DATE__/__TIME__/__TIMESTAMP__), matched by key before '='. A define
    with no value means value 1 to the compiler (`-DX` is `-DX=1`, `-DF(x)`
    is `-DF(x)=1`), so it is spelled that way and the two compare equal."""
    key = d.split("=", 1)[0]
    if key not in _DROP_DEFINE_KEYS:
        defines.append(d if "=" in d else d + "=1")


def canonicalize_flags(
    raw: List[str],
    repo_root: str,
    *,
    is_bazel: bool,
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    """Return (defines, includes, other_flags) in canonical form.

    - defines: sorted, deduped, '-D' stripped to 'KEY=VAL'; a later -U removes
      them (see _undefine)
    - includes: repo-relative, ORDER PRESERVED, dedup-adjacent only
    - other_flags: correctness-relevant only (ignorable + bazel-default dropped),
      sorted for stable comparison
    """
    defines: List[str] = []
    includes: List[str] = []
    other: List[str] = []
    undefined: List[str] = []  # names whose last word is -U (see _undefine)

    i = 0
    n = len(raw)
    # On the Bazel side the first argv token is the compiler/wrapper path
    # (driver mechanics). CMake File-API fragments don't include it.
    if is_bazel and n and _is_driver_token(raw[0]):
        i = 1
    while i < n:
        tok = raw[i]

        # driver flags that consume the next token (-o out.o, -isysroot /sdk,
        # -arch arm64, -c src). Pure invocation mechanics -- drop flag + arg.
        if tok in _DRIVER_PAIR_FLAGS and i + 1 < n:
            i += 2; continue
        # bare positional driver tokens (compiler path mid-argv, .o output)
        if _is_driver_token(tok):
            i += 1; continue

        # -Dfoo / -D foo
        if tok == "-D" and i + 1 < n:
            _redefine(defines, undefined, raw[i + 1]); _add_define(defines, raw[i + 1])
            i += 2; continue
        if tok.startswith("-D"):
            _redefine(defines, undefined, tok[2:]); _add_define(defines, tok[2:])
            i += 1; continue
        # -Ufoo / -U foo: cancels the defines collected so far (last wins)
        if tok == "-U" and i + 1 < n:
            _undefine(defines, undefined, raw[i + 1]); i += 2; continue
        if tok.startswith("-U"):
            _undefine(defines, undefined, tok[2:]); i += 1; continue

        # force-include flavors: -include FILE / -imacros FILE. The argument is
        # a FILE path (not a search dir), which each build system may spell
        # absolutely (CMake) or repo-relative (Bazel); normalize it to a
        # repo-relative path so the same forced header lines up. Emitted as a
        # single joined token so the flag+path pair compares as one unit.
        if tok in ("-include", "-imacros") and i + 1 < n:
            other.append(tok + " " + _to_repo_relative(raw[i + 1], repo_root))
            i += 2; continue

        # include flavors: -I, -isystem, -iquote, -idirafter (split or joined)
        if tok in ("-I", "-isystem", "-iquote", "-idirafter") and i + 1 < n:
            includes.append(_to_repo_relative(raw[i + 1], repo_root))
            i += 2; continue
        if tok.startswith("-I"):
            includes.append(_to_repo_relative(tok[2:], repo_root)); i += 1; continue

        # drop pure-noise bazel toolchain flags on the bazel side only. NOTE:
        # tolerated-default flags (-fstack-protector, -fdiagnostics-color, ...)
        # are intentionally NOT dropped here -- see BAZEL_TOLERATED_FLAG_PREFIXES:
        # they must survive so a flag both sides set cancels in the diff, and
        # they're filtered from the cosmetic bazel_only display in diff.py.
        if is_bazel and _matches_any(tok, BAZEL_NOISE_FLAG_PREFIXES):
            i += 1; continue
        # drop universally-ignorable flags on both sides
        if _matches_any(tok, IGNORABLE_FLAG_PREFIXES):
            i += 1; continue

        other.append(tok); i += 1

    # a -U that is the last word on its name stays a comparable flag
    other.extend("-U" + name for name in undefined)
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


def fold_xlinker(tokens: List[str]) -> List[str]:
    """`-Xlinker X` -> `-Wl,X`: the same linker argument in one token. Then an
    rpath given as two linker arguments (`-Wl,-rpath` followed by `-Wl,DIR`,
    whichever spelling produced them) is joined into `-Wl,-rpath,DIR`, which
    is what the linker receives, so every spelling of one rpath compares
    equal."""
    folded: List[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "-Xlinker" and i + 1 < len(tokens):
            folded.append("-Wl," + tokens[i + 1]); i += 2
        else:
            folded.append(tokens[i]); i += 1
    out: List[str] = []
    i = 0
    while i < len(folded):
        if (folded[i] == "-Wl,-rpath" and i + 1 < len(folded)
                and folded[i + 1].startswith("-Wl,")):
            out.append("-Wl,-rpath," + folded[i + 1][4:]); i += 2
        else:
            out.append(folded[i]); i += 1
    return out


def canonicalize_link_flags(raw: List[str], *, is_bazel: bool) -> Tuple[str, ...]:
    """Extract correctness-relevant LINK flags from a link command.

    Input is either CMake's link.commandFragments of role 'flags' (already just
    flags) or a Bazel CppLink argv (flags mixed with the linker path, -o output,
    object/archive inputs, and -l/-L lib refs). We drop:
      - the linker/wrapper path and -o output (driver mechanics)
      - object/archive/lib INPUTS and -l/-L refs (that's the link closure, diffed
        separately as deps -- not link flags)
      - hardcoded toolchain/platform link noise (BAZEL_DEFAULT_LINK_PREFIXES) on
        the bazel side, and build-type-ish flags (IGNORABLE_LINK_PREFIXES) on both
    What remains are genuine link flags (-pthread, -Wl,--gc-sections,
    -static-libstdc++, -rdynamic, ...). Returned sorted+deduped (order-insensitive).
    """
    out: List[str] = []
    i = 0
    if is_bazel and raw and _is_link_input(raw[0]):
        i = 1  # leading linker/wrapper path
    # `-Xlinker X` and `-Wl,X` pass the same argument to the linker; fold the
    # two-token form first, so it compares as one flag on either side and goes
    # through the same noise filters (Bazel's rpaths, below).
    raw = list(raw[:i]) + fold_xlinker(list(raw[i:]))
    n = len(raw)
    while i < n:
        tok = raw[i]
        if tok in _LINK_PAIR_FLAGS and i + 1 < n:
            i += 2; continue
        if _is_link_input(tok):
            i += 1; continue
        if is_bazel and (_is_bazel_solib_rpath(tok)
                         or _matches_any(tok, BAZEL_DEFAULT_LINK_PREFIXES)):
            i += 1; continue
        if _matches_any(tok, IGNORABLE_LINK_PREFIXES):
            i += 1; continue
        out.append(tok); i += 1
    return tuple(sorted(set(out)))
