"""Extract a CanonicalModel from a Maven module's FORKED javac argument files.

Maven has no action graph like Bazel aquery, and its compiler plugin doesn't
expose a resolved command line through the File-API-style channels CMake has.
The faithful source is the **forked compiler argument file**: run

    mvn clean compile -Dmaven.compiler.fork=true

and the maven-compiler-plugin writes the exact javac argv it launches to
    <module>/target/...JavacCompiler<digits>arguments
(one quoted token per line). This is the REAL command line, not a reconstruction
-- the decided "capture, don't synthesize" choice. A module may emit MULTIPLE
argfiles (e.g. a multi-release jar compiles META-INF/versions/N separately);
each becomes its own JavaCompile Action.

First cut (by decision): ARGV FLOOR ONLY. We store the raw argv as the action;
the classpath stays as m2 jar PATHS inside it. Coordinate-identity dep
annotations (group:artifact:version, scope) are deferred until the differ needs
them -- like a raw-Bazel frontend that only has argv.

Usage:
    python3 extract_maven.py <module_dir> <repo_root> <out.json>
        # reads <module_dir>/target/*arguments  (run the forked compile first)
"""

from __future__ import annotations

import glob
import os
import sys
from typing import List

from model import (Action, BuildSystem, CanonicalModel, Target, TargetKind,
                   TargetRole)
from serialize import dump_model

_ARGFILE_GLOB = "*JavacCompiler*arguments"


def _parse_argfile(path: str) -> List[str]:
    """One quoted token per line -> argv. The maven-compiler-plugin double-quotes
    every token (even multi-word ones like the -Xplugin spec), so stripping the
    surrounding quotes is sufficient and faithful."""
    argv: List[str] = []
    with open(path) as f:
        for line in f:
            tok = line.rstrip("\n")
            if not tok:
                continue
            if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
                tok = tok[1:-1]
            argv.append(tok)
    return argv


def _module_artifact_id(module_dir: str) -> str:
    """Best-effort module name: the <artifactId> from the module's pom.xml, else
    the directory name. (No XML dep -- a cheap scan; the action argv is the
    faithful part, the target name is just an identity handle.)"""
    pom = os.path.join(module_dir, "pom.xml")
    if os.path.isfile(pom):
        import re
        text = open(pom).read()
        # the module's own artifactId is the first <artifactId> NOT inside
        # <parent>...</parent>; strip the parent block then take the first.
        no_parent = re.sub(r"<parent>.*?</parent>", "", text, flags=re.DOTALL)
        m = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", no_parent)
        if m:
            return m.group(1)
    return os.path.basename(os.path.abspath(module_dir))


def extract(module_dir: str, repo_root: str) -> CanonicalModel:
    model = CanonicalModel(build_system=BuildSystem.MAVEN, repo_root=repo_root)
    target_dir = os.path.join(module_dir, "target")
    argfiles = sorted(glob.glob(os.path.join(target_dir, _ARGFILE_GLOB)))
    if not argfiles:
        raise FileNotFoundError(
            f"no {_ARGFILE_GLOB} in {target_dir}; run "
            f"'mvn clean compile -Dmaven.compiler.fork=true' first")

    name = _module_artifact_id(module_dir)
    # A Maven module's compile output is a jar; model it as one target carrying a
    # JavaCompile action per argfile (multi-release jars produce several).
    target = Target(name=name, kind=TargetKind.STATIC, role=TargetRole.PRODUCTION)
    for af in argfiles:
        argv = _parse_argfile(af)
        if argv:
            target.actions.append(Action(mnemonic="JavaCompile",
                                         arguments=tuple(argv)))
    model.add(target)
    return model


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: extract_maven.py <module_dir> <repo_root> <out.json>")
    module_dir, repo_root, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
    dump_model(extract(module_dir, repo_root), out)
    print(f"wrote {out}")