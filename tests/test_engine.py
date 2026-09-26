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

from config import MigrationConfig
from diff import DiffNotes, Severity, diff_models, summarize
from model import (Action, BuildSystem, CanonicalModel, Dependency, Target,
                   TargetKind, TargetRole)

REPO = "/work/proj"


def tu_from_raw(source, raw, is_bazel):
    """Build a synthesized CppCompile Action from raw argv (the differ
    canonicalizes it). `is_bazel` is accepted for call-site compatibility but
    the actual side comes from the model's build_system tag set in _bs()."""
    return Action(mnemonic="CppCompile", arguments=tuple(list(raw) + ["-c", source]))


def _link(raw):
    """A CppLink Action carrying link-flag argv."""
    return Action(mnemonic="CppLink", arguments=tuple(raw))


def _bs(model, is_bazel):
    """Tag a hand-built model with its build system (drives reconstruct's
    noise-stripping). Tests build the CMake side with is_bazel=False."""
    model.build_system = BuildSystem.BAZEL if is_bazel else BuildSystem.CMAKE
    model.repo_root = REPO
    return model


def _model(action, deps=(), is_bazel=False):
    m = _bs(CanonicalModel(), is_bazel)
    m.add(Target("lib", TargetKind.STATIC, actions=[action],
                 role=TargetRole.PRODUCTION,
                 deps=[Dependency(n) for n in deps]))
    return m


def test_toolchain_defaults_do_not_break_convergence():
    cmake_raw = ["-I", "/work/proj/include", "-DFOO=1", "-std=c++17", "-Wall"]
    # Bazel side: same meaning, but with injected defaults + absolute->same path
    bazel_raw = ["-iquote", "/work/proj", "-I", "/work/proj/include",
                 "-DFOO=1", "-std=c++17", "-Wall",
                 "-fno-canonical-system-headers", "-frandom-seed=abc",
                 "-g0", "-O2"]
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)
    res = summarize(diff_models(a, b))
    assert res["converged"], res
    assert res["errors"] == 0, res


def test_shared_tolerated_default_flag_does_not_fabricate_diff():
    # Regression for the Ladybird finding: a project explicitly sets
    # -fstack-protector-strong / -fdiagnostics-color=always on BOTH sides. The
    # old canonicalizer stripped these prefixes from the Bazel side only, so the
    # shared flag no longer cancelled and fabricated a false cmake_only diff.
    # They must now survive canonicalization and cancel.
    shared = ["-std=c++23", "-fstack-protector-strong", "-fdiagnostics-color=always"]
    cmake_raw = list(shared)
    # Bazel side has the same explicit flags PLUS its own injected default
    # (-fstack-protector) and pure noise.
    bazel_raw = list(shared) + ["-fstack-protector", "-fno-canonical-system-headers",
                                "-frandom-seed=abc"]
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)
    discs = diff_models(a, b)
    res = summarize(discs)
    assert res["converged"], res
    assert not any(d.kind == "flags_diff" for d in discs), \
        [d for d in discs if d.kind == "flags_diff"]


def test_cmake_stronger_variant_than_bazel_default_is_caught():
    # If CMake requests -fstack-protector-strong but Bazel only has the injected
    # default -fstack-protector, that's a REAL difference and must surface (the
    # strip must not paper over it).
    cmake_raw = ["-std=c++23", "-fstack-protector-strong"]
    bazel_raw = ["-std=c++23", "-fstack-protector"]  # weaker default only
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)
    discs = diff_models(a, b)
    fd = next(d for d in discs if d.kind == "flags_diff")
    assert "-fstack-protector-strong" in fd.cmake_only
    # and the bazel-only default is filtered from the cosmetic display
    assert "-fstack-protector" not in (fd.bazel_only or [])


def test_bazel_only_tolerated_default_filtered_from_display():
    # Bazel injects a tolerated default the project didn't set; when it's the
    # only difference on the bazel side it's tolerated AND hidden from display.
    cmake_raw = ["-std=c++23", "-Wall", "-DNEED=1"]
    bazel_raw = ["-std=c++23", "-fdiagnostics-color=always"]  # missing -Wall
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)
    discs = diff_models(a, b)
    fd = next(d for d in discs if d.kind == "flags_diff")
    assert "-Wall" in fd.cmake_only
    assert "-fdiagnostics-color=always" not in (fd.bazel_only or [])


def test_missing_cmake_flag_is_caught():
    cmake_raw = ["-DFOO=1", "-DNEEDED=1", "-std=c++17", "-fno-exceptions"]
    bazel_raw = ["-DFOO=1", "-std=c++17"]  # dropped NEEDED and -fno-exceptions
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)
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
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)
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
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)
    assert summarize(diff_models(a, b))["converged"], "pure reorder must not fail"


def test_missing_include_root_is_caught():
    # But a CMake include root genuinely ABSENT on the bazel side is an error.
    a = _model(tu_from_raw("src/a.cpp", ["-I", "/work/proj/a", "-I", "/work/proj/b"],
                           is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", ["-I", "/work/proj/a"], is_bazel=True), is_bazel=True)
    discs = diff_models(a, b)
    assert any(d.kind == "includes_diff" for d in discs), "missing root must be caught"


def test_missing_external_link_dep_is_error():
    # Only EXTERNAL deps are checked post-union (internal dep names are noise
    # once libraries are compared as a TU-set). A missing system lib is an error.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False)],
                 deps=[Dependency("z", external=True)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True)]))
    discs = diff_models(a, b)
    assert any(d.kind == "missing_dep" and d.severity == "error" for d in discs)


