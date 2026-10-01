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

"""Asymmetric parity diff: CMake reference model (A) vs Bazel model (B).

Produces a worklist of discrepancies for the fix loop. Each iteration of the
migrate loop calls this; an empty worklist (for a full round) means "done".
Parity has two stages, both reported here:
  COMPILE PARITY    -- per-TU flag/define/include equivalence (project-wide
                       TU-set; a source the reference compiles several ways,
                       e.g. in a shared/static twin, is matched against ANY of
                       its variants -- see _union_tus) + external
                       link-dependency closure.
  LINK CONSISTENCY  -- per name-aligned executable/shared-lib, equivalent link
                       flags (link_flags_diff).

ASYMMETRY: a flag present in CMake but missing in Bazel is a discrepancy
(under-specified Bazel target -> likely wrong build). A flag present only in
Bazel is tolerated (toolchain defaults already stripped in canonicalize). The
one exception is defines, where an EXTRA Bazel define can change behavior, so
extra defines are reported at lower severity.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from canonicalize import BAZEL_TOLERATED_FLAG_PREFIXES, PIC_FLAGS
from config import MigrationConfig
from model import (CanonicalModel, TargetKind, TargetRole, TranslationUnit)
from reconstruct import TargetView, reconstruct


def _display_bazel_only(flags) -> list:
    """Cosmetic filter for the `bazel_only` side of a flag diff. Bazel-injected
    tolerated defaults (-fstack-protector, -fdiagnostics-color, ...) are kept
    through canonicalization so they CANCEL against a project flag both sides
    set (see canonicalize.BAZEL_TOLERATED_FLAG_PREFIXES). When only Bazel has
    them they're harmless toolchain noise, so drop them from the reported
    bazel_only list to keep the worklist readable. Never affects severity --
    bazel_only is always tolerated; this only tidies the display."""
    return sorted(f for f in flags
                  if not any(f == p or f.startswith(p)
                             for p in BAZEL_TOLERATED_FLAG_PREFIXES))


class Severity(str, Enum):
    ERROR = "error"      # blocks parity: must fix to converge
    WARN = "warn"        # likely benign (e.g. extra bazel define) but surfaced


class Kind(str, Enum):
    MISSING_TARGET = "missing_target"
    EXTRA_TARGET = "extra_target"
    KIND_MISMATCH = "kind_mismatch"
    MISSING_TU = "missing_tu"
    EXTRA_TU = "extra_tu"
    DEFINES_DIFF = "defines_diff"
    INCLUDES_DIFF = "includes_diff"
    FLAGS_DIFF = "flags_diff"
    LINK_FLAGS_DIFF = "link_flags_diff"   # executable/shared-lib link flags
    MISSING_DEP = "missing_dep"
    # Java (CompileGroup) source-set comparison:
    MISSING_JAVA_SRC = "missing_java_src"  # .java compiled in A but not B
    EXTRA_JAVA_SRC = "extra_java_src"      # .java compiled in B but not A
    # test-specific (only emitted when cfg.include_tests):
    MISSING_TEST_TU = "missing_test_tu"     # test source compiled in cmake, not bazel
    EXTRA_TEST_TU = "extra_test_tu"         # test source compiled in bazel, not cmake
    TEST_BINARY_COUNT = "test_binary_count"  # differing number of test executables


@dataclass
class Discrepancy:
    kind: str
    severity: str
    target: str
    detail: str
    tu: Optional[str] = None
    cmake_only: Optional[list] = None  # present in A, absent in B  -> must add
    bazel_only: Optional[list] = None  # present in B, absent in A  -> usually ok


def _diff_tu(target: str, a: TranslationUnit, b: TranslationUnit,
             cfg: "MigrationConfig") -> List[Discrepancy]:
    out: List[Discrepancy] = []

    # defines: order-insensitive set compare; both directions matter.
    # Reviewer-approved ignores (cfg) are dropped from BOTH sides first.
    a_def = {d for d in a.defines if not cfg.define_ignored(d)}
    b_def = {d for d in b.defines if not cfg.define_ignored(d)}
    if a_def - b_def or b_def - a_def:
        out.append(Discrepancy(
            kind=Kind.DEFINES_DIFF.value,
            severity=Severity.ERROR.value if (a_def - b_def) else Severity.WARN.value,
            target=target, tu=a.source,
            detail="defines differ",
            cmake_only=sorted(a_def - b_def),
            bazel_only=sorted(b_def - a_def),
        ))

    # includes: PRESENCE check (order currently NOT enforced -- see below).
    # Two normalizations, applied to both sides first:
    #   1. include_map: rewrite differing spellings of the same dep root to a
    #      canonical token, so the check is PRESERVED (the dep must still be
    #      present). Collapse adjacent dups the rewrite produces (Bazel's
    #      external/X and bazel-out/.../external/X both -> the token).
    #   2. include_ignored: drop blind-spot prefixes entirely.
    #
    # We require every CMake include root to be PRESENT on the Bazel side, but
    # do NOT (yet) enforce relative ORDER. Order only changes the build when the
    # same header name is reachable from two roots whose order differs; proving
    # that requires enumerating headers on disk (a collision check). Until that
    # verifier exists, enforcing order produced benign false positives (e.g.
    # boringssl: project `include` vs vendored gtest roots, disjoint headers,
    # reordered). See docs/FUTURE-include-order-collision-check.md.
    a_inc = _norm_includes(a.includes, cfg)
    b_inc = _norm_includes(b.includes, cfg)
    missing = [i for i in a_inc if i not in set(b_inc)]
    if missing:
        out.append(Discrepancy(
            kind=Kind.INCLUDES_DIFF.value,
            severity=Severity.ERROR.value,
            target=target, tu=a.source,
            detail="cmake include root missing on bazel side",
            cmake_only=missing,
            bazel_only=[i for i in b_inc if i not in set(a_inc)],
        ))
    # A root present on both sides but reachable on the Bazel side ONLY
    # through -iquote is not the same search directory: the reference's -I
    # serves `#include <x.h>`, Bazel's -iquote does not (Bazel puts the
    # workspace and genfiles roots on -iquote for every compile, so a
    # reference `-I<src>` root always *looks* present). Reported apart from
    # `missing` so the fix reads: make the root angle-capable (`includes`).
    b_quote = set(_norm_includes(b.quote_only, cfg))
    a_quote = set(_norm_includes(a.quote_only, cfg))
    quote_only = [i for i in a_inc
                  if i in set(b_inc) and i in b_quote and i not in a_quote]
    if quote_only:
        out.append(Discrepancy(
            kind=Kind.INCLUDES_DIFF.value,
            severity=Severity.ERROR.value,
            target=target, tu=a.source,
            detail="cmake include root present on bazel side only as -iquote "
                   "(quoted includes): `#include <...>` cannot find it there; "
                   "put it in `includes` or an -I copt",
            cmake_only=quote_only,
            bazel_only=[],
        ))

    # other flags: asymmetric subset -- A must be subset of B.
    # Reviewer-approved ignores are dropped from both sides first.
    a_fl = {f for f in a.flags if not cfg.flag_ignored(f)}
    b_fl = {f for f in b.flags if not cfg.flag_ignored(f)}
    if a_fl - b_fl:
        out.append(Discrepancy(
            kind=Kind.FLAGS_DIFF.value,
            severity=Severity.ERROR.value,
            target=target, tu=a.source,
            detail="cmake compile flags missing on bazel side",
            cmake_only=sorted(a_fl - b_fl),
            bazel_only=_display_bazel_only(b_fl - a_fl),
        ))
    return out


def _diff_link_flags(name: str, ta: "TargetView", tb: "TargetView",
                     cfg: "MigrationConfig") -> List[Discrepancy]:
    """Asymmetric link-flag subset for one aligned executable/shared-lib: every
    CMake link flag must be present on the Bazel side; extra Bazel link flags
    are tolerated. Reviewer-approved differences (cfg.link_flag_ignored) are
    dropped from both sides first. Mirrors the compile-flag check."""
    a_lf = {f for f in ta.link_flags if not cfg.link_flag_ignored(f)}
    b_lf = {f for f in tb.link_flags if not cfg.link_flag_ignored(f)}
    if a_lf - b_lf:
        return [Discrepancy(
            kind=Kind.LINK_FLAGS_DIFF.value,
            severity=Severity.ERROR.value,
            target=name,
            detail="cmake link flags missing on bazel side",
            cmake_only=sorted(a_lf - b_lf),
            bazel_only=sorted(b_lf - a_lf),
        )]
    return []


def _norm_includes(includes, cfg) -> tuple:
    """Apply include_map rewrites then drop ignored prefixes, collapsing the
    consecutive duplicates a rewrite can create (e.g. external/X and its
    bazel-out twin both map to the same token). Order preserved."""
    out = []
    for inc in includes:
        mapped = cfg.map_include(inc)
        if cfg.include_ignored(mapped) or cfg.include_ignored(inc):
            continue
        if not out or out[-1] != mapped:
            out.append(mapped)
    return tuple(out)


# Roles the parity diff actually compares. Everything else (dashboard,
# aggregate, codegen, test, unknown) is retained in the model and reported
# separately for inspection, but never produces a discrepancy. Flip TEST on
# here when the test-migration phase begins -- no re-extraction needed.
PARTICIPATING_ROLES = {TargetRole.PRODUCTION}


def _participating(views: Dict[str, TargetView], cfg: "MigrationConfig") -> set:
    return {n for n, t in views.items()
            if t.role in PARTICIPATING_ROLES and not cfg.target_excluded(n)}


def _apply_target_map(views: Dict[str, TargetView],
                      target_map: Dict[str, str]) -> Dict[str, TargetView]:
    """Rename Bazel-side target VIEWS into the CMake namespace using a declared
    map (cmake_name -> bazel_name), so intentionally-renamed targets align.

    The map is authored by whoever generates the BUILD files -- an explicit
    input, not a fuzzy guess. Returns a re-keyed dict; deps (which reference
    target names) are renamed too so link-closure comparison lines up.
    """
    if not target_map:
        return views
    from dataclasses import replace
    b2cmake = {bazel: cmake for cmake, bazel in target_map.items()}
    renamed: Dict[str, TargetView] = {}
    for name, t in views.items():
        t.name = b2cmake.get(name, name)
        t.deps = [replace(d, name=b2cmake.get(d.name, d.name)) for d in t.deps]
        renamed[t.name] = t
    return renamed


_LIBRARY_KINDS = {TargetKind.STATIC, TargetKind.SHARED, TargetKind.OBJECT,
                  TargetKind.INTERFACE}


@dataclass
class TUVariant:
    """One compilation of a source by one target: the TU plus who compiled it.
    A source compiled by several targets of a side (CMake's shared/static
    twins; a Bazel cc_library built both PIC and non-PIC) has several."""
    tu: TranslationUnit
    target: str
    kind: str          # TargetKind value of the owning target

    def signature(self):
        return (self.tu.defines, self.tu.includes, self.tu.flags)


def _union_tus(views: Dict[str, TargetView], names,
               cfg: "MigrationConfig") -> Dict[str, List[TUVariant]]:
    """Pool TUs of the given target views into one source-keyed map (the TU-SET
    comparison): grouping/renames/fold-ins don't matter -- every compiled source
    lands in one flat map keyed by repo-relative path. Keys are run through
    cfg.map_source so generated-source grouping asymmetries (e.g. CMake's single
    AUTOMOC bundle vs Bazel's per-header moc_*.cpp) collapse to one token.

    EVERY VARIANT of a source is kept, in sorted-target order (sorted: `names`
    is usually a set, and the report must not depend on the hash seed).
    CMake's shared/static twin (add_library(foo SHARED) + add_library(
    foo-static STATIC) from one source list) compiles each file twice with
    different argv (-Dfoo_EXPORTS, -fPIC, sometimes project defines), and a
    Bazel cc_library compiles it once, PIC or not as the toolchain decides --
    or twice when both a binary and a cc_shared_library consume it. Keeping
    one representative would compare the Bazel TU against an arbitrary twin,
    so migrations would rename targets or suppress *_EXPORTS markers to land
    on the twin they match. Instead the comparison (_diff_tu_variants)
    accepts a Bazel TU that satisfies ANY CMake variant. Two targets
    compiling a source with identical canonical argv contribute one variant
    (the first in sorted order names it)."""
    out: Dict[str, List[TUVariant]] = {}
    seen: Dict[str, set] = {}
    for n in sorted(names):
        for tu in views[n].tus:
            key = cfg.map_source(tu.key())
            v = TUVariant(tu, n, views[n].kind.value)
            sig = v.signature()
            if sig in seen.setdefault(key, set()):
                continue
            seen[key].add(sig)
            out.setdefault(key, []).append(v)
    return out


def _pic(v: TUVariant) -> bool:
    """Is this variant the position-independent one? A CMake SHARED target's
    objects are PIC by construction; otherwise the argv says (-fPIC/-fpic).
    Used only as a tie-break when two variants are equally close."""
    return (v.kind == TargetKind.SHARED.value
            or any(f in PIC_FLAGS for f in v.tu.flags))


def _variant_distance(discs: List[Discrepancy]) -> Tuple[int, int]:
    """How far a Bazel TU is from one CMake variant, from _diff_tu's findings:
    (tokens the Bazel side is MISSING -- the errors -- , tokens it has EXTRA at
    warn level). An error without a cmake_only list counts as one token."""
    missing = sum(len(d.cmake_only or []) or 1 for d in discs
                  if d.severity == Severity.ERROR.value)
    extra = sum(len(d.bazel_only or []) for d in discs
                if d.severity != Severity.ERROR.value)
    return missing, extra


def _diff_tu_variants(target: str, a_vars: List[TUVariant], b_var: TUVariant,
                      cfg: "MigrationConfig", name_variants: bool) -> List[Discrepancy]:
    """Compare ONE Bazel TU against every CMake variant of the same source.

    The Bazel TU converges if it satisfies the asymmetric checks (_diff_tu)
    against AT LEAST ONE variant: nothing is reported then, whichever twin it
    was. Otherwise the findings against the CLOSEST variant are reported --
    the one with the fewest missing tokens, then the fewest extra ones, then
    the one of the same kind as the Bazel TU (PIC objects belong to the shared
    twin, non-PIC to the static one), then the first in sorted-target order,
    so the report is deterministic. With `name_variants` (either side has
    more than one variant of this source) the detail names both the chosen
    CMake target and the Bazel target the TU came from, so the reader knows
    which argv pair to look at; a plain one-to-one source keeps the bare
    detail text.
    """
    best = None
    for av in a_vars:
        discs = _diff_tu(target, av.tu, b_var.tu, cfg)
        if not discs:
            return []
        missing, extra = _variant_distance(discs)
        key = (missing, extra, _pic(av) != _pic(b_var))
        if best is None or key < best[0]:
            best = (key, av, discs)
    _, av, discs = best
    if name_variants:
        which = (f"closest of {len(a_vars)} cmake variants" if len(a_vars) > 1
                 else "cmake variant")
        suffix = (f" ({which}: {av.target} [{av.kind}]; "
                  f"bazel TU from {b_var.target})")
        for d in discs:
            d.detail += suffix
    # CMake's DEFINE_SYMBOL marker (`<target>_EXPORTS`, on every SHARED/MODULE
    # target) is the one define a migration meets on every shared-only
    # library, and the fix is always the same; say so where it is reported
    marker = _export_macro(av.target)
    for d in discs:
        if d.kind == Kind.DEFINES_DIFF.value and marker in (d.cmake_only or []):
            d.detail += (f" [{marker} is the DEFINE_SYMBOL marker CMake adds to "
                         f"every SHARED/MODULE target and no static twin matches: "
                         f"carry it in local_defines, or record it in ignore.defines]")
    return discs


def _export_macro(target: str) -> str:
    """CMake's default DEFINE_SYMBOL for a target: the name as a C identifier
    (`fmt-c` -> `fmt_c`) plus `_EXPORTS`."""
    return re.sub(r"[^A-Za-z0-9_]", "_", target) + "_EXPORTS"


def _sans_pic(v: TUVariant):
    """A variant's signature with the position-independence flags removed:
    the PIC and non-PIC compiles of one Bazel cc_library are equal under it."""
    d, i, f = v.signature()
    return d, i, tuple(x for x in f if x not in PIC_FLAGS)


def _diff_tu_unions(target: str, a_union: Dict[str, List[TUVariant]],
                    b_union: Dict[str, List[TUVariant]],
                    cfg: "MigrationConfig") -> List[Discrepancy]:
    """The TU-set comparison for the sources both sides compile: every Bazel
    variant must match some CMake variant (_diff_tu_variants). A CMake
    variant no Bazel TU matches is not a finding on its own -- Bazel builds
    a source once (PIC or not) where CMake's twin builds it twice."""
    out: List[Discrepancy] = []
    for src in sorted(set(a_union) & set(b_union)):
        multi = len(a_union[src]) > 1 or len(b_union[src]) > 1
        results = [(bv, _diff_tu_variants(target, a_union[src], bv, cfg, multi))
                   for bv in b_union[src]]
        # Bazel compiles a source twice when both an archive and a shared
        # library consume it (`.o` and `.pic.o`), and the two compiles
        # differ ONLY by the PIC flag. Which ones the toolchain builds is
        # not a BUILD-file decision, so such PIC twins are judged as ONE
        # compile: the twin closest to the reference speaks for both (a
        # shared-only reference, every TU -fPIC, gets no `-fPIC missing`
        # error for the non-PIC archive compile, and no second copy of a
        # finding both twins share). The PIC twin wins a tie: it is the one
        # the shared library links.
        by_twin: Dict[tuple, List[tuple]] = {}
        for bv, discs in results:
            by_twin.setdefault(_sans_pic(bv), []).append((bv, discs))
        for twins in by_twin.values():
            twins.sort(key=lambda r: (_variant_distance(r[1]), not _pic(r[0])))
            out.extend(twins[0][1])
    return out


