#!/usr/bin/env python3
"""Tests for examples/ladybird/workspace/Meta/fetch_vcpkg_git_archives.py.

The script's job is to produce the four `vcpkg_from_git` tarballs *without*
running CMake or vcpkg. What is worth testing is not the git plumbing (that needs
the network) but the two decisions the script makes on its own:

  1. the LIST of archives comes from the committed pin, never from parsing --
     because a first version derived the list from skia's portfile and got 8
     instead of 4 while missing libyuv entirely (`declare_external_from_git`
     declares; feature-conditional `get_externals` picks);
  2. resolving an archive NAME to a clone URL is a text lookup over portfiles,
     and every pinned name must resolve or the script must fail loudly rather
     than silently fetch a subset.
"""

import importlib.util
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "examples" / "ladybird" / "workspace" / "Meta" / "fetch_vcpkg_git_archives.py"


class expect_exit:
    """Minimal assertRaises(SystemExit), since this repo's tests are plain functions."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        assert exc_type is SystemExit, "expected SystemExit, got %r" % (exc_type,)
        self.exception = exc
        return True


def load(ladybird_root: Path):
    """Import the script with LADYBIRD_ROOT pointed at a fixture tree."""
    import os

    old = os.environ.get("LADYBIRD_ROOT")
    os.environ["LADYBIRD_ROOT"] = str(ladybird_root)
    try:
        spec = importlib.util.spec_from_file_location(f"fvga_{id(ladybird_root)}", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("LADYBIRD_ROOT", None)
        else:
            os.environ["LADYBIRD_ROOT"] = old


SKIA_PORTFILE = """
include("${CMAKE_CURRENT_LIST_DIR}/skia-functions.cmake")
declare_external_from_git(piex
    URL "https://android.googlesource.com/platform/external/piex.git"
    REF "bb217acdca1cc0c16b704669dd6f91a1b509c406"
    LICENSE_FILE LICENSE
)
declare_external_from_git(spirv-tools
    URL "https://github.com/KhronosGroup/SPIRV-Tools.git"
    REF "2d14d2e76aa7de72404b17078eda15c20a6a0389"
)
set(required_externals expat piex zlib wuffs)
get_externals(${required_externals})
"""

ANGLE_PORTFILE = """
set(ANGLE_THIRDPARTY_ZLIB_COMMIT 4028ebf8710ee39d2286cb0f847f9b95c59f84d8)
checkout_in_path(
    "${SOURCE_PATH}/third_party/zlib"
    "https://chromium.googlesource.com/chromium/src/third_party/zlib"
    "${ANGLE_THIRDPARTY_ZLIB_COMMIT}"
)
"""

LIBYUV_PORTFILE = """
vcpkg_from_git(
    OUT_SOURCE_PATH SOURCE_PATH
    URL https://chromium.googlesource.com/libyuv/libyuv
    REF d98915a654d3564e4802a0004add46221c4e4348
    PATCHES cmake.diff
)
"""

PIN = """
VCPKG_GIT_ARCHIVES = {
    'angle-4028ebf8710ee39d2286cb0f847f9b95c59f84d8.tar.gz': '%s',
    'libyuv-d98915a654d3564e4802a0004add46221c4e4348.tar.gz': '%s',
    'skia-bb217acdca1cc0c16b704669dd6f91a1b509c406.tar.gz': '%s',
}
""" % ("a" * 128, "b" * 128, "c" * 128)


def make_tree(tmp: Path, *, pin: str = PIN, overlay_angle: bool = True) -> Path:
    root = tmp / "ladybird"
    (root / "Build" / "vcpkg" / "ports" / "skia").mkdir(parents=True)
    (root / "Build" / "vcpkg" / "ports" / "libyuv").mkdir(parents=True)
    (root / "Build" / "vcpkg" / "ports" / "angle").mkdir(parents=True)
    (root / "Build" / "vcpkg" / "ports" / "skia" / "portfile.cmake").write_text(SKIA_PORTFILE)
    (root / "Build" / "vcpkg" / "ports" / "libyuv" / "portfile.cmake").write_text(LIBYUV_PORTFILE)
    # The builtin angle port exists but (as upstream) has no checkout_in_path;
    # the overlay is what carries it. That asymmetry is the point of the test.
    (root / "Build" / "vcpkg" / "ports" / "angle" / "portfile.cmake").write_text("# builtin angle\n")
    if overlay_angle:
        d = root / "Meta" / "CMake" / "vcpkg" / "overlay-ports" / "angle"
        d.mkdir(parents=True)
        (d / "portfile.cmake").write_text(ANGLE_PORTFILE)
    (root / "vcpkg_git_archives.bzl").write_text(pin)
    return root


def test_pin_is_the_authority_not_the_portfiles():
    """skia's portfile declares 2 refs; only the pinned one is fetched.

    This is the regression test for the bug that made me rewrite the script:
    deriving the set from `declare_external_from_git` over-collects, because
    feature-conditional get_externals() decides what is actually used.
    """
    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t))
        mod = load(root)
        pinned = mod.committed_hashes()
        assert len(pinned) == 3
        assert "skia-bb217acdca1cc0c16b704669dd6f91a1b509c406.tar.gz" in pinned
        # declared in the portfile, NOT pinned -> must not be fetched
        assert "skia-2d14d2e76aa7de72404b17078eda15c20a6a0389.tar.gz" not in pinned

def test_missing_pin_is_a_hard_error():
    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t), pin="# nothing pinned\n")
        mod = load(root)
        with expect_exit() as cm:
            mod.fetch(root / "Build" / "vcpkg", Path(t) / "out")
        assert "no pinned archives" in str(cm.exception)


def test_resolves_all_three_call_syntaxes():
    """declare_external_from_git, checkout_in_path and a bare vcpkg_from_git."""
    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t))
        mod = load(root)
        urls = mod.resolve_urls(root / "Build" / "vcpkg", sorted(mod.committed_hashes()))
        assert urls["angle-4028ebf8710ee39d2286cb0f847f9b95c59f84d8.tar.gz"] == "https://chromium.googlesource.com/chromium/src/third_party/zlib"
        assert urls["libyuv-d98915a654d3564e4802a0004add46221c4e4348.tar.gz"] == "https://chromium.googlesource.com/libyuv/libyuv"
        assert urls["skia-bb217acdca1cc0c16b704669dd6f91a1b509c406.tar.gz"] == "https://android.googlesource.com/platform/external/piex.git"

def test_expands_a_ref_held_in_a_set_variable():
    """angle's REF is ${ANGLE_THIRDPARTY_ZLIB_COMMIT}, not a literal."""
    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t))
        mod = load(root)
        found = mod.refs_in_portfile(
            root / "Meta" / "CMake" / "vcpkg" / "overlay-ports" / "angle" / "portfile.cmake"
        )
        assert "angle-4028ebf8710ee39d2286cb0f847f9b95c59f84d8.tar.gz" in found

