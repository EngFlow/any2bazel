"""Estimate CMake-to-Bazel migration effort from extracted migration artifacts.

This is deliberately a transparent engineering-effort heuristic, not a quote.
It consumes the CMake reference model and, once available, the parity diff. The
model measures migration surface; the diff measures the remaining work.
LLM/API spend is reported only when priced token usage is supplied separately.

Usage:
    python3 scripts/estimate_cost.py model.cmake.json
    python3 scripts/estimate_cost.py model.cmake.json --diff diff.json \
        --hourly-rate 180 > migration-estimate.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from typing import Optional

from model import TargetKind, TargetRole
from serialize import load_model


# Hours added for each remaining discrepancy. These are intentionally visible:
# calibrate them with completed migrations rather than treating them as truth.
DIFF_HOURS = {
    "missing_target": 1.5,
    "kind_mismatch": 1.0,
    "missing_tu": 0.20,
    "missing_java_src": 0.20,
    "defines_diff": 0.20,
    "includes_diff": 0.25,
    "flags_diff": 0.20,
    "link_flags_diff": 0.35,
    "missing_dep": 0.75,
    "missing_test_tu": 0.15,
    "test_binary_count": 0.25,
}


def _round_hour(value: float) -> float:
    return round(value * 2) / 2


def model_metrics(model) -> dict:
    targets = list(model.targets.values())
    roles = Counter(t.role.value for t in targets)
    kinds = Counter(t.kind.value for t in targets)
    compile_actions = sum(
        1 for target in targets for action in target.actions
        if "Compile" in action.mnemonic
    )
    external_deps = {
        dep.name for target in targets for dep in target.deps if dep.external
    }
    return {
        "targets": len(targets),
        "roles": dict(sorted(roles.items())),
        "kinds": dict(sorted(kinds.items())),
        "compile_actions": compile_actions,
        "external_dependencies": len(external_deps),
        "configured_files": len(model.configured_files),
        "production_targets": roles[TargetRole.PRODUCTION.value],
        "test_targets": roles[TargetRole.TEST.value],
        "codegen_targets": roles[TargetRole.CODEGEN.value],
        "unknown_targets": roles[TargetRole.UNKNOWN.value],
        "executables": kinds[TargetKind.EXECUTABLE.value],
    }


def estimate(model, diff: Optional[dict] = None, hourly_rate: Optional[float] = None,
             llm_input_tokens: Optional[int] = None,
             llm_output_tokens: Optional[int] = None,
             llm_input_per_million: Optional[float] = None,
             llm_output_per_million: Optional[float] = None) -> dict:
    metrics = model_metrics(model)
    # Initial porting effort, before Bazel extraction reveals concrete gaps.
    likely = (
        4.0
        + metrics["production_targets"] * 0.75
        + metrics["compile_actions"] * 0.12
        + metrics["executables"] * 0.75
        + metrics["external_dependencies"] * 1.0
        + metrics["test_targets"] * 0.15
        + metrics["configured_files"] * 1.5
        + metrics["codegen_targets"] * 8.0
        + metrics["unknown_targets"] * 2.0
    )
    discrepancy_counts = Counter()
    errors = warnings = 0
    if diff is not None:
        discrepancy_counts = Counter(d.get("kind", "unknown")
                                     for d in diff.get("discrepancies", []))
        errors = int(diff.get("errors", 0))
        warnings = int(diff.get("warnings", 0))
        likely += sum(DIFF_HOURS.get(kind, 0.25) * count
                      for kind, count in discrepancy_counts.items())

    risk_flags = []
    if metrics["codegen_targets"]:
        risk_flags.append("build-time code generation is outside the current MVP")
    if metrics["configured_files"]:
        risk_flags.append("configure-time generated files need a separate parity review")
    if metrics["unknown_targets"]:
        risk_flags.append("some target roles are unclassified and need scope review")
    if diff is None:
        risk_flags.append("no Bazel diff supplied; estimate covers discovery and initial port only")

    risk_multiplier = 1.0 + (0.10 if risk_flags else 0.0)
    low = max(2.0, likely * 0.70)
    high = likely * (1.60 * risk_multiplier)
    likely = _round_hour(likely)
    low, high = _round_hour(low), _round_hour(high)
    rounds = max(1, math.ceil(1 + errors / max(8, metrics["compile_actions"] ** 0.5 * 3)))
    if metrics["codegen_targets"] or metrics["configured_files"]:
        rounds += 1

    result = {
        "schema_version": 1,
        "estimator": "cmake2bazel-engineering-effort-heuristic-v1",
        "input": metrics,
        "remaining_diff": {
            "available": diff is not None,
            "errors": errors,
            "warnings": warnings,
            "by_kind": dict(sorted(discrepancy_counts.items())),
        },
        "estimate": {
            "engineering_hours": {"low": low, "likely": likely, "high": high},
            "migration_rounds": {"low": max(1, rounds - 1), "likely": rounds,
                                 "high": rounds + 1 + len(risk_flags)},
            "risk_flags": risk_flags,
        },
        "assumptions": [
            "one engineer already familiar with Bazel reviews the migration",
            "external dependencies can be resolved without writing new rules",
            "generated code, packaging, and install rules are scoped separately",
            "LLM/API spend is excluded until model-specific usage and pricing are recorded",
        ],
    }
    if hourly_rate is not None:
        result["estimate"]["engineering_cost"] = {
            "currency": "USD",
            "hourly_rate": hourly_rate,
            "low": _round_hour(low * hourly_rate),
            "likely": _round_hour(likely * hourly_rate),
            "high": _round_hour(high * hourly_rate),
        }
    llm_values = (llm_input_tokens, llm_output_tokens, llm_input_per_million,
                  llm_output_per_million)
    if any(value is not None for value in llm_values):
        if any(value is None for value in llm_values):
            result["estimate"]["llm_api_cost"] = {
                "available": False,
                "reason": "provide input/output token totals and both per-million prices",
            }
        else:
            result["estimate"]["llm_api_cost"] = {
                "available": True,
                "currency": "USD",
                "input_tokens": llm_input_tokens,
                "output_tokens": llm_output_tokens,
                "input_per_million": llm_input_per_million,
                "output_per_million": llm_output_per_million,
                "total": round(
                    llm_input_tokens * llm_input_per_million / 1_000_000
                    + llm_output_tokens * llm_output_per_million / 1_000_000, 4),
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="CMake model JSON from extract_cmake.py")
    parser.add_argument("--diff", help="optional diff.json from diff.py")
    parser.add_argument("--hourly-rate", type=float,
                        help="optional fully-loaded engineering hourly rate in USD")
    parser.add_argument("--llm-input-tokens", type=int,
                        help="observed total input tokens for this migration")
    parser.add_argument("--llm-output-tokens", type=int,
                        help="observed total output tokens for this migration")
    parser.add_argument("--llm-input-per-million", type=float,
                        help="pricing snapshot: USD per million input tokens")
    parser.add_argument("--llm-output-per-million", type=float,
                        help="pricing snapshot: USD per million output tokens")
    args = parser.parse_args()
    diff = None
    if args.diff:
        with open(args.diff) as f:
            diff = json.load(f)
    print(json.dumps(estimate(
        load_model(args.model), diff, args.hourly_rate, args.llm_input_tokens,
        args.llm_output_tokens, args.llm_input_per_million,
        args.llm_output_per_million), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
