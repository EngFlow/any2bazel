"""Tests for emit_vcpkg_bazel.py's capture reader (finding 30).

The emitter's authoritative input is a *capture*: vcpkg itself, acting through an
`x-asset-sources=x-script` hook, hands us the fully-expanded (url, sha512, dst)
for every download. That is exact where static portfile parsing cannot be -- but
the `dst` vcpkg passes is NOT the filename the portfile asked for, and the first
version of this reader assumed it was.

vcpkg mangles it two ways (both in scripts/cmake/vcpkg_download_distfile.cmake):

  1. in-flight downloads go to "<final>.<pid>.part";
  2. if a file already exists at "<final>" with the WRONG hash, vcpkg splices the
     first 8 hex chars of the expected SHA512 in before the extension and retries
     there. A failed earlier attempt leaving a 0-byte file behind is enough to
     trigger it -- which is exactly what a killed capture leaves.

Because the reader keyed its map by filename, (2) made four distfiles appear
under two names each, and the emitter produced 83 http_file rules for 79 real
files: four phantom rules that would fetch a URL and then fail their own
integrity check. Keying by SHA512 -- the identity the sha->label index it feeds
was always using anyway -- is the fix. These tests pin both manglings, the
sha-keying, and the by-difference discovery of the git-sourced archives.
"""

import hashlib
import importlib.util
import os
import re
import subprocess
import tempfile

_EMIT = os.path.join(
    os.path.dirname(__file__), "..", "examples", "ladybird", "workspace",
    "Meta", "emit_vcpkg_bazel.py")


