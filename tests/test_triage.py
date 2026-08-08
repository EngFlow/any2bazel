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

# No `if __name__ == "__main__"` runner here on purpose. There used to be one in
# every test file, and in this file it sat MID-FILE -- so four tests appended after
# it were defined, never called, and the file still printed "6/6 passed". The third
# instance of this session's recurring bug: a report that cannot count what it does
# not reach. `python3 tests/run_all.py` enumerates the module instead, so a test's
# POSITION in the file cannot decide whether it runs; it also fails if a file
# defines no tests at all. Run a single file with `run_all.py <name-substring>`.
