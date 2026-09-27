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

"""Ask the compiler what it injects, so `toolchain_includes` is fact, not recall.

`bazel aquery` shows the argv Bazel HANDS to the compiler. Anything the
compiler adds after that -- a wrapper script's `--sysroot`, a patched driver's
`-idirafter $STAGING_DIR/usr/include` (OpenWrt), the sysroot's builtin search
dirs, ccache/distcc/vendor-SDK shims -- is invisible to the diff and shows up
as an `includes_diff` on every TU, because the reference build (CMake's
CMAKE_C_FLAGS) spells those roots out. The any2bazel.json lever for that is
`toolchain_includes`, and its entries must be true: this probe runs the very
compiler command the Bazel toolchain runs, with the env it runs under, and
prints what the driver actually does:

  * `-v -E -x <lang> /dev/null`  -> the effective `#include "..."` / `<...>`
                                    search lists, the cc1 command with every
                                    include flag the driver injected, the
                                    driver-level options (COLLECT_GCC_OPTIONS)
                                    a wrapper prepended
  * `-### -x <lang> /dev/null -o` -> the link command: `-L` dirs, `-rpath-link`,
                                    `--sysroot` the driver adds

and ends with the JSON snippet to paste. Nothing is compiled or linked
(`-E` to nowhere; `-###` prints without executing).

Usage:
    python3 scripts/probe_toolchain.py [--env KEY=VAL]... [--lang c|c++] \
        [--json] -- <compiler> [compiler args...]

    # a plain host toolchain: what gcc searches with no -I at all
    python3 scripts/probe_toolchain.py -- /usr/bin/gcc
    # a cross toolchain with a wrapper (OpenWrt): the driver Bazel's
    # cc_toolchain_config names, with the env its `env_set` feature gives it
    python3 scripts/probe_toolchain.py --env STAGING_DIR=/…/staging_dir/target-… \
        -- /…/toolchain/bin/mips64-openwrt-linux-musl-gcc

Run it twice (with and without a wrapper's gating variable) to see what the
variable adds. Every root printed is a search directory the compiler uses
with NO `-I` on the command line -- exactly what Bazel's argv will never
show and the reference's will.
"""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

# Compile-side flags that name a search directory, joined (-Ifoo) or split
# (-I foo). clang's -### spells its builtin dirs as -internal-isystem.
_INCLUDE_FLAGS = ("-I", "-isystem", "-idirafter", "-iquote", "-iprefix",
                  "-iwithprefix", "-iwithprefixbefore", "-isysroot",
                  "-internal-isystem", "-internal-externc-isystem")
# Link-side search directories.
_LINK_DIR_FLAGS = ("-L",)


def _norm(path: str) -> str:
    """Lexical normalization only (gcc prints `bin/../lib/gcc/../../…`); no
    realpath, so the printed spelling stays comparable to what CMake passed."""
    return posixpath.normpath(path.replace(os.sep, "/")) if path else path


def _dedup(items):
    seen = set()
    out = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _split_dir_flags(argv: List[str], flags: Tuple[str, ...]) -> List[Tuple[str, str]]:
    """(flag, dir) pairs from an argv, accepting `-Ifoo` and `-I foo`. Longest
    flag wins so `-isystem` is not read as `-i` + `system`. `--sysroot=` and
    `-rpath-link=` are handled by their own readers."""
    out = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        hit = None
        for f in sorted(flags, key=len, reverse=True):
            if tok == f and i + 1 < len(argv):
                hit = (f, argv[i + 1]); i += 2; break
            if tok.startswith(f) and len(tok) > len(f) and not tok[len(f)].startswith("-"):
                hit = (f, tok[len(f):]); i += 1; break
        if hit is None:
            i += 1
            continue
        out.append((hit[0], _norm(hit[1])))
    return out


def _sysroot(argv: List[str]) -> Optional[str]:
    for i, tok in enumerate(argv):
        if tok.startswith("--sysroot="):
            return _norm(tok[len("--sysroot="):])
        if tok == "--sysroot" and i + 1 < len(argv):
            return _norm(argv[i + 1])
    return None


def _rpath_links(argv: List[str]) -> List[str]:
    """-rpath-link=A:B, -rpath-link A:B, and the -Wl,-rpath-link=… form a
    driver forwards; each colon-separated dir becomes one entry."""
    out = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        val = None
        if tok.startswith("-Wl,"):
            parts = tok[4:].split(",")
            for j, p in enumerate(parts):
                if p.startswith("-rpath-link="):
                    val = p[len("-rpath-link="):]
                elif p == "-rpath-link" and j + 1 < len(parts):
                    val = parts[j + 1]
        elif tok.startswith("-rpath-link="):
            val = tok[len("-rpath-link="):]
        elif tok == "-rpath-link" and i + 1 < len(argv):
            val = argv[i + 1]; i += 1
        if val:
            out.extend(_norm(p) for p in val.split(":") if p)
        i += 1
    return _dedup(out)


