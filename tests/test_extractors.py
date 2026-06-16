"""End-to-end test: synthetic File-API + aquery JSON -> models -> diff.

The JSON fixtures here mirror the real schemas (codemodel-v2 target objects;
aquery ActionGraphContainer). They let us validate the extractors and the full
pipeline without cmake/bazel installed. Replace with captured real output once
available -- the extractor code does not change.

The fixture describes the SAME tiny project from both sides:
  target 'mylib' (static): compiles src/a.cpp with -DFOO=1 -Iinclude -std=c++17
and asserts the pipeline converges (zero errors), even though the bazel argv
carries the usual injected toolchain defaults.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import extract_bazel
import extract_cmake
from diff import diff_models, summarize

REPO = "/work/proj"

# ---- CMake File API codemodel reply (minimal but real-shaped) ----------------
CODEMODEL = {
    "kind": "codemodel",
    "configurations": [{
        "name": "Debug",
        "targets": [{"id": "mylib::@abc", "name": "mylib", "jsonFile": "target-mylib.json"}],
    }],
}
TARGET_MYLIB = {
    "name": "mylib",
    "type": "STATIC_LIBRARY",
    "sources": [{"path": "src/a.cpp"}],
    "compileGroups": [{
        "language": "CXX",
        "sourceIndexes": [0],
        "compileCommandFragments": [{"fragment": "-std=c++17 -Wall"}],
        "defines": [{"define": "FOO=1"}],
        "includes": [{"path": "/work/proj/include"}],
    }],
    "dependencies": [],
}

# ---- Bazel aquery ActionGraphContainer (same project) ------------------------
AQUERY = {
    "artifacts": [
        {"id": 1, "pathFragmentId": 10},   # src/a.cpp
        {"id": 2, "pathFragmentId": 11},   # libmylib.a
    ],
    "pathFragments": [
        {"id": 10, "label": "a.cpp", "parentId": 12},
        {"id": 12, "label": "src"},
        {"id": 11, "label": "libmylib.a", "parentId": 13},
        {"id": 13, "label": "bazel-out"},
    ],
    "targets": [{"id": 100, "label": "//:mylib"}],
    "actions": [
        {
            "mnemonic": "CppCompile",
            "targetId": 100,
            "outputIds": [],
            "arguments": [
                "/usr/bin/clang", "-c", "src/a.cpp",
                "-DFOO=1", "-I", "/work/proj/include", "-std=c++17", "-Wall",
                # injected toolchain defaults that must NOT cause a diff:
                "-iquote", "/work/proj", "-fno-canonical-system-headers",
                "-frandom-seed=xyz", "-g0", "-O2",
            ],
        },
        {
            "mnemonic": "CppArchive",
            "targetId": 100,
            "outputIds": [2],
            "arguments": ["/usr/bin/ar", "rcs", "libmylib.a", "a.o"],
        },
    ],
}


def _write_cmake_fixture(root):
    reply = os.path.join(root, "build", ".cmake", "api", "v1", "reply")
    os.makedirs(reply)
    with open(os.path.join(reply, "index-0.json"), "w") as f:
        json.dump({"objects": [{"kind": "codemodel", "jsonFile": "codemodel.json"}]}, f)
    with open(os.path.join(reply, "codemodel.json"), "w") as f:
        json.dump(CODEMODEL, f)
    with open(os.path.join(reply, "target-mylib.json"), "w") as f:
        json.dump(TARGET_MYLIB, f)
    return os.path.join(root, "build")


def test_full_pipeline_converges():
    with tempfile.TemporaryDirectory() as root:
        build = _write_cmake_fixture(root)
        a = extract_cmake.extract(build, REPO)

        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(AQUERY, f)
        b = extract_bazel.extract(aq_path, REPO)

        # CMake keeps the target name; Bazel keys by full label (//:mylib ->
        # ':mylib') to avoid cross-package name collisions. Library comparison
        # is by source path, so the differing names still converge.
        assert "mylib" in a.targets, a.targets.keys()
        assert ":mylib" in b.targets, b.targets.keys()
        assert a.targets["mylib"].kind.value == "static_library"
        assert b.targets[":mylib"].kind.value == "static_library"

        res = summarize(diff_models(a, b))
        assert res["converged"], json.dumps(res, indent=2)
        assert res["errors"] == 0, json.dumps(res, indent=2)


def test_cmake_extracts_canonical_flags():
    with tempfile.TemporaryDirectory() as root:
        build = _write_cmake_fixture(root)
        a = extract_cmake.extract(build, REPO)
        tu = a.targets["mylib"].tus[0]
        assert tu.source == "src/a.cpp"
        assert "FOO=1" in tu.defines
        assert "include" in tu.includes          # made repo-relative
        assert "-std=c++17" in tu.flags


def test_bazel_infers_kind_and_drops_defaults():
    with tempfile.TemporaryDirectory() as root:
        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(AQUERY, f)
        b = extract_bazel.extract(aq_path, REPO)
        tu = b.targets[":mylib"].tus[0]   # full-label key
        # toolchain defaults must be gone after canonicalization
        assert not any("random-seed" in fl for fl in tu.flags)
        assert "-fno-canonical-system-headers" not in tu.flags
        assert "include" in tu.includes


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
