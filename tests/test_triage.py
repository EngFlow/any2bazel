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

"""Tests for the triage summary (diff.json -> grouped worklist)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from triage import render, triage

DIFF = {
    "converged": False, "errors": 3, "warnings": 1,
    "discrepancies": [
        {"kind": "flags_diff", "severity": "error", "target": "<libraries>",
         "tu": "a.cc", "cmake_only": ["-std=gnu++17"], "bazel_only": ["-std=c++17"]},
        {"kind": "flags_diff", "severity": "error", "target": "<libraries>",
         "tu": "b.cc", "cmake_only": ["-std=gnu++17"], "bazel_only": ["-std=c++17"]},
        {"kind": "missing_dep", "severity": "error", "target": "<external>",
         "cmake_only": ["z"], "bazel_only": None},
        {"kind": "extra_tu", "severity": "warn", "target": "<libraries>",
         "tu": "x.cc", "cmake_only": None, "bazel_only": None},
    ],
    "excluded": {"cmake": {"dashboard": ["Nightly"]}, "bazel": {"test": ["t1", "t2"]}},
}


def test_groups_by_kind_with_histogram():
    t = triage(DIFF)
    kinds = {k["kind"]: k for k in t["kinds"]}
    assert kinds["flags_diff"]["count"] == 2
    # the systematic flag floats to the top of the histogram
    assert kinds["flags_diff"]["cmake_only"][0] == ("-std=gnu++17", 2)
    assert kinds["missing_dep"]["cmake_only"][0] == ("z", 1)


def test_kinds_sorted_by_frequency():
    t = triage(DIFF)
    counts = [k["count"] for k in t["kinds"]]
    assert counts == sorted(counts, reverse=True)


def test_filter_to_single_kind():
    t = triage(DIFF, only_kind="missing_dep")
    assert [k["kind"] for k in t["kinds"]] == ["missing_dep"]


def test_excluded_passed_through():
    t = triage(DIFF)
    assert t["excluded"]["bazel"]["test"] == ["t1", "t2"]


def test_render_caps_bazel_only_tail():
    # 40 distinct bazel_only values -> render caps at 25 + an overflow line
    discs = [{"kind": "flags_diff", "severity": "error", "target": "<libraries>",
              "tu": f"f{i}.cc", "cmake_only": ["-std=gnu++17"],
              "bazel_only": [f"-flag{i}"]} for i in range(40)]
    diff = {"converged": False, "errors": 40, "warnings": 0,
            "discrepancies": discs, "excluded": {}}
    text = render(triage(diff), top=25)
    assert "+15 more distinct values" in text
    # cmake_only (actionable) is never capped
    assert "-std=gnu++17" in text


def test_render_shows_bazel_only_flags_even_when_converged():
    diff = {"converged": True, "errors": 0, "warnings": 0, "discrepancies": [],
            "excluded": {},
            "bazel_only_flags": {"note": "...",
                                 "compile": {"-Wno-error=parentheses": 24, "-w": 3},
                                 "link": {}}}
    text = render(triage(diff))
    assert "bazel-only flags (never a discrepancy; review): 2 compile, 0 link" in text
    assert "compile (TU pairs): -Wno-error=parentheses x24" in text


def test_render_without_bazel_only_flags_is_unchanged():
    # diff.json written before the field existed renders as before
    assert "bazel-only" not in render(triage(DIFF))


def test_render_shows_levels():
    diff = {"converged": True, "errors": 0, "warnings": 0, "discrepancies": [],
            "excluded": {},
            "levels": {"note": "...", "differs": True,
                       "cmake": {"CXX": {"opt": {"-O3": 94}, "debug": {"(none)": 94},
                                         "debug_format": {"(none)": 94}}},
                       "bazel": {"CXX": {"opt": {"-O2": 94}, "debug": {"-g0": 94},
                                         "debug_format": {"(none)": 94}}}}}
    text = render(triage(diff))
    assert "opt/debug levels (DIFFERS; not part of the diff):" in text
    assert "opt: -O3 x94" in text and "opt: -O2 x94" in text
    assert "format: (none) x94" in text


def test_render_shows_ndebug_and_bazel_only_defines():
    diff = {"converged": True, "errors": 0, "warnings": 1534, "discrepancies": [],
            "excluded": {},
            "ndebug": {"note": "...", "cmake": {"(none)": 1534},
                       "bazel": {"NDEBUG": 1534}, "bazel_added": 1534,
                       "bazel_missing": 0, "differs": True},
            "bazel_only_flags": {"note": "...", "compile": {}, "link": {},
                                 "defines": {"NDEBUG": 1534}}}
    text = render(triage(diff))
    assert ("NDEBUG / assertions (DIFFERS): cmake (none) x1534  |  bazel NDEBUG x1534;"
            " bazel adds it on 1534 compared TU pairs, lacks it on 0") in text, text
    assert "bazel-only defines (WARN only): 1" in text
    assert "defines (TU pairs): NDEBUG x1534" in text


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