def _load():
    spec = importlib.util.spec_from_file_location("emit_vcpkg_bazel", _EMIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


emit = _load()

# A real pair from the capture. giflib's tarball arrived under both
# "giflib-6.1.3.tar.gz" and "giflib-6-fb1d6319.1.3.tar.gz": the tag lands
# mid-version, because CMake's "extension" is everything from the FIRST dot. A
# naive "strip the tag before .tar.gz" fix would not have worked.
GIFLIB_SHA = "fb1d63196947" + "0" * 116


def test_strips_the_part_suffix():
    """vcpkg downloads to "<final>.<pid>.part" and renames on success."""
    assert emit.canonical_filename(
        "/dl/pnggroup-libpng-v1.6.58.tar.gz.25998.part", "ab" * 64
    ) == "pnggroup-libpng-v1.6.58.tar.gz"


def test_strips_the_hash_disambiguation_tag():
    """The sha8 tag is spliced after the first dot-free segment, mid-version."""
    assert emit.canonical_filename(
        "/dl/giflib-6-fb1d6319.1.3.tar.gz", GIFLIB_SHA) == "giflib-6.1.3.tar.gz"


def test_strips_both_manglings_together():
    """A retry that is also in flight carries both."""
    assert emit.canonical_filename(
        "/dl/giflib-6-fb1d6319.1.3.tar.gz.4242.part",
        GIFLIB_SHA) == "giflib-6.1.3.tar.gz"


def test_leaves_an_unmangled_name_alone():
    assert emit.canonical_filename("/dl/parsetab.py", "ab" * 64) == "parsetab.py"
    assert emit.canonical_filename(
        "/dl/cmake-4.4.0-linux-x86_64.tar.gz", "ab" * 64
    ) == "cmake-4.4.0-linux-x86_64.tar.gz"


def test_a_hex_chunk_that_is_not_the_hash_is_not_stripped():
    """Only the expected SHA512's own prefix is a disambiguation tag. A
    hex-looking chunk of a legitimate filename (a git ref, say) must survive."""
    name = "KhronosGroup-EGL-Registry-3ae2b7c48690d2ce13cc6db3db02dfc0572be65e.tar.gz"
    assert emit.canonical_filename("/dl/" + name, "ab" * 64) == name


def _write(rows, prefix=b""):
    path = os.path.join(tempfile.mkdtemp(), "assets.tsv")
    with open(path, "wb") as f:
        f.write(prefix)
        for r in rows:
            f.write(("\t".join(r) + "\n").encode())
    return path


def test_capture_is_keyed_by_sha_so_two_names_collapse():
    """The actual bug: one distfile under two names must be ONE http_file."""
    url = "https://github.com/x/giflib/archive/6.1.3.tar.gz"
    cap = emit.load_capture(_write([
        (url, GIFLIB_SHA, "/dl/giflib-6.1.3.tar.gz.999.part"),
        (url, GIFLIB_SHA, "/dl/giflib-6-fb1d6319.1.3.tar.gz"),
    ]))
    assert list(cap) == [GIFLIB_SHA], cap
    assert cap[GIFLIB_SHA][1] == "giflib-6.1.3.tar.gz"


def test_capture_tolerates_a_nul_hole_from_a_killed_run():
    """The capture is append-only across restarts; a run killed mid-write leaves
    the file starting with a block of NULs (the O_APPEND offset outlives the
    bytes). That must not parse as a row, nor abort the read."""
    cap = emit.load_capture(
        _write([("https://e/x.tar.gz", "cd" * 64, "/dl/x.tar.gz")],
               prefix=b"\x00" * 705))
    assert list(cap) == ["cd" * 64]
    assert cap["cd" * 64][1] == "x.tar.gz"


def test_capture_rejects_a_row_whose_hash_is_not_a_sha512():
    cap = emit.load_capture(_write([
        ("https://e/a.tar.gz", "short", "/dl/a.tar.gz"),
        ("https://e/b.tar.gz", "zz" * 64, "/dl/b.tar.gz"),   # 128 chars, not hex
        ("https://e/c.tar.gz", "ef" * 64, "/dl/c.tar.gz"),
    ]))
    assert list(cap) == ["ef" * 64]


def test_repo_names_are_unique_per_distfile_not_per_filename():
    """downloads/ is flat and vcpkg disambiguates colliding basenames itself, so
    two distinct distfiles can share one. A repo-name collision in MODULE.bazel
    is silent (the second http_file just loses), so the hash is in the name."""
    a = emit.bazel_name("source.tar.gz", "ab" * 64)
    b = emit.bazel_name("source.tar.gz", "cd" * 64)
    assert a != b
    assert a.startswith("vcpkg_source_tar_gz_")


def test_observed_git_archives_finds_what_the_asset_cache_never_saw():
    """vcpkg_from_git bypasses asset caching, so its archives are invisible to
    the capture -- and to the static parse (skia reaches 10 of them through its
    own `declare_external_from_git` wrapper, angle's are behind ${URL}/${REF}).
    So identify them by DIFFERENCE against what landed on disk. 0-byte files are
    failed attempts, not inputs; non-archives are byproducts (parsetab.py is
    PLY's generated parse table, written by angle's build, never fetched)."""
    dl = tempfile.mkdtemp()
    served = b"a distfile the asset cache served"
    served_sha = hashlib.sha512(served).hexdigest()
    for name, data in [("served.tar.gz", served),
                       ("skia-deadbeef.tar.gz", b"git archive output"),
                       ("parsetab.py", b"# generated by PLY"),
                       ("zero.tar.gz", b"")]:
        with open(os.path.join(dl, name), "wb") as f:
            f.write(data)

    distfiles = {served_sha: ("https://e/served.tar.gz", "served.tar.gz",
                              "port", "capture")}
    archives, byproducts = emit.observed_git_archives(dl, distfiles)

    assert [n for n, _ in archives] == ["skia-deadbeef.tar.gz"]
    assert [n for n, _ in byproducts] == ["parsetab.py"]
    assert archives[0][1] == hashlib.sha512(b"git archive output").hexdigest()


def test_emitting_from_the_capture_alone_needs_no_vcpkg():
    """The committed capture IS the pin, so a consumer must be able to regenerate
    the Bazel rules from it with no vcpkg checkout, no CMake and no network. The
    static parse is only a cross-check under --assets, so its unavailability is a
    note, not an error -- while WITHOUT --assets it stays fatal (finding 28: never
    silently undercount the closure)."""
    import subprocess
    tsv = os.path.join(
        os.path.dirname(__file__), "..", "examples", "ladybird", "workspace",
        "Meta", "vcpkg_assets.tsv")
    env = dict(os.environ, LADYBIRD_ROOT=tempfile.mkdtemp(),
               VCPKG_ROOT=tempfile.mkdtemp())

    ok = subprocess.run(["python3", _EMIT, "--assets", tsv, "--index"],
                        capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stderr
    assert "no local vcpkg checkout" in ok.stderr
    # The real pin: 76 captured distfiles + the ONE vcpkg host tool the capture
    # could not see (finding 38). It is 77, not 78, and the arithmetic is the
    # finding in miniature: cmake is in BOTH the capture and the tool pin, because
    # the capturing machine's cmake was the wrong version and got downloaded, while
    # its ninja was exactly right and did not. Union by sha, so cmake counts once.
    shas = re.findall(r"^    '([0-9a-f]{128})':", ok.stdout, re.M)
    assert len(shas) == 77, len(shas)
    assert len(set(shas)) == 77
    repos = re.findall(r"@(vcpkg_[A-Za-z0-9_]+)//file:", ok.stdout)
    assert len(set(repos)) == 77, "repo names must be unique per distfile"
    # ...and the tool pins must survive the no-vcpkg path, which is the whole
    # reason they are committed as a TSV rather than derived at emit time.
    assert "ninja-linux-1.13.2.zip" in ok.stdout

    bad = subprocess.run(["python3", _EMIT, "--index"],
                         capture_output=True, text=True, env=env)
    assert bad.returncode == 2, bad.stderr
    assert "UNDERCOUNTS" in bad.stderr


def test_gnu_urls_get_mirror_alternatives():
    """Finding 29's predicted payoff, now load-bearing: vcpkg's x-script hook is
    handed ONE url per attempt, so the mirror redundancy portfiles encode never
    reaches it and a single 502 kills the fetch (observed twice --
    ftpmirror.gnu.org during the capture, and again during the first Bazel fetch
    of all 76). http_file takes a LIST, so moving fetching to Bazel fixes it
    rather than relocating it. Safe because `integrity` content-addresses every
    URL: a mirror serving wrong bytes fails the hash, so it can only cost time."""
    urls = emit.urls_for("https://ftpmirror.gnu.org/gnu/automake/automake-1.17.tar.gz")
    assert urls[0] == "https://ftpmirror.gnu.org/gnu/automake/automake-1.17.tar.gz"
    assert len(urls) > 1
    # The path after the mirror prefix must be carried over verbatim.
    for u in urls:
        assert u.endswith("/automake/automake-1.17.tar.gz"), u
    assert len(set(urls)) == len(urls), "no duplicate urls"


def test_non_mirrored_urls_are_left_as_a_single_entry():
    """Only prefixes with known-equivalent mirrors expand; a GitHub tarball has no
    mirror and must not acquire a fabricated one."""
    u = "https://github.com/madler/zlib/archive/v1.3.1.tar.gz"
    assert emit.urls_for(u) == [u]

# No `if __name__ == "__main__"` runner here on purpose. There used to be one in
# every test file, and in this file it sat MID-FILE -- so four tests appended after
# it were defined, never called, and the file still printed "6/6 passed". The third
# instance of this session's recurring bug: a report that cannot count what it does
# not reach. `python3 tests/run_all.py` enumerates the module instead, so a test's
# POSITION in the file cannot decide whether it runs; it also fails if a file
# defines no tests at all. Run a single file with `run_all.py <name-substring>`.


# --------------------------------------------------------------------------
# vcpkg's OWN host tools (finding 38)
#
# The capture cannot see a tool the capturing machine already has:
# vcpkg_find_acquire_program probes the host first, so my /usr/bin/ninja at
# exactly the required 1.13.2 kept ninja out of the pin entirely, and a machine
# without it got `distfile MISSING FROM INDEX ... ninja-linux.zip` followed by
# x-block-origin correctly refusing the network. cmake was in the pin only by
# luck (host 4.2.3 vs required 4.4.0).
#
# The fix reads vcpkg's own tool metadata instead of the capture, so what gets
# pinned no longer depends on what happens to be installed anywhere. These tests
# pin that property, the filename vcpkg will look for, and the scoping.

TOOLS_JSON = """{
  "tools": [
    {"name": "ninja", "os": "linux", "arch": "x64", "version": "1.13.2",
     "url": "https://example.test/ninja-linux.zip", "sha512": "%s",
     "archive": "ninja-linux-1.13.2.zip"},
    {"name": "ninja", "os": "windows", "arch": "x64", "version": "1.13.2",
     "url": "https://example.test/ninja-win.zip", "sha512": "%s"},
    {"name": "cmake", "os": "linux", "arch": "x64", "version": "4.4.0",
     "url": "https://example.test/cmake-4.4.0-linux-x86_64.tar.gz", "sha512": "%s",
     "archive": "cmake-4.4.0-linux-x86_64.tar.gz"},
    {"name": "node", "os": "linux", "arch": "x64", "version": "24.18.0",
     "url": "https://example.test/node.tar.gz", "sha512": "%s"},
    {"name": "git", "os": "linux", "arch": "x64", "version": "2.7.4"}
  ]
}""" % ("a" * 128, "b" * 128, "c" * 128, "d" * 128)


def _vcpkg_with_tools(tmpdir, body=TOOLS_JSON):
    scripts = os.path.join(tmpdir, "scripts")
    os.makedirs(scripts, exist_ok=True)
    with open(os.path.join(scripts, "vcpkg-tools.json"), "w") as f:
        f.write(body)
    return tmpdir


def test_tool_pins_come_from_vcpkg_metadata_not_the_capture():
    """The whole point: the pin must not depend on what is installed locally."""
    with tempfile.TemporaryDirectory() as d:
        tools = emit.tool_distfiles(vcpkg=_vcpkg_with_tools(d))
    assert set(tools) == {"a" * 128, "c" * 128}, \
        "expected exactly the linux/x64 cmake+ninja pins, got %r" % (list(tools),)


def test_tool_pin_uses_the_archive_name_vcpkg_will_look_for():
    """The asset script is asked for vcpkg's OWN filename, not the URL basename.

    ninja's URL basename is ninja-linux.zip but vcpkg stores (and looks for)
    ninja-linux-1.13.2.zip. Emitting the URL basename would put a row in the index
    under a name vcpkg never asks about -- a pin that looks present and is not.
    """
    with tempfile.TemporaryDirectory() as d:
        tools = emit.tool_distfiles(vcpkg=_vcpkg_with_tools(d))
    assert tools["a" * 128][1] == "ninja-linux-1.13.2.zip"


def test_tool_pins_are_scoped_to_what_this_build_can_invoke():
    """node/dotnet/powershell are ~400MB no port in this closure ever runs."""
    assert "node" not in emit.BUILD_TOOLS
    assert set(emit.BUILD_TOOLS) == {"cmake", "ninja"}
    with tempfile.TemporaryDirectory() as d:
        vc = _vcpkg_with_tools(d)
        names = {r[2] for r in emit.tool_distfiles(vcpkg=vc).values()}
        everything = {r[2] for r in emit.tool_distfiles(want=None, vcpkg=vc).values()}
    assert "vcpkg-tool:node" not in names
    assert "vcpkg-tool:node" in everything, "want=None must mean every tool"


def test_a_tool_with_no_url_is_skipped():
    """`git` has no url: vcpkg expects it from the system, so there is nothing to pin."""
    with tempfile.TemporaryDirectory() as d:
        rows = emit.tool_distfiles(want=None, vcpkg=_vcpkg_with_tools(d))
    assert not any(r[2] == "vcpkg-tool:git" for r in rows.values())


def test_missing_tool_metadata_is_an_error_not_an_empty_pin():
    """Silently emitting no tool pins is what broke a clone: fail loudly instead."""
    with tempfile.TemporaryDirectory() as d:
        try:
            emit.tool_distfiles(vcpkg=d)   # exists, but has no vcpkg-tools.json
        except emit.VcpkgUnavailable as e:
            assert "vcpkg-tools.json" in str(e)
        else:
            raise AssertionError("expected VcpkgUnavailable")


def test_the_committed_pin_carries_ninja_and_cmake():
    """The regression test for the actual bug report, against the committed files.

    Ulf's clone failed at `Detecting compiler hash` because ninja was absent from
    the index; cmake was present. Both must be there now, and the index is what
    the asset script resolves through, so check the index -- not just the
    http_file list.
    """
    ws = os.path.join(os.path.dirname(__file__), "..", "examples", "ladybird",
                      "workspace")
    index = open(os.path.join(ws, "vcpkg_index.bzl")).read()
    for tool in ("ninja-linux-1.13.2.zip", "cmake-4.4.0-linux-x86_64.tar.gz"):
        assert tool in index, "%s is missing from the distfile index" % tool
    distfiles = open(os.path.join(ws, "vcpkg_distfiles.bzl")).read()
    assert "ninja-build/ninja/releases/download/v1.13.2/ninja-linux.zip" in distfiles

    # ...and bzlmod only creates a repo the MODULE names, so an http_file with no
    # use_repo entry is invisible: that asymmetry is why the emitter emits the
    # use_repo list too.
    module = open(os.path.join(ws, "MODULE.bazel")).read()
    names = re.findall(r"name = '(vcpkg_ninja[A-Za-z0-9_]*)'", distfiles)
    assert names, "no ninja http_file found"
    for n in names:
        assert "'%s'" % n in module, "%s is fetched but not named in use_repo" % n


# --- host prerequisites (finding 39) ---------------------------------------
#
# The class of input that CANNOT be pinned: tools vcpkg has no Linux download
# for. What is tested here is therefore not "the pin is complete" but "the gap is
# named accurately and early" -- and above all that it does not cry wolf, because
# a preflight with false positives demands packages nothing needs and gets
# deleted by the third person who hits it.

def _acquire(tmpdir, program, body):
    d = os.path.join(tmpdir, "scripts", "cmake")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "vcpkg_find_acquire_program(%s).cmake" % program),
              "w") as f:
        f.write(body)


