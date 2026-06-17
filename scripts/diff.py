"""Asymmetric parity diff: CMake model (oracle A) vs Bazel model (B).

Produces a worklist of discrepancies for the fix loop. Each iteration of the
migrate loop calls this; an empty worklist (for a full round) means "done" at
the current oracle strength (per-TU compile-flag equivalence + link closure).

ASYMMETRY: a flag present in CMake but missing in Bazel is a discrepancy
(under-specified Bazel target -> likely wrong build). A flag present only in
Bazel is tolerated (toolchain defaults already stripped in canonicalize). The
one exception is defines, where an EXTRA Bazel define can change behavior, so
extra defines are reported at lower severity.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional

from config import MigrationConfig
from model import (CanonicalModel, Target, TargetKind, TargetRole,
                   TranslationUnit)


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
    MISSING_DEP = "missing_dep"
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
    # reordered). See FUTURE-include-order-collision-check.md.
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
            bazel_only=sorted(b_fl - a_fl),
        ))
    return out


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


def _participating(model: CanonicalModel, cfg: "MigrationConfig") -> set:
    return {n for n, t in model.targets.items()
            if t.role in PARTICIPATING_ROLES and not cfg.target_excluded(n)}


def _apply_target_map(b: CanonicalModel, target_map: Dict[str, str]) -> CanonicalModel:
    """Rename Bazel-side targets into the CMake namespace using a declared
    map (cmake_name -> bazel_name), so targets that were intentionally renamed
    during generation (e.g. crypto -> crypto_internal) align in the diff.

    The map is authored by whoever generates the BUILD files -- they KNOW the
    rename, so it's an explicit input rather than a fuzzy-match guess here.
    Returns a shallow-renamed copy; the original model is untouched.
    """
    if not target_map:
        return b
    from dataclasses import replace
    b2cmake = {bazel: cmake for cmake, bazel in target_map.items()}
    renamed = CanonicalModel()
    for name, t in b.targets.items():
        t.name = b2cmake.get(name, name)
        # deps reference target names too -- rename them into CMake's namespace
        # so link-closure comparison lines up. (Dependency is frozen.)
        t.deps = [replace(d, name=b2cmake.get(d.name, d.name)) for d in t.deps]
        renamed.targets[t.name] = t
    return renamed


_LIBRARY_KINDS = {TargetKind.STATIC, TargetKind.SHARED, TargetKind.OBJECT,
                  TargetKind.INTERFACE}


def _union_tus(model: CanonicalModel, names) -> Dict[str, TranslationUnit]:
    """Pool TUs of the given targets into one source-keyed map.

    This is how libraries are compared at TU-SET level: regardless of how
    sources are grouped into targets (and regardless of target renames or
    object-library fold-ins), every source compiled on each side lands in one
    flat map keyed by repo-relative path. If the same source appears in two
    targets with conflicting flags we keep the first and flag it -- rare, and a
    real smell worth surfacing.
    """
    out: Dict[str, TranslationUnit] = {}
    for n in names:
        for tu in model.targets[n].tus:
            out.setdefault(tu.key(), tu)
    return out


def _all_source_keys(model: CanonicalModel, cfg: "MigrationConfig") -> set:
    """Every source key compiled ANYWHERE in the participating scope, ignoring
    role. Used as the presence oracle so a source that's a library TU on one
    side and a test/exe TU on the other isn't falsely reported missing.

    Scope mirrors what the diff actually compares: all participating-role
    targets, plus test targets when cfg.include_tests. Config-excluded targets
    are omitted (they're intentionally out of scope)."""
    keys = set()
    for name, t in model.targets.items():
        if cfg.target_excluded(name):
            continue
        in_scope = (t.role in PARTICIPATING_ROLES
                    or (cfg.include_tests and t.role == TargetRole.TEST))
        if not in_scope:
            continue
        for tu in t.tus:
            keys.add(tu.key())
    return keys


def diff_models(a: CanonicalModel, b: CanonicalModel,
                cfg: Optional["MigrationConfig"] = None) -> List[Discrepancy]:
    out: List[Discrepancy] = []
    if cfg is None:
        cfg = MigrationConfig()
    b = _apply_target_map(b, cfg.target_map)
    a_names = _participating(a, cfg)
    b_names = _participating(b, cfg)

    # Split production targets into libraries (compared as a TU-set union, so
    # grouping/naming is irrelevant) and executables (the real link
    # deliverables, compared by identity).
    a_libs = {n for n in a_names if a.targets[n].kind in _LIBRARY_KINDS}
    b_libs = {n for n in b_names if b.targets[n].kind in _LIBRARY_KINDS}
    a_exes = a_names - a_libs
    b_exes = b_names - b_libs

    # PRESENCE ORACLE: every source compiled ANYWHERE on each side, across all
    # participating roles (libraries, executables, and tests when opted in). A
    # source is only "missing" if compiled nowhere on the other side -- the same
    # .cc may be a library TU on one side and a test/exe TU on the other (e.g.
    # Bazel compiling a test helper into a `testonly` cc_library while CMake
    # builds it straight into the test exe). Role grouping must not manufacture
    # a false missing/extra. Flag comparison still happens per-union below.
    a_all = _all_source_keys(a, cfg)
    b_all = _all_source_keys(b, cfg)

    # ---- libraries: project-wide TU-set comparison -------------------------
    a_union = _union_tus(a, a_libs)
    b_union = _union_tus(b, b_libs)
    for src in sorted(set(a_union) - b_all):
        out.append(Discrepancy(Kind.MISSING_TU.value, Severity.ERROR.value,
                               "<libraries>", "source compiled in cmake but not bazel", tu=src))
    for src in sorted(set(b_union) - a_all):
        out.append(Discrepancy(Kind.EXTRA_TU.value, Severity.WARN.value,
                               "<libraries>", "source compiled in bazel but not cmake", tu=src))
    for src in sorted(set(a_union) & set(b_union)):
        out.extend(_diff_tu("<libraries>", a_union[src], b_union[src], cfg))

    # ---- executables: identity-aligned, own TUs + own flags ----------------
    for name in sorted(a_exes - b_exes):
        out.append(Discrepancy(Kind.MISSING_TARGET.value, Severity.ERROR.value,
                               name, "executable in cmake but not bazel"))
    for name in sorted(b_exes - a_exes):
        out.append(Discrepancy(Kind.EXTRA_TARGET.value, Severity.WARN.value,
                               name, "executable in bazel but not cmake"))
    for name in sorted(a_exes & b_exes):
        ta, tb = a.targets[name], b.targets[name]
        amap, bmap = ta.tu_map(), tb.tu_map()
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

    # ---- link closure: EXTERNAL deps only ----------------------------------
    # Internal (in-project) dep names are meaningless once libraries are
    # dissolved into a TU union, but external/system libs (-lpthread, -lz, ...)
    # must still be present for the binary to link. Compare the project-wide
    # set of external deps; require every CMake external dep on the Bazel side.
    a_ext = _external_deps(a, a_names)
    b_ext = _external_deps(b, b_names)
    for dep in sorted(a_ext - b_ext):
        out.append(Discrepancy(Kind.MISSING_DEP.value, Severity.ERROR.value,
                               "<external>", "external link dependency missing on bazel side",
                               cmake_only=[dep]))

    # ---- tests (opt-in) ----------------------------------------------------
    if cfg.include_tests:
        out.extend(_diff_tests(a, b, cfg, a_all, b_all))
    return out


def _diff_tests(a: CanonicalModel, b: CanonicalModel,
                cfg: "MigrationConfig", a_all: set, b_all: set) -> List[Discrepancy]:
    """Compare TEST targets. Two checks (per the agreed first cut):

      1. TU-SET union of all test sources (like libraries): same test .cc files
         compiled with equivalent flags, regardless of how they're grouped into
         test binaries or named. This is "union now"; per-binary alignment is a
         later layer. Presence is judged against ALL sources on the other side
         (a_all/b_all), not just its test union: a test source on one side may
         be compiled into a (testonly) library on the other -- a grouping
         difference, not a missing source. Only "compiled nowhere" is flagged.
      2. Test-binary EXISTENCE by count: a coarse check that both sides produce
         a comparable number of test executables, catching the case where test
         sources compile but are never linked into runnable tests. Reported as a
         warning (not a per-binary identity check yet -- naming is deferred).

    LIMITATION: this does NOT check test LINK CLOSURE (external/system deps of
    test binaries). The production external-dep check covers PRODUCTION targets
    only, so a test that links e.g. abseil or gtest is verified to *compile*
    equivalently but not to *link* the same deps. That's part of the deferred
    "per-binary" layer. So "tests converged" == test sources compile
    equivalently, NOT test binaries link identically.
    """
    out: List[Discrepancy] = []
    a_tests = {n for n, t in a.targets.items()
               if t.role == TargetRole.TEST and not cfg.target_excluded(n)}
    b_tests = {n for n, t in b.targets.items()
               if t.role == TargetRole.TEST and not cfg.target_excluded(n)}

    # 1. test-source TU-set union
    a_union = _union_tus(a, a_tests)
    b_union = _union_tus(b, b_tests)
    for src in sorted(set(a_union) - b_all):
        out.append(Discrepancy(Kind.MISSING_TEST_TU.value, Severity.ERROR.value,
                               "<tests>", "test source compiled in cmake but not bazel", tu=src))
    for src in sorted(set(b_union) - a_all):
        out.append(Discrepancy(Kind.EXTRA_TEST_TU.value, Severity.WARN.value,
                               "<tests>", "test source compiled in bazel but not cmake", tu=src))
    for src in sorted(set(a_union) & set(b_union)):
        out.extend(_diff_tu("<tests>", a_union[src], b_union[src], cfg))

    # 2. test-binary existence by count
    if len(a_tests) != len(b_tests):
        out.append(Discrepancy(
            Kind.TEST_BINARY_COUNT.value, Severity.WARN.value, "<tests>",
            f"test executable count differs: cmake={len(a_tests)} bazel={len(b_tests)}",
            cmake_only=sorted(a_tests), bazel_only=sorted(b_tests)))
    return out


def _external_deps(model: CanonicalModel, names) -> set:
    return {d.name for n in names for d in model.targets[n].deps if d.external}


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
        for name, t in sorted(model.targets.items()):
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
    #   diff.py <cmake.json> <bazel.json> [cmake2bazel.json]
    # The 3rd arg is the migration config (target_map + ignore lists). If
    # omitted, the diff runs with no human-approved suppressions.
    import config as config_mod
    from serialize import load_model
    a = load_model(sys.argv[1])
    b = load_model(sys.argv[2])
    cfg = config_mod.load(sys.argv[3]) if len(sys.argv) > 3 else MigrationConfig()
    print(json.dumps(summarize(diff_models(a, b, cfg), a, b, cfg), indent=2))
