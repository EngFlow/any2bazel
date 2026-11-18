#!/usr/bin/env python3
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

"""How much of the build is rebuilt when the workspace status changes.

A value read from the repository (a commit hash, `git describe`) reaches a
Bazel build through `--workspace_status_command`. Every action that reads
`bazel-out/stable-status.txt` reruns when a `STABLE_` value changes, and so
does every action that reads an output that changed. A commit hash changes on
every commit, so a stamp that lands in the wrong place is paid for on every
commit. This script finds the stamped actions in the action graph and sorts
what reads their outputs. It runs no build.

Usage:
    bazel aquery '//...' --output=jsonproto --noinclude_commandline > aquery.all.json
    python3 stamp_scope.py aquery.all.json [--json] [--fail-over N]

The aquery must be UNFILTERED: the mnemonic-filtered one extract_bazel.py
takes holds only compiles and links, so the stamped actions are not in it.

What it reports:

  WILL RERUN   Actions that read a stamped output directly and cannot skip
               it: the action does not prune its inputs (a code generator
               such as Qt's moc, a genrule, an archive), or the stamped output
               is a source file it compiles. This count is exact. One compile,
               for a version written into one generated source file, is what
               a narrow stamp looks like.
  MAY RERUN    Compiles that list a stamped HEADER, or a header generated from
               stamped data, among the headers they could include. Bazel
               reruns such a compile only if it did include the header, so
               this is an upper bound, and often a loose one: on CMake's own
               sources 8 to 10 compiles reran where up to 1052 could have.

and then everything downstream, also a bound, since an action whose output
comes out unchanged stops the chain.

To measure instead of bound: build, `git commit --allow-empty`, build again,
and read Bazel's "N processes" line; `git reset --soft HEAD~1` undoes the
commit. Compare with what the reference build recompiles after the same
commit. A reference that reads git only when it is configured recompiles
nothing.

Actions that read only `volatile-status.txt` are listed but not followed:
Bazel does not rerun an action because the volatile file alone changed.

`--fail-over N` exits 1 when more than N actions WILL RERUN, for use as a
check.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from typing import Dict, List, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_bazel import (_COMPILE, _JAVAC, _TSPROGRAM,  # noqa: E402
                           _build_path_index, _label_index)

STABLE = "stable-status.txt"
VOLATILE = "volatile-status.txt"
_COMPILES = _COMPILE | _JAVAC | _TSPROGRAM
# A stamped file with one of these extensions, read by an action that prunes
# its inputs, is a header that action may or may not include. Anything else it
# reads (its own source file, above all) it certainly uses.
_HEADER_EXTS = (".h", ".hh", ".hpp", ".hxx", ".inc", ".inl", ".def", ".tcc", ".ipp")


def stamp_scope(container: dict) -> dict:
    """Stamped actions and what is downstream of the stable-status readers.

    Works on the depSet DAG without expanding any action's input closure:
    a depSet is "tainted" once it holds a tainted artifact, directly or through
    a transitive depSet, and an action is downstream once one of its input
    depSets is tainted. Each depSet and action is visited once.
    """
    paths = _build_path_index(container)
    labels = _label_index(container)
    actions = container.get("actions", [])
    dsets = container.get("depSetOfFiles", [])

    holders: Dict[int, List[int]] = collections.defaultdict(list)   # artifact -> depSets holding it directly
    parents: Dict[int, List[int]] = collections.defaultdict(list)   # depSet -> depSets that include it
    for d in dsets:
        for aid in d.get("directArtifactIds", []):
            holders[aid].append(d["id"])
        for child in d.get("transitiveDepSetIds", []):
            parents[child].append(d["id"])
    consumers: Dict[int, List[int]] = collections.defaultdict(list)  # depSet -> action indexes
    for n, a in enumerate(actions):
        for dsid in a.get("inputDepSetIds", []):
            consumers[dsid].append(n)

    def readers(artifact_ids: Set[int]) -> Set[int]:
        """Indexes of the actions whose inputs include any of these artifacts."""
        seen_ds: Set[int] = set()
        stack = [ds for aid in artifact_ids for ds in holders.get(aid, [])]
        out: Set[int] = set()
        while stack:
            ds = stack.pop()
            if ds in seen_ds:
                continue
            seen_ds.add(ds)
            out.update(consumers.get(ds, []))
            stack.extend(parents.get(ds, []))
        return out

    status = {aid: os.path.basename(p) for aid, p in paths.items()
              if os.path.basename(p) in (STABLE, VOLATILE)}
    stable_ids = {aid for aid, name in status.items() if name == STABLE}
    volatile_ids = {aid for aid, name in status.items() if name == VOLATILE}
    stable_readers = readers(stable_ids)
    volatile_only = readers(volatile_ids) - stable_readers

    # Follow the stamped outputs through the actions that certainly read them.
    # `discoversInputs` marks an action Bazel prunes inputs for after it runs
    # (C++ compiles, from their .d files): such an action certainly reads its
    # own source, and only possibly reads a header. The first generation is
    # exact. Later ones hold if each output really changes; a header anywhere
    # along the way puts its possible includers in `may`.
    chain = set(stable_readers)
    frontier = set(stable_readers)
    will: List[int] = []
    may: Set[int] = set()
    first = True
    while frontier:
        outs = {oid for n in frontier for oid in actions[n].get("outputIds", [])}
        headers = {oid for oid in outs if paths.get(oid, "").endswith(_HEADER_EXTS)}
        reading = readers(outs) - chain
        reading_non_header = readers(outs - headers)
        certain = {n for n in reading
                   if not actions[n].get("discoversInputs") or n in reading_non_header}
        may |= reading - certain
        if first:
            will, first = sorted(certain), False
        chain |= certain
        frontier = certain
    # Later generations rerun only if an output changed, so they do not
    # excuse a compile from the list of possible includers.
    may -= set(will)

    # Everything downstream of the stable readers, one generation at a time.
    downstream: List[int] = []
    seen = set(stable_readers)
    frontier = set(stable_readers)
    while frontier:
        outs = {oid for n in frontier for oid in actions[n].get("outputIds", [])}
        nxt = readers(outs) - seen
        downstream.extend(sorted(nxt))
        seen |= nxt
        frontier = nxt

    def describe(n: int) -> dict:
        a = actions[n]
        return {"mnemonic": a.get("mnemonic", ""),
                "target": labels.get(a.get("targetId"), ""),
                "output": paths.get(a.get("primaryOutputId"), "")}

    def by_kind(ns) -> dict:
        return dict(collections.Counter(actions[n].get("mnemonic", "") for n in ns).most_common())

    return {
        "actions": len(actions),
        "stamped": [describe(n) for n in sorted(stable_readers)],
        "volatile_only": [describe(n) for n in sorted(volatile_only)],
        "will_rerun": len(will),
        "will_rerun_by_mnemonic": by_kind(will),
        "will_rerun_examples": [describe(n) for n in will[:20]],
        "may_rerun": len(may),
        "may_rerun_by_mnemonic": by_kind(sorted(may)),
        "downstream": len(downstream),
        "downstream_by_mnemonic": by_kind(downstream),
    }


def render(r: dict, limit: int = 8) -> str:
    out = [f"actions in the graph: {r['actions']}"]
    if not r["stamped"] and not r["volatile_only"]:
        out.append("stamped actions: none (nothing reads the workspace status)")
        return "\n".join(out)
    out.append(f"stamped actions, rerun when a STABLE_ value changes: {len(r['stamped'])}")
    for s in r["stamped"][:limit]:
        out.append(f"  {s['mnemonic']:<20} {s['target']:<30} -> {s['output']}")
    if len(r["stamped"]) > limit:
        out.append(f"  ... and {len(r['stamped']) - limit} more")
    if r["volatile_only"]:
        out.append(f"actions reading only {VOLATILE} (not rerun by a status change): {len(r['volatile_only'])}")

    def kinds(d: dict) -> str:
        return ", ".join(f"{v} {k}" for k, v in d.items()) or "none"

    out.append(f"reading a stamped output directly, WILL RERUN: {r['will_rerun']}  ({kinds(r['will_rerun_by_mnemonic'])})")
    for c in r["will_rerun_examples"][:limit]:
        out.append(f"  {c['mnemonic']:<20} {c['target']:<30} {c['output']}")
    if r["will_rerun"] > limit:
        out.append(f"  ... and {r['will_rerun'] - limit} more")
    out.append(f"could include a stamped header, MAY RERUN (upper bound): {r['may_rerun']}  ({kinds(r['may_rerun_by_mnemonic'])})")
    share = 100.0 * r["downstream"] / r["actions"] if r["actions"] else 0.0
    out.append(f"all downstream (upper bound): {r['downstream']} of {r['actions']} ({share:.1f}%)")
    if r["may_rerun"]:
        out.append("A stamped header: measure what a commit rebuilds (see --help) before trusting the bound.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("aquery", help="`bazel aquery '//...' --output=jsonproto`, unfiltered")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--fail-over", type=int, metavar="N", default=None,
                    help="exit 1 when more than N actions will rerun on a status change")
    args = ap.parse_args(argv)
    with open(args.aquery) as f:
        result = stamp_scope(json.load(f))
    print(json.dumps(result, indent=1) if args.json else render(result))
    if args.fail_over is not None and result["will_rerun"] > args.fail_over:
        print(f"FAIL: {result['will_rerun']} actions will rerun when the workspace status changes "
              f"(limit {args.fail_over})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
