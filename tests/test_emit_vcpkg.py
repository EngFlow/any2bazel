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
    # The real pin: 76 distfiles, every one keyed by its own sha512.
    shas = re.findall(r"^    '([0-9a-f]{128})':", ok.stdout, re.M)
    assert len(shas) == 76, len(shas)
    assert len(set(shas)) == 76
    repos = re.findall(r"@(vcpkg_[A-Za-z0-9_]+)//file:", ok.stdout)
    assert len(set(repos)) == 76, "repo names must be unique per distfile"

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
