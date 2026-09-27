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

"""Tests for probe_toolchain.py (what does the compiler inject?).

The parsers are exercised on captured gcc / clang / OpenWrt-driver output
shapes, and the end-to-end probe on a FAKE compiler: a shell script that
behaves like OpenWrt's patched gcc driver -- it adds `-idirafter
$STAGING_DIR/usr/include` and `-L$STAGING_DIR/usr/lib` only when STAGING_DIR
is set -- so the env-gated case is covered without a cross toolchain.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from probe_toolchain import main, parse_link, parse_verbose, probe

_PROBE = os.path.join(os.path.dirname(__file__), "..", "scripts", "probe_toolchain.py")

# A gcc-shaped `-v -E` stderr, the OpenWrt driver's: the shell wrapper's
# --sysroot shows in COLLECT_GCC_OPTIONS, the patched driver's -idirafter on
# the cc1 line, one nonexistent dir, and the two search lists.
_GCC_VERBOSE = """\
Reading specs from /tc/lib/gcc/mips64-openwrt-linux-musl/14.4.0/specs
Target: mips64-openwrt-linux-musl
COLLECT_GCC_OPTIONS='--sysroot=/tc/bin//..' '-v' '-E' '-x' 'c' '-march=mips64' '-EB'
 /tc/bin/../libexec/gcc/mips64-openwrt-linux-musl/14.4.0/cc1 -E -quiet -v -iprefix /tc/bin/../lib/gcc/mips64-openwrt-linux-musl/14.4.0/ -isysroot /tc/bin//.. -idirafter /stg/usr/include /dev/null -meb -march=mips64
ignoring nonexistent directory "/tc/bin//../usr/local/include"
ignoring duplicate directory "/tc/bin/../lib/gcc/../../lib/gcc/mips64-openwrt-linux-musl/14.4.0/include"
#include "..." search starts here:
#include <...> search starts here:
 /tc/bin/../lib/gcc/mips64-openwrt-linux-musl/14.4.0/../../../../mips64-openwrt-linux-musl/sys-include
 /tc/bin/../lib/gcc/mips64-openwrt-linux-musl/14.4.0/include
 /stg/usr/include
End of search list.
COMPILER_PATH=/tc/bin/../libexec/gcc/mips64-openwrt-linux-musl/14.4.0/
COLLECT_GCC_OPTIONS='--sysroot=/tc/bin//..' '-v' '-E' '-x' 'c' '-march=mips64' '-EB'
"""

_GCC_LINK = """\
COLLECT_GCC_OPTIONS='--sysroot=/tc/bin//..' '-o' '/dev/null' '-march=mips64'
 /tc/bin/../libexec/gcc/mips64-openwrt-linux-musl/14.4.0/cc1 -quiet -idirafter /stg/usr/include /dev/null -o /tmp/cc1.s
 /tc/bin/../lib/gcc/mips64-openwrt-linux-musl/14.4.0/../../../../mips64-openwrt-linux-musl/bin/as -EB -o /tmp/cc1.o /tmp/cc1.s
 /tc/bin/../libexec/gcc/mips64-openwrt-linux-musl/14.4.0/collect2 -plugin /tc/liblto_plugin.so "--sysroot=/tc/bin//.." --eh-frame-hdr -o /dev/null -L /stg/usr/lib -rpath-link /stg/usr/lib -L/tc/bin/../lib/gcc/mips64-openwrt-linux-musl/14.4.0 -L/tc/bin/../lib/gcc /tmp/cc1.o -lgcc "-rpath-link=/tc/bin//../lib:/tc/bin//../usr/lib" -lc
"""

# clang quotes every token and spells builtin dirs -internal-isystem.
_CLANG_VERBOSE = """\
clang version 23.1.0
 "/opt/llvm/bin/clang-23" "-cc1" "-triple" "x86_64-unknown-linux-gnu" "-E" "-internal-isystem" "/opt/llvm/lib/clang/23/include" "-internal-externc-isystem" "/usr/include" "-x" "c" "/dev/null"
