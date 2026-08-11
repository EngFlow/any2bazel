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


# ---------------------------------------------------------------------------
# Finding 36: what a fresh clone lacks. The cheap guards for the five things that
# made `git clone && bazel build` fail after Build/full was closed -- every one of
# them green on the machine that developed it, which is the whole problem.
# ---------------------------------------------------------------------------
def test_the_asset_script_resolves_absolute_paths():
    """The two path bugs that made every distfile lookup miss.

    vcpkg receives the index as an EXECROOT-RELATIVE path and invokes the
    asset-cache script from its OWN working directory, so both the index path and
    the paths inside it must be absolutized before vcpkg is exec'd. Fixing only one
    moves the failure down a line (`awk: cannot open` becomes `cp: cannot stat`),
    and either way vcpkg reports "no asset cache hits" and x-block-origin refuses
    the network -- a message that blames the pin rather than the path.

    It survived because the dev machine's vcpkg checkout already had
    downloads/tools/cmake-4.4.0-linux from an earlier `Meta/ladybird.py vcpkg` run,
    so vcpkg never asked the script for a tool at all.
    """
    sh = _read("Meta/vcpkg_build.sh")
    assert "EXECROOT" in sh, "the index path is still execroot-relative"
    # The index's VALUES too, not just its path: awk finding the row is half the job.
    assert 'root "/" $2' in sh, \
        "index entries are not absolutized, so cp runs from vcpkg's cwd"


def test_the_vcpkg_checkout_glob_is_the_finding_35_pattern_and_is_known():
    """//Build/vcpkg:tree globs a tree a fresh clone does not have, allow_empty.

    Deliberately NOT asserted as fixed -- it is not. This is the same
    `glob(["**"], allow_empty = True)` over a foreign tree finding 35 was about,
    one directory over: on a clone it matches exactly one file (its own
    BUILD.bazel) and reports nothing. What is pinned here is that the gap is
    DOCUMENTED, so nobody reads the top of the README and concludes a clone works.

    Making it allow_empty = False is the real fix and breaks the dev loop until
    Build/vcpkg is a git_repository (gap 7). A test saying "this is a known hole"
    is worth more than one pretending the hole is closed.
    """
    tree = _read("Build/vcpkg/BUILD.bazel")
    assert "allow_empty = True" in tree, \
        "if this is now False the gap is closed -- update this test and the README"
    # The .git exclusion is a LIE the no-sandbox action gets away with: vcpkg
    # resolves versioned ports with `git read-tree`, so the excluded tree is
    # load-bearing. Pinned so the contradiction stays visible.
    assert ".git/**" in tree
    readme = _example_readme()
    assert "Build/vcpkg" in readme, "the missing checkout is not documented"
    assert "read-tree" in readme, "the load-bearing .git is not documented"


def test_the_network_claim_is_scoped_to_vcpkgs_own_downloader():
    """`requires-network: "0"` enforces nothing, and one port calls pip.

    x-block-origin governs vcpkg's OWN downloader; the angle overlay-port runs
    `pip install ply` via x_vcpkg_get_python_packages, which is not a distfile and
    never reaches the pin. Nothing catches it either: `requires-network: "0"` is a
    scheduling hint, `no-sandbox: "1"` means there is no namespace to enforce it
    in, and `use_default_shell_env = True` hands the action this machine's
    HTTP_PROXY -- so pip silently succeeded for months.

    A control that is not enforced is indistinguishable from one that is not there
    -- finding 35's sentence about globs, applied to an execution_requirements key.
    Until ply is pinned, the guard is that the README scopes the claim honestly
    instead of repeating "zero network access".
    """
    bzl = _read("vcpkg.bzl")
    assert "no-sandbox" in bzl and "use_default_shell_env = True" in bzl, \
        "if either changed, re-derive whether the network is actually blocked"
    readme = _example_readme()
    assert "pip install ply" in readme, "the pip hole is not documented"
    assert "scheduling hint" in readme, \
        "requires-network is still presented as if it blocked the network"