def _port(tmpdir, port, body, filename="portfile.cmake"):
    d = os.path.join(tmpdir, "ports", port, os.path.dirname(filename))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(tmpdir, "ports", port, filename), "w") as f:
        f.write(body)


NASM_LIKE = """set(program_name nasm)
set(program_version 3.01)
set(brew_package_name "nasm")
set(apt_package_name "nasm")
if(CMAKE_HOST_WIN32)
    set(download_urls "https://example.com/nasm-win64.zip")
    set(download_filename "nasm-win64.zip")
    set(download_sha512 %s)
endif()
""" % ("a" * 128)


def test_a_windows_only_download_is_a_host_prerequisite_on_linux():
    """The nasm bug: URLs exist, but only inside if(CMAKE_HOST_WIN32).

    Reading `download_urls` without asking WHICH BRANCH it is in reports nasm as
    a pinnable download, which is how six ports' worth of hard dependency stayed
    invisible until libvpx died on it 20 minutes into a build.
    """
    with tempfile.TemporaryDirectory() as d:
        _acquire(d, "NASM", NASM_LIKE)
        _port(d, "libvpx", "vcpkg_find_acquire_program(NASM)\n")
        reqs = emit.host_tool_requirements(ports=["libvpx"], vcpkg=d)
    assert "nasm" in reqs, reqs
    binary, apt, users, _alts = reqs["nasm"]
    assert (binary, apt, users) == ("nasm", "nasm", ["libvpx"])


