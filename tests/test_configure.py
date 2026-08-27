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