# Java source roots: a .java path is keyed by its PACKAGE-rooted form (the path
# below the source root), which is invariant across build systems that disagree
# on the repo prefix / source-root layout (Bazel 'guava/src/...' vs Maven
# 'src/...' vs 'src/main/java/...'). We strip the longest matching root marker.
_JAVA_SRC_ROOTS = ("src/main/java/", "src/test/java/", "javatests/", "java/", "src/")


def _java_src_key(path: str) -> str:
    """Package-rooted key for a .java source, robust to differing repo/source-root
    prefixes. 'guava/src/com/x/A.java' and 'src/com/x/A.java' both -> 'com/x/A.java'.
    Falls back to the basename if no known root marker is present."""
    for root in _JAVA_SRC_ROOTS:
        i = path.find(root)
        if i != -1:
            return path[i + len(root):]
    return path


def _union_java_sources(views: Dict[str, TargetView], names) -> Dict[str, str]:
    """Pool every Java source across all compile groups of the given targets into
    one map: package-rooted key -> original path. Mirrors _union_tus: grouping
    (Maven's 2 groups vs Bazel's 1) is irrelevant; the SOURCE SET is what must
    match. First occurrence wins for the displayed original path."""
    out: Dict[str, str] = {}
    for n in names:
        for g in views[n].compile_groups:
            for src in g.sources:
                out.setdefault(_java_src_key(src), src)
    return out


