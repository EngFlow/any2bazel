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
