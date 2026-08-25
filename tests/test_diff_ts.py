# Copyright 2026 EngFlow GmbH
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

"""Tests for diff_ts.py -- the standalone npm-vs-Bazel TS emit diff.

Synthetic <tscompile> models (the shape extract_npm.py / extract_bazel.py
produce). The load-bearing case is the `.d.ts` false positive: gulp-tsb runs
tsc with declaration=true internally (for incremental signature hashing) then
discards the .d.ts, so the instrumentation captures a .d.ts the build never
writes, while the Bazel side (plain tsc, no declaration) emits none. The differ
must treat .d.ts as noise so this does NOT read as an emit divergence -- but
must still REPORT that it stripped them.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import diff_ts


def _model(actions):
    return {"targets": {"<tscompile>": {"actions": actions}}}


def _act(src, outputs):
    return {"mnemonic": "TsCompile", "inputs": [src], "outputs": outputs}


def test_dts_only_on_npm_is_not_a_diff():
    # npm emits .js/.js.map/.d.ts; bazel emits .js/.js.map. The .d.ts is the
    # discarded declaration artifact -- must not surface as an ext diff.
    npm = _model([
        _act("src/a.ts", ["out/a.js", "out/a.js.map", "out/a.d.ts"]),
        _act("src/b.ts", ["out/b.js", "out/b.js.map", "out/b.d.ts"]),
    ])
    bz = _model([
        _act("src/a.ts", ["out-build/a.js", "out-build/a.js.map"]),
        _act("src/b.ts", ["out-build/b.js", "out-build/b.js.map"]),
    ])
    npm_only, bazel_only, ext_diffs, stripped = diff_ts.diff(npm, bz)
    assert npm_only == []
    assert bazel_only == []
    assert ext_diffs == [], ext_diffs
    # ...but the strip is reported: every npm source had a .d.ts suppressed.
    assert stripped == {"npm": 2, "bazel": 0}, stripped


def test_real_ext_diff_still_surfaces():
    # A genuine disagreement -- npm produced a .js.map bazel didn't -- must
    # still be caught after .d.ts stripping.
    npm = _model([_act("src/a.ts", ["out/a.js", "out/a.js.map", "out/a.d.ts"])])
    bz = _model([_act("src/a.ts", ["out-build/a.js"])])
    npm_only, bazel_only, ext_diffs, _ = diff_ts.diff(npm, bz)
    assert npm_only == [] and bazel_only == []
    assert len(ext_diffs) == 1
    assert "src/a.ts" in ext_diffs[0]
    assert ".js.map" in ext_diffs[0]
    # the ignored .d.ts must not appear in the reported extension sets
    assert ".d.ts" not in ext_diffs[0]


def test_source_only_on_one_side():
    npm = _model([
        _act("src/a.ts", ["out/a.js"]),
        _act("src/only_npm.ts", ["out/only_npm.js"]),
    ])
    bz = _model([
        _act("src/a.ts", ["out-build/a.js"]),
        _act("src/only_bazel.ts", ["out-build/only_bazel.js"]),
    ])
    npm_only, bazel_only, ext_diffs, _ = diff_ts.diff(npm, bz)
    assert npm_only == ["src/only_npm.ts"]
    assert bazel_only == ["src/only_bazel.ts"]
    assert ext_diffs == []


def test_compound_extension_tokens():
    # .js.map must read as one token, not ".map"; .d.ts as one token (and then
    # be stripped), not ".ts".
    assert diff_ts._extensions(["x.js.map"]) == {".js.map"}
    assert diff_ts._extensions(["x.js"]) == {".js"}
    # .d.ts is recognized (compound) AND stripped -> empty significant set.
    assert diff_ts._extensions(["x.d.ts"]) == set()
    assert diff_ts._extensions(["x.js", "x.js.map", "x.d.ts"]) == {".js", ".js.map"}


def test_same_source_multiple_actions_union():
    # A source compiled in several actions (e.g. synthetic file.ts) unions its
    # extensions; a .d.ts in any of them is stripped but still counted.
    npm = _model([
        _act("src/dup.ts", ["out/dup.js"]),
        _act("src/dup.ts", ["out/dup.js.map", "out/dup.d.ts"]),
    ])
    bz = _model([_act("src/dup.ts", ["out-build/dup.js", "out-build/dup.js.map"])])
    npm_only, bazel_only, ext_diffs, stripped = diff_ts.diff(npm, bz)
    assert npm_only == [] and bazel_only == []
    assert ext_diffs == [], ext_diffs
    assert stripped["npm"] == 1


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