def _all_source_keys(views: Dict[str, TargetView], cfg: "MigrationConfig") -> set:
    """Every source key compiled ANYWHERE in the participating scope, ignoring
    role. Used as the presence reference so a source that's a library TU on one
    side and a test/exe TU on the other isn't falsely reported missing.

    Scope mirrors what the diff actually compares: all participating-role
    targets, plus test targets when cfg.include_tests. Config-excluded targets
    are omitted (they're intentionally out of scope)."""
    keys = set()
    for name, t in views.items():
        if cfg.target_excluded(name):
            continue
        in_scope = (t.role in PARTICIPATING_ROLES
                    or (cfg.include_tests and t.role == TargetRole.TEST))
        if not in_scope:
            continue
        for tu in t.tus:
            keys.add(cfg.map_source(tu.key()))
    return keys


def diff_models(a: CanonicalModel, b: CanonicalModel,
                cfg: Optional["MigrationConfig"] = None) -> List[Discrepancy]:
    out: List[Discrepancy] = []
    if cfg is None:
        cfg = MigrationConfig()
    # Reconstruct raw actions into comparable views (TUs + link flags + deps),
    # build-system-specific interpretation keyed on each model's tag.
    a_views = reconstruct(a)
    b_views = _apply_target_map(reconstruct(b), cfg.target_map)
    a_names = _participating(a_views, cfg)
    b_names = _participating(b_views, cfg)

    # Split production targets into libraries (compared as a TU-set union, so
    # grouping/naming is irrelevant) and executables (the real link
    # deliverables, compared by identity).
    a_libs = {n for n in a_names if a_views[n].kind in _LIBRARY_KINDS}
    b_libs = {n for n in b_names if b_views[n].kind in _LIBRARY_KINDS}
    a_exes = a_names - a_libs
    b_exes = b_names - b_libs

    # PRESENCE REFERENCE: every source compiled ANYWHERE on each side, across all
    # participating roles (libraries, executables, and tests when opted in). A
    # source is only "missing" if compiled nowhere on the other side -- the same
    # .cc may be a library TU on one side and a test/exe TU on the other (e.g.
    # Bazel compiling a test helper into a `testonly` cc_library while CMake
    # builds it straight into the test exe). Role grouping must not manufacture
    # a false missing/extra. Flag comparison still happens per-union below.
    a_all = _all_source_keys(a_views, cfg)
    b_all = _all_source_keys(b_views, cfg)

    # ---- libraries: project-wide TU-set comparison -------------------------
    # Each side's union keeps every variant of a source (shared/static twins);
    # a Bazel TU passes if it matches ANY CMake variant (_diff_tu_variants).
    a_union = _union_tus(a_views, a_libs, cfg)
    b_union = _union_tus(b_views, b_libs, cfg)
    for src in sorted(set(a_union) - b_all):
        out.append(Discrepancy(Kind.MISSING_TU.value, Severity.ERROR.value,
                               "<libraries>", "source compiled in cmake but not bazel", tu=src))
    for src in sorted(set(b_union) - a_all):
        out.append(Discrepancy(Kind.EXTRA_TU.value, Severity.WARN.value,
                               "<libraries>", "source compiled in bazel but not cmake", tu=src))
    out.extend(_diff_tu_unions("<libraries>", a_union, b_union, cfg))

    # ---- executables: identity-aligned, own TUs + own flags ----------------
    for name in sorted(a_exes - b_exes):
        out.append(Discrepancy(Kind.MISSING_TARGET.value, Severity.ERROR.value,
                               name, "executable in cmake but not bazel"))
    for name in sorted(b_exes - a_exes):
        out.append(Discrepancy(Kind.EXTRA_TARGET.value, Severity.WARN.value,
                               name, "executable in bazel but not cmake"))
    for name in sorted(a_exes & b_exes):
        ta, tb = a_views[name], b_views[name]
        # TU keys go through cfg.map_source (see _union_tus) so codegen grouping
        # asymmetries collapse to a shared token on both sides.
        amap = {cfg.map_source(k): v for k, v in ta.tu_map().items()}
        bmap = {cfg.map_source(k): v for k, v in tb.tu_map().items()}
        # Presence is judged against the whole other side (a_all/b_all), not just
        # this exe's own TUs: a source this exe compiles may live in a library on
        # the other side (or vice versa) -- that's a grouping difference, not a
        # missing source. Only a source compiled NOWHERE on the other side is a
        # real gap.
        for src in sorted(set(amap) - b_all):
            out.append(Discrepancy(Kind.MISSING_TU.value, Severity.ERROR.value,
                                   name, "source compiled in cmake but not bazel", tu=src))
        for src in sorted(set(bmap) - a_all):
            out.append(Discrepancy(Kind.EXTRA_TU.value, Severity.WARN.value,
                                   name, "source compiled in bazel but not cmake", tu=src))
        for src in sorted(set(amap) & set(bmap)):
            out.extend(_diff_tu(name, amap[src], bmap[src], cfg))
        # link flags: asymmetric subset, same policy as compile flags -- every
        # CMake link flag must be present on the Bazel side; extra Bazel link
        # flags tolerated (toolchain link noise already stripped). Reviewer
        # ignores applied to both sides.
        out.extend(_diff_link_flags(name, ta, tb, cfg))

    # ---- Java: project-wide source-SET comparison --------------------------
    # Java compiles a whole source set per action; how those sources are grouped
    # into compile actions differs across builds (Maven 2 groups -- main +
    # multi-release -- vs Bazel 1). So, exactly like C++ libraries, we pool every
    # Java source across all groups into one set keyed by package-rooted path
    # (robust to repo/source-root prefix differences) and compare the sets.
    # NOTE: javac FLAG comparison is deferred (the Bazel side is a JavaBuilder
    # wrapper argv whose real javac flags are nested -- needs canonicalization
    # designed against this real data). Source-set parity is what's checked now.
    a_java = _union_java_sources(a_views, a_names)
    b_java = _union_java_sources(b_views, b_names)
    for key in sorted(set(a_java) - set(b_java)):
        out.append(Discrepancy(Kind.MISSING_JAVA_SRC.value, Severity.ERROR.value,
                               "<java>", "java source compiled in A but not B",
                               tu=a_java[key]))
    for key in sorted(set(b_java) - set(a_java)):
        out.append(Discrepancy(Kind.EXTRA_JAVA_SRC.value, Severity.WARN.value,
                               "<java>", "java source compiled in B but not A",
                               tu=b_java[key]))

    # ---- link closure: EXTERNAL deps only ----------------------------------
    # Internal (in-project) dep names are meaningless once libraries are
    # dissolved into a TU union, but external/system libs (-lpthread, -lz, ...)
    # must still be present for the binary to link. Compare the project-wide
    # set of external deps; require every CMake external dep on the Bazel side.
    # A dep spelled differently per build (CMake's archive basename 'Catch2Main'
    # vs Bazel's 'catch2_main', or 'OpenSSL::SSL' vs 'ssl') is aligned by an
    # explicit, recorded cfg.dep_map entry -- not a fuzzy match -- so a residual
    # missing_dep is a genuinely-absent dep, not a naming artifact.
    # An "external" dep naming a target that EXISTS as an in-project target
    # (either side) is not truly external -- it's an internal lib the CMake
    # File-API surfaced as a link fragment (common with cyclically-linked
    # static libs, whose .a is repeated on the link line but which are not a
    # direct structural dependency). Such names are verified via the compile
    # TU-set of that target, so drop them from the external closure.
    _internal_names = set(a_views) | set(b_views)
    a_ext = {cfg.dep_map.get(d, d) for d in _external_deps(a_views, a_names)
             if cfg.dep_map.get(d, d) not in _internal_names and d not in _internal_names}
    b_ext = {d for d in _external_deps(b_views, b_names) if d not in _internal_names}
    for dep in sorted(a_ext - b_ext):
        out.append(Discrepancy(Kind.MISSING_DEP.value, Severity.ERROR.value,
                               "<external>", "external link dependency missing on bazel side",
                               cmake_only=[dep]))

    # ---- tests (opt-in) ----------------------------------------------------
    if cfg.include_tests:
        out.extend(_diff_tests(a_views, b_views, cfg, a_all, b_all))
    return out