def test_a_tool_with_a_real_linux_download_is_not_a_prerequisite():
    """meson/gn ARE downloadable on Linux -- demanding them from apt is wrong."""
    with tempfile.TemporaryDirectory() as d:
        _acquire(d, "MESON", 'set(program_name meson)\n'
                             'set(apt_package_name "meson")\n'
                             'set(download_urls "https://example.com/meson.tar.gz")\n')
        _port(d, "vcpkg-tool-meson", "vcpkg_find_acquire_program(MESON)\n")
        reqs = emit.host_tool_requirements(ports=["vcpkg-tool-meson"], vcpkg=d)
    assert reqs == {}, "a downloadable tool must not be a host prerequisite: %r" % reqs


def test_a_vcpkg_fetch_tool_is_not_a_prerequisite():
    """NINJA sets no download_urls: it delegates to `vcpkg fetch`, which IS pinned.

    Without this, the fix for finding 38 (pin ninja) and the fix for finding 39
    (name what cannot be pinned) contradict each other about ninja.
    """
    with tempfile.TemporaryDirectory() as d:
        _acquire(d, "NINJA", "z_use_vcpkg_fetch(NINJA)\n")
        _port(d, "vcpkg-cmake", "vcpkg_find_acquire_program(NINJA)\n")
        reqs = emit.host_tool_requirements(ports=["vcpkg-cmake"], vcpkg=d)
    assert reqs == {}, "ninja comes from the tools.json pin, not the host: %r" % reqs


