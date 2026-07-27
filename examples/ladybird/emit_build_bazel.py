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

ROOT = "/home/ubuntu/ladybird-work"
MODEL = os.path.join(ROOT, "model.cmake.full.json")
VCPKG_LIB = os.path.join(ROOT, "Build/full/vcpkg_installed/x64-linux-dynamic/lib")
VCPKG = "//Build/full/vcpkg_installed/x64-linux-dynamic"
RUST_PKG = "//Build/full/cargo/build/x86_64-unknown-linux-gnu/release"

# Global defines already set in .bazelrc; do not re-emit per target.
GLOBAL_DEFINES = {
    "USE_VULKAN=1", "ENABLE_COMPILETIME_FORMAT_CHECK", "USE_FONTCONFIG=1",
    "_FORTIFY_SOURCE=3", "USE_VULKAN_DMABUF_IMAGES=1", "_FILE_OFFSET_BITS=64",
    "NDEBUG",
}
# System libs with no vcpkg .so (linked via linkopts on the final binary).
SYSTEM_LIBS = {"dl", "m", "pthread", "vulkan", "pulse"}
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
    so, ar = set(), set()
    for f in os.listdir(VCPKG_LIB):
        if f.startswith("lib") and ".so" in f: so.add(f[3:].split(".so")[0])
        elif f.startswith("lib") and f.endswith(".a"): ar.add(f[3:-2])
    return so, ar

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
    return sorted(set(srcs))

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
                    incs.append(os.path.relpath(p, ROOT))
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
    return ("UNKNOWN", nm)

def _emit_srcs(srcs):
    print("    srcs = [")
    for s in srcs:
        if s.startswith("Build/full/Libraries/LibWeb/"):
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


def main():
    targets = load()
    so, ar = vcpkg_available()
    libs = {n: t for n, t in targets.items() if t.get("role") == "production" and is_lib(t)}
    exes = {n: t for n, t in targets.items()
            if t.get("role") == "production" and t.get("kind") == "executable"}
    only = sys.argv[1:] if len(sys.argv) > 1 else sorted(libs)
    for name in only:
        is_exe = name in exes
        if name not in libs and not is_exe:
            print(f"# {name}: not a production lib/exe", file=sys.stderr); continue
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
        print(f"# === {name} ({t['kind']}, {len(srcs)} TU) ===")
        if rustdeps: print(f"#   RUST deps (deferred): {rustdeps}")
        if unknown: print(f"#   UNKNOWN deps: {unknown}")
        print(f"{rule}(")
        print(f"    name = {name!r},")
        _emit_srcs(srcs)
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
            if "vcpkg_installed" in i:
                copt_toks += ["-isystem", i]  # third-party: suppress -Werror
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

def lib_hdr_glob(name, srcs):
    # infer the source dir from the first src (Libraries/LibX or Services/X)
    if srcs:
        return os.path.dirname(srcs[0]).split("/")[0] + "/" + srcs[0].split("/")[1]
    return name

if __name__ == "__main__":
    main()
