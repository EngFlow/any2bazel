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

"""The vcpkg dependency edge: what the build asks for vs. what the shim declares.

Ring 2 moved Ladybird's 34 external deps off the CMake reference tree
(Build/full/vcpkg_installed/...) and onto the tree Bazel builds itself
(//:vcpkg_installed, consumed through //Meta/vcpkg:<port>). The interesting
failure mode is not "does it build" -- it built -- but the two kinds of silent
drift that swap makes possible:

  1. a dep label the emitters produce that the shim package does not declare.
     Bazel catches that as a dangling label, but only for the configuration you
     happen to build; and the emitters are what a future dep bump re-runs.
  2. a *lingering* reference to the CMake tree. That is the one that hides: the
     reference tree exists on this machine, so a leftover -isystem or -L into it
     keeps working locally and only fails for the person cloning the repo, which
     is the entire point of the exercise ("can I build it on my machine?").

So these tests read the checked-in artifacts and assert the two halves agree.
The removal test (move the CMake tree aside, rebuild clean, compare the rendered
layout tree) is the real proof; this is the cheap regression guard for it.
"""

import os
import re

_WS = os.path.join(os.path.dirname(__file__), "..", "examples", "ladybird",
                   "workspace")

# Every file that participates in the dependency edge.
_BUILD_FILES = [
    "BUILD.bazel",
    "Libraries/LibWeb/BUILD.bazel",
    "codegen_root.bzl",
    "bazelrc.txt",
]


def _read(rel):
    with open(os.path.join(_WS, rel)) as f:
        return f.read()


def _shim_targets():
    """The port names //Meta/vcpkg declares a target for."""
    txt = _read("Meta/vcpkg/BUILD.bazel")
    names = set(re.findall(r'name = "([^"]+)"', txt))
    # The SHARED list comprehension generates one target per name in it.
    names |= set(re.findall(r'"([A-Za-z0-9_]+)"',
                            txt.split("SHARED = [", 1)[1].split("]", 1)[0]))
    return names


def _requested_ports():
    """The vcpkg ports the emitted BUILD files depend on.

    Two spellings, both real: the literal label //Meta/vcpkg:<port>, and
    `VCPKG + ":<port>"` in AK's hand-written block (VCPKG is the package
    constant the emitter defines). Matching only the first spelling would have
    quietly excused fmt/simdutf/mimalloc from every check below.
    """
    ports = set()
    for rel in _BUILD_FILES:
        txt = _read(rel)
        ports |= set(re.findall(r"//Meta/vcpkg:([A-Za-z0-9_]+)", txt))
        ports |= set(re.findall(r'VCPKG \+ ":([A-Za-z0-9_]+)"', txt))
    return ports


def test_every_requested_port_is_declared():
    """A dep label with no target behind it is a build that breaks on clone."""
    missing = _requested_ports() - _shim_targets()
    assert not missing, "requested but not declared by //Meta/vcpkg: %s" % sorted(missing)


def test_the_shim_declares_every_dep_the_build_asks_for():
    """Pins the count, so a silently-dropped port shows up as a test failure.

    41 targets in the shim, 41 asked for by label -- they match exactly, which
    is the useful state: no port declared that nothing uses, none used that
    nothing declares. (Writing this test is what turned up that AK's four deps
    use a different label spelling; see _requested_ports.)
    """
    assert len(_requested_ports()) == 41, sorted(_requested_ports())
    assert _requested_ports() == _shim_targets()


def test_no_build_file_still_reads_the_cmake_vcpkg_tree():
    """The host escape this whole task existed to remove.

    Deliberately checks the .bazelrc too: the -isystem / -L / -rpath-link /
    -rpath into Build/full were global flags, i.e. invisible at the target that
    depended on them, which is exactly why they survived so long.
    """
    offenders = [rel for rel in _BUILD_FILES
                 if "Build/full/vcpkg_installed" in _read(rel)]
    assert not offenders, "still reference the CMake vcpkg tree: %s" % offenders


def test_vcpkg_include_dirs_ride_on_the_dep_edge_not_a_global_flag():
    """The non-root include dirs (skia, harfbuzz, libxml2) must be declared.

    CMake passed these as per-target -isystem and the first Bazel port copied
    that; now they are `include_dirs` on the port that owns them, so a TU that
    includes <skia/...> has to depend on skia. If they came back as copts, the
    undeclared-include hole would be back with them.
    """
    shim = _read("Meta/vcpkg/BUILD.bazel")
    for sub, port in (("include/skia", "skia"),
                      ("include/harfbuzz", "harfbuzz"),
                      ("include/libxml2", "xml2")):
        assert '"%s"' % sub in shim, "%s not declared by any port" % sub
        # and the port that declares it is the one named after it
        block = shim.split('name = "%s"' % port, 1)
        assert len(block) == 2, "no %s target" % port

    for rel in ("BUILD.bazel", "Libraries/LibWeb/BUILD.bazel", "bazelrc.txt"):
        txt = _read(rel)
        assert "vcpkg_installed/x64-linux-dynamic/include" not in txt, rel


def test_the_tree_is_built_once_for_both_configurations():
    """cfg = "exec" on vcpkg_lib's tree attr, and a transition rule for genrules.

    Without the pin, the target config and the exec config each get their own
    copy of a 45-minute build whose output is byte-identical -- and a genrule
    tool linked against the exec copy cannot find the target copy at the rpath
    baked into it. Cheap to assert, expensive to rediscover.
    """
    bzl = _read("vcpkg.bzl")
    lib = bzl.split("vcpkg_lib = rule(", 1)[1].split("\n)", 1)[0]
    assert 'cfg = "exec"' in lib, "vcpkg_lib's tree attr is not pinned to exec"
    assert "vcpkg_tree_for_exec" in bzl
    # The genrule that RUNS an exec-config tool takes the transitioned target.
    assert "//:vcpkg_installed_exec" in _read("codegen_root.bzl")


def test_static_libs_go_on_the_link_line_by_path():
    """-lwoff2dec would prefer a .so; only the .a exists, and -l would find the
    system copy if one were installed. Path beats name here."""
    bzl = _read("vcpkg.bzl")
    impl = bzl.split("def _vcpkg_lib_impl", 1)[1].split("\nvcpkg_lib = rule", 1)[0]
    assert 'root + "/lib/lib" + n + ".a"' in impl
    # and the shared ones are -l, so the loader's SONAME lookup keeps working
    assert 'flags.append("-l" + n)' in impl