def test_a_windows_only_call_site_is_not_a_linux_prerequisite():
    """openssl and vcpkg-make ask for CLANG only under MSVC -- clang is not needed.

    The first version of this derivation reported clang, which is a false alarm
    on every Linux machine, and a preflight that demands a 2GB toolchain nobody
    uses is one that gets ignored.
    """
    with tempfile.TemporaryDirectory() as d:
        _acquire(d, "CLANG", 'set(program_name clang)\n'
                             'set(apt_package_name "clang")\n')
        _port(d, "vcpkg-make", """
if(VCPKG_DETECTED_CMAKE_ASM_COMPILER_ID STREQUAL "MSVC")
    vcpkg_find_acquire_program(CLANG)
endif()
""")
        reqs = emit.host_tool_requirements(ports=["vcpkg-make"], vcpkg=d)
    assert reqs == {}, "an MSVC-only call site is not a Linux prerequisite: %r" % reqs


def test_a_windows_only_subdirectory_is_not_scanned():
    """openssl splits by FILE: windows/portfile.cmake asks for CLANG at top level.

    No if() guards it -- the guard is the include() in the parent -- so only the
    path says it is Windows-only.
    """
    with tempfile.TemporaryDirectory() as d:
        _acquire(d, "CLANG", "set(program_name clang)\n")
        _acquire(d, "PERL", 'set(program_name perl)\nset(apt_package_name "perl")\n')
        _port(d, "openssl", "vcpkg_find_acquire_program(CLANG)\n",
              filename="windows/portfile.cmake")
        _port(d, "openssl", "vcpkg_find_acquire_program(PERL)\n",
              filename="unix/portfile.cmake")
        reqs = emit.host_tool_requirements(ports=["openssl"], vcpkg=d)
    assert set(reqs) == {"perl"}, \
        "windows/ must be skipped and unix/ must not be: %r" % reqs


def test_the_else_of_a_negated_windows_test_is_the_windows_branch():
    """if(NOT VCPKG_TARGET_IS_WINDOWS) ... else() <-- that else is Windows.

    dav1d is written exactly this way. Treating every else() as reachable puts
    the Windows-only GASPREPROCESSOR into a Linux preflight.
    """
    with tempfile.TemporaryDirectory() as d:
        _acquire(d, "GASPREPROCESSOR", "set(program_name gas-preprocessor.pl)\n")
        _acquire(d, "NASM", NASM_LIKE)
        _port(d, "dav1d", """
if(NOT VCPKG_TARGET_IS_WINDOWS)
    vcpkg_find_acquire_program(NASM)
else()
    vcpkg_find_acquire_program(GASPREPROCESSOR)
endif()
""")
        reqs = emit.host_tool_requirements(ports=["dav1d"], vcpkg=d)
    assert set(reqs) == {"nasm"}, reqs


