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

"""Tests for stamp_scope (what a workspace-status change rebuilds)."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from stamp_scope import main, render, stamp_scope


class Graph:
    """A small aquery container, built by naming files and actions."""

    def __init__(self):
        self.c = {"artifacts": [], "actions": [], "targets": [], "depSetOfFiles": [],
                  "pathFragments": []}
        self._art = {}
        self._tgt = {}

    def art(self, path):
        if path not in self._art:
            n = len(self._art) + 1
            # One fragment per file is enough: the script reads whole paths.
            self.c["pathFragments"].append({"id": n, "label": path})
            self.c["artifacts"].append({"id": n, "pathFragmentId": n})
            self._art[path] = n
        return self._art[path]

    def depset(self, files=(), transitive=()):
        n = len(self.c["depSetOfFiles"]) + 1
        d = {"id": n}
        if files:
            d["directArtifactIds"] = [self.art(f) for f in files]
        if transitive:
            d["transitiveDepSetIds"] = list(transitive)
        self.c["depSetOfFiles"].append(d)
        return n

    def action(self, mnemonic, target, inputs, outputs, depsets=()):
        if target not in self._tgt:
            self._tgt[target] = len(self._tgt) + 1
            self.c["targets"].append({"id": self._tgt[target], "label": target})
        outs = [self.art(o) for o in outputs]
        ds = list(depsets) + ([self.depset(inputs)] if inputs else [])
        a = {"mnemonic": mnemonic, "targetId": self._tgt[target], "inputDepSetIds": ds,
             "outputIds": outs, "primaryOutputId": outs[0]}
        if mnemonic == "CppCompile":
            a["discoversInputs"] = True      # as aquery reports for C++ compiles
        self.c["actions"].append(a)


STATUS = "bazel-out/stable-status.txt"
VOLATILE = "bazel-out/volatile-status.txt"


def narrow():
    """The revision goes into one generated source file, as a reference build does."""
    g = Graph()
    g.action("Genrule", "//:gen_rev", ["rev.cpp.in", STATUS], ["bin/rev.cpp"])
    g.action("CppCompile", "//:lib", ["bin/rev.cpp", "src/a.h"], ["bin/rev.o"])
    for s in ("a", "b", "c"):
        g.action("CppCompile", "//:lib", [f"src/{s}.cpp", "src/a.h"], [f"bin/{s}.o"])
    g.action("CppArchive", "//:lib", ["bin/rev.o", "bin/a.o", "bin/b.o", "bin/c.o"], ["bin/lib.a"])
    g.action("CppLink", "//:app", ["bin/lib.a"], ["bin/app"])
    return g.c


def wide():
    """The revision goes into a header that reaches every compile through a shared depSet."""
    g = Graph()
    g.action("Genrule", "//:gen_version_h", ["version.h.in", STATUS], ["bin/version.h"])
    headers = g.depset(["bin/version.h", "src/a.h"])
    shared = g.depset(transitive=[headers])          # reached through a transitive depSet
    for s in ("a", "b", "c", "d"):
        g.action("CppCompile", "//:lib", [f"src/{s}.cpp"], [f"bin/{s}.o"], depsets=[shared])
    g.action("CppArchive", "//:lib", ["bin/a.o", "bin/b.o", "bin/c.o", "bin/d.o"], ["bin/lib.a"])
    g.action("CppLink", "//:app", ["bin/lib.a"], ["bin/app"])
    return g.c


def header_chain():
    """The revision reaches a header at one remove: a rule copies it out of the stamped source."""
    g = Graph()
    g.action("Genrule", "//:gen_rev", ["rev.cpp.in", STATUS], ["bin/rev.cpp"])
    g.action("Genrule", "//:version_h", ["bin/rev.cpp"], ["bin/version.h"])
    g.action("CppCompile", "//:lib", ["bin/rev.cpp"], ["bin/rev.o"])
    for s in ("a", "b", "c"):
        g.action("CppCompile", "//:lib", [f"src/{s}.cpp", "bin/version.h"], [f"bin/{s}.o"])
    return g.c


def generator_that_cannot_prune():
    """A stamped header listed as an input of code generators, as Qt's moc lists every header."""
    g = Graph()
    g.action("Genrule", "//:gen_version_h", ["version.h.in", STATUS], ["bin/version.h"])
    for s in ("a", "b", "c"):
        g.action("QtMoc", "//:lib", [f"src/{s}.h", "bin/version.h"], [f"bin/moc_{s}.cpp"])
        g.action("CppCompile", "//:lib", [f"bin/moc_{s}.cpp"], [f"bin/moc_{s}.o"])
    g.action("CppCompile", "//:lib", ["src/about.cpp", "bin/version.h"], ["bin/about.o"])
    return g.c


