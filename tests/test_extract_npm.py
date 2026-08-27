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

"""Tests for the npm frontend (instrumented build NDJSON -> action IR).

Synthetic NDJSON fixtures matching the schema preload.mjs emits, so no node
dependency. Covers the three mnemonics the preload produces today
(EsbuildBundle, EsbuildTransform, Spawn) plus the role classification.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import extract_npm
from model import BuildSystem, TargetRole


def _ndjson(records):
    return "".join(json.dumps(r) + "\n" for r in records)


def _write(tmp, records):
    p = os.path.join(tmp, "actions.ndjson")
    with open(p, "w") as f:
        f.write(_ndjson(records))
    return p


def test_esbuild_bundle_action_captured():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            {
                "mnemonic": "EsbuildBundle",
                "arguments": ["--bundle=true", "--format=esm",
                              "--plugins=css-external,file-content-mapper"],
                "inputs": ["src/vs/workbench/workbench.desktop.main.ts",
                           "src/vs/base/common/event.ts"],
                "outputs": ["out/vs/workbench/workbench.desktop.main.js"],
            },
        ])
        m = extract_npm.extract(path, "/repo")
        assert m.build_system == BuildSystem.NPM
        # target keyed on first output -- stable across runs of the same entry
        name = "out/vs/workbench/workbench.desktop.main.js"
        assert name in m.targets, list(m.targets.keys())
        t = m.targets[name]
        assert t.role == TargetRole.PRODUCTION
        assert len(t.actions) == 1
        act = t.actions[0]
        assert act.mnemonic == "EsbuildBundle"
        # plugin names survive in argv, plugin closures don't
        assert any("--plugins=" in a for a in act.arguments)
        # inputs/outputs are the resolved annotation -- straight from metafile
        assert act.inputs == ("src/vs/workbench/workbench.desktop.main.ts",
                              "src/vs/base/common/event.ts")
        assert act.outputs == ("out/vs/workbench/workbench.desktop.main.js",)


def test_bundle_target_name_uses_entry_point_not_first_output():
    # vscode's workbench bundle includes ~20 static assets (svg, ttf, etc.) in
    # its esbuild metafile alongside the entry-point .js. Sorted lexically the
    # SVGs come first, so naming the target after outputs[0] used to label the
    # whole bundle "media/apple-dark.svg". The entryPoints `out` field is what
    # the build system was asked to produce -- use that.
    entry_arg = (
        '--entryPoints=[{"in":"/repo/out-build/vs/workbench/'
        'workbench.desktop.main.js","out":"vs/workbench/workbench.desktop.main"}]'
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            {"mnemonic": "EsbuildBundle",
             "arguments": [entry_arg,
                           "--outdir=/repo/out-build"],
             "inputs": [],
             "outputs": [
                "/repo/out-build/media/apple-dark.svg",
                "/repo/out-build/media/apple-light.svg",
                "/repo/out-build/vs/workbench/workbench.desktop.main.js",
             ]},
        ])
        m = extract_npm.extract(path, "/repo")
        expected = "vs/workbench/workbench.desktop.main.js"
        assert expected in m.targets, list(m.targets.keys())
        # asset path must not have become its own target
        assert "out-build/media/apple-dark.svg" not in m.targets


def test_esbuild_transform_pools_into_one_synthetic_target():
    # The per-file transpile pass is N esbuild.transform calls. They group
    # under a single <transform> target -- the Java-compile-group analog --
    # rather than one target per file (which would explode the model).
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            {"mnemonic": "EsbuildTransform",
             "arguments": ["--loader=ts", "--target=es2024",
                           "--sourcefile=src/a.ts"],
             "inputs": ["src/a.ts"], "outputs": []},
            {"mnemonic": "EsbuildTransform",
             "arguments": ["--loader=ts", "--target=es2024",
                           "--sourcefile=src/b.ts"],
             "inputs": ["src/b.ts"], "outputs": []},
        ])
        m = extract_npm.extract(path, "/repo")
        assert "<transform>" in m.targets
        actions = m.targets["<transform>"].actions
        assert len(actions) == 2
        srcs = {a.inputs[0] for a in actions}
        assert srcs == {"src/a.ts", "src/b.ts"}


def test_spawn_dashboard_classification():
    # tsgo / eslint / stylelint produce no shippable artifact -> DASHBOARD,
    # so they show up under `excluded` rather than getting diffed.
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            {"mnemonic": "Spawn",
             "arguments": ["/abs/node_modules/.bin/tsgo",
                           "--project", "src/tsconfig.json", "--noEmit"]},
            {"mnemonic": "Spawn",
             "arguments": ["/abs/node_modules/.bin/eslint", "src"]},
            {"mnemonic": "Spawn",
             "arguments": ["/usr/bin/node", "scripts/something.js"]},
        ])
        m = extract_npm.extract(path, "/repo")
        # Targets are basenames; tsgo/eslint -> DASHBOARD, node -> UNKNOWN
        assert m.targets["tsgo"].role == TargetRole.DASHBOARD
        assert m.targets["eslint"].role == TargetRole.DASHBOARD
        assert m.targets["node"].role == TargetRole.UNKNOWN


def test_test_role_inferred_from_output_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            {"mnemonic": "EsbuildBundle",
             "arguments": [],
             "inputs": [],
             "outputs": ["out/vs/base/test/common/buffer.test.js"]},
        ])
        m = extract_npm.extract(path, "/repo")
        t = m.targets["out/vs/base/test/common/buffer.test.js"]
        assert t.role == TargetRole.TEST


def test_tscompile_pools_into_one_synthetic_target():
    # gulp-tsb's full TS compile fires getEmitOutput once per source file; the
    # preload wraps it and emits a TsCompile per call. They aggregate under
    # <tscompile> the same way EsbuildTransform calls aggregate under
    # <transform>.
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            {"mnemonic": "TsCompile",
             "arguments": ["--target=99", "--module=99", "--outDir=out"],
             "inputs": ["/repo/src/a.ts"],
             "outputs": ["/repo/out/a.js", "/repo/out/a.d.ts"]},
            {"mnemonic": "TsCompile",
             "arguments": ["--target=99", "--module=99", "--outDir=out"],
             "inputs": ["/repo/src/b.ts"],
             "outputs": ["/repo/out/b.js", "/repo/out/b.d.ts"]},
        ])
        m = extract_npm.extract(path, "/repo")
        assert "<tscompile>" in m.targets, list(m.targets.keys())
        t = m.targets["<tscompile>"]
        assert t.role == TargetRole.PRODUCTION
        assert len(t.actions) == 2
        # Paths still get normalized against repo_root.
        assert t.actions[0].inputs == ("src/a.ts",)
        assert t.actions[0].outputs == ("out/a.js", "out/a.d.ts")


def test_paths_are_made_repo_relative():
    # The preload emits absolute paths (esbuild's metafile + the live argv).
    # The extractor must normalize anything under repo_root so models are
    # portable across checkouts -- inputs, outputs, target name (when derived
    # from outputs), AND embedded path args like --sourcefile=.
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            {"mnemonic": "EsbuildBundle",
             "arguments": ["--bundle=true",
                           "--entryPoints=/repo/src/main.ts",
                           "--sourcefile=/repo/src/main.ts"],
             "inputs": ["/repo/src/main.ts", "/repo/src/lib/a.ts"],
             "outputs": ["/repo/out/main.js"]},
            {"mnemonic": "EsbuildTransform",
             "arguments": ["--sourcefile=/repo/src/x.ts"],
             "inputs": ["/repo/src/x.ts"], "outputs": []},
        ])
        m = extract_npm.extract(path, "/repo")
        # Target name is derived from outputs and must also be rel.
        assert "out/main.js" in m.targets, list(m.targets.keys())
        bundle = m.targets["out/main.js"].actions[0]
        assert bundle.inputs == ("src/main.ts", "src/lib/a.ts")
        assert bundle.outputs == ("out/main.js",)
        assert "--entryPoints=src/main.ts" in bundle.arguments
        assert "--sourcefile=src/main.ts" in bundle.arguments
        # Same treatment for transform.
        tr = m.targets["<transform>"].actions[0]
        assert tr.inputs == ("src/x.ts",)
        assert "--sourcefile=src/x.ts" in tr.arguments


def test_partial_nlast_line_does_not_break_extract():
    # The build can be killed mid-flush -- a half-written final line must not
    # take down the whole extract. Earlier valid records still land.
    with tempfile.TemporaryDirectory() as tmp:
        good = json.dumps({"mnemonic": "EsbuildBundle", "arguments": [],
                           "inputs": [], "outputs": ["out/a.js"]})
        with open(os.path.join(tmp, "a.ndjson"), "w") as f:
            f.write(good + "\n{not json\n")
        m = extract_npm.extract(os.path.join(tmp, "a.ndjson"), "/repo")
        assert "out/a.js" in m.targets


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