def test_external_dep_naming_an_in_project_target_is_not_missing():
    # A dep flagged external=True whose name matches an in-project target is
    # NOT truly external -- the CMake File-API surfaces cyclically-linked static
    # libs as repeated link fragments (e.g. common<->core), so `common` shows up
    # on core's link line as an "external" even though it's an in-project lib.
    # Its sources are verified via the TU-set, so it must NOT be reported as a
    # missing external dep even when the bazel side doesn't repeat the fragment.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("common", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("common/c.cpp", ["-DFOO=1"], is_bazel=False)]))
    a.add(Target("core", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("core/a.cpp", ["-DFOO=1"], is_bazel=False)],
                 deps=[Dependency("common", external=True),
                       Dependency("z", external=True)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("common", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("common/c.cpp", ["-DFOO=1"], is_bazel=True)]))
    b.add(Target("core", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("core/a.cpp", ["-DFOO=1"], is_bazel=True)],
                 deps=[Dependency("z", external=True)]))  # no repeated `common`
    discs = diff_models(a, b)
    # `common` must NOT be a missing external dep (it's an in-project target)...
    assert not any(d.kind == "missing_dep" and "common" in (d.cmake_only or [])
                   for d in discs), discs
    # ...but a genuinely-absent external (drop `z` on bazel) still is.
    b2 = _bs(CanonicalModel(), is_bazel=True)
    b2.add(Target("common", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                  actions=[tu_from_raw("common/c.cpp", ["-DFOO=1"], is_bazel=True)]))
    b2.add(Target("core", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                  actions=[tu_from_raw("core/a.cpp", ["-DFOO=1"], is_bazel=True)]))
    discs2 = diff_models(a, b2)
    assert any(d.kind == "missing_dep" and "z" in (d.cmake_only or [])
               for d in discs2), discs2
def test_bazel_link_input_libs_inferred_as_deps():
    # Regression for the Ladybird finding: Bazel feeds link deps to the linker
    # as depset INPUTS by path, not always as -l argv tokens. The extractor now
    # records library inputs on the link action, and reconstruct infers deps
    # from them -- so an external archive/solib is visible even with no -l flag.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", ["-DFOO=1"], is_bazel=False)],
                 deps=[Dependency("fmt", external=True)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    # No -lfmt on argv; the solib is a declared link INPUT (external/ path),
    # versioned like a real vcpkg/bzlmod solib.
    link = Action(mnemonic="CppLink", arguments=("/usr/bin/gcc", "-o", "app"),
                  inputs=("external/fmt+/libfmt.so.12.2.0",))
    b.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", ["-DFOO=1"], is_bazel=True), link]))
    # fmt must be inferred from the link input and cancel the cmake dep.
    assert summarize(diff_models(a, b))["converged"], diff_models(a, b)


def test_bazel_link_input_static_archive_inferred_as_dep():
    # A statically-linked external archive fed as a link input (no -l flag).
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", [], is_bazel=False)],
                 deps=[Dependency("simdutf", external=True)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    link = Action(mnemonic="CppLink", arguments=("/usr/bin/gcc", "-o", "app"),
                  inputs=("external/simdutf+/libsimdutf.a",))
    b.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", [], is_bazel=True), link]))
    assert summarize(diff_models(a, b))["converged"], diff_models(a, b)


def test_framework_link_flag_inferred_as_external_dep():
    # macOS `-framework <name>` is a two-token linker flag that should be
    # inferred as an external dep, just like `-l<name>`.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", ["-DFOO=1"], is_bazel=False)],
                 deps=[Dependency("CoreFoundation", external=True),
                       Dependency("Security", external=True)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    link = Action(mnemonic="CppLink",
                  arguments=("/usr/bin/clang++", "-o", "app",
                             "-framework", "CoreFoundation",
                             "-framework", "Security"))
    b.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", ["-DFOO=1"], is_bazel=True), link]))
    assert summarize(diff_models(a, b))["converged"], diff_models(a, b)


def test_framework_dep_missing_is_error():
    # A framework present on the CMake side but absent from the Bazel link
    # argv must be reported as a missing_dep error.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", [], is_bazel=False)],
                 deps=[Dependency("CoreFoundation", external=True),
                       Dependency("SystemConfiguration", external=True)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    link = Action(mnemonic="CppLink",
                  arguments=("/usr/bin/clang++", "-o", "app",
                             "-framework", "CoreFoundation"))
    b.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", [], is_bazel=True), link]))
    discs = diff_models(a, b)
    assert any(d.kind == "missing_dep" and d.cmake_only == ["SystemConfiguration"]
               for d in discs)


def test_framework_and_l_flags_combined():
    # Both `-framework <name>` and `-l<name>` on the same link line.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", [], is_bazel=False)],
                 deps=[Dependency("CoreFoundation", external=True),
                       Dependency("z", external=True)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    link = Action(mnemonic="CppLink",
                  arguments=("/usr/bin/clang++", "-o", "app",
                             "-framework", "CoreFoundation", "-lz"))
    b.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", [], is_bazel=True), link]))
    assert summarize(diff_models(a, b))["converged"], diff_models(a, b)


def test_external_dep_name_aligned_by_dep_map():
    # CMake records the archive basename ('Catch2Main'); Bazel the target/file
    # name ('catch2_main'). An explicit dep_map aligns them -> converges.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", ["-DFOO=1"], is_bazel=False)],
                 deps=[Dependency("Catch2Main", external=True)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", ["-DFOO=1"], is_bazel=True)],
                 deps=[Dependency("catch2_main", external=True)]))
    # without the map, the name mismatch is a real (reported) gap
    assert not summarize(diff_models(a, b))["converged"]
    # with it, they align
    cfg = MigrationConfig(dep_map={"Catch2Main": "catch2_main"})
    assert summarize(diff_models(a, b, cfg))["converged"], diff_models(a, b, cfg)


def test_dep_map_does_not_swallow_a_real_gap():
    # dep_map only renames the entries it lists; an unrelated absent dep still
    # surfaces.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", ["-DFOO=1"], is_bazel=False)],
                 deps=[Dependency("Catch2Main", external=True),
                       Dependency("ssl", external=True)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("m.cpp", ["-DFOO=1"], is_bazel=True)],
                 deps=[Dependency("catch2_main", external=True)]))
    cfg = MigrationConfig(dep_map={"Catch2Main": "catch2_main"})
    discs = diff_models(a, b, cfg)
    assert any(d.kind == "missing_dep" and d.cmake_only == ["ssl"] for d in discs)


def test_internal_dep_rename_is_not_flagged():
    # internal (non-external) deps are no longer compared -- a rename mismatch
    # on an internal dep must NOT produce a discrepancy.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False)],
                 deps=[Dependency("crypto", external=False)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True)],
                 deps=[Dependency("crypto_internal", external=False)]))
    assert summarize(diff_models(a, b))["converged"]


