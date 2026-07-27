#!/usr/bin/env python3
"""Ring 1c: emit the LibWeb cc_library INTO its own package (Libraries/LibWeb).

LibWeb is special: it owns the Ring 1b codegen (codegen.bzl genrules), so its
1961 TUs split into 1273 checked-in srcs (package-relative) and 688 generated
srcs referenced as genrule outputs (package-relative labels). Generated headers
(<LibWeb/CSS/PropertyID.h> etc.) resolve via includes=[".."], which puts both
Libraries (source root) and bazel-bin/Libraries (genfiles root) on the search
path. The 688/692 generated src/hdr lists live in generated_srcs.bzl.

Mirrors Meta/emit_build_bazel.py for defines/flags/deps; paths rebased to the
package. Emits the cc_library block on stdout for splicing into BUILD.bazel.
"""
import json, os, re

ROOT = "/home/ubuntu/ladybird-work"
MODEL = os.path.join(ROOT, "model.cmake.full.json")
PKG_PREFIX = "Libraries/LibWeb/"
GEN_PREFIX = "Build/full/Libraries/LibWeb/"
VCPKG = "//Build/full/vcpkg_installed/x64-linux-dynamic"
RUST_PKG = "//Build/full/cargo/build/x86_64-unknown-linux-gnu/release"

GLOBAL_DEFINES = {
    "USE_VULKAN=1", "ENABLE_COMPILETIME_FORMAT_CHECK", "USE_FONTCONFIG=1",
    "_FORTIFY_SOURCE=3", "USE_VULKAN_DMABUF_IMAGES=1", "_FILE_OFFSET_BITS=64",
    "NDEBUG",
}
SYSTEM_LIBS = {"dl", "m", "pthread", "vulkan"}


def global_flags():
    rc = open(os.path.join(ROOT, ".bazelrc")).read()
    return set(re.findall(r"--(?:cxxopt|copt)=(\S+)", rc))


GLOBAL_FLAGS = global_flags()


def load():
    return json.load(open(MODEL))["targets"]


def genrule_outputs():
    bz = open(os.path.join(ROOT, "Libraries/LibWeb/codegen.bzl")).read()
    return set(re.findall(r"'([A-Za-z0-9_/]+\.(?:cpp|h|cc))'", bz))


def vcpkg_available():
    so, ar = set(), set()
    lib = os.path.join(ROOT, "Build/full/vcpkg_installed/x64-linux-dynamic/lib")
    for f in os.listdir(lib):
        if f.startswith("lib") and ".so" in f:
            so.add(f[3:].split(".so")[0])
        elif f.startswith("lib") and f.endswith(".a"):
            ar.add(f[3:-2])
    return so, ar


def target_srcs(t):
    srcs = []
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile":
            continue
        srcs += [i for i in a["inputs"] if i.endswith((".cpp", ".c", ".cc", ".S"))]
    return sorted(set(srcs))


def target_defines(t):
    defs = set()
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile":
            continue
        for x in a["arguments"]:
            if x.startswith("-D"):
                d = x[2:]
                if d not in GLOBAL_DEFINES:
                    defs.add(d.replace('"', '\\"'))
    return sorted(defs)


def target_flags(t):
    flags = set()
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile":
            continue
        for x in a["arguments"]:
            if x.startswith(("-f", "-m", "-p")) and x not in GLOBAL_FLAGS:
                flags.add(x)
    return sorted(flags)


def target_private_includes(t):
    globalroots = {ROOT, ROOT + "/Libraries", ROOT + "/Services",
                   ROOT + "/Build/full", ROOT + "/Build/full/Libraries",
                   ROOT + "/Build/full/Services",
                   ROOT + "/Build/full/vcpkg_installed/x64-linux-dynamic/include"}
    incs = []
    for a in t["actions"]:
        if a["mnemonic"] != "CppCompile":
            continue
        args = a["arguments"]
        i = 0
        while i < len(args):
            if args[i] in ("-I", "-isystem"):
                p = args[i + 1]
                if p in globalroots:
                    pass
                elif p.startswith(ROOT):
                    incs.append(os.path.relpath(p, ROOT))
                elif p.startswith("/"):
                    incs.append(p)
                i += 1
            i += 1
    return sorted(set(incs))



def lagom_to_target(name, targets):
    stem = name[len("lagom-"):]
    if stem == "ak":
        return "AK"
    cand = "Lib" + stem
    for t in targets:
        if t.lower() == cand.lower():
            return t
    return None

def main():
    targets = load()
    so, ar = vcpkg_available()
    libs = {n: t for n, t in targets.items()
            if t.get("role") == "production"
            and t.get("kind") in ("shared_library", "static_library", "object_library")}
    t = targets["LibWeb"]
    gen_outs = genrule_outputs()

    all_srcs = target_srcs(t)
    checked_in = []
    for s in all_srcs:
        if s.startswith(PKG_PREFIX):
            checked_in.append(s[len(PKG_PREFIX):])
        elif s.startswith(GEN_PREFIX):
            rel = s[len(GEN_PREFIX):]
            assert rel in gen_outs, f"generated src not a genrule output: {rel}"
        else:
            raise SystemExit(f"unexpected LibWeb src outside package: {s}")

    defs = target_defines(t)
    flags = target_flags(t)
    incs = target_private_includes(t)

    deps, sysdeps, unknown = [], [], []
    for d in t.get("deps", []):
        nm = d["name"]
        if not d.get("external"):
            if nm in libs:
                deps.append("//:%s" % nm)
            continue
        if nm.startswith("lagom-"):
            tgt = lagom_to_target(nm, targets)
            if tgt and tgt in libs:
                deps.append("//:%s" % tgt)
            continue
        if nm.endswith("_rust"):
            deps.append(RUST_PKG + ":" + nm)
            deps.append("//Build/full/Libraries:rust_ffi_headers")
        elif nm in so or nm in ar:
            deps.append(VCPKG + ":" + nm)
        elif nm in SYSTEM_LIBS:
            sysdeps.append(nm)
        else:
            unknown.append(nm)

    copt_toks = list(flags)
    for i in incs:
        if i == GEN_PREFIX.rstrip("/"):
            continue  # LibWeb's own gendir -> includes=[".."]
        if "vcpkg_installed" in i:
            copt_toks += ["-isystem", i]
        else:
            copt_toks.append("-I" + i)

    deps.append("//:all_source_headers")
    out = []
    out.append(f"# === LibWeb ({t['kind']}, {len(all_srcs)} TU: "
               f"{len(checked_in)} checked-in + {len(all_srcs)-len(checked_in)} generated) ===")
    if unknown:
        out.append(f"#   UNKNOWN deps: {sorted(set(unknown))}")
    if sysdeps:
        out.append(f"#   SYSTEM libs (linkopts on binary): {sorted(set(sysdeps))}")
    out.append("cc_library(")
    out.append("    name = 'LibWeb',")
    out.append("    srcs = [")
    for s in sorted(checked_in):
        out.append(f"        {s!r},")
    out.append("    ] + LIBWEB_GENERATED_SRCS,")
    out.append('    hdrs = glob(["**/*.h"], allow_empty = True) + LIBWEB_GENERATED_HDRS,')
    out.append('    includes = [".."],')
    if defs:
        out.append("    local_defines = %r," % defs)
    if copt_toks:
        out.append("    copts = [%s]," % ", ".join("%r" % x for x in copt_toks))
    out.append("    deps = [")
    for d in sorted(set(deps)):
        out.append(f"        {d!r},")
    out.append("    ],")
    out.append(")")
    print("\n".join(out))


if __name__ == "__main__":
    main()