def _diff_tests(a_views: Dict[str, TargetView], b_views: Dict[str, TargetView],
                cfg: "MigrationConfig", a_all: set, b_all: set) -> List[Discrepancy]:
    """Apply the COMPILE-PARITY stage to TEST targets. Two checks:

      1. TU-SET union of all test sources (like libraries): same test .cc files
         compiled with equivalent flags, regardless of how they're grouped into
         test binaries or named. Presence is judged against ALL sources on the
         other side (a_all/b_all), not just its test union: a test source on one
         side may be compiled into a (testonly) library on the other -- a
         grouping difference, not a missing source. Only "compiled nowhere" is
         flagged.
      2. Test-binary EXISTENCE by count: a coarse check that both sides produce
         a comparable number of test executables, catching the case where test
         sources compile but are never linked into runnable tests.

    SCOPE: tests get the COMPILE-PARITY stage only. LINK CONSISTENCY (per-binary
    link flags and link-dep closure) runs for PRODUCTION executables/shared
    libs, not for test binaries: tests align by source-set union, not by binary
    identity, so there's no per-binary link command to compare. So with tests
    on, "converged" means production artifacts are link-consistent AND all test
    SOURCES compile equivalently -- not that each test binary links identically.
    Extending link consistency to per-test-binary is a natural future increment.
    """
    out: List[Discrepancy] = []
    a_tests = {n for n, t in a_views.items()
               if t.role == TargetRole.TEST and not cfg.target_excluded(n)}
    b_tests = {n for n, t in b_views.items()
               if t.role == TargetRole.TEST and not cfg.target_excluded(n)}

    # 1. test-source TU-set union
    a_union = _union_tus(a_views, a_tests, cfg)
    b_union = _union_tus(b_views, b_tests, cfg)
    for src in sorted(set(a_union) - b_all):
        out.append(Discrepancy(Kind.MISSING_TEST_TU.value, Severity.ERROR.value,
                               "<tests>", "test source compiled in cmake but not bazel", tu=src))
    for src in sorted(set(b_union) - a_all):
        out.append(Discrepancy(Kind.EXTRA_TEST_TU.value, Severity.WARN.value,
                               "<tests>", "test source compiled in bazel but not cmake", tu=src))
    out.extend(_diff_tu_unions("<tests>", a_union, b_union, cfg))

    # 2. test-binary existence by count
    if len(a_tests) != len(b_tests):
        out.append(Discrepancy(
            Kind.TEST_BINARY_COUNT.value, Severity.WARN.value, "<tests>",
            f"test executable count differs: cmake={len(a_tests)} bazel={len(b_tests)}",
            cmake_only=sorted(a_tests), bazel_only=sorted(b_tests)))
    return out