#include "..." search starts here:
#include <...> search starts here:
 /opt/llvm/lib/clang/23/include
 /usr/include
End of search list.
"""

_CLANG_LINK = """\
 "/opt/llvm/bin/clang-23" "-cc1" "-triple" "x86_64-unknown-linux-gnu" "-emit-obj" "-x" "c" "/dev/null" "-o" "/tmp/x.o"
 "/usr/bin/ld" "--sysroot=/" "-o" "/dev/null" "-L/opt/llvm/lib/clang/23/lib/x86_64-unknown-linux-gnu" "-L/usr/lib64" "/tmp/x.o" "-lc"
"""

# The fake driver: OpenWrt's behaviour in 20 lines of sh. Prints gcc-shaped
# -v output on `-v`, gcc-shaped command lines on `-###`, and gates the
# staging-dir injection on STAGING_DIR exactly like the patched gcc.c.
_FAKE_CC = r"""#!/bin/sh
extra_i=""; extra_l=""
if [ -n "$STAGING_DIR" ]; then
  extra_i="-idirafter $STAGING_DIR/usr/include"
  extra_l="-L$STAGING_DIR/usr/lib -rpath-link=$STAGING_DIR/usr/lib"
fi
case " $* " in
  *" -### "*)
    echo " /fake/libexec/cc1 -quiet -isysroot /fake $extra_i /dev/null -o /tmp/f.s" >&2
    echo " /fake/libexec/collect2 --sysroot=/fake -o /dev/null $extra_l -L/fake/lib/gcc /tmp/f.o -lc" >&2
    ;;
  *" -v "*)
    cat >&2 <<EOF
COLLECT_GCC_OPTIONS='--sysroot=/fake' '-v' '-E' '-x' 'c'
 /fake/libexec/cc1 -E -quiet -v -isysroot /fake $extra_i /dev/null
ignoring nonexistent directory "/fake/usr/local/include"
#include "..." search starts here:
#include <...> search starts here:
 /fake/bin/../include
 ${STAGING_DIR:+$STAGING_DIR/usr/include}
End of search list.
EOF
    ;;
  *) echo "fake-cc: unexpected args: $*" >&2; exit 3 ;;