def test_renamed_library_converges_without_a_map():
    # TU-set comparison: libraries align by source path, so a library rename
    # (crypto -> crypto_internal) needs NO target map to converge.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("crypto", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("crypto/a.cpp", ["-DFOO=1"], is_bazel=False)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("crypto_internal", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("crypto/a.cpp", ["-DFOO=1"], is_bazel=True)]))
    assert summarize(diff_models(a, b))["converged"]


def test_object_library_foldin_converges():
    # CMake keeps fipsmodule as a separate object lib; Bazel folds its sources
    # into crypto_internal. TU-set union makes this a non-issue.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("crypto", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("crypto/a.cpp", ["-DFOO=1"], is_bazel=False)]))
    a.add(Target("fipsmodule", TargetKind.OBJECT, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("crypto/fips/b.cpp", ["-DFOO=1"], is_bazel=False)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("crypto_internal", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("crypto/a.cpp", ["-DFOO=1"], is_bazel=True),
                      tu_from_raw("crypto/fips/b.cpp", ["-DFOO=1"], is_bazel=True)]))
    assert summarize(diff_models(a, b))["converged"]


def test_source_map_reconciles_codegen_bundling_asymmetry():
    # CMake's AUTOMOC bundles every moc_*.cpp into ONE mocs_compilation.cpp TU;
    # Bazel compiles each moc_*.cpp separately. Same generated meta-object code,
    # different (build-system-specific) source paths, so the path-keyed TU diff
    # reports the CMake bundle as missing_tu and each Bazel moc TU as extra_tu.
    # A source_map collapsing both spellings to one token reconciles them.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("app/main.cpp", ["-DFOO=1"], is_bazel=False),
                          tu_from_raw("build/app_autogen/mocs_compilation.cpp",
                                      ["-DFOO=1"], is_bazel=False)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("app/main.cpp", ["-DFOO=1"], is_bazel=True),
                          tu_from_raw("bazel-out/k8-fastbuild/bin/moc/moc_A.cpp",
                                      ["-DFOO=1"], is_bazel=True),
                          tu_from_raw("bazel-out/k8-fastbuild/bin/moc/moc_B.cpp",
                                      ["-DFOO=1"], is_bazel=True)]))
    # Without the map: the CMake bundle is a hard missing_tu error.
    res = summarize(diff_models(a, b))
    assert not res["converged"]
    kinds = {d["kind"] for d in res["discrepancies"] if d["severity"] == "error"}
    assert "missing_tu" in kinds

    # With it: both sides' moc paths collapse to "@moc" and the diff converges.
    cfg = MigrationConfig(source_map=(
        ("build/app_autogen/mocs_compilation.cpp", "@moc"),
        ("bazel-out/k8-fastbuild/bin/moc/", "@moc"),
    ))
    assert summarize(diff_models(a, b, cfg))["converged"]


def _variant_models(cmake_libs, bazel_libs):
    """One source (src/x.cc) compiled into several libraries per side, each
    library a {name: [defines]} entry -- a multi-variant source."""
    def build(libs, is_bazel):
        m = _bs(CanonicalModel(), is_bazel)
        for name, defs in libs.items():
            raw = ["-D" + d for d in defs] + ["-std=c++17"]
            m.add(Target(name, TargetKind.STATIC, role=TargetRole.PRODUCTION,
                         actions=[tu_from_raw("src/x.cc", raw, is_bazel)]))
        return m
    return build(cmake_libs, False), build(bazel_libs, True)


def test_each_cmake_variant_is_paired_with_its_best_bazel_variant():
    # libprotobuf / libprotobuf-lite style: the same source, two flag sets, on
    # both sides. Every variant has an exact counterpart -> converged.
    a, b = _variant_models({"proto": ["FULL"], "proto_lite": ["LITE"]},
                           {"pkg:proto": ["FULL"], "pkg:proto_lite": ["LITE"]})
    res = summarize(diff_models(a, b))
    assert res["converged"], res


