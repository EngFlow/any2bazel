"""Tests for the transparent migration-effort estimator."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from estimate_cost import estimate, model_metrics
from model import (Action, BuildSystem, CanonicalModel, ConfiguredFile,
                   Dependency, Target, TargetKind, TargetRole)


def _model():
    model = CanonicalModel(build_system=BuildSystem.CMAKE, repo_root="/work/repo")
    model.add(Target(
        "app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
        actions=[Action("CppCompile"), Action("CppLink")],
        deps=[Dependency("fmt", external=True)],
    ))
    model.add(Target("generator", TargetKind.UNKNOWN, role=TargetRole.CODEGEN))
    model.add_configured_file(ConfiguredFile("config.h", "/tmp/config.h"))
    return model


def test_metrics_count_migration_surface():
    metrics = model_metrics(_model())
    assert metrics["production_targets"] == 1
    assert metrics["compile_actions"] == 1
    assert metrics["external_dependencies"] == 1
    assert metrics["codegen_targets"] == 1


def test_diff_increases_estimate_and_identifies_risk():
    initial = estimate(_model())
    with_diff = estimate(_model(), {
        "errors": 2,
        "warnings": 0,
        "discrepancies": [
            {"kind": "missing_dep"},
            {"kind": "flags_diff"},
        ],
    })
    assert with_diff["estimate"]["engineering_hours"]["likely"] > \
        initial["estimate"]["engineering_hours"]["likely"]
    assert "build-time code generation is outside the current MVP" in \
        with_diff["estimate"]["risk_flags"]


def test_optional_hourly_rate_produces_engineering_cost_only():
    result = estimate(_model(), hourly_rate=200)
    cost = result["estimate"]["engineering_cost"]
    assert cost["currency"] == "USD"
    assert cost["likely"] == result["estimate"]["engineering_hours"]["likely"] * 200
    assert "LLM/API spend is excluded" in result["assumptions"][-1]


def test_priced_observed_tokens_produce_separate_llm_cost():
    result = estimate(_model(), llm_input_tokens=2_000_000,
                      llm_output_tokens=500_000, llm_input_per_million=3,
                      llm_output_per_million=15)
    cost = result["estimate"]["llm_api_cost"]
    assert cost["available"]
    assert cost["total"] == 13.5


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
