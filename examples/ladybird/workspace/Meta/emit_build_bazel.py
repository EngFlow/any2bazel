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
MODEL = os.path.join(ROOT, "model.cmake.full.json")
VCPKG = "//Meta/vcpkg"
RUST_PKG = "//Build/full/cargo/build/x86_64-unknown-linux-gnu/release"

# Global defines already set in .bazelrc; do not re-emit per target.
GLOBAL_DEFINES = {
    "USE_VULKAN=1", "ENABLE_COMPILETIME_FORMAT_CHECK", "USE_FONTCONFIG=1",
    "_FORTIFY_SOURCE=3", "USE_VULKAN_DMABUF_IMAGES=1", "_FILE_OFFSET_BITS=64",
    "NDEBUG",
}
# System libs with no vcpkg .so (linked via linkopts on the final binary).
SYSTEM_LIBS = {"dl", "m", "pthread", "vulkan", "pulse"}
# Qt6 CMake target -> rules_qt label.
QT_MAP = {
    "Qt6Core": "@qt//:QtCore",
    "Qt6Gui": "@qt//:QtGui",
    "Qt6Widgets": "@qt//:QtWidgets",
}

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

def target_flags(t):
    """Per-target compile flags (feature/warning) not covered by .bazelrc."""
    flags = set()
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile": continue
        for x in a["arguments"]:
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

def target_defines(t, name):
    defs = set()
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile": continue
        for x in a["arguments"]:
            if x.startswith("-D"):
                d = x[2:]
                if d not in GLOBAL_DEFINES:
                    # Bazel needs embedded quotes escaped in (local_)defines.
                    defs.add(d.replace('"', '\\"'))
    return sorted(defs)

def target_private_includes(t):
    """Per-target -isystem/-I under Build/full (the target's own gendir) not in
    the 6 global roots."""
    globalroots = {ROOT, ROOT+"/Libraries", ROOT+"/Services",
                   ROOT+"/Build/full", ROOT+"/Build/full/Libraries",
                   ROOT+"/Build/full/Services",
                   ROOT+"/Build/full/vcpkg_installed/x64-linux-dynamic/include"}
    incs = []
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile": continue
        args = a["arguments"]; i = 0
        while i < len(args):
            if args[i] in ("-I", "-isystem"):
                p = args[i+1]
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

def dep_label(d, targets, so, ar):
    nm = d["name"]
    if nm.startswith("lagom-"):
        tgt = lagom_to_target(nm, targets)
        if tgt == "LibWeb": return "//Libraries/LibWeb:LibWeb"
        return "//:%s" % tgt if tgt else None
    if nm.endswith("_rust"):
        return [RUST_PKG + ":" + nm, "//Build/full/Libraries:rust_ffi_headers"]
    if nm in so or nm in ar:
        return VCPKG + ":" + nm
    if nm in SYSTEM_LIBS:
        return ("SYS", nm)  # linkopt on final binary
    # Qt6 + GL: system .so under /usr/lib; CMake finds them via find_package.
    # Map the CMake target name to the -l library name; the /usr/lib search
    # path is a global -L in .bazelrc.
    # Qt6 is a real Bazel dep via rules_qt: its qt.local_repo discovers the host
    # SDK through qmake, so moc/rcc and the Qt cc_librarys all come from one SDK.
    if nm in QT_MAP:
        return QT_MAP[nm]
    SYS_MAP = {"GLX": "GLX", "OpenGL": "OpenGL"}
    if nm in SYS_MAP:
        return ("SYS", SYS_MAP[nm])
    return ("UNKNOWN", nm)

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
        rel = s[len("Build/full/"):] if s.startswith("Build/full/") else None
        if rel is not None and rel in GENERATED_BY_BAZEL:
            # Bazel generates this file; consume its genrule output.
            print(f"        {':' + rel!r},")
        elif s.startswith("Build/full/Libraries/LibWeb/"):
            lab = "//Libraries/LibWeb:" + s[len("Build/full/Libraries/LibWeb/"):]
            print(f"        {lab!r},")
        elif s.startswith("Build/full/Libraries/"):
            lab = "//Build/full/Libraries:" + s[len("Build/full/Libraries/"):]
            print(f"        {lab!r},")
        elif s.startswith("Build/full/Services/"):
            lab = "//Build/full/Services:" + s[len("Build/full/Services/"):]
            print(f"        {lab!r},")
        elif s.startswith("Build/full/UI/"):
            lab = "//Build/full/UI:" + s[len("Build/full/UI/"):]
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
    source_dir = ".",
    source_root = ":vcpkg_source_inputs",
    triplet = "x64-linux-dynamic",
    # Resume cache: makes a killed 45-minute build cheap to restart. Absolute by
    # necessity (the action's cwd is the execroot) and therefore a host escape --
    # it is a build-speed affordance, not part of the dependency graph.
    cache_dir = "/home/ubuntu/.cache/vcpkg-bazel",
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
load(":codegen_root.bzl", "root_codegen")
load(":vcpkg.bzl", "vcpkg_tree", "vcpkg_tree_for_exec")
load(":vcpkg_index.bzl", "VCPKG_DISTFILE_INDEX")