def test_a_cmake_variant_missing_on_bazel_is_caught():
    a, b = _variant_models({"proto": ["FULL"], "proto_lite": ["LITE"]},
                           {"pkg:proto_lite": ["LITE"]})
    discs = diff_models(a, b)
    errs = [d for d in discs if d.severity == Severity.ERROR.value]
    assert len(errs) == 1 and errs[0].kind == "defines_diff", errs
    assert errs[0].cmake_only == ["FULL"], errs


def test_an_extra_bazel_variant_is_tolerated():
    a, b = _variant_models({"proto_lite": ["LITE"]},
                           {"pkg:proto": ["FULL"], "pkg:proto_lite": ["LITE"]})
    discs = diff_models(a, b)
    assert summarize(discs)["converged"], discs
    assert discs == [], discs  # the matching variant was chosen, not FULL


def test_tied_pairings_prefer_the_smaller_difference():
    # Three CMake bundles collapse to one source token via source_map, each
    # with its own define; Bazel has a LIB0 TU and a TU with no define. LIB1
    # and LIB2 tie on errors against both; the pairing must be the one that
    # does not report LIB0 (another variant's define) as bazel_only.
    a, b = _bs(CanonicalModel(), False), _bs(CanonicalModel(), True)
    a.add(Target("l", TargetKind.STATIC, role=TargetRole.PRODUCTION, actions=[
        tu_from_raw(f"gen/a{i}.cc", [f"-DLIB{i}"], False) for i in range(3)]))
    b.add(Target("l", TargetKind.STATIC, role=TargetRole.PRODUCTION, actions=[
        tu_from_raw("gen/b0.cc", ["-DLIB0"], True),
        tu_from_raw("gen/b1.cc", [], True)]))
    discs = diff_models(a, b, MigrationConfig(source_map=(("gen/", "@gen"),)))
    assert [(d.cmake_only, d.bazel_only) for d in discs] == \
        [(["LIB1"], []), (["LIB2"], [])], discs


def test_tied_pairings_prefer_the_exact_variant_over_a_noisy_one():
    # Both Bazel variants produce no discrepancy against CMake {-std=c++17};
    # the one that only ADDS flags (-w, -Wno-error=foo) must not be chosen.
    from diff import _best_match
    from reconstruct import reconstruct
    a, b = _bs(CanonicalModel(), False), _bs(CanonicalModel(), True)
    a.add(Target("l", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("x.cc", ["-std=c++17"], False)]))
    for name, raw in (("a:noisy", ["-std=c++17", "-w", "-Wno-error=foo"]),
                      ("b:exact", ["-std=c++17"])):
        b.add(Target(name, TargetKind.STATIC, role=TargetRole.PRODUCTION,
                     actions=[tu_from_raw("x.cc", raw, True)]))
    ta = reconstruct(a)["l"].tus[0]
    b_vs = [reconstruct(b)[n].tus[0] for n in ("a:noisy", "b:exact")]
    discs, chosen = _best_match("<libraries>", ta, b_vs, MigrationConfig())
    assert discs == [] and chosen.flags == ("-std=c++17",), chosen


def test_multi_variant_verdict_does_not_depend_on_hash_seed():
    # Regression: libraries used to be pooled with dict.setdefault while
    # iterating a SET of names, so which variant got compared -- and the
    # verdict -- changed with PYTHONHASHSEED.
    import json
    import subprocess
    import tempfile
    from serialize import dump_model
    libs = {f"lib{i}": (["LITE"] if i % 2 else ["FULL"]) for i in range(8)}
    a, b = _variant_models(libs, {"pkg:" + k: v for k, v in libs.items()})
    b.targets.pop("pkg:lib0")  # FULL is still built by pkg:lib2, pkg:lib4, ...
    with tempfile.TemporaryDirectory() as d:
        pa, pb = os.path.join(d, "a.json"), os.path.join(d, "b.json")
        dump_model(a, pa)
        dump_model(b, pb)
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "diff.py")
        outs = set()
        for seed in range(8):
            env = dict(os.environ, PYTHONHASHSEED=str(seed))
            out = subprocess.run([sys.executable, script, pa, pb], env=env,
                                 capture_output=True, text=True, check=True).stdout
            outs.add(out)
    assert len(outs) == 1, "diff output varies with PYTHONHASHSEED"
    assert json.loads(outs.pop())["converged"]


def test_bazel_only_flags_are_reported_but_never_block():
    # The comparison is one-directional: flags only Bazel has never produce a
    # discrepancy, so a converged diff must still SAY they exist.
    cmake_raw = ["-std=c++17", "-Werror"]
    bazel_raw = ["-std=c++17", "-Werror", "-Wno-error=parentheses", "-w",
                 "-fstack-protector"]  # the last: tolerated toolchain default
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)
    notes = DiffNotes()
    discs = diff_models(a, b, notes=notes)
    res = summarize(discs, a, b, notes=notes)
    assert res["converged"] and discs == [], res
    assert res["bazel_only_flags"]["compile"] == {"-Wno-error=parentheses": 1, "-w": 1}
    assert res["bazel_only_flags"]["link"] == {}


