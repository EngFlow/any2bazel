"""Tests for the deterministic diff engine.

These prove the two behaviors the whole approach hinges on:
  1. Bazel's injected toolchain-default flags do NOT create false discrepancies
     (otherwise the migrate loop never converges).
  2. A genuinely missing CMake correctness flag IS caught (asymmetric subset).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from canonicalize import canonicalize_flags
from diff import Severity, diff_models, summarize
from model import (CanonicalModel, Dependency, Target, TargetKind,
                   TargetRole, TranslationUnit)

REPO = "/work/proj"


def tu_from_raw(source, raw, is_bazel):
    d, i, f = canonicalize_flags(raw, REPO, is_bazel=is_bazel)
    return TranslationUnit(source=source, defines=d, includes=i, flags=f)


def _model(tu, deps=()):
    m = CanonicalModel()
    m.add(Target("lib", TargetKind.STATIC, tus=[tu], role=TargetRole.PRODUCTION,
                 deps=[Dependency(n) for n in deps]))
    return m


def test_toolchain_defaults_do_not_break_convergence():
    cmake_raw = ["-I", "/work/proj/include", "-DFOO=1", "-std=c++17", "-Wall"]
    # Bazel side: same meaning, but with injected defaults + absolute->same path
    bazel_raw = ["-iquote", "/work/proj", "-I", "/work/proj/include",
                 "-DFOO=1", "-std=c++17", "-Wall",
                 "-fno-canonical-system-headers", "-frandom-seed=abc",
                 "-g0", "-O2"]
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True))
    res = summarize(diff_models(a, b))
    assert res["converged"], res
    assert res["errors"] == 0, res


def test_missing_cmake_flag_is_caught():
    cmake_raw = ["-DFOO=1", "-DNEEDED=1", "-std=c++17", "-fno-exceptions"]
    bazel_raw = ["-DFOO=1", "-std=c++17"]  # dropped NEEDED and -fno-exceptions
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True))
    discs = diff_models(a, b)
    res = summarize(discs)
    assert not res["converged"], res
    kinds = {d.kind for d in discs}
    assert "defines_diff" in kinds
    assert "flags_diff" in kinds
    # the missing define must be reported on the cmake_only side
    dd = next(d for d in discs if d.kind == "defines_diff")
    assert "NEEDED=1" in dd.cmake_only


def test_extra_bazel_define_is_warn_not_error():
    cmake_raw = ["-DFOO=1"]
    bazel_raw = ["-DFOO=1", "-DEXTRA=1"]
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True))
    discs = diff_models(a, b)
    dd = next(d for d in discs if d.kind == "defines_diff")
    assert dd.severity == Severity.WARN.value
    assert summarize(discs)["converged"], "extra bazel define must not block parity"


def test_include_order_must_be_preserved():
    cmake_raw = ["-I", "/work/proj/a", "-I", "/work/proj/b"]
    bazel_raw = ["-I", "/work/proj/b", "-I", "/work/proj/a"]  # reordered!
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True))
    discs = diff_models(a, b)
    assert any(d.kind == "includes_diff" for d in discs), "reorder must be caught"


def test_missing_link_dep_is_error():
    a = _model(tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False), deps=["zlib"])
    b = _model(tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True), deps=[])
    discs = diff_models(a, b)
    assert any(d.kind == "missing_dep" and d.severity == "error" for d in discs)


def test_nonparticipating_roles_are_excluded_not_diffed():
    # A dashboard target present only in cmake must NOT create a missing_target
    # discrepancy; it must appear in the excluded summary instead.
    a = CanonicalModel()
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False)]))
    a.add(Target("Nightly", TargetKind.UNKNOWN, role=TargetRole.DASHBOARD))
    b = CanonicalModel()
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True)]))
    discs = diff_models(a, b)
    res = summarize(discs, a, b)
    assert res["converged"], res
    assert not any(d.kind == "missing_target" for d in discs)
    assert "Nightly" in res["excluded"]["cmake"]["dashboard"]


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