def test_the_hsts_download_is_pinned_downstream_and_documented():
    """The input upstream fetches unpinned, pinned on our side instead.

    CMake downloads the HSTS preload table from Chromium's `main` at configure time
    -- unversioned -- and that is upstream's code, which we do not control. So the
    overlay pins it DOWNSTREAM: an http_file at an immutable commit + sha256, which
    the generator genrule consumes instead of the configure's leftovers under
    Build/caches. Three things have to hold together or the pin is decoration:
    the pin exists with a full commit sha (not `main`), the genrule reads it, and
    the reason it is a commit rather than a release tag is written down (a tag
    serves a different table, so pinning one would trade hermeticity for parity).
    """
    pin = _read("hsts_preload.bzl")
    assert re.search(r'HSTS_PRELOAD_COMMIT = "[0-9a-f]{40}"', pin), \
        "the HSTS pin must name a full commit sha"
    assert re.search(r'HSTS_PRELOAD_SHA256 = "[0-9a-f]{64}"', pin)
    assert "/main/net/http/" not in pin, "the pinned URL must not track main"

    codegen = _read("codegen_root.bzl")
    assert "@hsts_preload_json//file" in codegen, \
        "the generator genrule must consume the pinned file"
    assert "Build/caches/HSTSPreload" not in codegen, \
        "the unpinned CMake download path must be gone"

    readme = _example_readme()
    assert "hsts_preload.bzl" in readme and "unversioned" in readme.lower(), \
        "why the HSTS table needed a downstream pin is not documented"


def _example_readme():
    with open(os.path.join(_WS, "..", "README.md")) as f:
        return f.read()

def test_the_pip_installed_package_is_pinned_and_pip_cannot_reach_an_index():
    """Finding 36's substantive fix: the one dependency the capture cannot see.

    `x-block-origin` covers everything that goes through vcpkg's downloader, and
    the angle port's `pip install ply` does not go through it -- so the 76-distfile
    pin says nothing about ply and nothing blocked the fetch. Three properties make
    it a real pin rather than a comment, and all three have to hold together:

      * the wheel is named by an IMMUTABLE url + hash (files.pythonhosted.org is
        content-addressed; `pip install ply` resolves against whatever PyPI serves
        today),
      * pip is told it may not use an index at all, so an UNPINNED package is an
        error rather than a download -- the pip-side equivalent of x-block-origin,
      * and the proxy variables are unset, because an inherited HTTP_PROXY is
        precisely how this went unnoticed. --no-index alone would probably do; the
        lesson of finding 36 is that one unenforced control is not a control.
    """
    wheels = _read("vcpkg_python_packages.bzl")
    assert "files.pythonhosted.org" in wheels, "the wheel URL is not content-addressed"
    assert re.search(r'"sha256-[A-Za-z0-9+/=]{20,}"', wheels), "no integrity hash"
    sh = _read("Meta/vcpkg_build.sh")
    assert "PIP_NO_INDEX=1" in sh, "pip may still resolve from an index"
    assert "PIP_FIND_LINKS" in sh, "the pinned wheels are not offered to pip"
    assert re.search(r"unset .*HTTPS_PROXY", sh), \
        "an inherited proxy is how the pip fetch stayed invisible"
    # The wheel must be a declared INPUT of the action, not merely fetched: a repo
    # Bazel creates but no action depends on is not in the sandbox.
    assert "python_wheels" in _read("vcpkg.bzl")
    assert "python_wheels = ['@vcpkg_pywheel_ply//file']" in _read("BUILD.bazel")


