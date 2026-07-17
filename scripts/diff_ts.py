"""Diff the `<tscompile>` target between an npm model and a Bazel model.

MVP -- not integrated with the C++/Java diff.py pipeline. Compares the two
per-file (source -> outputs) maps that extract_npm.py and extract_bazel.py
produce and reports:

  * sources compiled on only one side,
  * sources whose emitted-file EXTENSIONS differ (.js/.js.map/.d.ts).

Absolute path prefixes differ between the two frontends (npm's IR has TS-
internal `out/vs/vs/...` paths, Bazel's IR has `out-build/vs/...`), so we key
outputs by their extension suffix only. That's the coarsest useful match --
enough to say "did both build systems agree on what to produce from src/X.ts?"
without conflating outDir naming with actual emit disagreement.

Usage:
    python3 diff_ts.py <npm.model.json> <bazel.model.json>
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List, Set, Tuple


def _extensions(paths) -> Set[str]:
    """Return the set of significant extensions in a bag of output paths.

    We treat `.js.map` and `.d.ts` as single-token extensions rather than
    ".map" / ".ts" -- otherwise the diff would report every side as producing
    ".ts" and hide the actual difference.
    """
    out: Set[str] = set()
    for p in paths:
        for ext in (".js.map", ".d.ts"):
            if p.endswith(ext):
                out.add(ext)
                break
        else:
            _, dot, rest = p.rpartition(".")
            if dot:
                out.add("." + rest)
    return out


def _by_source(model: dict) -> Dict[str, Set[str]]:
    """Build `source -> emitted extensions` for the `<tscompile>` target.

    Sources that appear more than once (e.g. the MonacoGenerator's synthetic
    `file.ts` fires 45x in the npm build) are merged: the union of extensions
    across all calls for that source is what the source's build produces.
    """
    result: Dict[str, Set[str]] = {}
    t = model.get("targets", {}).get("<tscompile>", {})
    for a in t.get("actions", []):
        for inp in a.get("inputs", []):
            result.setdefault(inp, set()).update(_extensions(a.get("outputs", [])))
    return result


def diff(npm_model: dict, bazel_model: dict) -> Tuple[List[str], List[str], List[str]]:
    npm = _by_source(npm_model)
    bz = _by_source(bazel_model)
    npm_only = sorted(set(npm) - set(bz))
    bazel_only = sorted(set(bz) - set(npm))
    ext_diffs: List[str] = []
    for src in sorted(set(npm) & set(bz)):
        if npm[src] != bz[src]:
            npm_ext = ",".join(sorted(npm[src]))
            bz_ext = ",".join(sorted(bz[src]))
            ext_diffs.append(f"{src}\tnpm={{{npm_ext}}}\tbazel={{{bz_ext}}}")
    return npm_only, bazel_only, ext_diffs


def main(argv):
    if len(argv) != 3:
        sys.exit("usage: diff_ts.py <npm.model.json> <bazel.model.json>")
    with open(argv[1]) as f:
        npm_model = json.load(f)
    with open(argv[2]) as f:
        bazel_model = json.load(f)
    npm_only, bazel_only, ext_diffs = diff(npm_model, bazel_model)
    print(f"sources npm-only:   {len(npm_only)}")
    print(f"sources bazel-only: {len(bazel_only)}")
    print(f"sources w/ ext diff: {len(ext_diffs)}")
    if npm_only[:5]:
        print("\nfirst 5 npm-only sources:")
        for s in npm_only[:5]:
            print(f"  {s}")
    if bazel_only[:5]:
        print("\nfirst 5 bazel-only sources:")
        for s in bazel_only[:5]:
            print(f"  {s}")
    if ext_diffs[:5]:
        print("\nfirst 5 extension diffs:")
        for d in ext_diffs[:5]:
            print(f"  {d}")


if __name__ == "__main__":
    main(sys.argv)
