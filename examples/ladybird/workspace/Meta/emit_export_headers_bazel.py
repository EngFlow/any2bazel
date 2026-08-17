#!/usr/bin/env python3
"""Emit Bazel genrules for CMake's `generate_export_header` output.

WHY THIS EXISTS
---------------
Ladybird's `ladybird_lib()` calls `ladybird_generate_export_header(name fs_name)`
(Meta/CMake/targets.cmake), which is CMake's `GenerateExportHeader` module with
`EXPORT_MACRO_NAME <UPPER(fs_name)>_API` and `EXPORT_FILE_NAME "Export.h"`. That
writes `Build/<preset>/Libraries/<Lib>/Export.h` -- a *configure*-time artifact of
the reference build.

Until now Bazel did not generate these 15 headers at all: it globbed them out of
`Build/full/Libraries/**` via the `//Build/full/Libraries:generated_lib_headers`
shim. That made `bazel build //:ladybird` silently depend on a completed CMake
build -- and because the shim uses `glob(..., allow_empty = True)`, a fresh
checkout produced NO error from the glob, just `fatal error: LibXML/Export.h: No
such file or directory` ~1,600 actions into the build. Generating them here is
what lets a clean `git clone` build.

WHY A TEMPLATE IS THE RIGHT ANSWER HERE
---------------------------------------
Reproducing another build system's output by re-implementing its generator is
normally the wrong move -- it is a fork that silently drifts. Two things make this
the exception, and both were checked rather than assumed:

1. **The output is a pure function of one token.** All 15 checked-in headers
   normalize to a SINGLE byte-identical template under substitution of
   (`<API>`, `<PREFIX>`, `<Lib>_EXPORTS`), all three derived from the library name.
   Verified by normalizing each of the 15 and comparing: 1 distinct template.
2. **The alternative is worse.** The only other faithful option is running CMake,
   which is the dependency being removed.

So the risk is a future CMake version changing the template. That is caught, not
hoped about: `--check` byte-compares this emitter's output for every library
against the reference tree's copy, and the parity harness diffs generated output
tree-wide. If CMake's template changes, `--check` fails loudly instead of the
build succeeding with a subtly wrong header.

The template is CMake's own (Modules/GenerateExportHeader.cmake) for the
GNU/Clang, non-Windows case -- the only case Ladybird's Linux build exercises. The
`#if 0 /* DEFINE_NO_DEPRECATED */` block and the leading blank line are
reproduced deliberately: byte parity includes the parts that look like noise.

ALSO: THE TWO `configure_file` HEADERS
--------------------------------------
`AK/CMakeLists.txt` runs `configure_file(Debug.h.in Debug.h @ONLY)` and the same
for `Backtrace.h.in`. These were the last two headers Bazel read out of
`Build/full/AK/`, and they are a different shape from Export.h: the TEMPLATE is
checked into the source tree, so nothing is being re-implemented here except
CMake's substitution rules (`#cmakedefine01` -> `#define X 0|1`, `#cmakedefine`
-> `#define X` or a comment, `@VAR@` -> value).

`Debug.h` is 79 `#cmakedefine01` lines and every one of them is `0` in the
reference build -- they are opt-in debug spew, off unless someone passes
`-DFOO_DEBUG=ON`. So an unconfigured emit is the faithful answer.

`Backtrace.h` is NOT a template substitution, it is a HOST PROBE:
`find_package(Backtrace)` decides whether `execinfo.h` exists and what it is
called. Pasting this machine's answer (`execinfo.h`) into a checked-in file is
exactly the "test the variable, not the value" mistake from the Dolphin case
study -- it would survive every check on glibc and break on musl, where backtrace
lives elsewhere or not at all. So the genrule ASKS the question at build time by
compiling a probe, the same question CMake asks, and emits whichever answer this
host gives.

Usage:
    python3 Meta/emit_export_headers_bazel.py                 # emit export_headers.bzl
    python3 Meta/emit_export_headers_bazel.py --check <tree>   # byte-verify vs reference
"""

