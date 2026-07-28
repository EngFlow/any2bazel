"""Diff the `<tscompile>` target between an npm model and a Bazel model.

MVP -- not integrated with the C++/Java diff.py pipeline. Compares the two
per-file (source -> outputs) maps that extract_npm.py and extract_bazel.py
produce and reports:

  * sources compiled on only one side,
  * sources whose emitted-file EXTENSIONS differ (.js/.js.map).

Absolute path prefixes differ between the two frontends (npm's IR has TS-
internal `out/vs/vs/...` paths, Bazel's IR has `out-build/vs/...`), so we key
outputs by their extension suffix only. That's the coarsest useful match --
enough to say "did both build systems agree on what to produce from src/X.ts?"
without conflating outDir naming with actual emit disagreement.

## Why `.d.ts` is ignored (differ-side canonicalization)

The npm side over-reports a `.d.ts` for EVERY source, but no `.d.ts` is
actually written to disk. gulp-tsb (build/lib/tsb/builder.ts) force-sets
`declaration = true` on the compiler settings so it can hash each file's
declaration signature for incremental rebuilds -- then DISCARDS the `.d.ts`
from its output stream unless the tsconfig genuinely requested declarations
(`if (!userWantsDeclarations) continue;`). The instrumentation shim wraps
`ts.getEmitOutput`, whose result still contains the `.d.ts` at that point
(before gulp-tsb drops it), so the captured action lists it. The Bazel side
runs plain `tsc` against a tsconfig with no `declaration`, so it never emits a
`.d.ts`. Verified on a real vscode build: 0 `.d.ts` on disk on either side,
7710 `.js` byte-identical.

`.d.ts` is therefore build-system NOISE in this parity target, not a real
artifact -- so it's stripped here, in the differ (the model stays a faithful
record of what `getEmitOutput` returned). This mirrors canonicalize.py's role
for the C/C++ path: hardcoded, universal noise stripped at diff time. The strip
is REPORTED (not silent): `diff()` returns how many sources had a `.d.ts`
suppressed, so an unexpected count is still visible. If a parity target ever
DOES request declarations on both sides, this constant is the one lever to
revisit.

Usage:
    python3 diff_ts.py <npm.model.json> <bazel.model.json>
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List, Set, Tuple

# Extensions that are not real emitted artifacts in this parity target and are
# stripped before comparison. See the module docstring for the full rationale.
_IGNORED_EXTS = frozenset({".d.ts"})

# Multi-dot extensions must be recognized as single tokens, else `.js.map`
# reads as `.map` and `.d.ts` as `.ts`, hiding real emit differences. Longest
# first so `.d.ts` wins over a naive `.ts` split.
_COMPOUND_EXTS = (".js.map", ".d.ts")


def _extensions(paths) -> Set[str]:
    """Return the set of significant, non-ignored extensions in a bag of output
    paths. Compound extensions (`.js.map`, `.d.ts`) are single tokens; ignored
    extensions (`.d.ts`) are dropped -- see the module docstring."""
    out: Set[str] = set()
    for p in paths:
        for ext in _COMPOUND_EXTS:
            if p.endswith(ext):
                matched = ext
                break
        else:
            _, dot, rest = p.rpartition(".")
            matched = ("." + rest) if dot else None
        if matched and matched not in _IGNORED_EXTS:
            out.add(matched)
    return out


def _had_ignored_ext(paths) -> bool:
    """True if any output path carries an ignored extension -- used to count
    (and thus surface) how many sources had a `.d.ts` stripped."""
    for p in paths:
        for ext in _IGNORED_EXTS:
            if p.endswith(ext):
                return True
    return False


def _by_source(model: dict) -> Tuple[Dict[str, Set[str]], int]:
    """Build `source -> emitted (non-ignored) extensions` for the `<tscompile>`
    target, plus a count of sources that had an ignored extension (`.d.ts`)
    stripped -- returned so the strip is reported, never silent.

    Sources that appear more than once (e.g. the MonacoGenerator's synthetic
    `file.ts` fires 45x in the npm build) are merged: the union of extensions
    across all calls for that source is what the source's build produces.
    """
    result: Dict[str, Set[str]] = {}
    stripped_srcs: Set[str] = set()
    t = model.get("targets", {}).get("<tscompile>", {})
    for a in t.get("actions", []):
        outs = a.get("outputs", [])
        exts = _extensions(outs)
        ignored = _had_ignored_ext(outs)
        for inp in a.get("inputs", []):
            result.setdefault(inp, set()).update(exts)
            if ignored:
                stripped_srcs.add(inp)
    return result, len(stripped_srcs)


def diff(npm_model: dict,
         bazel_model: dict) -> Tuple[List[str], List[str], List[str], Dict[str, int]]:
    npm, npm_stripped = _by_source(npm_model)
    bz, bz_stripped = _by_source(bazel_model)
    npm_only = sorted(set(npm) - set(bz))
    bazel_only = sorted(set(bz) - set(npm))
    ext_diffs: List[str] = []
    for src in sorted(set(npm) & set(bz)):
        if npm[src] != bz[src]:
            npm_ext = ",".join(sorted(npm[src]))
            bz_ext = ",".join(sorted(bz[src]))
            ext_diffs.append(f"{src}\tnpm={{{npm_ext}}}\tbazel={{{bz_ext}}}")
    stripped = {"npm": npm_stripped, "bazel": bz_stripped}
    return npm_only, bazel_only, ext_diffs, stripped


def main(argv):
    if len(argv) != 3:
        sys.exit("usage: diff_ts.py <npm.model.json> <bazel.model.json>")
    with open(argv[1]) as f:
        npm_model = json.load(f)
    with open(argv[2]) as f:
        bazel_model = json.load(f)
    npm_only, bazel_only, ext_diffs, stripped = diff(npm_model, bazel_model)
    print(f"sources npm-only:   {len(npm_only)}")
    print(f"sources bazel-only: {len(bazel_only)}")
    print(f"sources w/ ext diff: {len(ext_diffs)}")
    ignored = ",".join(sorted(_IGNORED_EXTS))
    print(f"sources w/ ignored ext ({ignored}) stripped: "
          f"npm={stripped['npm']} bazel={stripped['bazel']}")
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