def test_the_use_repo_list_includes_the_pip_wheels():
    """bzlmod needs every extension-created repo named in use_repo, and the wheels
    are created by the SAME extension as the 76 distfiles.

    The emitter derives the wheel names from vcpkg_python_packages.bzl rather than
    restating them, so this checks the round trip: what --use-repo prints must be
    exactly what MODULE.bazel says. A drift here is a "no such repository" error a
    long way from its cause, and it is the specific drift the pip pin introduces --
    the wheels are hand-maintained (no instrument can capture them), so they are the
    one part of this list a regeneration could silently drop.
    """
    import subprocess
    import sys
    # $LADYBIRD_ROOT scrubbed deliberately: another test in this suite points it at
    # a temp fixture checkout and the subprocess would INHERIT it, so this test
    # passed alone and failed in the full run. Found by run_all.py, which runs every
    # file in one process -- the per-file runners could not have surfaced it.
    env = {k: v for k, v in os.environ.items() if k != "LADYBIRD_ROOT"}
    out = subprocess.run(
        [sys.executable, "Meta/emit_vcpkg_bazel.py",
         "--assets", "Meta/vcpkg_assets.tsv", "--use-repo"],
        cwd=_WS, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    emitted = re.findall(r"'([^']+)'", out.stdout)
    block = _read("MODULE.bazel").split("vcpkg_deps = use_extension", 1)[1] \
        .split("use_repo(", 1)[1].split("\n)", 1)[0]
    assert emitted == re.findall(r"'([^']+)'", block), \
        "MODULE.bazel's use_repo has drifted from the emitter"
    assert "vcpkg_pywheel_ply" in emitted


def test_the_git_archive_staging_fails_when_the_archives_are_absent():
    """Finding 36's worst offender: three ways to succeed while copying nothing.

    The staging was `if [ -d "$D" ]; then cp "$D"/*.tar.gz ... 2>/dev/null || true;
    fi` -- a directory test that skips, a redirect that hides, and a `|| true` that
    forgives. On a fresh clone the directory does not exist, all four archives were
    silently absent, and skia failed ~20 minutes later with a googlesource URL that
    names neither this directory nor the tarball.

    So: no `|| true`, and an explicit check that names what is missing. This does not
    assert the archives are FETCHED -- they are not; `git archive` output has no URL
    to http_file, so vcpkg_git_archives.bzl records their hashes but nothing creates
    them (still gap 7). It asserts that their absence is loud.
    """
    sh = _read("Meta/vcpkg_build.sh")
    stage = sh.split("Pre-place the git-sourced externals", 1)[1].split("\n# ---", 1)[0]
    code = "\n".join(l for l in stage.splitlines() if not l.strip().startswith("#"))
    assert "|| true" not in code, "the copy can still silently do nothing"
    assert "2>/dev/null" not in code, "the copy still hides its own errors"
    assert "exit 1" in code, "a missing archive is not a hard failure"
    # And the hashes it points the reader at really are checked in.
    archives = _read("vcpkg_git_archives.bzl")
    assert archives.count(".tar.gz'") == 4, "the four pinned archives changed"

# No `if __name__ == "__main__"` runner here on purpose. There used to be one in
# every test file, and in this file it sat MID-FILE -- so four tests appended after
# it were defined, never called, and the file still printed "6/6 passed". The third
# instance of this session's recurring bug: a report that cannot count what it does
# not reach. `python3 tests/run_all.py` enumerates the module instead, so a test's
# POSITION in the file cannot decide whether it runs; it also fails if a file
# defines no tests at all. Run a single file with `run_all.py <name-substring>`.


def test_the_host_tool_list_reaches_the_action_as_a_declared_input():
    """The preflight is only real if the file is in the sandbox.

    Two ways this silently degrades to a no-op, both already made once in this
    tree: reading it via `dirname $0` (an sh_binary's data lives in
    <name>.runfiles/, not beside the wrapper Bazel execs -- the trap
    cargo_vendor.sh documents), or naming it in `data` instead of the action's
    inputs. The driver skips the check when $HOST_TOOLS is empty, so a broken
    wiring does not fail the build -- it just stops checking. Hence this test.
    """
    bzl = _read("vcpkg.bzl")
    assert "_host_tools" in bzl, "the host tool list is not an attr at all"
    assert "ctx.files._host_tools" in bzl, "not added to the action's inputs"
    assert "ctx.file._host_tools.path" in bzl, "the path is not passed as an argument"
    # Passed positionally, and the driver reads it from that same position.
    sh = _read("Meta/vcpkg_build.sh")
    assert 'HOST_TOOLS="${7-}"' in sh, "the driver does not read argument 7"
    # Count the top-level entries, tracking bracket depth: a comprehension
    # (`",".join([f.path for f in ...])`) contains a `]` of its own, and splitting
    # on the first one silently reads a truncated argument list -- which made this
    # test fail against correct code.
    args, depth = [], 0
    for line in bzl.split("arguments = [", 1)[1].splitlines():
        stripped = line.strip()
        if depth == 0 and stripped.startswith("]"):
            break
        if stripped and not stripped.startswith("#") and depth == 0:
            args.append(stripped)
        depth += line.count("[") - line.count("]")
    positions = args
    assert len(positions) == 7, \
        "the driver reads $7, so the action must pass 7 arguments, got %d" % len(
            positions)
    assert "_host_tools" in positions[-1], "the list must be the 7th argument"
    # And it must be exported, or the label does not resolve.
    assert 'exports_files(["vcpkg_host_tools.tsv"])' in _read("Meta/BUILD.bazel")
