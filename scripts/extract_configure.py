# Copyright 2026 EngFlow GmbH
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

"""Extract configure-time generated files from a CMake --trace JSON stream.

`configure_file(src dst [@ONLY])` runs at CMake *configure* time, leaving NO
node in the codemodel build graph (verified on zlib: zero isGenerated sources,
zero custom-command targets, despite generating zconf.h). The only faithful,
non-guessing way to see these is the trace:

    cmake -S <src> -B <build> --trace-expand --trace-format=json-v1 \
        2> trace.jsonl    # (trace goes to stderr)

Each configure_file event records (src, dst, options) plus the CMakeLists file
and line. We parse those into ConfiguredFile entries -- capturing BOTH the output
(for content matching) and the template+options (so the Bazel side can pick a
repo/workspace rule and reproduce the substitution). This is configure-time
generation ONLY; build-time generation (action-graph custom commands) is a
separate, not-yet-modeled concept.

We keep only PROJECT-owned generation (templates under the source or build
tree), dropping CMake's own machinery (toolchain/CPack templates under the CMake
install prefix). `is_compile_input` marks outputs that land on a known include
path -- those are the ones that affect the build and must match by content.
"""

from __future__ import annotations

import json
import os
from typing import Iterable, List, Optional

from model import ConfiguredFile

# Extensions whose generated output actually feeds compilation. A configure_file
# can also emit .pc / install .cmake / test CMakeLists.txt -- those are NOT
# compile inputs and must not be flagged as such.
_COMPILE_INPUT_EXTS = (".h", ".hpp", ".hh", ".hxx", ".inc", ".ipp",
                       ".c", ".cc", ".cpp", ".cxx", ".def")


def _under(path: str, roots: Iterable[str]) -> bool:
    ap = os.path.abspath(path)
    for r in roots:
        if not r:
            continue
        r = os.path.abspath(r)
        if ap == r or ap.startswith(r + os.sep):
            return True
    return False


def parse_configure_trace(
    trace_path: str,
    source_root: str,
    build_root: str,
    include_dirs: Optional[Iterable[str]] = None,
) -> List[ConfiguredFile]:
    """Parse configure_file events from a --trace-format=json-v1 stream.

    project_roots = {source_root, build_root}: a configure_file whose TEMPLATE
    lives outside both is CMake-internal (toolchain/CPack) and dropped.
    include_dirs: directories on the compile include path; an output under one
    is flagged is_compile_input (it can change what TUs see).
    """
    project_roots = [source_root, build_root]
    inc = [os.path.abspath(d) for d in (include_dirs or [])]
    out: List[ConfiguredFile] = []
    seen = set()

    with open(trace_path) as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("cmd") != "configure_file":
                continue
            args = [a for a in e.get("args", []) if a]
            if len(args) < 2:
                continue
            src, dst = args[0], args[1]
            options = tuple(args[2:])

            # project-owned only: template under source or build tree
            if not _under(src, project_roots):
                continue
            # canonical name: output relative to build_root (the gen root), else
            # to source_root, else basename
            name = _canonical_name(dst, build_root, source_root)
            if name in seen:
                continue
            seen.add(name)

            out.append(ConfiguredFile(
                name=name,
                output_path=os.path.abspath(dst),
                template=os.path.abspath(src),
                options=options,
                is_compile_input=(
                    dst.endswith(_COMPILE_INPUT_EXTS) and bool(inc)
                    and _under(dst, inc)),
            ))
    return out


def _canonical_name(dst: str, build_root: str, source_root: str) -> str:
    ad = os.path.abspath(dst)
    for root in (build_root, source_root):
        if root:
            r = os.path.abspath(root)
            if ad == r or ad.startswith(r + os.sep):
                return os.path.relpath(ad, r).replace(os.sep, "/")
    return os.path.basename(ad)
