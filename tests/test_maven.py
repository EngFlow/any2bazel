"""Tests for the Maven frontend (forked javac argfile -> action IR) and the
Java branch of reconstruct (JavaCompile -> CompileGroup).

Synthetic argfiles (the maven-compiler-plugin's quoted-token format), so no mvn
dependency. Proves a Java/Maven build flows through the same action model +
reconstruct framework as C/C++ -- the O(N) cross-language IR claim.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import extract_maven
from diff import diff_models, summarize
from model import (Action, BuildSystem, CanonicalModel, Target, TargetKind,
                   TargetRole)
from reconstruct import reconstruct


def _java_model(build_system, *src_lists):
    """A one-target Java model; each src_list is one JavaCompile action's argv
    (a -d + the sources), so we can vary grouping per side."""
    m = CanonicalModel(build_system=build_system, repo_root="/r")
    t = Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION)
    for srcs in src_lists:
        t.actions.append(Action(mnemonic="JavaCompile",
                                arguments=tuple(["-d", "/r/out"] + list(srcs))))
    m.add(t)
    return m

# the maven-compiler-plugin double-quotes every token, one per line
_ARGFILE = '\n'.join('"%s"' % t for t in [
    "-d", "/repo/mod/target/classes",
    "-classpath", "/repo/mod/target/classes:/home/.m2/repository/org/jspecify/"
                  "jspecify/1.0.0/jspecify-1.0.0.jar:",
    "-g", "-parameters",
    "-Xplugin:ErrorProne -Xep:Foo:ERROR",
    "/repo/mod/src/main/java/com/x/A.java",
    "/repo/mod/src/main/java/com/x/B.java",
]) + "\n"


def _write_module(root):
    mod = os.path.join(root, "mod")
    os.makedirs(os.path.join(mod, "target"))
    with open(os.path.join(mod, "pom.xml"), "w") as f:
        f.write("<project><parent><artifactId>parent-x</artifactId></parent>"
                "<artifactId>mymod</artifactId></project>")
    with open(os.path.join(mod, "target",
                           "org.codehaus.plexus.compiler.javac.JavacCompiler123arguments"),
              "w") as f:
        f.write(_ARGFILE)
    return mod


def test_maven_extracts_javacompile_action():
    with tempfile.TemporaryDirectory() as root:
        mod = _write_module(root)
        m = extract_maven.extract(mod, "/repo/mod")
        assert m.build_system == BuildSystem.MAVEN
        # module name from its own artifactId, not the parent's
        assert "mymod" in m.targets, m.targets.keys()
        acts = m.targets["mymod"].actions
        assert len(acts) == 1 and acts[0].mnemonic == "JavaCompile"
        # argv is the faithful captured command line
        assert "-classpath" in acts[0].arguments


def test_reconstruct_java_compile_group():
    with tempfile.TemporaryDirectory() as root:
        mod = _write_module(root)
        m = extract_maven.extract(mod, "/repo/mod")
        v = reconstruct(m)["mymod"]
        assert len(v.compile_groups) == 1
        g = v.compile_groups[0]
        # output dir is the group key, made repo-relative
        assert g.key == "target/classes", g.key
        # the two .java sources, repo-relative + sorted; flags split out
        assert g.sources == ("src/main/java/com/x/A.java",
                             "src/main/java/com/x/B.java"), g.sources
        assert "-g" in g.flags and "-classpath" in g.flags
        assert not any(s.endswith(".java") for s in g.flags)  # sources not in flags
        # C/C++ TU path stays empty for a Java target
        assert v.tus == []


def test_multi_release_two_argfiles_two_groups():
    with tempfile.TemporaryDirectory() as root:
        mod = _write_module(root)
        # a second argfile (multi-release jar: META-INF/versions/N)
        second = '\n'.join('"%s"' % t for t in [
            "-d", "/repo/mod/target/classes/META-INF/versions/9",
            "/repo/mod/src/main/java9/module-info.java",
        ]) + "\n"
        with open(os.path.join(mod, "target",
                  "org.codehaus.plexus.compiler.javac.JavacCompiler999arguments"),
                  "w") as f:
            f.write(second)
        m = extract_maven.extract(mod, "/repo/mod")
        assert len(m.targets["mymod"].actions) == 2
        groups = reconstruct(m)["mymod"].compile_groups
        keys = sorted(g.key for g in groups)
        assert keys == ["target/classes",
                        "target/classes/META-INF/versions/9"], keys


def test_java_source_set_converges_across_grouping_and_prefix():
    # Maven: 2 groups (main + multi-release), source-root prefix 'src/'.
    # Bazel: 1 group, prefix 'mod/src/main/java/'. Same logical sources ->
    # package-rooted keying aligns them; grouping difference is irrelevant.
    a = _java_model(BuildSystem.MAVEN,
                    ["/r/src/com/x/A.java", "/r/src/com/x/B.java"],
                    ["/r/src/module-info.java"])
    b = _java_model(BuildSystem.BAZEL,
                    ["/r/mod/src/main/java/com/x/A.java",
                     "/r/mod/src/main/java/com/x/B.java",
                     "/r/mod/src/main/java/module-info.java"])
    assert summarize(diff_models(a, b))["converged"], diff_models(a, b)


def test_missing_java_source_is_caught():
    # a .java compiled by A (maven) but absent on B (bazel) is a real error.
    a = _java_model(BuildSystem.MAVEN, ["/r/src/com/x/A.java", "/r/src/com/x/B.java"])
    b = _java_model(BuildSystem.BAZEL, ["/r/src/com/x/A.java"])
    discs = diff_models(a, b)
    assert any(d.kind == "missing_java_src" and d.tu.endswith("B.java")
               for d in discs), discs


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