def _commands(stderr: str) -> List[List[str]]:
    """The sub-commands a `-v`/`-###` run prints: lines that shlex-split into
    an argv whose first token is a program path (gcc indents them with one
    space; clang quotes every token). Other lines (banners, search lists,
    COLLECT_* env) are not commands."""
    cmds = []
    for line in stderr.splitlines():
        if not line.startswith((" ", '"')):
            continue
        try:
            argv = shlex.split(line)
        except ValueError:
            continue
        if argv and ("/" in argv[0] or argv[0].endswith(("cc1", "cc1plus", "clang"))):
            cmds.append(argv)
    return cmds


def _is_compiler_proper(argv0: str) -> bool:
    base = os.path.basename(argv0)
    return base in ("cc1", "cc1plus", "cc1obj", "cc1objplus") or base.startswith("clang")


def _is_linker(argv0: str) -> bool:
    base = os.path.basename(argv0)
    return (base.startswith(("collect2", "ld", "lld")) or base.endswith(("-ld", ".lld"))
            or base in ("ld64", "ld64.lld", "link"))


def parse_verbose(stderr: str, probe_args: List[str]) -> Dict:
    """Read a `-v -E` run: the two search lists, ignored (nonexistent)
    directories, the compiler-proper command's injected include flags, and
    the driver-level options a wrapper/specs added ahead of the probe's own
    args (COLLECT_GCC_OPTIONS minus what we passed)."""
    quote: List[str] = []
    angle: List[str] = []
    nonexistent: List[str] = []
    collect_opts: List[str] = []
    mode = None
    for line in stderr.splitlines():
        if line.startswith('#include "..." search starts here:'):
            mode = "quote"; continue
        if line.startswith("#include <...> search starts here:"):
            mode = "angle"; continue
        if line.startswith("End of search list."):
            mode = None; continue
        if line.startswith("ignoring nonexistent directory"):
            q = line.split('"')
            if len(q) >= 2:
                nonexistent.append(_norm(q[1]))
            continue
        if line.startswith("COLLECT_GCC_OPTIONS="):
            collect_opts = shlex.split(line[len("COLLECT_GCC_OPTIONS="):])
            continue
        if mode and line.startswith(" "):
            d = line.strip()
            if not d:
                continue
            # clang annotates framework dirs: "/path (framework directory)"
            if d.endswith(" (framework directory)"):
                d = d[:-len(" (framework directory)")]
            (quote if mode == "quote" else angle).append(_norm(d))
    own = set(probe_args)
    driver_added = [o for o in collect_opts if o not in own] if collect_opts else []
    compile_cmd = next((c for c in _commands(stderr) if _is_compiler_proper(c[0])), None)
    return {
        "include_search": {"quote": _dedup(quote), "angle": _dedup(angle)},
        "nonexistent": _dedup(nonexistent),
        "driver_options": driver_added,
        "compile_command": None if compile_cmd is None else {
            "argv0": compile_cmd[0],
            "include_flags": _split_dir_flags(compile_cmd, _INCLUDE_FLAGS),
            "sysroot": _sysroot(compile_cmd),
        },
    }


def parse_link(stderr: str) -> Optional[Dict]:
    """Read a `-###` run's link command: -L dirs, -rpath-link dirs, sysroot."""
    link = next((c for c in _commands(stderr) if _is_linker(c[0])), None)
    if link is None:
        return None
    return {
        "argv0": link[0],
        "library_dirs": _dedup(d for _, d in _split_dir_flags(link, _LINK_DIR_FLAGS)),
        "rpath_link": _rpath_links(link),
        "sysroot": _sysroot(link),
    }