esac
exit 0
"""


def _fake_compiler(body=_FAKE_CC):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "fake-gcc")
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def test_parse_verbose_gcc_shape():
    r = parse_verbose(_GCC_VERBOSE, ["-v", "-E", "-x", "c", "/dev/null"])
    # search list: normalized (bin/../lib/gcc/../../.. collapsed), in order
    assert r["include_search"]["angle"] == [
        "/tc/mips64-openwrt-linux-musl/sys-include",
        "/tc/lib/gcc/mips64-openwrt-linux-musl/14.4.0/include",
        "/stg/usr/include"], r
    assert r["include_search"]["quote"] == []
    assert r["nonexistent"] == ["/tc/usr/local/include"]
    # the driver-level options minus the probe's own args = what the wrapper
    # and the specs prepended
    assert r["driver_options"] == ["--sysroot=/tc/bin//..", "-march=mips64", "-EB"], r
    cc = r["compile_command"]
    assert cc["argv0"].endswith("/cc1")
    assert ("-idirafter", "/stg/usr/include") in cc["include_flags"]
    assert ("-isysroot", "/tc") in cc["include_flags"]
    assert ("-iprefix", "/tc/lib/gcc/mips64-openwrt-linux-musl/14.4.0") in cc["include_flags"]


def test_parse_link_gcc_shape():
    lk = parse_link(_GCC_LINK)
    assert lk["argv0"].endswith("/collect2")
    assert lk["sysroot"] == "/tc"
    assert lk["library_dirs"] == [
        "/stg/usr/lib", "/tc/lib/gcc/mips64-openwrt-linux-musl/14.4.0", "/tc/lib/gcc"], lk
    # split and joined -rpath-link forms, colon lists exploded, deduped
    assert lk["rpath_link"] == ["/stg/usr/lib", "/tc/lib", "/tc/usr/lib"], lk


def test_parse_clang_shapes():
    r = parse_verbose(_CLANG_VERBOSE, ["-v", "-E", "-x", "c", "/dev/null"])
    assert r["include_search"]["angle"] == ["/opt/llvm/lib/clang/23/include", "/usr/include"]
    assert r["driver_options"] == []  # clang prints no COLLECT_GCC_OPTIONS
    cc = r["compile_command"]
    assert cc["argv0"] == "/opt/llvm/bin/clang-23"
    assert ("-internal-isystem", "/opt/llvm/lib/clang/23/include") in cc["include_flags"]
    assert ("-internal-externc-isystem", "/usr/include") in cc["include_flags"]
    lk = parse_link(_CLANG_LINK)
    assert lk["argv0"] == "/usr/bin/ld"
    assert lk["library_dirs"] == [
        "/opt/llvm/lib/clang/23/lib/x86_64-unknown-linux-gnu", "/usr/lib64"], lk
    assert lk["sysroot"] == "/"


def test_probe_env_gated_injection_is_visible():
    # Without STAGING_DIR the fake driver injects nothing; with it, the
    # staging root appears on the search list, as a cc1 -idirafter and as a
    # link -L / -rpath-link. The env is passed to the probe, never inherited
    # by accident (the test's own environment has no STAGING_DIR).
    cc = _fake_compiler()
    os.environ.pop("STAGING_DIR", None)
    bare = probe([cc], {})
    assert bare["toolchain_includes"] == ["/fake/include"], bare
    assert bare["compile_command"]["include_flags"] == [("-isysroot", "/fake")]
    assert bare["link_command"]["library_dirs"] == ["/fake/lib/gcc"]
    assert bare["link_command"]["rpath_link"] == []

    staged = probe([cc], {"STAGING_DIR": "/stg"})
    assert staged["toolchain_includes"] == ["/fake/include", "/stg/usr/include"], staged
    assert ("-idirafter", "/stg/usr/include") in staged["compile_command"]["include_flags"]
    assert staged["link_command"]["library_dirs"] == ["/stg/usr/lib", "/fake/lib/gcc"]
    assert staged["link_command"]["rpath_link"] == ["/stg/usr/lib"]
    assert staged["driver_options"] == ["--sysroot=/fake"]
    assert staged["env"] == {"STAGING_DIR": "/stg"}
    assert staged["nonexistent"] == ["/fake/usr/local/include"]
    # the fake driver is not a symlink, so nothing to resolve
    assert staged["realpaths"] == {}


def test_cli_json_and_text():
    cc = _fake_compiler()
    p = subprocess.run([sys.executable, _PROBE, "--env", "STAGING_DIR=/stg", "--json",
                        "--", cc], capture_output=True, text=True, check=True)
    r = json.loads(p.stdout)
    assert r["toolchain_includes"] == ["/fake/include", "/stg/usr/include"]
    assert r["compiler"] == [cc] and r["lang"] == "c"
    p = subprocess.run([sys.executable, _PROBE, "--env", "STAGING_DIR=/stg", "--", cc],
                       capture_output=True, text=True, check=True)
    assert "-idirafter /stg/usr/include" in p.stdout
    assert "-L /stg/usr/lib" in p.stdout
    assert '"toolchain_includes"' in p.stdout and '"/stg/usr/include"' in p.stdout
    # --lang is forwarded as -x <lang>
    p = subprocess.run([sys.executable, _PROBE, "--lang", "c++", "--json", "--", cc],
                       capture_output=True, text=True, check=True)
    assert json.loads(p.stdout)["lang"] == "c++"


def test_failing_compiler_is_an_error_with_its_stderr():
    # a wrapper that refuses to run (e.g. without its gating variable) must
    # fail the probe loudly, with the driver's own message, not print an
    # empty search list
    cc = _fake_compiler("#!/bin/sh\necho 'STAGING_DIR not defined' >&2\nexit 1\n")
    import io
    from contextlib import redirect_stderr
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--", cc])
    assert rc == 1
    assert "STAGING_DIR not defined" in err.getvalue()
    assert main([]) == 2  # usage


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
