"""Triage a diff.json into a human-readable worklist.

A raw diff.json can have hundreds of per-TU discrepancies, but they almost
always collapse to a handful of SYSTEMATIC causes (one flag missing across every
TU, one vendored include root, a renamed executable). This deterministic
summary does the grouping the operator would otherwise do by hand each round:

  * per (kind, severity): a count, and the value->frequency histogram of the
    cmake_only / bazel_only entries that drive it
  * cmake_only is the actionable side (what to ADD on Bazel or accept in config)
  * the `excluded` map echoed back so skipped roles/targets stay visible

Reading the histogram: a value appearing on (nearly) every TU is systematic --
fix once (a copt, an include, a target_map / ignore entry) and it clears in
bulk. A value on one TU is local. This is decision input, not a fixer: it does
not edit BUILD files or the config.

Usage:
    python3 scripts/diff.py cmake.json bazel.json cfg.json > diff.json
    python3 scripts/triage.py diff.json
    python3 scripts/triage.py diff.json --kind flags_diff   # drill into one kind
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Dict, List, Optional


def _histogram(discs: List[dict], kind: str, side: str) -> Counter:
    c: Counter = Counter()
    for d in discs:
        if d.get("kind") == kind:
            for v in (d.get(side) or []):
                c[v] += 1
    return c


def triage(diff: dict, only_kind: Optional[str] = None) -> dict:
    discs = diff.get("discrepancies", [])
    by_kind: Counter = Counter()
    by_kind_sev: Dict[str, str] = {}
    for d in discs:
        by_kind[d["kind"]] += 1
        by_kind_sev[d["kind"]] = d.get("severity", "")

    kinds = [only_kind] if only_kind else sorted(by_kind, key=lambda k: -by_kind[k])
    out_kinds = []
    for k in kinds:
        if k not in by_kind:
            continue
        cmake_only = _histogram(discs, k, "cmake_only")
        bazel_only = _histogram(discs, k, "bazel_only")
        out_kinds.append({
            "kind": k,
            "severity": by_kind_sev[k],
            "count": by_kind[k],
            # most_common(): systematic causes float to the top
            "cmake_only": cmake_only.most_common(),
            "bazel_only": bazel_only.most_common(),
        })

    return {
        "converged": diff.get("converged"),
        "errors": diff.get("errors"),
        "warnings": diff.get("warnings"),
        "kinds": out_kinds,
        "excluded": diff.get("excluded", {}),
    }


def render(t: dict, top: int = 25) -> str:
    """Compact text rendering for terminal reading.

    cmake_only is the actionable side, shown in full. bazel_only is usually
    tolerated, so its long tail is capped at `top` with an explicit "+N more"
    line (never silently truncated). Use --json for the complete histogram.
    """
    lines = []
    status = "CONVERGED" if t["converged"] else "NOT converged"
    lines.append(f"{status}  errors={t['errors']}  warnings={t['warnings']}")
    for k in t["kinds"]:
        lines.append(f"\n[{k['severity']}] {k['kind']} x{k['count']}")
        for label, hist, cap in (
                ("cmake_only (add on bazel / accept in config)", k["cmake_only"], None),
                ("bazel_only (usually tolerated)", k["bazel_only"], top)):
            if not hist:
                continue
            lines.append(f"  {label}:")
            shown = hist if cap is None else hist[:cap]
            for val, n in shown:
                lines.append(f"    {n:5d}  {val}")
            if cap is not None and len(hist) > cap:
                lines.append(f"    ... +{len(hist) - cap} more distinct values "
                             f"(use --json for the full list)")
    if t["excluded"]:
        lines.append("\nexcluded (not diffed):")
        for side, groups in t["excluded"].items():
            if groups:
                summary = ", ".join(f"{role}={len(names)}" for role, names in groups.items())
                lines.append(f"  {side}: {summary}")
    return "\n".join(lines)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only_kind = None
    if "--kind" in sys.argv:
        only_kind = sys.argv[sys.argv.index("--kind") + 1]
    as_json = "--json" in sys.argv
    if not args:
        sys.exit("usage: triage.py <diff.json> [--kind <kind>] [--json]")
    with open(args[0]) as f:
        diff = json.load(f)
    result = triage(diff, only_kind)
    print(json.dumps(result, indent=2) if as_json else render(result))