def test_bazel_only_flags_respect_ignore_and_count_per_tu():
    cmake_raw = ["-std=c++17"]
    bazel_raw = ["-std=c++17", "-fno-strict-aliasing", "-Wno-unused"]
    a, b = _bs(CanonicalModel(), False), _bs(CanonicalModel(), True)
    for m, raw, bz in ((a, cmake_raw, False), (b, bazel_raw, True)):
        m.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                     actions=[tu_from_raw(f"src/{n}.cpp", raw, bz)
                              for n in ("a", "b", "c")]))
    notes = DiffNotes()
    summarize(diff_models(a, b, MigrationConfig(ignore_flags={"-Wno-unused"}), notes))
    assert dict(notes.bazel_only_flags) == {"-fno-strict-aliasing": 3}


def test_bazel_only_link_flags_are_reported():
    a = _bs(CanonicalModel(), is_bazel=False); a.add(_exe("app", ["-pthread"], False))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(_exe("app", ["-pthread", "-Wl,--export-dynamic"], True))
    notes = DiffNotes()
    res = summarize(diff_models(a, b, notes=notes), notes=notes)
    assert res["converged"], res
    assert res["bazel_only_flags"]["link"] == {"-Wl,--export-dynamic": 1}


def test_bazel_only_flags_are_collected_from_tests_when_opted_in():
    a, b = _bs(CanonicalModel(), False), _bs(CanonicalModel(), True)
    a.add(Target("t_test", TargetKind.EXECUTABLE, role=TargetRole.TEST,
                 actions=[tu_from_raw("test/t.cpp", ["-std=c++17"], False)]))
    b.add(Target("t:t_test", TargetKind.EXECUTABLE, role=TargetRole.TEST,
                 actions=[tu_from_raw("test/t.cpp", ["-std=c++17", "-w"], True)]))
    notes = DiffNotes()
    discs = diff_models(a, b, MigrationConfig(include_tests=True), notes)
    assert summarize(discs)["converged"], discs
    assert dict(notes.bazel_only_flags) == {"-w": 1}


def test_bazel_only_flags_come_from_the_exact_variant_when_one_exists():
    # Two Bazel variants of x.cc, one exact and one adding -w; the notes must
    # reflect the chosen (exact) pair, not the noisy one.
    a, b = _bs(CanonicalModel(), False), _bs(CanonicalModel(), True)
    a.add(Target("l", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("x.cc", ["-std=c++17"], False)]))
    for name, raw in (("a:noisy", ["-std=c++17", "-w", "-Wno-error=foo"]),
                      ("b:exact", ["-std=c++17"])):
        b.add(Target(name, TargetKind.STATIC, role=TargetRole.PRODUCTION,
                     actions=[tu_from_raw("x.cc", raw, True)]))
    notes = DiffNotes()
    assert diff_models(a, b, notes=notes) == []
    assert dict(notes.bazel_only_flags) == {}


def test_summary_without_notes_has_no_bazel_only_field():
    a = _model(tu_from_raw("src/a.cpp", ["-O2"], is_bazel=False), is_bazel=False)
    assert "bazel_only_flags" not in summarize(diff_models(a, a))


def test_levels_reported_when_diff_is_blind_to_them():
    # CMake Release compiles at -O3; Bazel -c opt at -O2. -O* is dropped from
    # the per-TU diff, so the diff converges -- the levels summary must not.
    a = _model(tu_from_raw("src/a.cpp", ["-O3", "-DNDEBUG"], is_bazel=False),
               is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", ["-O2", "-g0", "-DNDEBUG"], is_bazel=True),
               is_bazel=True)
    res = summarize(diff_models(a, b), a, b)
    assert res["converged"], res
    lv = res["levels"]
    assert lv["differs"]
    assert lv["cmake"] == {"CXX": {"opt": {"-O3": 1}, "debug": {"(none)": 1},
                                   "debug_format": {"(none)": 1}}}
    assert lv["bazel"] == {"CXX": {"opt": {"-O2": 1}, "debug": {"-g0": 1},
                                   "debug_format": {"(none)": 1}}}


def test_levels_last_flag_wins_and_split_by_language():
    a, b = _bs(CanonicalModel(), False), _bs(CanonicalModel(), True)
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION, actions=[
        tu_from_raw("src/a.c", ["-g", "-O2", "-O0"], False),
        tu_from_raw("src/b.cc", ["-g"], False)]))
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION, actions=[
        tu_from_raw("src/a.c", ["-O0", "-g"], True),
        tu_from_raw("src/b.cc", ["-g"], True),
        # a Bazel header-processing action is not a TU and is not counted
        Action("CppCompile", ("-xc++-header", "-fsyntax-only", "-O2",
                              "-c", "src/b.h"))]))
    lv = summarize(diff_models(a, b), a, b)["levels"]
    assert lv["cmake"]["C"] == {"opt": {"-O0": 1}, "debug": {"-g": 1}, "debug_format": {"(none)": 1}}
    assert lv["cmake"]["CXX"] == {"opt": {"(none)": 1}, "debug": {"-g": 1}, "debug_format": {"(none)": 1}}
    assert lv["bazel"] == lv["cmake"]
    assert not lv["differs"]