def _external_deps(views: Dict[str, TargetView], names) -> set:
    return {d.name for n in names for d in views[n].deps if d.external}


def excluded_summary(a: CanonicalModel, b: CanonicalModel,
                     cfg: "MigrationConfig") -> dict:
    """Targets NOT compared, grouped by reason and side, for separate inspection.

    This is the visible record of what the parity diff skipped and why -- so a
    skipped dashboard/codegen/test target, or a config-excluded third-party
    subtree, is an explicit, reviewable line item rather than a silent omission.
    """
    # tests participate when opted in, so they are no longer "excluded" then
    participating = set(PARTICIPATING_ROLES)
    if cfg.include_tests:
        participating.add(TargetRole.TEST)

    def by_reason(model: CanonicalModel) -> dict:
        out: Dict[str, List[str]] = {}
        for name, t in sorted(reconstruct(model).items()):
            if cfg.target_excluded(name):
                out.setdefault("config_excluded", []).append(name)
            elif t.role not in participating:
                out.setdefault(t.role.value, []).append(name)
        return out
    return {"cmake": by_reason(a), "bazel": by_reason(b)}


def summarize(discs: List[Discrepancy],
              a: Optional[CanonicalModel] = None,
              b: Optional[CanonicalModel] = None,
              cfg: Optional["MigrationConfig"] = None) -> dict:
    errors = [d for d in discs if d.severity == Severity.ERROR.value]
    result = {
        "total": len(discs),
        "errors": len(errors),
        "warnings": len(discs) - len(errors),
        "converged": len(errors) == 0,
        "discrepancies": [asdict(d) for d in discs],
    }
    if a is not None and b is not None:
        result["excluded"] = excluded_summary(a, b, cfg or MigrationConfig())
    return result


if __name__ == "__main__":
    import sys
    # Used as a CLI by the skill loop:
    #   diff.py <cmake.json> <bazel.json> [any2bazel.json]
    # The 3rd arg is the migration config (target_map + ignore lists). If
    # omitted, the diff runs with no human-approved suppressions.
    import config as config_mod
    from serialize import load_model
    a = load_model(sys.argv[1])
    b = load_model(sys.argv[2])
    cfg = config_mod.load(sys.argv[3]) if len(sys.argv) > 3 else MigrationConfig()
    print(json.dumps(summarize(diff_models(a, b, cfg), a, b, cfg), indent=2))
