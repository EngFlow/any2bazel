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

from model import CanonicalModel, Target, TranslationUnit


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


def _diff_tu(target: str, a: TranslationUnit, b: TranslationUnit) -> List[Discrepancy]:
    out: List[Discrepancy] = []

    # defines: order-insensitive set compare; both directions matter
    a_def, b_def = set(a.defines), set(b.defines)
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

    # other flags: asymmetric subset -- A must be subset of B
    a_fl, b_fl = set(a.flags), set(b.flags)
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


def diff_models(a: CanonicalModel, b: CanonicalModel) -> List[Discrepancy]:
    out: List[Discrepancy] = []
    a_names, b_names = set(a.targets), set(b.targets)

    for name in sorted(a_names - b_names):
        out.append(Discrepancy(Kind.MISSING_TARGET.value, Severity.ERROR.value,
                               name, "target in cmake but not bazel"))
    for name in sorted(b_names - a_names):
        out.append(Discrepancy(Kind.EXTRA_TARGET.value, Severity.WARN.value,
                               name, "target in bazel but not cmake"))

    for name in sorted(a_names & b_names):
        ta, tb = a.targets[name], b.targets[name]
        if ta.kind != tb.kind:
            out.append(Discrepancy(Kind.KIND_MISMATCH.value, Severity.ERROR.value,
                                   name, f"{ta.kind.value} vs {tb.kind.value}"))

        amap, bmap = ta.tu_map(), tb.tu_map()
        for src in sorted(set(amap) - set(bmap)):
            out.append(Discrepancy(Kind.MISSING_TU.value, Severity.ERROR.value,
                                   name, "source compiled in cmake but not bazel", tu=src))
        for src in sorted(set(bmap) - set(amap)):
            out.append(Discrepancy(Kind.EXTRA_TU.value, Severity.WARN.value,
                                   name, "source compiled in bazel but not cmake", tu=src))
        for src in sorted(set(amap) & set(bmap)):
            out.extend(_diff_tu(name, amap[src], bmap[src]))

        # link closure: every CMake dep identity must be present in Bazel
        a_deps = {d.name for d in ta.deps}
        b_deps = {d.name for d in tb.deps}
        for dep in sorted(a_deps - b_deps):
            out.append(Discrepancy(Kind.MISSING_DEP.value, Severity.ERROR.value,
                                   name, "link dependency missing on bazel side",
                                   cmake_only=[dep]))
    return out


def summarize(discs: List[Discrepancy]) -> dict:
    errors = [d for d in discs if d.severity == Severity.ERROR.value]
    return {
        "total": len(discs),
        "errors": len(errors),
        "warnings": len(discs) - len(errors),
        "converged": len(errors) == 0,
        "discrepancies": [asdict(d) for d in discs],
    }


if __name__ == "__main__":
    import sys
    # Used as a CLI by the skill loop: reads two model JSON files, prints diff.
    from serialize import load_model
    a = load_model(sys.argv[1])
    b = load_model(sys.argv[2])
    print(json.dumps(summarize(diff_models(a, b)), indent=2))