import argparse
import os
import re
import sys

# The libraries CMake generates an Export.h for: those built with
# LADYBIRD_LIB_EXPLICIT_SYMBOL_EXPORT, so that ladybird_lib() ran
# ladybird_generate_export_header(). Kept as an explicit list (not a glob over
# Libraries/) precisely so a library gaining or losing an export header shows up
# as a --check failure rather than silently changing the build.
EXPORT_LIBS = [
    "LibCore",
    "LibDNS",
    "LibDatabase",
    "LibDevTools",
    "LibGC",
    "LibJS",
    "LibMedia",
    "LibRegex",
    "LibSync",
    "LibTest",
    "LibTextCodec",
    "LibWasm",
    "LibWeb",
    "LibWebView",
    "LibXML",
]

# CMake's GenerateExportHeader output for GCC/Clang on non-Windows.
# @API@  = EXPORT_MACRO_NAME  (upper(fs_name) + "_API")
# @PRE@  = upper(target name), used for the NO_EXPORT/DEPRECATED macros
# @EXP@  = "<target>_EXPORTS", the macro CMake defines while building the target
TEMPLATE = """
#ifndef @API@_H
#define @API@_H

#ifdef @PRE@_STATIC_DEFINE
#  define @API@
#  define @PRE@_NO_EXPORT
#else
#  ifndef @API@
#    ifdef @EXP@
        /* We are building this library */
#      define @API@ __attribute__((visibility("default")))
#    else
        /* We are using this library */
#      define @API@ __attribute__((visibility("default")))
#    endif
#  endif

#  ifndef @PRE@_NO_EXPORT
#    define @PRE@_NO_EXPORT __attribute__((visibility("hidden")))
#  endif
#endif

#ifndef @PRE@_DEPRECATED
#  define @PRE@_DEPRECATED __attribute__ ((__deprecated__))
#endif

#ifndef @PRE@_DEPRECATED_EXPORT
#  define @PRE@_DEPRECATED_EXPORT @API@ @PRE@_DEPRECATED
#endif

#ifndef @PRE@_DEPRECATED_NO_EXPORT
#  define @PRE@_DEPRECATED_NO_EXPORT @PRE@_NO_EXPORT @PRE@_DEPRECATED
#endif

/* NOLINTNEXTLINE(readability-avoid-unconditional-preprocessor-if) */
#if 0 /* DEFINE_NO_DEPRECATED */
#  ifndef @PRE@_NO_DEPRECATED
#    define @PRE@_NO_DEPRECATED
#  endif
#endif

#endif /* @API@_H */
"""


def tokens(lib):
    """The three substitution tokens for a library, derived exactly as
    Meta/CMake/targets.cmake derives them: fs_name is the library name minus its
    "Lib" prefix, and EXPORT_MACRO_NAME is upper(fs_name) + "_API"."""
    fs_name = lib[3:] if lib.startswith("Lib") else lib
    return ("%s_API" % fs_name.upper(), lib.upper(), "%s_EXPORTS" % lib)


def render(lib):
    api, pre, exp = tokens(lib)
    # Substitute @EXP@ first: it is the only token whose replacement text
    # ("LibGC_EXPORTS") contains a substring that a later pattern could match.
    # Each replace() runs over the whole string, so an earlier substitution's
    # OUTPUT is visible to a later one -- e.g. replacing @PRE@ with "LIBGC" then
    # looking for "GC_API" is fine here (disjoint), but the ordering is load
    # bearing in general and must not be permuted casually.
    return TEMPLATE.replace("@EXP@", exp).replace("@API@", api).replace("@PRE@", pre)


