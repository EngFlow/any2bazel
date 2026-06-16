"""Tests for the deterministic diff engine.

Cornerstone behaviors the whole approach hinges on:
  1. Bazel's injected toolchain-default flags do NOT create false discrepancies
     (otherwise the migrate loop never converges).
  2. A genuinely missing CMake correctness flag IS caught (asymmetric subset).
Plus coverage of: TU-set library comparison (renames/fold-ins), role filtering,
and every migration-config lever (ignore flags/defines/includes, exclude_targets,
JSON round-trip).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from canonicalize import canonicalize_flags
from config import MigrationConfig
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


def test_include_presence_not_order():
    # Includes are compared by PRESENCE, not order (order enforcement is
    # deferred pending an on-disk header-collision verifier -- see
    # FUTURE-include-order-collision-check.md). A pure reorder, all roots
    # present, must NOT be flagged.
    cmake_raw = ["-I", "/work/proj/a", "-I", "/work/proj/b"]
    bazel_raw = ["-I", "/work/proj/b", "-I", "/work/proj/a"]  # reordered only
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True))
    assert summarize(diff_models(a, b))["converged"], "pure reorder must not fail"


def test_missing_include_root_is_caught():
    # But a CMake include root genuinely ABSENT on the bazel side is an error.
    a = _model(tu_from_raw("src/a.cpp", ["-I", "/work/proj/a", "-I", "/work/proj/b"],
                           is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", ["-I", "/work/proj/a"], is_bazel=True))
    discs = diff_models(a, b)
    assert any(d.kind == "includes_diff" for d in discs), "missing root must be caught"


def test_missing_external_link_dep_is_error():
    # Only EXTERNAL deps are checked post-union (internal dep names are noise
    # once libraries are compared as a TU-set). A missing system lib is an error.
    a = CanonicalModel()
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False)],
                 deps=[Dependency("z", external=True)]))
    b = CanonicalModel()
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True)]))
    discs = diff_models(a, b)
    assert any(d.kind == "missing_dep" and d.severity == "error" for d in discs)


def test_internal_dep_rename_is_not_flagged():
    # internal (non-external) deps are no longer compared -- a rename mismatch
    # on an internal dep must NOT produce a discrepancy.
    a = CanonicalModel()
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False)],
                 deps=[Dependency("crypto", external=False)]))
    b = CanonicalModel()
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True)],
                 deps=[Dependency("crypto_internal", external=False)]))
    assert summarize(diff_models(a, b))["converged"]


def test_renamed_library_converges_without_a_map():
    # TU-set comparison: libraries align by source path, so a library rename
    # (crypto -> crypto_internal) needs NO target map to converge.
    a = CanonicalModel()
    a.add(Target("crypto", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("crypto/a.cpp", ["-DFOO=1"], is_bazel=False)]))
    b = CanonicalModel()
    b.add(Target("crypto_internal", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("crypto/a.cpp", ["-DFOO=1"], is_bazel=True)]))
    assert summarize(diff_models(a, b))["converged"]


def test_object_library_foldin_converges():
    # CMake keeps fipsmodule as a separate object lib; Bazel folds its sources
    # into crypto_internal. TU-set union makes this a non-issue.
    a = CanonicalModel()
    a.add(Target("crypto", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("crypto/a.cpp", ["-DFOO=1"], is_bazel=False)]))
    a.add(Target("fipsmodule", TargetKind.OBJECT, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("crypto/fips/b.cpp", ["-DFOO=1"], is_bazel=False)]))
    b = CanonicalModel()
    b.add(Target("crypto_internal", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("crypto/a.cpp", ["-DFOO=1"], is_bazel=True),
                      tu_from_raw("crypto/fips/b.cpp", ["-DFOO=1"], is_bazel=True)]))
    assert summarize(diff_models(a, b))["converged"]


def test_ignore_flags_and_defines_suppress_diffs():
    # A reviewer-approved flag/define difference is suppressed via config and
    # the diff converges; without the config it's an error.
    cmake_raw = ["-DFOO=1", "-DBORINGSSL_DISPATCH_TEST", "-Wall",
                 "-Wctad-maybe-unsupported"]
    bazel_raw = ["-DFOO=1", "-Wall"]
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True))

    assert not summarize(diff_models(a, b))["converged"]  # un-suppressed: errors

    cfg = MigrationConfig(
        ignore_defines={"BORINGSSL_DISPATCH_TEST"},
        ignore_flags={"-Wctad-maybe-unsupported"},
    )
    assert summarize(diff_models(a, b, cfg))["converged"]  # suppressed: clean


def test_ignore_flag_prefixes():
    a = _model(tu_from_raw("src/a.cpp", ["-DFOO=1", "-Wthread-safety-analysis"],
                           is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True))
    cfg = MigrationConfig(ignore_flag_prefixes=("-Wthread-safety",))
    assert summarize(diff_models(a, b, cfg))["converged"]


def test_executables_still_aligned_by_identity():
    # executables are the link deliverables: a missing exe IS a real gap.
    a = CanonicalModel()
    a.add(Target("bssl", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("tool/m.cpp", ["-DFOO=1"], is_bazel=False)]))
    b = CanonicalModel()
    discs = diff_models(a, b)
    assert any(d.kind == "missing_target" for d in discs)


def test_ignore_include_prefixes():
    # A vendored third-party include root that differs by path (in-tree under
    # CMake, external under Bazel) is suppressed via include_prefixes.
    a = _model(tu_from_raw(
        "src/a.cpp", ["-DFOO=1", "-I", "/work/proj/third_party/gtest/include"],
        is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True))

    assert not summarize(diff_models(a, b))["converged"]  # un-suppressed

    cfg = MigrationConfig(ignore_include_prefixes=("third_party/",))
    assert summarize(diff_models(a, b, cfg))["converged"]  # suppressed


def test_include_map_canonicalizes_and_converges():
    # The same dependency root spelled differently on each side converges when
    # mapped to a canonical token -- WITHOUT ignoring (the check is preserved).
    a = _model(tu_from_raw(
        "src/a.cpp", ["-DFOO=1", "-I", "/work/proj/absl-install/include"],
        is_bazel=False))
    b = _model(tu_from_raw(
        "src/a.cpp", ["-DFOO=1", "-I", "/work/proj/external/absl+"],
        is_bazel=True))
    cfg = MigrationConfig(include_map=(
        ("absl-install/include", "@absl"),
        ("external/absl+", "@absl"),
    ))
    assert summarize(diff_models(a, b, cfg))["converged"]


def test_include_map_still_catches_a_missing_include():
    # The crucial difference from ignore: if the mapped dependency is genuinely
    # ABSENT on the bazel side, the map must still flag it (no blind spot).
    a = _model(tu_from_raw(
        "src/a.cpp", ["-DFOO=1", "-I", "/work/proj/absl-install/include"],
        is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True))  # no absl
    cfg = MigrationConfig(include_map=(("absl-install/include", "@absl"),))
    discs = diff_models(a, b, cfg)
    assert any(d.kind == "includes_diff" for d in discs), \
        "map must NOT hide a genuinely missing include"


def test_include_map_collapses_bazel_duplicate_roots():
    # Bazel often lists external/X and its bazel-out/.../external/X twin; both
    # map to one token and collapse, so they don't read as a reordering/extra.
    a = _model(tu_from_raw("src/a.cpp", ["-DFOO=1", "-I", "/work/proj/dep/include"],
                           is_bazel=False))
    b = _model(tu_from_raw(
        "src/a.cpp",
        ["-DFOO=1", "-I", "/work/proj/external/dep", "-I",
         "/work/proj/bazel-out/bin/external/dep"], is_bazel=True))
    cfg = MigrationConfig(include_map=(
        ("dep/include", "@dep"),
        ("bazel-out/bin/external/dep", "@dep"),
        ("external/dep", "@dep"),
    ))
    assert summarize(diff_models(a, b, cfg))["converged"]


def test_exclude_targets_suppresses_structural_diffs():
    # A whole target present only in cmake (e.g. vendored third-party) produces
    # missing_tu/missing_target unless excluded. exclude_targets is the only
    # lever for that (ignore only covers flags/defines/includes).
    a = CanonicalModel()
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False)]))
    a.add(Target("benchmark", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("third_party/benchmark/b.cpp", ["-DFOO=1"],
                                  is_bazel=False)]))
    b = CanonicalModel()
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 tus=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True)]))

    # without exclusion: benchmark's TU is a missing_tu error
    assert not summarize(diff_models(a, b))["converged"]

    # with exclusion: converges, and the excluded target is still reported
    cfg = MigrationConfig(exclude_targets={"benchmark"})
    res = summarize(diff_models(a, b, cfg), a, b, cfg)
    assert res["converged"], res
    assert not any(d.kind == "missing_tu" for d in res["discrepancies"])
    assert "benchmark" in res["excluded"]["cmake"]["config_excluded"]


def test_config_loads_all_fields_from_json():
    # The skill drives the diff via a cmake2bazel.json on disk, so loading must
    # populate every lever. Guards against a field added to the dataclass but
    # not wired into load().
    import json
    import tempfile
    import config as config_mod
    obj = {
        "bazel_args": ["--config=macos", "--copt=-fno-rtti"],
        "target_map": {"a": "b"},
        "exclude_targets": ["benchmark"],
        "ignore": {
            "defines": ["BORINGSSL_DISPATCH_TEST"],
            "flags": ["-fvisibility=hidden"],
            "flags_prefixes": ["-Wthread-safety"],
            "include_prefixes": ["third_party/"],
            "include_map": [{"from": "external/absl+", "to": "@absl"}],
        },
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(obj, f)
        path = f.name
    cfg = config_mod.load(path)
    os.unlink(path)
    assert cfg.bazel_args == ("--config=macos", "--copt=-fno-rtti")
    assert cfg.target_map == {"a": "b"}
    assert cfg.target_excluded("benchmark")
    assert cfg.define_ignored("BORINGSSL_DISPATCH_TEST")
    assert cfg.flag_ignored("-fvisibility=hidden")
    assert cfg.flag_ignored("-Wthread-safety-analysis")  # prefix match
    assert cfg.include_ignored("third_party/gtest/include")
    assert cfg.map_include("external/absl+/base") == "@absl/base"  # rewrite


def _test_models(cmake_test_srcs, bazel_test_srcs, n_cmake_bins=1, n_bazel_bins=1):
    """Build a/b models with a shared production lib plus test targets carrying
    the given sources, split across n_*_bins test executables."""
    def build(srcs, nbins, is_bazel):
        m = CanonicalModel()
        m.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                     tus=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=is_bazel)]))
        for i in range(nbins):
            chunk = srcs[i::nbins]
            m.add(Target(f"test{i}", TargetKind.EXECUTABLE, role=TargetRole.TEST,
                         tus=[tu_from_raw(s, ["-DFOO=1"], is_bazel=is_bazel) for s in chunk]))
        return m
    return (build(cmake_test_srcs, n_cmake_bins, False),
            build(bazel_test_srcs, n_bazel_bins, True))


def test_tests_excluded_by_default():
    # With include_tests off, a test source only in cmake must NOT be flagged.
    a, b = _test_models(["t/x_test.cc"], [])
    res = summarize(diff_models(a, b), a, b)
    assert res["converged"], res
    assert "test" in res["excluded"]["cmake"]  # reported as excluded, not diffed


def test_test_tu_union_compares_when_opted_in():
    # Same test sources, differently grouped into binaries -> converges (TU-set
    # union is grouping-agnostic, like libraries).
    srcs = ["t/a_test.cc", "t/b_test.cc", "t/c_test.cc"]
    a, b = _test_models(srcs, srcs, n_cmake_bins=1, n_bazel_bins=3)
    cfg = MigrationConfig(include_tests=True)
    assert summarize(diff_models(a, b, cfg), a, b, cfg)["converged"]


def test_missing_test_source_caught_when_opted_in():
    a, b = _test_models(["t/a_test.cc", "t/b_test.cc"], ["t/a_test.cc"])
    cfg = MigrationConfig(include_tests=True)
    discs = diff_models(a, b, cfg)
    assert any(d.kind == "missing_test_tu" and d.tu == "t/b_test.cc" for d in discs)


def test_test_binary_count_mismatch_is_warned():
    # Same sources, but cmake has 1 test binary and bazel has 2 -> count warn.
    srcs = ["t/a_test.cc", "t/b_test.cc"]
    a, b = _test_models(srcs, srcs, n_cmake_bins=1, n_bazel_bins=2)
    cfg = MigrationConfig(include_tests=True)
    discs = diff_models(a, b, cfg)
    cnt = [d for d in discs if d.kind == "test_binary_count"]
    assert cnt and cnt[0].severity == Severity.WARN.value
    # source compilation still converges (count diff is only a warning)
    assert summarize(discs)["converged"]


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
