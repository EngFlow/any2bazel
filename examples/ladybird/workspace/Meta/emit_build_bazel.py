#!/usr/bin/env python3
"""Ring 1c: emit BUILD.bazel cc_library targets from the CMake reference model.

Reads model.cmake.full.json and emits one cc_library per production library,
bottom-up by dependency layer. Global copts/defines/includes live in .bazelrc
(mirrored from Meta/CMake/compile_options.cmake); per-target we emit only srcs,
the target's own <Name>_EXPORTS + Skia-style defines, its private gendir
-isystem, and deps (internal -> //:Target, external -> vcpkg shim / system).

Generated headers (Ring 1b genrules) and configure-time headers are supplied
via the global -I roots into Build/full plus //Libraries/LibWeb codegen outputs.
"""
import json, os, sys, re
from collections import defaultdict

ROOT = os.environ.get("LADYBIRD_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.environ.get("LADYBIRD_MODEL") or os.path.join(ROOT, "model.cmake.full.json")
# The reference build tree, RELATIVE to the checkout: the model records source
# and include paths relative to the repo root, so every "is this a generated
# file?" test is a string prefix on this. An env var rather than a literal
# "Build/full" because a repin needs the new reference build to coexist with the
# old one, and CMake bakes the build dir into build.ninja -- so renaming the
# directory after the fact is not an option (learned the hard way at 71fb301a).
BUILD_REL = (os.environ.get("LADYBIRD_BUILD_REL") or "Build/full").strip("/") + "/"
VCPKG = "//Meta/vcpkg"
# Ring 2 part 3: the Rust crates are BUILT BY BAZEL now (cargo_ring.bzl in this
# same package), so the labels are local -- nothing points into Build/full/cargo.
# ONE label per crate dep, exactly mirroring CMake's target_link_libraries edge:
# //:<crate>_lib carries that crate's archive AND that crate's generated FFI
# headers. Deliberately NOT a shared link group over all ten archives -- see the
# block comment in cargo.bzl: the crates have no cross-crate symbol references at
# all, and grouping them makes the linker pull one crate's objects into a target
# that never asked for them.
RUST_LIB_FMT = "//:%s_lib"
# Crates whose FFI header the OWNING library includes with no directory prefix
# (`#include <RustFFI.h>`). Those libraries additionally depend on
# //:<crate>_bare_include, which carries ONLY that crate's ffi/<prefix> dir.
# It is a separate label from //:<crate>_lib on purpose: CcInfo include dirs
# propagate transitively, and 8 of the 10 crates emit a header named RustFFI.h,
# so a shared unprefixed dir let one crate's header satisfy another library's
# bare include (LibGfx compiled against LibRegex's).
RUST_BARE_INCLUDE_FMT = "//:%s_bare_include"


def _cargo():
    sys.path.insert(0, os.path.join(ROOT, "Meta"))
    import emit_cargo_bazel
    return emit_cargo_bazel


def rust_bare_include_crates():
    """The crates whose header is included bare -- IMPORTED, not mirrored.

    This used to be a hand-kept tuple beside the cargo emitter's derived set,
    with a comment saying the two were "kept in sync". They were not: the cargo
    emitter DERIVES the set by scanning the tree for a directory-less
    `#include <X>`, and a hand-kept copy of a derived set is the finding-23 bug in
    miniature. So it is read from the one place that computes it, and adding a
    bare include anywhere in Ladybird needs no edit in either file.
    """
    cargo = _cargo()
    return {c for c, on in
            cargo.crates_included_bare(cargo._bare_scan_specs()).items() if on}


def rust_binary_crates():
    """{CMake target name -> crate} for the build_rust_binary() crates.

    CMake's dependency edge on a binary crate is a custom target named
    `<BINARY_NAME>-build` (rust_crate.cmake), and that name is what turns up in
    the model as a dep -- `cranelift-compiler-build` on LibWasm. Mapping it back
    to the crate is what lets the emitter translate that edge instead of dropping
    it, which is precisely what it was doing: LibWasm's dep on the Cranelift
    crate was silently discarded, and the build failed 1,600 actions later on
    `CraneliftFFI.h: No such file or directory`.
    """
    return {b["bin"] + "-build": b for b in _cargo().binary_specs()}

# Global defines already set in .bazelrc; do not re-emit per target.
GLOBAL_DEFINES = {
    "USE_VULKAN=1", "ENABLE_COMPILETIME_FORMAT_CHECK", "USE_FONTCONFIG=1",
    "_FORTIFY_SOURCE=3", "USE_VULKAN_DMABUF_IMAGES=1", "_FILE_OFFSET_BITS=64",
    "NDEBUG",
}
# System libs with no vcpkg .so (linked via linkopts on the final binary).
# glib/gio/gobject and xkbcommon joined at 71fb301a: upstream added a
# pkg_check_modules(GIO) for UI/Qt/ExternalURLActivationToken + ExternalURLHandler,
# and xkbcommon arrives transitively with the now-required Qt6::GuiPrivate. Their
# include roots (/usr/include/glib-2.0, /usr/lib/*/glib-2.0/include) are absolute,
# so like libdrm's they cannot be per-target copts and live in .bazelrc's
# CPLUS_INCLUDE_PATH -- see the host-tool preflight todo: this whole set is a
# configure-time host probe that neither build system derives on our side yet.
SYSTEM_LIBS = {"dl", "m", "pthread", "vulkan", "pulse",
               "gio-2.0", "gobject-2.0", "glib-2.0", "xkbcommon"}


def qt_label(nm):
    """CMake's Qt6<Module> -> rules_qt's @qt//:Qt<Module>, by RULE not by table.

    This was a three-entry dict {Qt6Core, Qt6Gui, Qt6Widgets}, which is the same
    shape as every other capture in this tree: correct for the pin that was
    measured, silent about the next one. At 71fb301a upstream made
    Qt6::Positioning REQUIRED (UI/Qt/GeolocationProviderQt.cpp), the dict had no
    Qt6Positioning key, so the dep fell through to UNKNOWN and //:ladybird failed
    to compile with

        UI/Qt/GeolocationProviderQt.h:11:10: fatal error: QGeoPositionInfo:
            No such file or directory

    rules_qt names one cc_library per module in the discovered SDK, so the
    mapping is a rename, and every module the SDK has is already a label. A
    module the SDK does NOT have must still fail -- as an UNKNOWN dep naming it,
    which is why this returns None rather than a label it has not checked.
    """
    if not nm.startswith("Qt6") or len(nm) <= 3:
        return None
    return "@qt//:Qt" + nm[len("Qt6"):]

# CMake's AUTOMOC/AUTORCC output, prebuilt under Build/full/<target>_autogen.
# Bazel runs moc/rcc itself (qt_cc_moc / qt_cc_rcc), so these are dropped from
# srcs and the autogen include roots from copts.
AUTOGEN_MARKER = "_autogen/"

# Libs that export extern "C" FFI consumed by the prebuilt Rust archive and
# also consume it back (a static-archive <-> static-archive cycle GNU ld can't
# resolve in one pass). Whole-archive them so every FFI symbol is present
# before the rust archive references it (matches the reference build linking
# these as shared libs, where the cycle resolves at runtime).
ALWAYSLINK_LIBS = {"LibUnicode", "LibWeb"}

def global_flags():
    import re
    rc = open(os.path.join(ROOT, ".bazelrc")).read()
    return set(re.findall(r"--(?:cxxopt|copt)=(\S+)", rc))

GLOBAL_FLAGS = global_flags()


def bazelrc_host_includes():
    """The include roots .bazelrc hands every action via CPLUS_INCLUDE_PATH.

    These are the host escapes README gap 3 inventories. Read here so the
    emitter can CHECK the ones it drops against them rather than dropping them
    silently.
    """
    rc = open(os.path.join(ROOT, ".bazelrc")).read()
    out = set()
    for m in re.finditer(r"--(?:host_)?action_env=CPLUS_INCLUDE_PATH=(\S+)", rc):
        out |= {p for p in m.group(1).split(":") if p}
    return out


BAZELRC_HOST_INCLUDES = bazelrc_host_includes()
# Absolute include roots the model uses that .bazelrc does NOT carry. Collected
# during emission and reported at the end: a per-target message would repeat
# once per TU, and the useful unit is "which roots does this configuration need
# that the build does not provide".
MISSING_HOST_INCLUDES = set()
# Roots that arrive with a dep edge instead of an env var, so their absence from
# CPLUS_INCLUDE_PATH is correct rather than missing. Qt comes from rules_qt as a
# real Bazel dep (@qt//:Qt<Module> carries its own include dirs) and the vcpkg
# tree rides on //Meta/vcpkg:<port> as system_includes -- both deliberately NOT
# global -isystem any more, which is what finding 33 bought.
HOST_INCLUDE_EXEMPT = ("/usr/include/x86_64-linux-gnu/qt6",
                       "/usr/lib/x86_64-linux-gnu/qt6",
                       "vcpkg_installed")


def record_host_include(path):
    if any(x in path for x in HOST_INCLUDE_EXEMPT):
        return
    if path not in BAZELRC_HOST_INCLUDES:
        MISSING_HOST_INCLUDES.add(path)


def report_host_includes():
    """Print the shortfall to stderr. Not fatal: see record_host_include."""
    if not MISSING_HOST_INCLUDES:
        return
    sys.stderr.write(
        "WARNING: %d absolute include root(s) CMake compiles with are absent "
        "from\n         .bazelrc's CPLUS_INCLUDE_PATH, so a TU that needs one "
        "will fail with\n         'No such file or directory' far from here:\n"
        % len(MISSING_HOST_INCLUDES))
    for p in sorted(MISSING_HOST_INCLUDES):
        sys.stderr.write("    %s\n" % p)
    sys.stderr.write(
        "         Add them there (they cannot be per-target copts: Bazel "
        "rejects a path\n         outside the execution root), or give the dep "
        "a Bazel label that carries them.\n")

# Flags CMake puts on a target that must NOT be copied into a Bazel copt.
#
# -fPIE is the one that matters, and it is a crash, not a nit. CMake adds it to
# every executable target (CMAKE_POSITION_INDEPENDENT_CODE + an exe => -fPIE),
# and the capture faithfully recorded it -- so the generated cc_binary carried
# `copts = ['-fPIE']`. Bazel appends per-target copts AFTER the .bazelrc's
# --copt=-fPIC, and for GCC the LAST of -fPIC/-fPIE wins. So the UI/Qt objects
# were compiled -fPIE while every library around them was -fPIC.
#
# Under -fPIE, GCC may reference extern data DIRECTLY (PC-relative) instead of
# through the GOT, and the linker then materialises the definition in the
# executable with an R_X86_64_COPY relocation. For libraries built with Qt's
# `reduce_relocations` (every official/aqt Qt SDK; Debian's is built without it)
# that is fatal: QtCore accesses its own `QCoreApplication::self` PC-relative --
# its own BSS copy -- while QtGui reads the same symbol through the GOT, which
# the copy relocation has repointed at the EXECUTABLE's BSS. QApplication's
# constructor then sets qApp in one place and QtGui reads the other, still null.
# The first emit through a null sender segfaults: QGuiApplication::screenAdded
# from QWindowSystemInterface::handleScreenAdded, inside doActivate, on
# `mov 0x8(%rdi),%rbx` with rdi = 0.
#
# Qt's own headers say so and are right: qcompilerdetection.h #errors with
# "-fPIE is not sufficient ... Compile your code with -fPIC and without -fPIE"
# -- but only when __PIC__ is unset, and Bazel passes BOTH flags, so __PIC__ is
# defined at preprocess time and the guard never fires. The build was clean and
# the binary was broken.
#
# Dropping -fPIE is not a divergence from CMake's semantics: Bazel's own
# toolchain compiles the objects of a cc_binary PIC and links -pie, which is
# what CMake was asking for. Verified by removal: with -fPIE gone the binary has
# ZERO R_X86_64_COPY relocations (39 before, incl. qApp) and the GUI starts
# against an aqt 6.9.2 SDK, where before it segfaulted for both the xcb and the
# offscreen QPA plugin. -Wl,-z,nocopyreloc is NOT an alternative: it turns the
# same defect into a link error ("causes overflow in R_X86_64_PC32").
DROPPED_TARGET_FLAGS = {"-fPIE"}

def target_flags(t):
    """Per-target compile flags (feature/warning) not covered by .bazelrc."""
    flags = set()
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile": continue
        for x in a["arguments"]:
            if x in DROPPED_TARGET_FLAGS:
                continue
            if x.startswith(("-f", "-m", "-p")) and x not in GLOBAL_FLAGS:
                flags.add(x)
    return sorted(flags)

def load():
    m = json.load(open(MODEL))
    return m["targets"]

def is_lib(t): return t.get("kind") in ("shared_library", "static_library", "object_library")

def vcpkg_available():
    """The library names the vcpkg shim package declares a target for.

    Read out of Meta/vcpkg/BUILD.bazel rather than by listing the CMake tree's
    lib/ dir, so (a) the emitter does not need a built vcpkg tree at all, and
    (b) emitter and shim cannot drift: a dep the shim does not declare comes out
    as an UNKNOWN in the emitted BUILD file instead of a dangling label."""
    txt = open(os.path.join(ROOT, "Meta/vcpkg/BUILD.bazel")).read()
    names = set(re.findall(r"name = \"([^\"]+)\"", txt))
    names |= set(re.findall(r"\"([A-Za-z0-9_]+)\"", txt.split("SHARED = [", 1)[1]
                            .split("]", 1)[0]))
    return names, names

def lagom_to_target(name, targets):
    stem = name[len("lagom-"):]
    if stem == "ak": return "AK"
    cand = "Lib" + stem
    for t in targets:
        if t.lower() == cand.lower(): return t
    return None

def target_srcs(t):
    srcs = []
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile": continue
        srcs += [i for i in a["inputs"] if i.endswith((".cpp", ".c", ".cc", ".S"))]
    return sorted(s for s in set(srcs) if AUTOGEN_MARKER not in s)

def rewrite_host_path_define(d):
    """Rewrite a define whose VALUE is an absolute path to a Bazel-built binary.

    There is exactly one, and it was the last hardcoded
    /home/ubuntu/... in the emitted build:

        -DWASM_CRANELIFT_COMPILER_PATH="<builddir>/bin/cranelift-compiler"

    CMake bakes the reference build's absolute output path in, which is fine for
    CMake (the build tree does not move) and fatal for a checkout on any other
    machine -- the define alone made `git clone && bazel build` unusable even once
    the crate itself built.

    The fix is not "point it at bazel-bin" (that is the same escape with a
    different prefix). Ladybird already resolves the compiler through a lookup
    CHAIN (resolve_cranelift_compiler_path in CraneliftBridge.cpp):
    $LADYBIRD_CRANELIFT_COMPILER, then the compile-time path, then
    SIBLING-OF-SELF. Bazel puts every root-package output in ONE bin directory,
    so cranelift-compiler is already a sibling of WebContent, ladybird and the
    rest -- the third link in Ladybird's own chain finds it with no path baked in
    at all. So the define becomes the bare file name (link 2 is then a
    cwd-relative probe that harmlessly misses), and the binary is attached as
    `data` on LibWasm so it is really THERE, in the runfiles of everything that
    links LibWasm. The dependency is declared; the path is not asserted.
    """
    key, _, value = d.partition("=")
    literal = value.strip('"')
    if not literal.startswith("/"):
        return d
    for b in rust_binary_crates().values():
        if os.path.basename(literal) == b["output_name"]:
            return '%s="%s"' % (key, b["output_name"])
    return d


def target_defines(t, name):
    defs = set()
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile": continue
        for x in a["arguments"]:
            if x.startswith("-D"):
                d = rewrite_host_path_define(x[2:])
                if d not in GLOBAL_DEFINES:
                    # Bazel needs embedded quotes escaped in (local_)defines.
                    defs.add(d.replace('"', '\\"'))
    return sorted(defs)

def target_private_includes(t):
    """Per-target -isystem/-I under Build/full (the target's own gendir) not in
    the 6 global roots.

    Paths are NORMALIZED before the comparison, which is not cosmetic. CMake emits
    a target's own binary dir relative to itself, so the five service targets get
    `-IBuild/full/Services/WebContent/../..` -- the same directory as the global
    `-IBuild/full`, spelled differently. Compared as strings it is not in
    globalroots, so it came out as a per-target `-IBuild/full` copt on five
    targets plus WebContent: six copts pointing into CMake's build tree that
    supplied nothing any global root did not already supply, and which made the
    emitted build look like it needed Build/full when it did not.
    """
    globalroots = {ROOT, ROOT+"/Libraries", ROOT+"/Services",
                   ROOT+"/"+BUILD_REL.rstrip("/"), ROOT+"/"+BUILD_REL+"Libraries",
                   ROOT+"/"+BUILD_REL+"Services",
                   ROOT+"/"+BUILD_REL+"vcpkg_installed/x64-linux-dynamic/include"}
    incs = []
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile": continue
        args = a["arguments"]; i = 0
        while i < len(args):
            if args[i] in ("-I", "-isystem"):
                p = os.path.normpath(args[i+1])
                if p in globalroots:
                    pass
                elif p.startswith(ROOT):
                    rel = os.path.relpath(p, ROOT)
                    if AUTOGEN_MARKER not in rel + "/":
                        incs.append(rel)
                elif p.startswith("/"):
                    incs.append(p)  # system include (e.g. /usr/include/libdrm)
                i += 1
            i += 1
    return sorted(set(incs))


def target_embed_inputs(t):
    """Files pulled in via C++23 #embed; must be staged as compiler inputs."""
    import re as _re
    embeds = set()
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile": continue
        for src in a["inputs"]:
            p = os.path.join(ROOT, src)
            if not (src.endswith((".cpp", ".cc", ".c")) and os.path.exists(p)):
                continue
            txt = open(p, encoding="utf-8", errors="ignore").read()
            for m in _re.findall(r'#\s*embed\s+"([^"]+)"', txt):
                rel = os.path.normpath(os.path.join(os.path.dirname(src), m))
                if os.path.exists(os.path.join(ROOT, rel)):
                    embeds.add(rel)
    return sorted(embeds)

def rust_dep_labels(crate):
    """(deps, implementation_deps) for one Rust crate dep.

    `deps` is always //:<crate>_lib -- the archive plus the crate's PREFIXED
    headers, exactly CMake's target_link_libraries edge, and safe to propagate
    because the spelling <LibUnicode/RustFFI.h> is unique per crate.

    `implementation_deps` is //:<crate>_bare_include when the owning library
    spells the header with NO directory, and the distinction is the whole point.
    Eight of the ten crates emit a header literally named `RustFFI.h`, so an
    unprefixed include dir that reaches a second library makes a bare
    `#include <RustFFI.h>` bind to whichever dir sorted first on the command line
    -- silently, since both files exist.

    Splitting it into its own TARGET (cargo_bare_include) was necessary but NOT
    sufficient, and this is the second half of that lesson. Include dirs
    propagate along the C++ dep graph, not just out of one rule: LibGfx depends on
    LibTextCodec, which owns libtextcodec_rust's bare dir, so LibGfx's compiles
    received /libtextcodec_rust/ffi/LibTextCodec BEFORE its own
    /libgfx_rust/ffi/LibGfx and YUVData.cpp compiled against LibTextCodec's
    header ("'FFI' does not name a type"). A separate target only stops the dir
    leaking out of cargo_lib; it does not stop it leaking out of LibTextCodec.

    `implementation_deps` is Bazel's name for exactly the scope CMake's
    `target_include_directories(... PRIVATE)` has: the dep is used to COMPILE this
    library and is not part of its interface, so its include dirs stop here. That
    is a one-for-one translation of the CMake this build is a port of -- CMake's
    FFI_OUTPUT_DIR is added PRIVATE, to the owning library's own binary dir -- and
    it is the reason a bare include is unambiguous there and had to be made
    unambiguous here.
    """
    impl = []
    if crate in rust_bare_include_crates():
        impl.append(RUST_BARE_INCLUDE_FMT % crate)
    return [RUST_LIB_FMT % crate], impl


# How one CMake dep is translated. Four kinds, kept as named fields rather than
# as "a string, or a list, or a 2-tuple whose first element is a magic word" --
# which is what this was, and it stopped being safe the moment a translation
# needed to produce BOTH deps and implementation_deps (a 2-tuple, i.e. exactly
# the shape the ("SYS", name) sentinel already used).
class Dep:
    def __init__(self, deps=(), impl=(), sys=(), unknown=()):
        self.deps, self.impl = list(deps), list(impl)
        self.sys, self.unknown = list(sys), list(unknown)


def dep_label(d, targets, so, ar):
    nm = d["name"]
    if nm.startswith("lagom-"):
        tgt = lagom_to_target(nm, targets)
        if tgt == "LibWeb": return Dep(deps=["//Libraries/LibWeb:LibWeb"])
        return Dep(deps=["//:%s" % tgt]) if tgt else None
    if nm.endswith("_rust"):
        deps, impl = rust_dep_labels(nm)
        return Dep(deps=deps, impl=impl)
    # A build_rust_binary() crate. CMake's edge is on the custom target
    # `<bin>-build`, and it means two separate things that Bazel splits:
    # the generated FFI HEADER (a dep, via //:<crate>_lib) and the BINARY itself,
    # which is not linked at all -- LibWasm spawns it -- so it becomes runfiles
    # (see the `data` handling in emit_target).
    binaries = rust_binary_crates()
    if nm in binaries:
        b = binaries[nm]
        if not b["ffi_headers"]:
            return None
        deps, impl = rust_dep_labels(b["crate"])
        return Dep(deps=deps, impl=impl)
    # A staticlib crate's redundant `<crate>-build` custom target (CMake emits
    # both it and the imported-library dep); the library dep carries everything.
    if nm.endswith("-build"):
        return None
    if nm in so or nm in ar:
        return Dep(deps=[VCPKG + ":" + nm])
    if nm in SYSTEM_LIBS:
        return Dep(sys=[nm])  # linkopt on final binary
    # Qt6 + GL: system .so under /usr/lib; CMake finds them via find_package.
    # Map the CMake target name to the -l library name; the /usr/lib search
    # path is a global -L in .bazelrc.
    # Qt6 is a real Bazel dep via rules_qt: its qt.local_repo discovers the host
    # SDK through qmake, so moc/rcc and the Qt cc_librarys all come from one SDK.
    qt = qt_label(nm)
    if qt:
        # Plus @qt_plugins//:runtime_libs -- the PRIVATE libraries an SDK ships
        # beside Qt (aqt bundles ICU 73) which rules_qt does not stage, and which
        # libQt6Core cannot find on its own because its RUNPATH $ORIGIN expands to
        # Bazel's solib dir. Making them link inputs is what removed the need for
        # LD_LIBRARY_PATH=<sdk>/lib. Empty for a distro Qt. See qt_runtime.bzl /
        # finding 40.
        return Dep(deps=[qt, "@qt_plugins//:runtime_libs"])
    SYS_MAP = {"GLX": "GLX", "OpenGL": "OpenGL"}
    if nm in SYS_MAP:
        return Dep(sys=[SYS_MAP[nm]])
    return Dep(unknown=[nm])

# Generated sources that BAZEL now produces itself (a genrule output in the root
# package, listed in codegen_root.bzl). A model src under Build/full naming one
# of these must become the root-package label ':<path>' -- i.e. the genrule's
# output -- NOT a Build/full shim. Sourced from the codegen emitter so the two
# cannot drift: whatever it generates is what we consume.
def bazel_generated_root_srcs():
    sys.path.insert(0, os.path.join(ROOT, "Meta"))
    import emit_root_codegen_bazel as codegen
    outs = set()
    for py, cd, declared, produced in codegen.parse():
        _name, d = codegen.convert(py, cd, declared, produced)
        outs.update(d["outs"])
    _layout, flap = codegen.parse_flap()
    if flap:
        outs.add(flap["out"])
    for sh in codegen.parse_glslang():
        outs.add(sh["out"])
    return outs

GENERATED_BY_BAZEL = bazel_generated_root_srcs()

def _emit_srcs(srcs):
    print("    srcs = [")
    for s in srcs:
        rel = s[len(BUILD_REL):] if s.startswith(BUILD_REL) else None
        if rel is not None and rel in GENERATED_BY_BAZEL:
            # Bazel generates this file; consume its genrule output.
            print(f"        {':' + rel!r},")
        elif s.startswith(BUILD_REL + "Libraries/LibWeb/"):
            lab = "//Libraries/LibWeb:" + s[len(BUILD_REL + "Libraries/LibWeb/"):]
            print(f"        {lab!r},")
        elif s.startswith(BUILD_REL + "Libraries/"):
            lab = "//Build/full/Libraries:" + s[len(BUILD_REL + "Libraries/"):]
            print(f"        {lab!r},")
        elif s.startswith(BUILD_REL + "Services/"):
            lab = "//Build/full/Services:" + s[len(BUILD_REL + "Services/"):]
            print(f"        {lab!r},")
        elif s.startswith(BUILD_REL + "UI/"):
            lab = "//Build/full/UI:" + s[len(BUILD_REL + "UI/"):]
            print(f"        {lab!r},")
        else:
            print(f"        {s!r},")
    print("    ],")


# The root package's target set, in emission order. AK and the Qt autogen rules
# are emitted by hand-written blocks (below) because they are not one-to-one with
# a CMake target: AK needs the configure-generated Build/full/AK headers copied
# into an include root, and Qt's moc/rcc come from rules_qt instead of CMake's
# prebuilt ladybird_autogen. Everything else is emitted straight from the model.
#
# LibWeb lives in its own package (//Libraries/LibWeb, emit_libweb_bazel.py) and
# the Lib*Test*/JavaScriptTestRunnerMain targets are test scaffolding, so neither
# appears here.
ROOT_TARGETS = [
    "LibCompress", "LibCore", "LibCrypto", "LibDNS", "LibDatabase", "LibDiff",
    "LibFileSystem", "LibGC", "LibGfx", "LibHTTP", "LibIPC",
    "LibImageDecoderClient", "LibImageDecoders", "LibJS", "LibMain", "LibMedia",
    "LibRegex", "LibRequests", "LibSandbox", "LibSync", "LibSyntax", "LibTLS",
    "LibTextCodec", "LibThreading", "LibURL", "LibUnicode", "LibWakeLock",
    "LibWasm", "LibWebSocket", "LibXML", "LibDevTools", "LibWebView",
    "webcontentservice", "requestserverservice", "imagedecoderservice",
    "compositorservice", "webworkerservice", "ImageDecoder", "RequestServer",
    "Compositor", "WebWorker", "WebContent",
    # Host tool: emits the struct offsets flapc consumes (see codegen_root.bzl).
    # Bazel builds it in the same graph and runs it as a genrule tool -- the case
    # Bazel does natively and CMake has to bolt on.
    "generate_interpreter_layout",
]
# Emitted after the Qt autogen rules, since it consumes them.
QT_TARGETS = ["ladybird"]


def spawned_services():
    """The helper binaries the UI SPAWNS at runtime, read out of Ladybird's source.

    WHY THIS IS A DEPENDENCY AT ALL, and the bug it fixes.
    `bazel build //:ladybird` built the browser and NOTHING ELSE, because nothing in
    the graph said the browser needs its services -- they are found by PATH at
    runtime (get_paths_for_helper_process), not linked. So the recipe told people to
    name all six targets, and a build of just //:ladybird left whatever WebContent
    happened to be in bazel-bin from a previous build.

    Ulf hit the consequence in its most confusing form: ladybird dated Aug 20 next
    to a WebContent dated Aug 11, i.e. a browser from THIS pin talking to a service
    from the PREVIOUS one. Upstream had inserted ~3 IPC messages between the pins,
    shifting every id after them, so every message failed to decode:

      Failed to parse IPC message:
        Local endpoint error: Can't read past the end of the stream memory
        Peer endpoint error: Endpoint magic number mismatch, not my message!

    which reads like a codegen or ABI bug and is nothing of the kind. (The magic
    number in those frames, 0xffa5367a, is AK::string_hash("WebContentServer") --
    the CORRECT endpoint. The ids are what disagreed: his WebContent's numbering
    matched f9e34731 7/7 and 71fb301a 0/7.) His diagnosis: declare them, so they
    rebuild with the thing that spawns them. Exactly right.

    `data`, not `deps`: they are separate processes, not link inputs -- the same
    relationship LibWasm already has to cranelift-compiler, which this emitter
    already gets right (see rewrite_binary_path_define). data also puts them in the
    runfiles tree, which is what `bazel run` needs.

    DERIVED from HelperProcess.cpp, never hand-listed. The names are the string
    literals passed to launch_server_process<>, i.e. the same source of truth the
    runtime lookup uses; a hand-kept list here would be a sixth service away from
    silently reintroducing the bug (and upstream adds services -- Compositor is new
    since the previous pin). If the parse finds nothing, that is a hard failure
    rather than a build that quietly omits them again.
    """
    src = os.path.join(ROOT, "Libraries", "LibWebView", "HelperProcess.cpp")
    with open(src) as f:
        text = f.read()
    names = sorted(set(re.findall(r'launch_server_process<[^>]*>\(\s*"(\w+)"sv', text)))
    if not names:
        sys.exit("emit_build_bazel: found no launch_server_process<> calls in %s -- "
                 "the spawned-service list is DERIVED from them, and an empty list "
                 "would silently rebuild //:ladybird without its services (the Aug 11 "
                 "WebContent bug). Has the launch helper been rewritten?" % src)
    return names

# Ring 2's own targets: the vcpkg tree build action and its inputs. Emitted
# rather than hand-appended, because a hand-appended tail is a file the emitter
# would silently truncate on the next run -- exactly the drift this project keeps
# finding. The per-port cc deps that CONSUME this tree live in
# Meta/vcpkg/BUILD.bazel, which is hand-written and stable (one target per port).
VCPKG_TAIL = """
# ---------------------------------------------------------------------------
# Ring 2: build the vcpkg dependency tree AS A BAZEL ACTION, with zero network.
#
# Every download is resolved by SHA512 out of the 76 http_files Bazel fetched
# (vcpkg_index.bzl, generated from the capture); x-block-origin makes a miss a
# hard error rather than a silent fetch, which is what makes the hermeticity
# claim checkable rather than aspirational.
#
# The 34 external deps are consumed from this output via //Meta/vcpkg:<port>
# (vcpkg_lib, see vcpkg.bzl). Nothing in the build reads the CMake reference tree
# any more.
vcpkg_tree(
    name = "vcpkg_installed",
    distfiles = VCPKG_DISTFILE_INDEX,
    # The wheels for the Python packages a portfile pip-installs (angle asks for
    # `ply`). Pinned separately from the 76 distfiles because pip bypasses vcpkg's
    # asset cache entirely, so the capture cannot see them and x-block-origin
    # cannot block them -- see vcpkg_python_packages.bzl and finding 36.
    python_wheels = ['@vcpkg_pywheel_ply//file'],
    source_dir = ".",
    source_root = ":vcpkg_source_inputs",
    triplet = "x64-linux-dynamic",
    # Resume cache: OPT-IN, and empty here on purpose. Setting it makes a killed
    # 45-minute vcpkg build cheap to restart, but the path must be absolute (the
    # action's cwd is the execroot), so any value here is one developer's home
    # directory in a file everyone checks out -- it was
    # /home/ubuntu/.cache/vcpkg-bazel, the last absolute host path left in the
    # emitted build. Empty is also the honest default: it is the genuine
    # from-source build. Set this one attribute locally if you want resumability.
    # It is a build-speed affordance and no part of the dependency graph, which is
    # precisely why it must not be a checked-in constant.
    cache_dir = "",
    vcpkg_root = "Build/vcpkg",
    vcpkg_tree = "//Build/vcpkg:tree",
)

# The tree, forced into the exec configuration. //:generate_interpreter_layout is
# run as a genrule tool, so Bazel builds it in the exec config and it needs the
# .so files staged at the exec-config path baked into its rpath -- see the rule's
# doc for why a plain srcs entry would both miss and rebuild the tree.
vcpkg_tree_for_exec(
    name = "vcpkg_installed_exec",
    tree = ":vcpkg_installed",
)

# The manifest + overlays the build action needs, declared as inputs rather than
# read out of the ambient checkout (finding 23: the manifest is used VERBATIM,
# never reconstructed -- a hand-derived dep list loses feature selections on
# transitive deps, and libpng[apng] alone is 20 exported symbols).
filegroup(
    name = "vcpkg_source_inputs",
    srcs = ["vcpkg.json", "vcpkg-configuration.json"] + glob([
        "Meta/CMake/vcpkg/overlay-ports/**",
        "Meta/CMake/vcpkg/release-triplets/**",
        "Meta/CMake/vcpkg/base-triplets/**",
        # The 4 git-sourced externals. vcpkg_from_git bypasses asset caching
        # entirely (it shells out to `git fetch`, which no asset source
        # intercepts) but DOES honour a pre-placed downloads/<PORT>-<REF>.tar.gz,
        # so these are staged there by the driver. Discovered by difference
        # against what a completed run leaves in downloads/, not by parsing --
        # skia reaches ten externals through its own declare_external_from_git
        # wrapper and angle's are behind ${URL}/${REF} (finding 30).
        "Meta/CMake/vcpkg/git-archives/**",
    ], allow_empty = True),
)
"""


ALL_LOADS = '''load("@rules_qt//qt:defs.bzl", "qt_cc_moc", "qt_cc_rcc", "qt_qrc")
load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")
load(":cargo_ring.bzl", "cargo_ring")
load(":codegen_root.bzl", "root_codegen")
load(":export_headers.bzl", "export_headers")
load(":qt_runtime.bzl", "qt_conf", "qt_plugin_tree")
load(":vcpkg.bzl", "vcpkg_tree", "vcpkg_tree_for_exec")
load(":vcpkg_index.bzl", "VCPKG_DISTFILE_INDEX")

package(default_visibility = ["//visibility:public"])

# %d generator genrules for this package: the IPC endpoints, LibJS Bytecode/Op,
# LibHTTP's HSTS table, the Compositor WebGL replayer, the TIFF tag tables, the
# two SPIR-V shader headers and the Flap interpreter assembly. Bazel now
# GENERATES all of them instead of consuming CMake's prebuilt copies from
# Build/full.
root_codegen()

# CMake's generate_export_header() output (15 Export.h) and the two AK
# configure_file headers, generated by Bazel instead of read out of Build/full --
# generated by Meta/emit_export_headers_bazel.py, byte-verified with --check.
export_headers()

# The Rust ring: 10 cargo staticlib crates + flapc, built by Bazel from crates
# Bazel fetched, offline. This is what retired the prebuilt 260 MB
# librust_combined.a and its hand-run `ar -M` merge (README step 1b). It lives in
# the ROOT package because the crate sources are at the repo root and glob() is
# package-relative -- cargo_ring.bzl, generated by Meta/emit_cargo_bazel.py.
cargo_ring()

'''

ALL_SOURCE_HEADERS = '''# Mirror CMake's global -ILibraries -IServices -I. : every TU can include any
# in-tree header (<LibGfx/Palette.h>) whether or not there's a dep edge. CMake
# compiles with the whole source tree present; Bazel sandboxes to declared
# inputs, so we expose all source headers as one hdrs library that every
# cc_library depends on. (Generated headers arrive via their own genrule/import
# targets; this is source-tree headers only.)
cc_library(
    name = "all_source_headers",
    hdrs = glob([
        "AK/**/*.h",
        "Libraries/**/*.h",
        "Services/**/*.h",
        "UI/**/*.h",
    ], allow_empty = True),
    deps = [
        # EVERY generated header is Bazel's own now. The two
        # //Build/full/{Libraries,Services} header roots that used to be listed
        # here are gone, and with them the last thing the BUILD read out of
        # CMake's build tree.
        #
        # They were not supplying anything by the end, and that is the part worth
        # recording, because it is why they survived so long: a `glob(["**/*.h"])`
        # over a foreign tree cannot fail. The 709 headers under those roots were
        # 21 that Bazel generates and 688 LibWeb bindings headers that Bazel ALSO
        # generates -- so the roots were SHADOWING Bazel's own outputs, silently
        # winning or losing on include order, and the only reason a fresh clone
        # did not fail here was that it got no error either: allow_empty=True
        # turns "the tree is not there" into an empty glob and the build dies
        # ~1,600 actions later on a missing Export.h. Verified by removal:
        # Build/full/{Libraries,Services,UI} moved off the machine, all six
        # binaries rebuilt from scratch, --headless=text and
        # --headless=layout-tree byte-identical to the CMake reference.
        ":generated_libraries_headers",
        ":generated_services_headers",
        ":generated_shader_headers",
        ":generated_export_headers",
        ":generated_ak_headers",
        "//Libraries/LibWeb:generated_export_header",
    ],
)

VCPKG = "//Meta/vcpkg"
'''

AK_BLOCK_HEAD = '''
# NOTE: the ak_gen_headers genrule that COPIED AK/Debug.h and AK/Backtrace.h out
# of Build/full is gone. Bazel now generates both from their checked-in .in
# templates (export_headers.bzl): Debug.h by applying CMake's configure_file
# substitution, Backtrace.h by ASKING the host the same question
# find_package(Backtrace) asks. That removed the last AK dependency on a CMake
# build tree.

cc_library(
    name = "AK",
'''

AK_BLOCK_TAIL = '''    hdrs = glob(["AK/*.h"]),
    # AK-private defines (CMake PRIVATE) — local_defines so they don't leak to
    # consumers. FMT_SHARED/AK_HAS_CPPTRACE only affect AK's own TUs.
    local_defines = [
        "AK_EXPORTS",
        "AK_HAS_CPPTRACE=1",
        "FMT_SHARED",
    ],
    deps = [
        # AK/Debug.h + AK/Backtrace.h, generated from their .in templates
        # (export_headers.bzl). A cc_library belongs on a deps edge, not in hdrs:
        # in hdrs it contributes no include path, which is why every consumer
        # failed with "AK/Debug.h: No such file or directory" until this moved.
        # The include root travels with the dep, so dependents get it too.
        ":generated_ak_headers",
        VCPKG + ":fmt",
        VCPKG + ":simdutf",
        VCPKG + ":mimalloc",
        VCPKG + ":cpptrace",
    ],
)
'''


def emit_qt_autogen(moc_hdrs):
    print()
    print("# === Qt6 autogen via rules_qt ===")
    print("# qt_cc_moc runs moc over the Q_OBJECT headers; it takes the Qt include dirs")
    print("# from the toolchain's own CcInfo and stages the full header set, so no -I flags")
    print("# or header filegroups are hand-maintained here. qt_cc_rcc compiles the .qrc.")
    print("qt_cc_moc(")
    print('    name = "qt_moc",')
    print("    hdrs = [%s]," % ", ".join('"%s"' % h for h in moc_hdrs))
    print(")")
    print()
    print("qt_qrc(")
    print('    name = "qt_qrc",')
    print('    srcs = ["UI/Qt/ladybird.qrc"],')
    print('    data = ["UI/Icons/ladybird.png"],')
    print(")")
    print()
    print("qt_cc_rcc(")
    print('    name = "qt_rcc",')
    print('    srcs = [":qt_qrc"],')
    print(")")
    print()


QT_RUNTIME_BLOCK = '''
# === Qt6 RUNTIME: the plugins Qt dlopens, and the qt.conf that finds them ===
# Not a CMake target -- CMake does not need one, because its binary links a Qt
# whose baked-in prefix already points at the plugins of that same Qt. Bazel's
# does not: it links @qt's libraries and then Qt looks for plugins next to the
# executable, finds none, and falls back to the HOST's plugin directory. Loading
# another Qt build's QPA plugin into this one is the Ubuntu 24.04 SIGSEGV in
# QXcbConnection::initializeScreens; on a box where the versions agree it "works".
# qt_runtime.bzl has the full account (finding 40); both targets below are data of
# //:ladybird, so they are staged in bazel-bin AND in the runfiles tree.
qt_plugin_tree(
    name = "qt_plugins",
    plugins = ["@qt_plugins//:plugins"],
)

qt_conf(
    name = "qt_conf",
)
'''


def emit_qt_runtime():
    print(QT_RUNTIME_BLOCK, end="")


def moc_headers(exes=None):
    """UI/Qt headers with Q_OBJECT, i.e. the ones CMake's AUTOMOC would moc.

    Conditionally-compiled headers are filtered by whether the reference build
    COMPILES the matching .cpp, not by name. This used to end with

        return [h for h in hdrs if not h.endswith("GeolocationProviderQt.h")]

    justified by "Qt6::Positioning is not found in this configuration" -- true
    when written, false at 71fb301a, where upstream made Positioning required and
    CMake compiles GeolocationProviderQt.cpp. A name-based exclusion cannot
    notice that; asking the model can. Mocking a header CMake does not moc is a
    target Bazel builds and CMake does not; NOT mocking one it does is a missing
    vtable at link time, so the condition has to be the measurement.
    """
    qt_dir = os.path.join(ROOT, "UI/Qt")
    compiled = set()
    if exes:
        for t in exes.values():
            for a in t["actions"]:
                if a["mnemonic"] == "CppCompile":
                    compiled |= {os.path.basename(i) for i in a["inputs"]}
    hdrs = []
    for f in sorted(os.listdir(qt_dir)):
        if not f.endswith(".h"):
            continue
        if "Q_OBJECT" not in open(os.path.join(qt_dir, f), errors="ignore").read():
            continue
        # A Q_OBJECT header with a sibling .cpp is compiled-or-not with it. One
        # with NO sibling .cpp (a header-only QObject) has nothing to measure, so
        # it is moc'd -- AUTOMOC would.
        cpp = f[:-2] + ".cpp"
        if exes and os.path.exists(os.path.join(qt_dir, cpp)) and cpp not in compiled:
            continue
        hdrs.append("UI/Qt/" + f)
    return hdrs


def emit_target(name, targets, libs, exes, so, ar, header=True, body_only=False,
                extra_srcs=(), extra_data=()):
    """Emit one cc_library/cc_binary from the model.

    body_only: emit only `name =` + `srcs =` (AK's hand-written block supplies
    its hdrs/defines/deps, because its include root is a genrule).
    """
    if True:
        is_exe = name in exes
        if name not in libs and not is_exe:
            print(f"# {name}: not a production lib/exe", file=sys.stderr); return
        t = exes[name] if is_exe else libs[name]
        srcs = target_srcs(t)
        embeds = target_embed_inputs(t)
        defs = target_defines(t, name)
        incs = target_private_includes(t)
        flags = target_flags(t)
        deps, impl_deps, data, sysdeps, unknown = [], [], [], [], []
        binaries = rust_binary_crates()
        for d in t.get("deps", []):
            if not d.get("external"):
                if d["name"] == "LibWeb": deps.append("//Libraries/LibWeb:LibWeb")
                elif d["name"] in libs: deps.append("//:%s" % d["name"])
                # exe deps on service static libs / other production libs
                elif d["name"] in exes: pass  # exe->exe (spawn at runtime, not a link dep)
                elif d["name"] in binaries:
                    # A build_rust_binary() crate. For the STATICLIB crates CMake
                    # emits both this `<crate>-build` custom target AND an
                    # external dep on the imported library, so the -build edge is
                    # redundant and dropping it is right. For a BINARY crate it is
                    # the only edge there is -- and dropping it is what left
                    # Cranelift out of the Bazel graph entirely, so every browser
                    # binary died on `CraneliftFFI.h: No such file or directory`
                    # some 1,600 actions in.
                    #
                    # It carries two things Bazel keeps apart:
                    #   deps -- the generated FFI header, via //:<crate>_lib
                    #           (headers-only: there is no archive to link).
                    #   data -- the EXECUTABLE, which is not a link input at all.
                    #           LibWasm spawns it at run time, so it belongs in
                    #           the runfiles of everything that links LibWasm.
                    #           That, plus rewrite_host_path_define turning the
                    #           baked-in absolute path into a bare file name, is
                    #           what makes Ladybird's own sibling-of-self lookup
                    #           find it in any checkout instead of only in mine.
                    b = binaries[d["name"]]
                    if b["ffi_headers"]:
                        rdeps, rimpl = rust_dep_labels(b["crate"])
                        deps.extend(rdeps)
                        impl_deps.extend(rimpl)
                    data.append("//:" + b["bin"])
                continue
            lab = dep_label(d, targets, so, ar)
            if lab is None: continue
            deps.extend(lab.deps)
            impl_deps.extend(lab.impl)
            sysdeps.extend(lab.sys)
            unknown.extend(lab.unknown)
        rule = "cc_binary" if is_exe else "cc_library"
        if header:
            print(f"# === {name} ({t['kind']}, {len(srcs)} TU) ===")
        if unknown: print(f"#   UNKNOWN deps: {unknown}")
        if not body_only:
            print(f"{rule}(")
            print(f"    name = {name!r},")
        _emit_srcs(list(extra_srcs) + srcs)
        if body_only:
            return
        if not is_exe:
            print(f'    hdrs = glob(["{lib_hdr_glob(name, srcs)}/**/*.h"], allow_empty = True),')
            if name in ALWAYSLINK_LIBS:
                print("    alwayslink = True,")
        if defs:
            print("    local_defines = %r," % defs)
        if embeds:
            print("    additional_compiler_inputs = %r," % embeds)
        copt_toks = list(flags)
        for i in incs:
            if i.startswith("/"):
                # System include roots (/usr Qt6, libdrm, glib) can't be
                # per-target copts: Bazel rejects a path outside the execution
                # root even with -isystem. They live in .bazelrc's
                # CPLUS_INCLUDE_PATH (mirroring CMake's find_package dirs) --
                # and CHECKED against it, not just skipped. A silent `continue`
                # here means a root CMake uses and .bazelrc lacks produces no
                # output at all: I transcribed three of glib's four roots by
                # hand and the build failed 3,800 actions later on
                # `gio/gdesktopappinfo.h: No such file or directory`, which is
                # the same hand-copied-fact bug as everything else in this
                # repin. Reported, not fixed automatically: which roots are
                # acceptable host escapes is a judgement (see README gap 3),
                # so the emitter's job is to refuse to hide the difference.
                record_host_include(i)
                continue
            if i.startswith(BUILD_REL + "Libraries/"):
                # CMake's per-library FFI dir (FFI_OUTPUT_DIR defaults to the
                # library's own binary dir), which is where its crate's
                # RustFFI.h lands. Pointing this at the CMake tree is what kept
                # the build dependent on Build/full -- and it also MASKED a real
                # bug: it shadowed the ambiguous ffi/ roots, so a bare
                # <RustFFI.h> silently resolved here instead of to another
                # crate's header. Bazel's equivalent dir travels with the
                # //:<crate>_lib dep (cargo.bzl), so this copt is simply dropped.
                pass
            elif "vcpkg_installed" in i:
                # Not a copt any more. The vcpkg include dirs (include/,
                # include/skia, include/harfbuzz, include/libxml2) are carried by
                # the //Meta/vcpkg:<port> dep as system_includes, so the include
                # path arrives WITH the dep edge -- a target that includes
                # <skia/...> without depending on skia now fails to compile,
                # which is the whole point of declaring inputs.
                pass
            elif i in (BUILD_REL.rstrip("/") + "/UI", BUILD_REL.rstrip("/") + "/UI/Qt"):
                # CMake's UI gendir, whose only non-autogen contents are the two
                # SPIR-V shader headers (WebContentViewLinux{Frag,Vert}Shader.h).
                # Bazel generates both itself and carries them on
                # :generated_shader_headers with includes=["UI/Qt"], so the
                # include path arrives through the dep graph. Dropped, not
                # relocated -- and checked by removal: with Build/full/UI moved
                # off the machine, //:ladybird still builds and renders
                # byte-identically.
                pass
            else:
                copt_toks.append("-I" + i)
        if copt_toks:
            print("    copts = [%s]," % ", ".join("%r" % x for x in copt_toks))
        # System libs (dl/pthread/vulkan/pulse) become linkopts. On a
        # cc_library these PROPAGATE to the final binary's link (matches CMake,
        # where the lib's INTERFACE/PRIVATE system deps flow to the executable).
        if sysdeps:
            print("    linkopts = [%s]," % ", ".join("%r" % ("-l"+l) for l in sorted(set(sysdeps))))
        # Runtime tools this target SPAWNS: not link inputs, but real inputs.
        # On a cc_library `data` propagates into the runfiles of every binary
        # that links it, which is exactly the reach cranelift-compiler needs
        # (LibWasm is linked by WebContent, WebWorker and ladybird).
        data = list(data) + list(extra_data)
        if data:
            print("    data = [%s]," % ", ".join("%r" % x for x in sorted(set(data))))
        # PRIVATE deps: used to compile this library, not part of its interface,
        # so their include dirs stop here. Bazel's name for CMake's PRIVATE, and
        # the only thing that keeps a crate's unprefixed FFI dir from reaching a
        # library that has its OWN crate's RustFFI.h to find -- see
        # rust_dep_labels(). Not valid on a cc_binary (nothing depends on it, so
        # everything it has is already private).
        if impl_deps and not is_exe:
            print("    implementation_deps = [%s]," %
                  ", ".join("%r" % x for x in sorted(set(impl_deps))))
        elif impl_deps:
            deps.extend(impl_deps)
        deps.append("//:all_source_headers")
        alldeps = sorted(set(deps))
        if alldeps:
            print("    deps = [")
            for d in alldeps: print(f"        {d!r},")
            print("    ],")
        print(")")
        print()


def main():
    targets = load()
    so, ar = vcpkg_available()
    libs = {n: t for n, t in targets.items() if t.get("role") == "production" and is_lib(t)}
    exes = {n: t for n, t in targets.items()
            if t.get("role") == "production" and t.get("kind") == "executable"}
    if len(sys.argv) > 1:
        # Explicit target list: emit just those bodies (for inspection/diffing).
        for name in sys.argv[1:]:
            emit_target(name, targets, libs, exes, so, ar)
        return

    sys.path.insert(0, os.path.join(ROOT, "Meta"))
    import emit_root_codegen_bazel as codegen
    n_codegen = (len(codegen.parse()) + len(codegen.parse_glslang())
                 + (2 if all(codegen.parse_flap()) else 0))

    print(ALL_LOADS % n_codegen, end="")
    print(ALL_SOURCE_HEADERS)
    print(AK_BLOCK_HEAD, end="")
    emit_target("AK", targets, libs, exes, so, ar, header=False, body_only=True)
    print(AK_BLOCK_TAIL, end="")
    for name in ROOT_TARGETS:
        emit_target(name, targets, libs, exes, so, ar)
    emit_qt_autogen(moc_headers(exes))
    emit_qt_runtime()
    for name in QT_TARGETS:
        emit_target(name, targets, libs, exes, so, ar,
                    extra_srcs=[":qt_moc", ":qt_rcc"],
                    # The Qt plugins + qt.conf: runtime inputs of the GUI, in the
                    # same sense as the cranelift-compiler binary LibWasm spawns.
                    # Plus the five helper processes the UI spawns by path: see
                    # spawned_services() for why `bazel build //:ladybird` used to
                    # leave a stale WebContent behind, and what that looks like.
                    extra_data=[":qt_conf", ":qt_plugins"] +
                               [":" + s for s in spawned_services()])
    print(VCPKG_TAIL, end="")
    # Last, so it is the final thing on stderr rather than buried in the middle.
    report_host_includes()


def lib_hdr_glob(name, srcs):
    # Infer the source dir from the first IN-TREE src (Libraries/LibX,
    # Services/X). Generated srcs live under Build/full and must not be used:
    # they would make the glob "Build/full/**/*.h", i.e. every CMake-generated
    # header, undeclared-input hole and all.
    for s in srcs:
        if not s.startswith("Build/"):
            return s.split("/")[0] + "/" + s.split("/")[1]
    return name

if __name__ == "__main__":
    main()