GPERF_LIKE = """
function(vcpkg_run_autoreconf shell_cmd work_dir)
    find_program(ACLOCAL NAMES aclocal)
    find_program(AUTORECONF NAMES autoreconf)
    find_program(LIBTOOLIZE NAMES libtoolize glibtoolize)
    if(missing)
        message(FATAL_ERROR "${PORT} currently requires the following programs from the system package manager:
    autoconf autoconf-archive automake libtoolize

    On Debian and Ubuntu derivatives:
        sudo apt install autoconf autoconf-archive automake libtool
")
    endif()
endfunction()
"""


def test_a_fatal_error_naming_apt_packages_is_a_prerequisite_too():
    """The SECOND mechanism, found after the first shipped.

    vcpkg-make never calls vcpkg_find_acquire_program for autotools: it uses bare
    find_program and raises FATAL_ERROR with an apt line. Enumerating only
    acquire-program calls misses all of it -- which is how gperf failed on
    autoconf AFTER nasm was fixed.
    """
    with tempfile.TemporaryDirectory() as d:
        _port(d, "vcpkg-make", GPERF_LIKE, filename="vcpkg_make.cmake")
        reqs = emit.host_tool_requirements(ports=["vcpkg-make"], vcpkg=d)
    assert set(reqs) == {"autoconf", "autoconf-archive", "automake", "libtool"}, reqs
    # libtoolize OR glibtoolize satisfies libtool: alternatives, not two requirements.
    binary, apt, _users, alts = reqs["libtool"]
    assert binary == "libtoolize" and apt == "libtool" and alts == ["glibtoolize"]
    # autoconf-archive is m4 macros: no binary exists to probe for, and claiming
    # otherwise would report it satisfied on a machine that lacks it.
    assert reqs["autoconf-archive"][0] == "", reqs["autoconf-archive"]


def test_a_warning_about_system_packages_is_not_a_prerequisite():
    """angle WARNS about mesa-common-dev and separately FATAL_ERRORs on arch.

    A file-level 'does it contain FATAL_ERROR and an apt line' check staples the
    two together and demands mesa-common-dev, which no build step here requires.
    Advice is not a requirement.
    """
    with tempfile.TemporaryDirectory() as d:
        _port(d, "angle", """
if (VCPKG_TARGET_IS_LINUX)
    message(WARNING "${PORT} currently requires the following libraries from the system package manager:\\n    mesa-common-dev\\n\\nThese can be installed via apt-get install mesa-common-dev.")
endif()
message(FATAL_ERROR "Unsupported architecture: ${VCPKG_TARGET_ARCHITECTURE}")
""")
        reqs = emit.host_tool_requirements(ports=["angle"], vcpkg=d)
    assert reqs == {}, "a WARNING is advice, not a prerequisite: %r" % reqs


def test_the_committed_host_tool_list_names_nasm_and_autotools():
    """Regression test for both bug reports, against the committed file."""
    rows = emit.load_host_tools()
    assert rows, "Meta/vcpkg_host_tools.tsv is missing or empty"
    by_apt = {apt: (names, users) for names, apt, users in rows}
    # Ulf's first failure: libvpx, ~20 minutes in.
    assert "nasm" in by_apt, list(by_apt)
    assert by_apt["nasm"][0] == ["nasm"]
    assert "libvpx" in by_apt["nasm"][1]
    # Ulf's second failure: gperf, via vcpkg-make.
    for apt in ("autoconf", "automake", "libtool", "autoconf-archive"):
        assert apt in by_apt, "%s missing from the host tool list" % apt
    # ...and clang must NOT be there (MSVC-only call sites).
    assert "clang" not in by_apt, "clang is an MSVC-only requirement"


def test_the_preflight_reports_every_missing_tool_at_once():
    """The actual complaint: one tool per 20-minute build.

    The preflight's value is entirely in reporting the WHOLE set in one run, so
    this checks the shape of the output, not just the exit code.
    """
    ws = os.path.join(os.path.dirname(__file__), "..", "examples", "ladybird",
                      "workspace")
    script = open(os.path.join(ws, "Meta", "vcpkg_build.sh")).read()
    body = script.split("# --- host prerequisites")[1].split(
        "# --- a writable vcpkg root")[0]
    # $HOST_TOOLS arrives as the driver's 7th argument (vcpkg.bzl names the file
    # as a declared input and passes its path); set it the same way here rather
    # than editing the extracted body, so the test exercises the real contract.
    body = "HOST_TOOLS=%s\n%s" % (
        os.path.join(ws, "Meta", "vcpkg_host_tools.tsv"), body)
    with tempfile.TemporaryDirectory() as d:
        # A PATH with everything EXCEPT nasm and libtoolize. Empty stubs are
        # enough: the preflight uses `command -v`, and running the real tools is
        # not the point.
        for tool in ("perl", "python3", "pkg-config", "aclocal", "autoreconf"):
            p = os.path.join(d, tool)
            open(p, "w").close()
            os.chmod(p, 0o755)
        r = subprocess.run(["/bin/bash", "-c", body], capture_output=True,
                           text=True, env={"PATH": d})
    assert r.returncode == 1, "expected a hard failure, got %d\n%s" % (
        r.returncode, r.stderr)
    # BOTH, from one run -- that is the whole feature.
    assert "nasm" in r.stderr and "libtoolize" in r.stderr, r.stderr
    assert "needed by: dav1d" in r.stderr, r.stderr
    # One pasteable line, and the tools that ARE present must not be in it.
    apt_line = [l for l in r.stderr.splitlines() if "sudo apt install" in l]
    assert len(apt_line) == 1, r.stderr
    assert "nasm" in apt_line[0] and "libtool" in apt_line[0]
    assert "perl" not in apt_line[0], "must not demand a tool that is present"