def probe(compiler_argv: List[str], env_overrides: Dict[str, str],
          lang: str = "c") -> Dict:
    """Run both probes and assemble the report. Raises RuntimeError with the
    driver's stderr if the compile probe fails (a wrapper refusing to run
    without its gating variable is the typical cause -- pass --env)."""
    env = dict(os.environ)
    env.update(env_overrides)
    compile_args = ["-v", "-E", "-x", lang, "/dev/null"]
    p = subprocess.run(compiler_argv + compile_args, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"{' '.join(compiler_argv + compile_args)} exited {p.returncode}:\n"
            + p.stderr.rstrip())
    report = parse_verbose(p.stderr, compile_args)
    # link probe: -### prints the commands and runs nothing; the -o target is
    # never created.
    link_args = ["-###", "-x", lang, "/dev/null", "-o", "/dev/null"]
    q = subprocess.run(compiler_argv + link_args, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    report["link_command"] = parse_link(q.stderr) if q.returncode == 0 else None
    report["link_probe_error"] = None if q.returncode == 0 else q.stderr.rstrip()
    resolved = shutil.which(compiler_argv[0]) or compiler_argv[0]
    dirs = report["include_search"]["quote"] + report["include_search"]["angle"]
    report["realpaths"] = {d: os.path.realpath(d) for d in dirs
                           if os.path.isdir(d) and os.path.realpath(d) != d}
    report.update({
        "compiler": compiler_argv,
        "resolved": os.path.realpath(resolved) if os.path.exists(resolved) else resolved,
        "env": dict(env_overrides),
        "lang": lang,
        # What to paste: every directory on the <...> search list is one the
        # compiler searches with no -I on the command line. Verify each against
        # the reference's argv before pasting (a root the reference does not
        # pass is not needed -- and would fail the required-on-cmake check).
        "toolchain_includes": list(report["include_search"]["angle"]),
    })
    return report


def render(r: Dict) -> str:
    L = []
    L.append(f"compiler : {' '.join(r['compiler'])}")
    if r["resolved"] != r["compiler"][0]:
        L.append(f"           -> {r['resolved']}")
    L.append("env      : " + (" ".join(f"{k}={v}" for k, v in r["env"].items()) or "(inherited only)"))
    L.append(f"language : {r['lang']}")

    L.append("\ndriver options added by the wrapper/specs (not in the probe's own argv):")
    L.extend(f"  {o}" for o in r["driver_options"]) if r["driver_options"] else L.append("  (none reported)")

    cc = r.get("compile_command")
    L.append("\ncompile: include flags the driver injected into the compiler-proper command:")
    if cc is None:
        L.append("  (no cc1/clang command found in -v output)")
    else:
        L.append(f"  [{cc['argv0']}]")
        if cc["sysroot"]:
            L.append(f"  --sysroot {cc['sysroot']}")
        if cc["include_flags"]:
            L.extend(f"  {f} {d}" for f, d in cc["include_flags"])
        else:
            L.append("  (none)")

    real = r.get("realpaths", {})
    for key, title in (("quote", '#include "..." search list:'),
                       ("angle", "#include <...> search list:")):
        L.append(f"\n{title}")
        dirs = r["include_search"][key]
        if not dirs:
            L.append("  (none)")
        # a symlinked dir is shown with its target: the reference may spell
        # the same directory the other way, and the config must use the
        # reference's spelling (the diff compares paths, never the disk)
        L.extend(f"  {d}" + (f"  (-> {real[d]})" if d in real else "") for d in dirs)
    if r["nonexistent"]:
        L.append("\nignored nonexistent directories:")
        L.extend(f"  {d}" for d in r["nonexistent"])

    lk = r.get("link_command")
    L.append("\nlink: search paths the driver injected into the linker command:")
    if lk is None:
        L.append("  (no linker command found)"
                 + (f"; -### failed:\n{r['link_probe_error']}" if r.get("link_probe_error") else ""))
    else:
        L.append(f"  [{lk['argv0']}]")
        if lk["sysroot"]:
            L.append(f"  --sysroot {lk['sysroot']}")
        L.extend(f"  -L {d}" for d in lk["library_dirs"])
        L.extend(f"  -rpath-link {d}" for d in lk["rpath_link"])
        if not lk["library_dirs"] and not lk["rpath_link"]:
            L.append("  (none)")

    L.append("\nany2bazel.json -- keep only the roots the reference passes on every TU,"
             "\nspelled the way the reference's argv spells them:")
    L.append('  "toolchain_includes": ' + json.dumps(r["toolchain_includes"], indent=4))
    return "\n".join(L)


def main(argv: List[str]) -> int:
    env: Dict[str, str] = {}
    lang = "c"
    as_json = False
    i = 0
    while i < len(argv) and argv[i] != "--":
        a = argv[i]
        if a == "--env" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, v = argv[i + 1].split("=", 1); env[k] = v; i += 2; continue
        if a == "--lang" and i + 1 < len(argv):
            lang = argv[i + 1]; i += 2; continue
        if a == "--json":
            as_json = True; i += 1; continue
        break
    compiler = argv[i + 1:] if i < len(argv) and argv[i] == "--" else argv[i:]
    if not compiler:
        print("usage: probe_toolchain.py [--env KEY=VAL]... [--lang c|c++] [--json] "
              "-- <compiler> [args...]", file=sys.stderr)
        return 2
    try:
        r = probe(compiler, env, lang)
    except (RuntimeError, OSError) as e:
        print(f"probe_toolchain: {e}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2) if as_json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