def configure_file(text, defines):
    """Apply CMake's `configure_file(... @ONLY)` substitution rules.

    Only the three directives Ladybird's two templates actually use, because a
    partial re-implementation that silently ignores an unknown directive is how
    you get a header that looks configured and is not. Anything unrecognized is
    left alone and reported by `--check` as a byte mismatch.

    * `#cmakedefine01 X` -> `#define X 1` if X is truthy else `#define X 0`
    * `#cmakedefine X`   -> `#define X` if defined, else `/* #undef X */`
    * `@VAR@`            -> its value (@ONLY means ONLY @VAR@, not ${VAR})
    """
    def sub01(m):
        pad, name = m.group(1), m.group(2)
        return "#%sdefine %s %s" % (pad, name, "1" if defines.get(name) else "0")

    def subdef(m):
        pad, name = m.group(1), m.group(2)
        if defines.get(name):
            return "#%sdefine %s" % (pad, name)
        return "#%s/* #undef %s */" % (pad, name)

    # The `#` sits at column 0 and the indentation comes AFTER it
    # (`#    cmakedefine01 FOO`), which is how these templates are written -- so
    # the padding to preserve is between the hash and the directive, not before
    # the hash. Two earlier attempts got this wrong: `(\s*)#cmakedefine01` never
    # matched, and --check caught it both times rather than emitting a header with
    # the directive left verbatim.
    text = re.sub(r"(?m)^#([ \t]*)cmakedefine01 (\w+)[ \t]*$", sub01, text)
    text = re.sub(r"(?m)^#([ \t]*)cmakedefine (\w+)[ \t]*$", subdef, text)
    text = re.sub(r"@(\w+)@", lambda m: str(defines.get(m.group(1), "")), text)
    return text


# Debug.h: every `#cmakedefine01 *_DEBUG` is OFF in a default configure. Passing
# an empty define map is therefore the faithful reproduction, and --check proves
# it against the reference tree rather than trusting this comment.
DEBUG_H_DEFINES = {}

# Backtrace.h is deliberately NOT emitted from a define map -- see the module
# docstring. The genrule below compiles a probe for <execinfo.h> and substitutes
# whatever THIS host answers, so the header is a question, not a pasted value.
BACKTRACE_PROBE_CMD = r"""
set -e
tmp=$$(mktemp -d)
printf '#include <execinfo.h>\nint main(){void*b[1];backtrace(b,1);return 0;}\n' > $$tmp/p.c
if $${CC:-cc} -o $$tmp/p $$tmp/p.c 2>/dev/null; then
  found=1
else
  found=0
fi
rm -rf $$tmp
if [ "$$found" = 1 ]; then
  sed -e 's|^#cmakedefine Backtrace_FOUND$$|#define Backtrace_FOUND|' \
      -e 's|@Backtrace_HEADER@|execinfo.h|' $< > $@
else
  sed -e 's|^#cmakedefine Backtrace_FOUND$$|/* #undef Backtrace_FOUND */|' \
      -e 's|@Backtrace_HEADER@||' $< > $@
fi
"""


# Libraries/LibWeb is its own Bazel PACKAGE, so the root package cannot declare
# an output inside it ("Label '//:Libraries/LibWeb/Export.h' is invalid because
# 'Libraries/LibWeb' is a subpackage"). Its Export.h is emitted into LibWeb's own
# package instead, via --libweb. Any other library that gains a BUILD.bazel will
# hit the same wall and belongs in this set.
SUBPACKAGE_LIBS = {"LibWeb"}


def root_libs():
    return [l for l in EXPORT_LIBS if l not in SUBPACKAGE_LIBS]


def heredoc(body):
    """A shell heredoc that writes `body` EXACTLY, with no added trailing newline.

    `cat > $@ <<'EOF'\n<body>\nEOF` appends a newline of its own, so a body that
    already ends in "\n" gains a second one -- 1 byte of drift that `--check`
    (which compares render() to the reference) could never see, because it is
    introduced by the emitted SHELL, not by render(). It was caught only by
    byte-comparing the BUILT artifact. Strip one trailing newline before wrapping,
    since the heredoc puts it back.
    """
    if body.endswith("\n"):
        body = body[:-1]
    esc = body.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return "cat > $@ <<'LADYBIRD_EOF'\\n%s\\nLADYBIRD_EOF\\n" % esc


