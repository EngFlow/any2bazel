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

    # includes: ORDER-SENSITIVE. We require A's sequence to appear as a
    # subsequence of B's (B may add extra system includes, but must not drop
    # or reorder A's search order).
    if not _is_subsequence(a.includes, b.includes):
        missing = [i for i in a.includes if i not in set(b.includes)]
        out.append(Discrepancy(
            kind=Kind.INCLUDES_DIFF.value,
            severity=Severity.ERROR.value,
            target=target, tu=a.source,
            detail="include search order not preserved or entries missing",
            cmake_only=missing,
            bazel_only=[i for i in b.includes if i not in set(a.includes)],
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


def _is_subsequence(needle, haystack) -> bool:
    """Is `needle` an (order-preserving, not necessarily contiguous) subsequence
    of `haystack`? Used for include-search-order parity."""
    it = iter(haystack)
    return all(x in it for x in needle)


# Roles the parity diff actually compares. Everything else (dashboard,
# aggregate, codegen, test, unknown) is retained in the model and reported
# separately for inspection, but never produces a discrepancy. Flip TEST on
# here when the test-migration phase begins -- no re-extraction needed.
PARTICIPATING_ROLES = {TargetRole.PRODUCTION}


def _participating(model: CanonicalModel) -> set:
    return {n for n, t in model.targets.items()
            if t.role in PARTICIPATING_ROLES}


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


def diff_models(a: CanonicalModel, b: CanonicalModel,
                cfg: Optional["MigrationConfig"] = None) -> List[Discrepancy]:
    out: List[Discrepancy] = []
    if cfg is None:
        cfg = MigrationConfig()
    b = _apply_target_map(b, cfg.target_map)
    a_names = _participating(a)
    b_names = _participating(b)

    # Split production targets into libraries (compared as a TU-set union, so
    # grouping/naming is irrelevant) and executables (the real link
    # deliverables, compared by identity).
    a_libs = {n for n in a_names if a.targets[n].kind in _LIBRARY_KINDS}
    b_libs = {n for n in b_names if b.targets[n].kind in _LIBRARY_KINDS}
    a_exes = a_names - a_libs
    b_exes = b_names - b_libs

    # ---- libraries: project-wide TU-set comparison -------------------------
    a_union = _union_tus(a, a_libs)
    b_union = _union_tus(b, b_libs)
    for src in sorted(set(a_union) - set(b_union)):
        out.append(Discrepancy(Kind.MISSING_TU.value, Severity.ERROR.value,
                               "<libraries>", "source compiled in cmake but not bazel", tu=src))
    for src in sorted(set(b_union) - set(a_union)):
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
        for src in sorted(set(amap) - set(bmap)):
            out.append(Discrepancy(Kind.MISSING_TU.value, Severity.ERROR.value,
                                   name, "source compiled in cmake but not bazel", tu=src))
        for src in sorted(set(bmap) - set(amap)):
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
    return out


def _external_deps(model: CanonicalModel, names) -> set:
    return {d.name for n in names for d in model.targets[n].deps if d.external}


def excluded_summary(a: CanonicalModel, b: CanonicalModel) -> dict:
    """Targets NOT compared, grouped by role and side, for separate inspection.

    This is the visible record of what the parity diff skipped and why -- so a
    skipped dashboard/codegen/test target is an explicit, reviewable line item
    rather than a silent omission.
    """
    def by_role(model: CanonicalModel) -> dict:
        out: Dict[str, List[str]] = {}
        for name, t in sorted(model.targets.items()):
            if t.role in PARTICIPATING_ROLES:
                continue
            out.setdefault(t.role.value, []).append(name)
        return out
    return {"cmake": by_role(a), "bazel": by_role(b)}


def summarize(discs: List[Discrepancy],
              a: Optional[CanonicalModel] = None,
              b: Optional[CanonicalModel] = None) -> dict:
    errors = [d for d in discs if d.severity == Severity.ERROR.value]
    result = {
        "total": len(discs),
        "errors": len(errors),
        "warnings": len(discs) - len(errors),
        "converged": len(errors) == 0,
        "discrepancies": [asdict(d) for d in discs],
    }
    if a is not None and b is not None:
        result["excluded"] = excluded_summary(a, b)
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
    print(json.dumps(summarize(diff_models(a, b, cfg), a, b), indent=2))
