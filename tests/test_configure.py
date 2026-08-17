"""Tests for configure-time generated-file extraction (extract_configure.py).

Synthetic --trace-format=json-v1 streams, so no cmake dependency. Covers: only
project-owned configure_file events are kept (CMake-internal templates dropped),
is_compile_input flags only header/source outputs on an include path, and
options/template are captured.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from extract_configure import parse_configure_trace

SRC = "/proj/src"
BUILD = "/proj/build"


def _trace(*events):
    """Write events as a json-v1 trace stream; return its path."""
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for e in events:
        f.write(json.dumps(e) + "\n")
    f.close()
    return f.name


def _cf(src, dst, *opts):
    return {"cmd": "configure_file", "args": [src, dst, *opts],
            "file": SRC + "/CMakeLists.txt", "line": 1}


def test_compile_input_header_flagged():
    # a generated header landing on an include dir -> is_compile_input
    t = _trace(_cf(f"{SRC}/zconf.h.cmakein", f"{BUILD}/zconf.h"))
    cfs = parse_configure_trace(t, SRC, BUILD, include_dirs=[BUILD])
    os.unlink(t)
    assert len(cfs) == 1
    c = cfs[0]
    assert c.name == "zconf.h"
    assert c.is_compile_input
    assert c.template == f"{SRC}/zconf.h.cmakein"


def test_pc_and_install_files_are_benign():
    # .pc / install .cmake outputs are NOT compile inputs even on an include dir
    t = _trace(
        _cf(f"{SRC}/zlib.pc.cmakein", f"{BUILD}/zlib.pc", "@ONLY"),
        _cf(f"{SRC}/Config.cmake.in", f"{BUILD}/ZLIBConfig.cmake", "@ONLY"),
    )
    cfs = parse_configure_trace(t, SRC, BUILD, include_dirs=[BUILD])
    os.unlink(t)
    assert len(cfs) == 2
    assert not any(c.is_compile_input for c in cfs)


def test_cmake_internal_templates_dropped():
    # configure_file whose template is outside the project tree (CMake's own
    # toolchain/CPack machinery) must be dropped.
    t = _trace(
        _cf("/opt/cmake/Modules/CMakeSystem.cmake.in", f"{BUILD}/CMakeSystem.cmake"),
        _cf(f"{SRC}/zconf.h.cmakein", f"{BUILD}/zconf.h"),
    )
    cfs = parse_configure_trace(t, SRC, BUILD, include_dirs=[BUILD])
    os.unlink(t)
    assert [c.name for c in cfs] == ["zconf.h"]


def test_options_captured():
    t = _trace(_cf(f"{SRC}/foo.h.in", f"{BUILD}/foo.h", "@ONLY"))
    cfs = parse_configure_trace(t, SRC, BUILD, include_dirs=[BUILD])
    os.unlink(t)
    assert cfs[0].options == ("@ONLY",)


def test_no_include_dirs_means_not_compile_input():
    # without include dirs we cannot assert compile-input status -> False (we do
    # not guess); the file is still recorded.
    t = _trace(_cf(f"{SRC}/zconf.h.cmakein", f"{BUILD}/zconf.h"))
    cfs = parse_configure_trace(t, SRC, BUILD, include_dirs=[])
    os.unlink(t)
    assert len(cfs) == 1 and not cfs[0].is_compile_input

# No `if __name__ == "__main__"` runner here on purpose. There used to be one in
# every test file, and in this file it sat MID-FILE -- so four tests appended after
# it were defined, never called, and the file still printed "6/6 passed". The third
# instance of this session's recurring bug: a report that cannot count what it does
# not reach. `python3 tests/run_all.py` enumerates the module instead, so a test's
# POSITION in the file cannot decide whether it runs; it also fails if a file
# defines no tests at all. Run a single file with `run_all.py <name-substring>`.