def test_overlay_shadows_the_builtin_port():
    """--overlay-ports wins; the builtin angle portfile declares nothing."""
    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t))
        mod = load(root)
        paths = mod.all_portfiles(root / "Build" / "vcpkg")
        angle = [p for p in paths if p.parent.name == "angle"]
        assert len(angle) == 1, "angle must appear once, from the overlay"
        assert "overlay-ports" in str(angle[0])

def test_unresolvable_pinned_name_fails_loudly():
    """A pin naming a port no portfile declares must not silently fetch a subset."""
    pin = PIN.replace("skia-bb217acdca1cc0c16b704669dd6f91a1b509c406", "ghost-" + "0" * 40)
    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t), pin=pin)
        mod = load(root)
        with expect_exit() as cm:
            mod.resolve_urls(root / "Build" / "vcpkg", sorted(mod.committed_hashes()))
        msg = str(cm.exception)
        assert "ghost-" in msg
        assert "no vcpkg_from_git call found" in msg

def test_a_ref_that_is_not_a_sha_is_refused():
    """vcpkg_from_git requires a commit SHA; a branch name must not be accepted."""
    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t))
        pf = root / "Build" / "vcpkg" / "ports" / "libyuv" / "portfile.cmake"
        pf.write_text(LIBYUV_PORTFILE.replace("d98915a654d3564e4802a0004add46221c4e4348", "main"))
        mod = load(root)
        assert mod.refs_in_portfile(pf) == {}


def test_a_file_with_the_wrong_hash_is_not_accepted_as_cached():
    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t))
        mod = load(root)
        out = Path(t) / "out"
        out.mkdir()
        name = "libyuv-d98915a654d3564e4802a0004add46221c4e4348.tar.gz"
        (out / name).write_bytes(b"corrupt")
        assert mod.sha512(out / name) != "b" * 128

def test_sha512_matches_hashlib():
    import hashlib

    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t))
        mod = load(root)
        f = Path(t) / "x"
        f.write_bytes(b"hello world" * 1000)
        assert mod.sha512(f) == hashlib.sha512(b"hello world" * 1000).hexdigest()