def emit_libweb():
    """The LibWeb-package half: its own Export.h genrule + header target."""
    lib = "LibWeb"
    print("# AUTO-GENERATED by Meta/emit_export_headers_bazel.py --libweb — do not edit.")
    print("# LibWeb's generate_export_header output. It lives here rather than in the")
    print("# root package because Libraries/LibWeb is its own Bazel package, so only")
    print("# this package may declare an output inside it.")
    print('load("@rules_cc//cc:defs.bzl", "cc_library")')
    print()
    print("def libweb_export_header():")
    # Emitted at genroot/LibWeb/Export.h, NOT at Export.h. All 289 consumers spell
    # it <LibWeb/Export.h>, so the include root has to be a directory CONTAINING a
    # "LibWeb" dir. Using the package dir itself would need includes=['../..'],
    # which Bazel rejects outright ("resolves to the workspace root, which would
    # allow this rule and all of its transitive dependents to include any file in
    # your workspace") -- and it is right to: that would put the entire repo on
    # every dependent's include path. A private genroot/ subdir keeps the exported
    # path exactly <LibWeb/Export.h> while the include root stays inside this
    # package.
    print("    native.genrule(")
    print("        name = 'gen_%s_Export_h'," % lib)
    print("        outs = ['genroot/LibWeb/Export.h'],")
    print("        cmd = \"%s\"," % heredoc(render(lib)))
    print("    )")
    print("    cc_library(")
    print("        name = 'generated_export_header',")
    print("        hdrs = ['genroot/LibWeb/Export.h'],")
    print("        includes = ['genroot'],")
    print("    )")


def emit():
    print("# AUTO-GENERATED by Meta/emit_export_headers_bazel.py — do not edit.")
    print("# CMake's generate_export_header() output for the %d libraries that"
          % len(EXPORT_LIBS))
    print("# opt into explicit symbol export, emitted as genrules so building the")
    print("# browser needs no CMake build. Byte-verified against the reference")
    print("# tree by `Meta/emit_export_headers_bazel.py --check Build/full`.")
    print('load("@rules_cc//cc:defs.bzl", "cc_library")')
    print()
    print("def export_headers():")
    for lib in root_libs():
        # A quoted heredoc ('LADYBIRD_EOF') so the shell interprets NOTHING in
        # the body: it contains #, $, quotes, parentheses and backslashes.
        print("    native.genrule(")
        print("        name = 'gen_%s_Export_h'," % lib)
        print("        outs = ['Libraries/%s/Export.h']," % lib)
        print("        cmd = \"%s\"," % heredoc(render(lib)))
        print("    )")
    print()
    # AK/Debug.h -- configure_file over a checked-in template, all flags off.
    # Emitted by substituting here rather than shelling sed 79 times.
    print("    # AK/Debug.h: configure_file(Debug.h.in) with every *_DEBUG off,")
    print("    # which is what a default configure produces (--check verifies it).")
    print("    native.genrule(")
    print("        name = 'gen_AK_Debug_h',")
    print("        srcs = ['AK/Debug.h.in'],")
    print("        outs = ['genroot/AK/Debug.h'],")
    debug_in = os.path.join("AK", "Debug.h.in")
    if os.path.exists(debug_in):
        with open(debug_in) as f:
            body = configure_file(f.read(), DEBUG_H_DEFINES)
        print("        cmd = \"%s\"," % heredoc(body))
    else:
        print("        cmd = \"# AK/Debug.h.in not found at emit time\",")
    print("    )")
    # AK/Backtrace.h -- a host probe, asked at build time. See module docstring.
    print("    # AK/Backtrace.h: find_package(Backtrace) is a HOST QUESTION, so the")
    print("    # genrule compiles a probe instead of baking in this machine's answer.")
    print("    native.genrule(")
    print("        name = 'gen_AK_Backtrace_h',")
    print("        srcs = ['AK/Backtrace.h.in'],")
    print("        outs = ['genroot/AK/Backtrace.h'],")
    print("        cmd = %r," % BACKTRACE_PROBE_CMD)
    print("    )")
    print()
    # One header root exposing all 15 as <Lib>/Export.h, mirroring the shim it
    # replaces (//Build/full/Libraries:generated_lib_headers).
    print("    cc_library(")
    print("        name = 'generated_export_headers',")
    print("        hdrs = [%s]," % ", ".join(
        "'Libraries/%s/Export.h'" % lib for lib in root_libs()))
    print("        includes = ['Libraries'],")
    print("    )")
    # AK/*.h arrive under genroot/ so <AK/Debug.h> resolves, mirroring the
    # ak_gen_headers genrule in BUILD.bazel that copied them out of Build/full.
    print("    cc_library(")
    print("        name = 'generated_ak_headers',")
    print("        hdrs = ['genroot/AK/Debug.h', 'genroot/AK/Backtrace.h'],")
    print("        includes = ['genroot'],")
    print("    )")