def test_a_moved_pin_is_reported_as_a_stale_capture_not_a_windows_only_fetch():
    """The capture wins -- so when it is WRONG, the message has to say which.

    Under --assets the capture REPLACES the static portfile parse, deliberately:
    a portfile is a CMake program, so the static regex cannot see through its
    platform branches, and every row the static parse had and the capture did not
    turned out to be a Windows-only fetch. That reasoning was checked once, on
    three rows (libiconv, pthreads4w, dirent, all behind
    `if(VCPKG_TARGET_IS_WINDOWS)`), and then FROZEN INTO THE MESSAGE: the emitter
    printed every casualty as an entry "vcpkg never asked for on this platform".

    Ladybird's 71fb301a repin is where that came due. vcpkg.json pins sdl3
    3.2.28 (the reference build agrees: vcpkg_installed/vcpkg/info/ holds
    sdl3_3.2.28_x64-linux-dynamic.list) and the versions-db derivation resolves
    it correctly. The capture predates the repin and holds release-3.4.12, so the
    RIGHT answer was discarded in favour of a stale one -- and the diagnostic
    asserted a reason that happened to be false, which is why nobody looked. The
    checked-in vcpkg_distfiles.bzl still fetches 3.4.12.

    The distinction is available without any new input: a dropped row whose URL
    FAMILY the capture also has is the same upstream project at a different
    version. That is a stale capture. A row whose family the capture has never
    seen at all is genuinely a fetch this platform does not do.
    """
    cap = {
        "a" * 128: ("https://github.com/libsdl-org/SDL/archive/release-3.4.12.tar.gz",
                    "libsdl-org-SDL-release-3.4.12.tar.gz", "captured", "capture"),
        "b" * 128: ("https://sqlite.org/2026/sqlite-autoconf-3530300.tar.gz",
                    "sqlite-autoconf-3530300.tar.gz", "captured", "capture"),
    }
    derived = {
        # THE case: same project, the version vcpkg.json actually pins.
        "c" * 128: ("https://github.com/libsdl-org/SDL/archive/release-3.2.28.tar.gz",
                    "libsdl-org-SDL-release-3.2.28.tar.gz", "sdl3", "versions-db"),
        # Genuinely platform-only: nothing from this upstream is in the capture.
        "d" * 128: ("https://ftpmirror.gnu.org/gnu/libiconv/libiconv-1.19.tar.gz",
                    "libiconv-1.19.tar.gz", "libiconv", "versions-db"),
        "e" * 128: ("https://github.com/tronkko/dirent/archive/1.26.tar.gz",
                    "tronkko-dirent-1.26.tar.gz", "dirent", "versions-db"),
        # A row the capture HAS (by sha) is not a casualty at all.
        "b" * 128: ("https://sqlite.org/2026/sqlite-autoconf-3530300.tar.gz",
                    "sqlite-autoconf-3530300.tar.gz", "sqlite3", "versions-db"),
    }
    platform_only, stale = emit.classify_static_only(derived, cap)
    assert stale == [("c" * 128, "a" * 128)], stale
    assert sorted(platform_only) == ["d" * 128, "e" * 128], platform_only

    # And the emitter SAYS it, in those words, on the real tree's data. Run as a
    # subprocess so this exercises the message and not just the classifier.
    import subprocess
    ws = os.path.join(os.path.dirname(__file__), "..", "examples", "ladybird",
                      "workspace")
    tsv = os.path.join(ws, "Meta", "vcpkg_assets.tsv")
    r = subprocess.run(["python3", _EMIT, "--assets", tsv, "--index"],
                       capture_output=True, text=True,
                       env=dict(os.environ, LADYBIRD_ROOT=tempfile.mkdtemp(),
                                VCPKG_ROOT=tempfile.mkdtemp()))
    assert r.returncode == 0, r.stderr
    # With no vcpkg there is no derivation to compare against, so neither
    # category can appear -- the message must not be printed speculatively.
    assert "STALE CAPTURE" not in r.stderr, r.stderr
    assert "never asked for on this platform" not in r.stderr, r.stderr

    # Structurally: the old wording claimed the reason for EVERY dropped row.
    # Whatever the emitter says about platform-only entries, it may only say it
    # about rows that survived the classification.
    with open(_EMIT) as f:
        src = f.read()
    body = src.split("def main(", 1)[1]
    assert "classify_static_only(" in body, \
        "main() must classify the dropped rows, not assert a reason for them"
    assert "static_only = sorted(set(distfiles) - set(cap))" not in body, \
        "the unclassified set-difference is back"


