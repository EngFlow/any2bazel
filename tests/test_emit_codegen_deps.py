"""Tests for the ninja-DEPENDS invariant in the Ladybird codegen emitter.

The invariant: when turning a CMake/ninja CUSTOM_COMMAND into a Bazel genrule,
the *declared dependency list of the build edge* is authoritative for `srcs` --
the command line is not. A generator may read inputs it was never handed as
arguments; e.g. Ladybird's `generate_dom_tree.py` follows a
`<link rel="stylesheet" href="MediaControls.css">` out of its input HTML, so
`MediaControls.css` is a real input that never appears on the command line.
CMake declares it in DEPENDS; an emitter that scrapes only the command line
drops it, and the sandboxed Bazel action then dies with FileNotFoundError.

So: srcs == union(command-line paths, in-package DEPENDS entries).
"""

import importlib.util
import os
import sys

_EMITTER = os.path.join(
    os.path.dirname(__file__), "..", "examples", "ladybird", "emit_codegen_bazel.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("emit_codegen_bazel", _EMITTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


emit = _load()
ROOT = emit.ROOT
PKG = ROOT + "/Libraries/LibWeb"


def _dom_tree_command():
    """The real generate_dom_tree.py invocation, as it appears in build.ninja."""
    return (
        "/usr/bin/python3 %s/Meta/Generators/generate_dom_tree.py"
        " -h HTML/MediaControlsDOM.h.tmp -c HTML/MediaControlsDOM.cpp.tmp"
        " -i %s/HTML/MediaControls.html -s MediaControlsDOM -n Web::HTML"
        " --html-tags %s/HTML/TagNames.h" % (ROOT, PKG, PKG)
    )


def test_depends_only_input_is_added_to_srcs():
    # MediaControls.css is in DEPENDS but NOT on the command line.
    declared = [
        ROOT + "/Meta/Generators/generate_dom_tree.py",
        PKG + "/HTML/MediaControls.html",
        PKG + "/HTML/TagNames.h",
        PKG + "/HTML/MediaControls.css",
    ]
    _, d = emit.convert(_dom_tree_command(), PKG, declared)
    assert "HTML/MediaControls.css" in d["srcs"], d["srcs"]
    # ...and it stays off the command line: it is an implicit read, not an arg.
    assert "MediaControls.css" not in d["args"]


def test_command_line_only_inputs_still_declared():
    _, d = emit.convert(_dom_tree_command(), PKG, [])
    assert "HTML/MediaControls.html" in d["srcs"]
    assert "HTML/TagNames.h" in d["srcs"]


def test_no_duplicate_srcs_when_depends_repeats_command_line():
    declared = [PKG + "/HTML/MediaControls.html", PKG + "/HTML/TagNames.h"]
    _, d = emit.convert(_dom_tree_command(), PKG, declared)
    assert len(d["srcs"]) == len(set(d["srcs"])), d["srcs"]


def test_relative_traversal_in_depends_is_normalized():
    # The ImageDecoder IPC edges reach their .ipc input through a `../..` path;
    # normalizing keeps it from being mistaken for a second, distinct input.
    declared = [os.path.normpath(PKG + "/../LibWeb/HTML/MediaControls.css")]
    _, d = emit.convert(_dom_tree_command(), PKG, declared)
    assert d["srcs"].count("HTML/MediaControls.css") == 1, d["srcs"]


def test_generator_script_not_added_as_src():
    # The Meta/Generators tree is staged via the //Meta:generators filegroup.
    declared = [ROOT + "/Meta/Generators/generate_dom_tree.py"]
    _, d = emit.convert(_dom_tree_command(), PKG, declared)
    assert not any("generate_dom_tree.py" in s for s in d["srcs"]), d["srcs"]


def test_outs_come_from_tmp_arguments():
    _, d = emit.convert(_dom_tree_command(), PKG, [])
    assert d["outs"] == ["HTML/MediaControlsDOM.h", "HTML/MediaControlsDOM.cpp"]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