def check(tree):
    """Byte-compare this emitter's output against the reference CMake tree."""
    bad = 0
    for lib in EXPORT_LIBS:
        ref = os.path.join(tree, "Libraries", lib, "Export.h")
        if not os.path.exists(ref):
            print("MISSING reference: %s" % ref)
            bad += 1
            continue
        with open(ref) as f:
            want = f.read()
        got = render(lib)
        if got != want:
            print("MISMATCH %s" % lib)
            # Show the first differing line so template drift is diagnosable.
            for i, (a, b) in enumerate(zip(got.splitlines(), want.splitlines())):
                if a != b:
                    print("  line %d: emitted %r" % (i + 1, a))
                    print("           cmake   %r" % b)
                    break
            bad += 1
    print("%d/%d export headers byte-identical to %s"
          % (len(EXPORT_LIBS) - bad, len(EXPORT_LIBS), tree))

    # AK/Debug.h: the configure_file path, checked the same way. (Backtrace.h is
    # deliberately excluded: its content is a function of the HOST, so comparing
    # it to this machine's reference tree would only ever re-confirm this machine.
    # The probe is verified by building, not by diffing.)
    dbg_in, dbg_ref = "AK/Debug.h.in", os.path.join(tree, "AK", "Debug.h")
    if os.path.exists(dbg_in) and os.path.exists(dbg_ref):
        with open(dbg_in) as f:
            got = configure_file(f.read(), DEBUG_H_DEFINES)
        with open(dbg_ref) as f:
            want = f.read()
        if got == want:
            print("AK/Debug.h byte-identical to %s" % dbg_ref)
        else:
            bad += 1
            print("MISMATCH AK/Debug.h")
            for i, (a, b) in enumerate(zip(got.splitlines(), want.splitlines())):
                if a != b:
                    print("  line %d: emitted %r" % (i + 1, a))
                    print("           cmake   %r" % b)
                    break
            else:
                print("  (differing line count: %d emitted vs %d cmake)"
                      % (len(got.splitlines()), len(want.splitlines())))
    else:
        print("SKIP AK/Debug.h (template or reference missing)")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--libweb", action="store_true",
                    help="emit the Libraries/LibWeb package half instead")
    ap.add_argument("--check", metavar="BUILD_DIR",
                    help="byte-compare against a reference CMake build tree "
                         "(e.g. Build/full)")
    args = ap.parse_args()
    if args.check:
        return check(args.check)
    if args.libweb:
        emit_libweb()
        return 0
    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