def test_narrow_stamp_is_one_compile_and_what_links_it():
    r = stamp_scope(narrow())
    assert [s["target"] for s in r["stamped"]] == ["//:gen_rev"]
    assert r["will_rerun"] == 1 and r["will_rerun_by_mnemonic"] == {"CppCompile": 1}
    assert r["will_rerun_examples"][0]["output"] == "bin/rev.o"
    assert r["may_rerun"] == 0
    assert r["downstream_by_mnemonic"] == {"CppCompile": 1, "CppArchive": 1, "CppLink": 1}
    assert r["actions"] == 7


def test_stamped_header_may_reach_every_compile_that_can_see_it():
    r = stamp_scope(wide())
    assert r["will_rerun"] == 0          # a compile reruns only if it included the header
    assert r["may_rerun"] == 4
    assert r["downstream"] == 6          # four compiles, the archive, the link


def test_header_generated_from_a_stamped_file_counts_its_possible_includers():
    r = stamp_scope(header_chain())
    assert r["will_rerun_by_mnemonic"] == {"Genrule": 1, "CppCompile": 1}
    assert r["may_rerun"] == 3


def test_generator_that_cannot_prune_will_rerun_whether_or_not_it_uses_the_header():
    r = stamp_scope(generator_that_cannot_prune())
    assert r["will_rerun_by_mnemonic"] == {"QtMoc": 3}
    assert r["may_rerun"] == 1           # about.cpp; the moc outputs' compiles are downstream only
    assert r["downstream_by_mnemonic"] == {"QtMoc": 3, "CppCompile": 4}


def test_no_stamping_reports_none():
    g = Graph()
    g.action("CppCompile", "//:lib", ["src/a.cpp"], ["bin/a.o"])
    r = stamp_scope(g.c)
    assert r["stamped"] == [] and r["downstream"] == 0 and r["will_rerun"] == 0
    assert "none" in render(r)


def test_volatile_only_reader_is_listed_and_not_followed():
    # Bazel does not rerun an action because the volatile file alone changed.
    g = Graph()
    g.action("Genrule", "//:gen_date", ["date.in", VOLATILE], ["bin/date.cpp"])
    g.action("CppCompile", "//:lib", ["bin/date.cpp"], ["bin/date.o"])
    r = stamp_scope(g.c)
    assert r["stamped"] == []
    assert [v["target"] for v in r["volatile_only"]] == ["//:gen_date"]
    assert r["will_rerun"] == 0 and r["downstream"] == 0


def test_action_reading_both_status_files_counts_as_stamped():
    g = Graph()
    g.action("Genrule", "//:gen", [STATUS, VOLATILE], ["bin/rev.cpp"])
    r = stamp_scope(g.c)
    assert len(r["stamped"]) == 1 and r["volatile_only"] == []


def test_shared_depset_reached_by_two_paths_is_counted_once():
    g = Graph()
    g.action("Genrule", "//:gen", [STATUS], ["bin/v.h"])
    leaf = g.depset(["bin/v.h"])
    left, right = g.depset(transitive=[leaf]), g.depset(transitive=[leaf])
    g.action("CppCompile", "//:lib", ["src/a.cpp"], ["bin/a.o"], depsets=[left, right])
    assert stamp_scope(g.c)["may_rerun"] == 1


def test_render_says_to_measure_when_a_header_is_stamped():
    assert "measure" in render(stamp_scope(wide()))
    assert "measure" not in render(stamp_scope(narrow()))


def test_cli_fail_over_and_json(tmp_path, capsys):
    p = tmp_path / "aquery.json"
    p.write_text(json.dumps(generator_that_cannot_prune()))
    assert main([str(p), "--fail-over", "1"]) == 1
    assert "3 actions will rerun" in capsys.readouterr().err
    assert main([str(p), "--fail-over", "3"]) == 0
    capsys.readouterr()
    assert main([str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["will_rerun"] == 3
    p.write_text(json.dumps(narrow()))
    assert main([str(p), "--fail-over", "1"]) == 0