def _capture_script():
    ws = os.path.join(os.path.dirname(__file__), "..", "examples", "ladybird",
                      "workspace")
    with open(os.path.join(ws, "Meta", "vcpkg_capture_assets.sh")) as f:
        return f.read()


def test_a_failed_download_makes_the_capture_fail_because_vcpkg_will_not():
    """vcpkg exits 0 on a capture that lost downloads. Ask me how I know.

    The 71fb301a re-capture printed "All requested installations completed
    successfully in: 49 min", exited 0, and wrote 72 rows -- while angle's
    `gni-to-cmake.py` had FAILED to download (a transient TLS error: this
    sandbox's clock was briefly behind the certificate's validity window,
    "certificate is not yet valid"). Diffing against the committed capture showed
    72 rows where the old pin had 76: FIVE URLs gone, four of them WebKit files
    nothing had reported on at all.

    That is the mechanism worth remembering: a failed download HALTS its portfile,
    so the four `vcpkg_download_distfile` calls after it in angle were never made,
    never requested, and so never captured. The recorder logs each tuple BEFORE
    fetching, so the one that failed is present and the four that were never asked
    for are simply absent -- a pin that is missing five URLs and looks complete.
    Emitting from it would produce rules that fetch nothing for angle, failing
    much later inside a port build.

    So the capture must judge itself, and it cannot do that from vcpkg's exit
    code. Verified against a fake vcpkg that calls the recorder with an
    unreachable URL and then exits 0 like the real one: the script exits 1 and
    names the URL.
    """
    sh = _capture_script()
    # The recorder must report failures to the DRIVER. A variable cannot: the
    # recorder is a separate process per download.
    rec = sh.split("cat > \"$REC\"", 1)[1].split("chmod +x", 1)[0]
    assert re.search(r'printf .*>> "\$FAILED"', rec), \
        "the recorder does not report a failed download to the driver"
    # And the driver must refuse to bless the result.
    tail = sh.split("chmod +x", 1)[1]
    assert 'if [ -s "$FAILED" ]' in tail, \
        "the driver never checks whether any download failed"
    assert re.search(r'if \[ -s "\$FAILED" \][\s\S]{0,800}?exit 1', tail), \
        "a capture with failed downloads still exits 0"
    # The message has to say the non-obvious part, or a reader re-runs the emitter
    # on the incomplete file.
    assert "HALTS its portfile" in tail, \
        "the message does not explain that later downloads in that port are missing"


def test_the_capture_bounds_stalls_not_transfer_size():
    """`--max-time` capped how long a download may legitimately TAKE.

    It killed OpenGL-Registry at 22MB of a perfectly healthy transfer, reported it
    as "FAILED to fetch", fell through to the origin, and did it again on every
    re-run -- a capture that could not finish, blaming the mirror. The property
    actually wanted is "no progress for a while", which cannot mistake a big file
    for a dead one.
    """
    sh = _capture_script()
    code = "\n".join(l.split("#", 1)[0] for l in sh.splitlines())
    assert "--max-time" not in code, \
        ("--max-time bounds total transfer time, so it fails big-but-healthy "
         "downloads; use --speed-time/--speed-limit")
    assert "--speed-time" in code and "--speed-limit" in code, \
        "no stall timeout at all: a dead mirror hangs the capture forever"


def test_the_capture_is_resumable():
    """A ~50-minute network-bound job that truncates its output on start.

    Every interruption -- sandbox restart, dead mirror, timeout -- cost the whole
    run (todo c2affe6b). Appending plus the final dedupe makes a re-run resume
    instead: a tuple recorded twice is free.
    """
    sh = _capture_script()
    code = "\n".join(l.split("#", 1)[0] for l in sh.splitlines())
    assert ': > "$OUT"' not in code, "the capture truncates its own output on start"
    assert 'touch "$OUT"' in code, "the capture does not append to an existing run"
    assert re.search(r"sort -u -t\$'\\t' -k1,2 -o \"\$OUT\" \"\$OUT\"", code), \
        "without the dedupe, appending duplicates rows"