def test_levels_language_on_one_side_only_does_not_set_differs():
    # `differs` compares levels for languages BOTH sides compile; a language
    # only one side has is a missing-TU matter, reported but not a level diff.
    a = _model(tu_from_raw("src/a.c", ["-O2"], is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", ["-O3"], is_bazel=True), is_bazel=True)
    lv = summarize(diff_models(a, b), a, b)["levels"]
    assert set(lv["cmake"]) == {"C"} and set(lv["bazel"]) == {"CXX"}, lv
    assert lv["differs"] is False


def test_debug_level_and_format_are_tracked_separately():
    # -g3 emits macro information that -g1 does not; sharing -gdwarf-4 must
    # not hide that. A format flag alone enables debug info at level -g.
    a = _model(tu_from_raw("src/a.cpp", ["-g3", "-gdwarf-4"], is_bazel=False),
               is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", ["-g1", "-gdwarf-4"], is_bazel=True),
               is_bazel=True)
    lv = summarize(diff_models(a, b), a, b)["levels"]
    assert lv["cmake"]["CXX"]["debug"] == {"-g3": 1}
    assert lv["bazel"]["CXX"]["debug"] == {"-g1": 1}
    assert lv["cmake"]["CXX"]["debug_format"] == {"-gdwarf-4": 1}
    assert lv["differs"]
    c = _model(tu_from_raw("src/a.cpp", ["-gdwarf-5"], is_bazel=True), is_bazel=True)
    lc = summarize(diff_models(a, c), a, c)["levels"]["bazel"]["CXX"]
    assert (lc["debug"], lc["debug_format"]) == ({"-g": 1}, {"-gdwarf-5": 1})


def test_levels_skip_a_bazel_target_excluded_under_its_mapped_name():
    # target_map renames pkg:tool to tool, which is excluded: the diff skips
    # it, so the levels must not count it either.
    def m(name, raw, is_bazel):
        model = _bs(CanonicalModel(), is_bazel)
        model.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                         actions=[tu_from_raw("src/a.cpp", ["-O2"], is_bazel)]))
        model.add(Target(name, TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                         actions=[tu_from_raw("tool/m.cpp", raw, is_bazel)]))
        return model
    a, b = m("tool", ["-O2"], False), m("pkg:tool", ["-O0"], True)
    cfg = MigrationConfig(target_map={"tool": "pkg:tool"}, exclude_targets={"tool"})
    lv = summarize(diff_models(a, b, cfg), a, b, cfg)["levels"]
    assert lv["bazel"]["CXX"]["opt"] == {"-O2": 1}, lv
    assert not lv["differs"], lv


def test_levels_count_only_compared_targets():
    a = _bs(CanonicalModel(), False)
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-O2"], False)]))
    a.add(Target("vendored", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("third_party/z.c", ["-O3"], False)]))
    a.add(Target("t_test", TargetKind.EXECUTABLE, role=TargetRole.TEST,
                 actions=[tu_from_raw("test/t.cpp", ["-O0"], False)]))
    lv = summarize([], a, a, MigrationConfig(exclude_targets={"vendored"}))["levels"]
    assert lv["cmake"] == {"CXX": {"opt": {"-O2": 1}, "debug": {"(none)": 1},
                                   "debug_format": {"(none)": 1}}}


def test_ignore_flags_and_defines_suppress_diffs():
    # A reviewer-approved flag/define difference is suppressed via config and
    # the diff converges; without the config it's an error.
    cmake_raw = ["-DFOO=1", "-DBORINGSSL_DISPATCH_TEST", "-Wall",
                 "-Wctad-maybe-unsupported"]
    bazel_raw = ["-DFOO=1", "-Wall"]
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)

    assert not summarize(diff_models(a, b))["converged"]  # un-suppressed: errors

    cfg = MigrationConfig(
        ignore_defines={"BORINGSSL_DISPATCH_TEST"},
        ignore_flags={"-Wctad-maybe-unsupported"},
    )
    assert summarize(diff_models(a, b, cfg))["converged"]  # suppressed: clean


def test_force_include_path_normalized():
    # `-include FILE` / `-imacros FILE` carry a FILE path (a forced header), not
    # a search dir. CMake spells it absolutely, Bazel repo-relative -- the same
    # forced header must line up (normalized to repo-relative), so a pure
    # spelling difference converges...
    cmake_raw = ["-DFOO=1", "-include", "/work/proj/Externals/x/fix.h"]
    bazel_raw = ["-DFOO=1", "-include", "Externals/x/fix.h"]
    a = _model(tu_from_raw("src/a.cpp", cmake_raw, is_bazel=False), is_bazel=False)
    b = _model(tu_from_raw("src/a.cpp", bazel_raw, is_bazel=True), is_bazel=True)
    assert summarize(diff_models(a, b))["converged"], diff_models(a, b)

    # ...but a DIFFERENT forced header (or a missing one) is still a real gap.
    b2 = _model(tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True), is_bazel=True)
    discs = diff_models(a, b2)
    assert any(d.kind == "flags_diff" and
               any("fix.h" in f for f in (d.cmake_only or [])) for d in discs), discs


def test_ignore_flag_prefixes():
    a = _model(tu_from_raw("src/a.cpp", ["-DFOO=1", "-Wthread-safety-analysis"],
                           is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True), is_bazel=True)
    cfg = MigrationConfig(ignore_flag_prefixes=("-Wthread-safety",))
    assert summarize(diff_models(a, b, cfg))["converged"]