package(default_visibility = ["//visibility:public"])

# %d generator genrules for this package: the IPC endpoints, LibJS Bytecode/Op,
# LibHTTP's HSTS table, the Compositor WebGL replayer, the TIFF tag tables, the
# two SPIR-V shader headers and the Flap interpreter assembly. Bazel now
# GENERATES all of them instead of consuming CMake's prebuilt copies from
# Build/full.
root_codegen()

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
        # Bazel-generated headers (the genrules above) come FIRST so they win
        # over the CMake copies; the Build/full roots below now only supply what
        # Bazel does not yet generate (Rust FFI headers, CMake's
        # generate_export_header Export.h).
        ":generated_libraries_headers",
        ":generated_services_headers",
        ":generated_shader_headers",
        "//Build/full/Libraries:generated_lib_headers",
        "//Build/full/Services:generated_service_headers",
    ],
)

VCPKG = "//Meta/vcpkg"
'''

AK_BLOCK_HEAD = '''
# Configure-generated headers live in Build/full/AK; expose as AK/*.h via a copy.
genrule(
    name = "ak_gen_headers",
    srcs = ["Build/full/AK/Debug.h", "Build/full/AK/Backtrace.h"],
    outs = ["genroot/AK/Debug.h", "genroot/AK/Backtrace.h"],
    cmd = "mkdir -p $(RULEDIR)/genroot/AK && " +
          "cp $(location Build/full/AK/Debug.h) $(RULEDIR)/genroot/AK/Debug.h && " +
          "cp $(location Build/full/AK/Backtrace.h) $(RULEDIR)/genroot/AK/Backtrace.h",
)

cc_library(
    name = "AK",
'''

AK_BLOCK_TAIL = '''    hdrs = glob(["AK/*.h"]) + [":ak_gen_headers"],
    # AK-private defines (CMake PRIVATE) — local_defines so they don't leak to
    # consumers. FMT_SHARED/AK_HAS_CPPTRACE only affect AK's own TUs.
    local_defines = [
        "AK_EXPORTS",
        "AK_HAS_CPPTRACE=1",
        "FMT_SHARED",
    ],
    includes = ["genroot"],
    deps = [
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


def moc_headers():
    """UI/Qt headers with Q_OBJECT, i.e. the ones CMake's AUTOMOC would moc.

    GeolocationProviderQt.h is excluded: it is only compiled when Qt6::Positioning
    is found, which this configuration does not have (the model shows no
    GeolocationProviderQt.cpp compile), so mocking it would be a target Bazel
    builds and CMake does not.
    """
    qt_dir = os.path.join(ROOT, "UI/Qt")
    hdrs = []
    for f in sorted(os.listdir(qt_dir)):
        if not f.endswith(".h"):
            continue
        if "Q_OBJECT" not in open(os.path.join(qt_dir, f), errors="ignore").read():
            continue
        hdrs.append("UI/Qt/" + f)
    return [h for h in hdrs if not h.endswith("GeolocationProviderQt.h")]


def emit_target(name, targets, libs, exes, so, ar, header=True, body_only=False,
                extra_srcs=()):
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
        deps, rustdeps, sysdeps, unknown = [], [], [], []
        for d in t.get("deps", []):
            if not d.get("external"):
                if d["name"] == "LibWeb": deps.append("//Libraries/LibWeb:LibWeb")
                elif d["name"] in libs: deps.append("//:%s" % d["name"])
                # exe deps on service static libs / other production libs
                elif d["name"] in exes: pass  # exe->exe (spawn at runtime, not a link dep)
                continue
            lab = dep_label(d, targets, so, ar)
            if lab is None: continue
            if isinstance(lab, list):
                deps.extend(lab)
            elif isinstance(lab, tuple):
                (sysdeps if lab[0]=="SYS" else unknown).append(lab[1])
            else:
                deps.append(lab)
        rule = "cc_binary" if is_exe else "cc_library"
        if header:
            print(f"# === {name} ({t['kind']}, {len(srcs)} TU) ===")
        if rustdeps: print(f"#   RUST deps (deferred): {rustdeps}")
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
                # System include roots (/usr Qt6, libdrm) can't be per-target
                # copts: Bazel rejects a path outside the execution root even
                # with -isystem. They live as global -isystem in .bazelrc
                # (mirroring CMake's find_package include dirs).
                continue
            if "vcpkg_installed" in i:
                # Not a copt any more. The vcpkg include dirs (include/,
                # include/skia, include/harfbuzz, include/libxml2) are carried by
                # the //Meta/vcpkg:<port> dep as system_includes, so the include
                # path arrives WITH the dep edge -- a target that includes
                # <skia/...> without depending on skia now fails to compile,
                # which is the whole point of declaring inputs.
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
    emit_qt_autogen(moc_headers())
    for name in QT_TARGETS:
        emit_target(name, targets, libs, exes, so, ar,
                    extra_srcs=[":qt_moc", ":qt_rcc"])
    print(VCPKG_TAIL, end="")


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