def test_executables_still_aligned_by_identity():
    # executables are the link deliverables: a missing exe IS a real gap.
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("bssl", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("tool/m.cpp", ["-DFOO=1"], is_bazel=False)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    discs = diff_models(a, b)
    assert any(d.kind == "missing_target" for d in discs)


def _exe(name, link_raw, is_bazel, src="tool/m.cpp"):
    return Target(name, TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                  actions=[tu_from_raw(src, ["-DFOO=1"], is_bazel=is_bazel),
                           _link(link_raw)])


def test_missing_link_flag_is_caught():
    # A CMake link flag absent on the bazel side is a link_flags_diff error.
    a = _bs(CanonicalModel(), is_bazel=False); a.add(_exe("app", ["-pthread", "-rdynamic"], False))
    b = _bs(CanonicalModel(), is_bazel=True); b.add(_exe("app", ["-pthread"], True))
    discs = diff_models(a, b)
    lf = [d for d in discs if d.kind == "link_flags_diff"]
    assert lf and lf[0].target == "app"
    assert "-rdynamic" in lf[0].cmake_only


def test_extra_bazel_link_flag_tolerated():
    # Asymmetric: an extra link flag only on the bazel side must NOT fail.
    a = _bs(CanonicalModel(), is_bazel=False); a.add(_exe("app", ["-pthread"], False))
    b = _bs(CanonicalModel(), is_bazel=True); b.add(_exe("app", ["-pthread", "-Wl,--gc-sections"], True))
    assert summarize(diff_models(a, b))["converged"]


def test_link_toolchain_noise_stripped():
    # Bazel link noise (macOS/objc/sysroot mechanics + -o/output) must not show
    # up as a diff against a clean CMake link line.
    a = _bs(CanonicalModel(), is_bazel=False); a.add(_exe("app", ["-pthread"], False))
    b = _bs(CanonicalModel(), is_bazel=True); b.add(_exe("app", [
        "external/.../cc_wrapper.sh", "-o", "bazel-out/bin/app",
        "bazel-out/bin/_objs/app/m.o", "bazel-out/bin/libfoo.a",
        "-pthread", "-Wl,-oso_prefix,.", "-headerpad_max_install_names",
        "-fobjc-link-runtime", "-mmacosx-version-min=15.2", "-lc++", "-lm",
    ], True))
    assert summarize(diff_models(a, b))["converged"], "link noise must be stripped"


def test_ignore_link_flags_suppresses():
    a = _bs(CanonicalModel(), is_bazel=False); a.add(_exe("app", ["-fvisibility=hidden", "-pthread"], False))
    b = _bs(CanonicalModel(), is_bazel=True); b.add(_exe("app", ["-pthread"], True))
    assert not summarize(diff_models(a, b))["converged"]
    cfg = MigrationConfig(ignore_link_flags={"-fvisibility=hidden"})
    assert summarize(diff_models(a, b, cfg))["converged"]


def test_ignore_include_prefixes():
    # A vendored third-party include root that differs by path (in-tree under
    # CMake, external under Bazel) is suppressed via include_prefixes.
    a = _model(tu_from_raw(
        "src/a.cpp", ["-DFOO=1", "-I", "/work/proj/third_party/gtest/include"],
        is_bazel=False))
    b = _model(tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True), is_bazel=True)

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
    b = _model(tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True), is_bazel=True)  # no absl
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
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False)]))
    a.add(Target("benchmark", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("third_party/benchmark/b.cpp", ["-DFOO=1"],
                                  is_bazel=False)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True)]))

    # without exclusion: benchmark's TU is a missing_tu error
    assert not summarize(diff_models(a, b))["converged"]

    # with exclusion: converges, and the excluded target is still reported
    cfg = MigrationConfig(exclude_targets={"benchmark"})
    res = summarize(diff_models(a, b, cfg), a, b, cfg)
    assert res["converged"], res
    assert not any(d.kind == "missing_tu" for d in res["discrepancies"])
    assert "benchmark" in res["excluded"]["cmake"]["config_excluded"]


def test_renamed_bazel_target_is_listed_as_excluded():
    # target_map renames pkg:app to app; exclude_targets names app. The diff
    # skips it, so the excluded summary must list it on the Bazel side too.
    a = _bs(CanonicalModel(), is_bazel=False); a.add(_exe("app", [], False))
    b = _bs(CanonicalModel(), is_bazel=True); b.add(_exe("pkg:app", [], True))
    cfg = MigrationConfig(target_map={"app": "pkg:app"}, exclude_targets={"app"})
    res = summarize(diff_models(a, b, cfg), a, b, cfg)
    assert res["discrepancies"] == [], res
    assert res["excluded"]["bazel"] == {"config_excluded": ["pkg:app"]}, res
    assert res["excluded"]["cmake"] == {"config_excluded": ["app"]}, res


def test_target_map_still_renames_a_dependency_with_no_target_node():
    # An external dep spelled foo by CMake and pkg:foo by Bazel, aligned by a
    # target_map entry; no pkg:foo target exists. The entry must still rename
    # the dependency reference, as it always has.
    def m(dep, is_bazel):
        model = _bs(CanonicalModel(), is_bazel)
        model.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                         deps=[Dependency(dep, external=True)],
                         actions=[tu_from_raw("src/a.cpp", [], is_bazel)]))
        return model
    a, b = m("foo", False), m("pkg:foo", True)
    assert diff_models(a, b, MigrationConfig(target_map={"foo": "pkg:foo"})) == []


def test_config_loads_all_fields_from_json():
    # The skill drives the diff via a any2bazel.json on disk, so loading must
    # populate every lever. Guards against a field added to the dataclass but
    # not wired into load().
    import json
    import tempfile
    import config as config_mod
    obj = {
        "bazel_args": ["--config=macos", "--copt=-fno-rtti"],
        "target_map": {"a": "b"},
        "dep_map": {"Catch2Main": "catch2_main"},
        "exclude_targets": ["benchmark"],
        "ignore": {
            "defines": ["BORINGSSL_DISPATCH_TEST"],
            "flags": ["-fvisibility=hidden"],
            "flags_prefixes": ["-Wthread-safety"],
            "link_flags": ["-rdynamic"],
            "link_flags_prefixes": ["-Wl,--gc"],
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
    assert cfg.dep_map == {"Catch2Main": "catch2_main"}
    assert cfg.target_excluded("benchmark")
    assert cfg.define_ignored("BORINGSSL_DISPATCH_TEST")
    assert cfg.flag_ignored("-fvisibility=hidden")
    assert cfg.flag_ignored("-Wthread-safety-analysis")  # prefix match
    assert cfg.link_flag_ignored("-rdynamic")
    assert cfg.link_flag_ignored("-Wl,--gc-sections")  # prefix match
    assert cfg.include_ignored("third_party/gtest/include")
    assert cfg.map_include("external/absl+/base") == "@absl/base"  # rewrite


def test_bazel_args_accepts_flags_and_flag_values():
    import config as config_mod
    for ok in ([], ["--config=release"], ["-c", "opt"],
               ["--config", "release", "--copt=-O3"],
               ["--compilation_mode=opt", "--cxxopt=-std=c++17", "--force_pic"]):
        assert config_mod.validate_bazel_args(ok) == tuple(ok), ok


def test_bazel_args_rejects_a_command_line():
    # A whole aquery command in the field (seen in practice): spliced into
    # `bazel aquery <args> <query>` it yields two query expressions.
    import config as config_mod
    bad_lists = (
        ["aquery", 'mnemonic("CppCompile|CppLink|CppArchive", //...)',
         "--output=jsonproto"],
        ["--config=release", "build"],        # '=' flag takes no separate value
        ["-c", "opt", "//..."],               # one value per flag
        "--config=release",                   # a string, not a list
    )
    for bad in bad_lists:
        try:
            config_mod.validate_bazel_args(bad)
        except ValueError as e:
            if isinstance(bad, list):
                assert repr(bad[-1] if bad[0].startswith("-") else bad[0]) in str(e), e
        else:
            raise AssertionError(f"accepted {bad!r}")


def test_bazel_args_rejects_a_non_string_entry():
    import config as config_mod
    try:
        config_mod.validate_bazel_args(["-c", 3])
    except ValueError as e:
        assert "3" in str(e) and "not a string" in str(e), e
    else:
        raise AssertionError("accepted a non-string entry")


def test_config_load_refuses_malformed_bazel_args():
    import json
    import tempfile
    import config as config_mod
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"bazel_args": ["aquery", "//...", "--output=jsonproto"]}, f)
        path = f.name
    try:
        config_mod.load(path)
    except ValueError as e:
        assert "'aquery'" in str(e), e
    else:
        raise AssertionError("malformed bazel_args loaded without error")
    finally:
        os.unlink(path)


def _test_models(cmake_test_srcs, bazel_test_srcs, n_cmake_bins=1, n_bazel_bins=1):
    """Build a/b models with a shared production lib plus test targets carrying
    the given sources, split across n_*_bins test executables."""
    def build(srcs, nbins, is_bazel):
        m = _bs(CanonicalModel(), is_bazel=False)
        m.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                     actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=is_bazel)]))
        for i in range(nbins):
            chunk = srcs[i::nbins]
            m.add(Target(f"test{i}", TargetKind.EXECUTABLE, role=TargetRole.TEST,
                         actions=[tu_from_raw(s, ["-DFOO=1"], is_bazel=is_bazel) for s in chunk]))
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


def test_cross_role_source_not_falsely_missing():
    # A source that is a TEST TU on the cmake side but a (testonly) LIBRARY TU
    # on the bazel side must NOT be reported missing -- it's compiled on both,
    # just grouped under different roles (abseil per_thread_sem_test pattern).
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False)]))
    a.add(Target("sem_test", TargetKind.EXECUTABLE, role=TargetRole.TEST,
                 actions=[tu_from_raw("t/sem_test.cc", ["-DFOO=1"], is_bazel=False)]))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True)]))
    # bazel compiles the test source into a testonly LIBRARY, not a test exe
    b.add(Target("sem_test_common", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("t/sem_test.cc", ["-DFOO=1"], is_bazel=True)]))
    cfg = MigrationConfig(include_tests=True)
    discs = diff_models(a, b, cfg)
    assert not any(d.tu == "t/sem_test.cc" for d in discs), \
        "cross-role source compiled on both sides must not be flagged"


def test_source_compiled_nowhere_still_caught():
    # The flip side: a test source compiled NOWHERE on the bazel side IS a real
    # missing_test_tu (must survive the cross-role relaxation).
    a, b = _test_models(["t/only_in_cmake_test.cc"], [])
    # give bazel a production lib so the models aren't trivially empty
    b.add(Target("extra", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/z.cpp", ["-DFOO=1"], is_bazel=True)]))
    cfg = MigrationConfig(include_tests=True)
    discs = diff_models(a, b, cfg)
    assert any(d.kind == "missing_test_tu" and d.tu == "t/only_in_cmake_test.cc"
               for d in discs)


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
    a = _bs(CanonicalModel(), is_bazel=False)
    a.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=False)]))
    a.add(Target("Nightly", TargetKind.UNKNOWN, role=TargetRole.DASHBOARD))
    b = _bs(CanonicalModel(), is_bazel=True)
    b.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                 actions=[tu_from_raw("src/a.cpp", ["-DFOO=1"], is_bazel=True)]))
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
