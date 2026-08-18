#!/usr/bin/env python3
"""Ring 1c: emit the LibWeb cc_library INTO its own package (Libraries/LibWeb).

LibWeb is special: it owns the Ring 1b codegen (codegen.bzl genrules), so its
1961 TUs split into 1273 checked-in srcs (package-relative) and 688 generated
srcs referenced as genrule outputs (package-relative labels). Generated headers
(<LibWeb/CSS/PropertyID.h> etc.) resolve via includes=[".."], which puts both
Libraries (source root) and bazel-bin/Libraries (genfiles root) on the search
path. The 688/692 generated src/hdr lists live in generated_srcs.bzl.

Mirrors Meta/emit_build_bazel.py for defines/flags/deps; paths rebased to the
package. Emits the COMPLETE Libraries/LibWeb/BUILD.bazel on stdout -- loads,
package(), the codegen macro calls and the cc_library -- so the checked-in file
is reproducible rather than a hand-spliced copy of this block.
"""
import json, os, re, sys

ROOT = os.environ.get("LADYBIRD_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.environ.get("LADYBIRD_MODEL") or os.path.join(ROOT, "model.cmake.full.json")
PKG_PREFIX = "Libraries/LibWeb/"
# see emit_build_bazel.BUILD_REL: the reference build dir is an env var, because a
# repin needs two reference trees side by side and build.ninja cannot be moved.
BUILD_REL = (os.environ.get("LADYBIRD_BUILD_REL") or "Build/full").strip("/") + "/"
GEN_PREFIX = BUILD_REL + "Libraries/LibWeb/"
VCPKG = "//Meta/vcpkg"
# See emit_build_bazel.py: the crates are Bazel-built now, and each crate is ONE
# target (//:<crate>_lib) carrying its own archive and its own generated FFI
# headers -- one for one with CMake's per-library edge, so LibWeb links the four
# crates it uses and nothing else. Plus, for a crate whose header LibWeb includes
# with no directory, an implementation_deps edge -- IMPORTED from
# emit_build_bazel rather than reimplemented, so the two emitters cannot disagree
# about which crates those are (none of LibWeb's four today; the mechanism is here
# because "none today" is a measurement, not an invariant).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emit_build_bazel import rust_dep_labels
# The crate directories inside THIS package, and the non-Rust build-script inputs
# that live here, both derived by emit_cargo_bazel from Cargo.toml and the
# reference build's depfiles. Imported rather than restated for one concrete
# reason: this filegroup used to hardcode `CSS/Rust/**` and `Layout/Rust/**` with
# allow_empty = False, upstream consolidated both crates into LibWeb/Rust, and the
# glob then matched nothing -- which is a LOADING-time failure, so no target could
# report it. The root package's glob over the same crates is one list; so is this.
import emit_cargo_bazel

GLOBAL_DEFINES = {
    "USE_VULKAN=1", "ENABLE_COMPILETIME_FORMAT_CHECK", "USE_FONTCONFIG=1",
    "_FORTIFY_SOURCE=3", "USE_VULKAN_DMABUF_IMAGES=1", "_FILE_OFFSET_BITS=64",
    "NDEBUG",
}
SYSTEM_LIBS = {"dl", "m", "pthread", "vulkan"}

# LibWeb exports extern "C" FFI that the prebuilt Rust archive consumes and also
# consumes it back (a static-archive <-> static-archive cycle GNU ld cannot
# resolve in one pass); whole-archive it so every FFI symbol is present before
# the rust archive references it. Same reason as //:LibUnicode in
# emit_build_bazel.py's ALWAYSLINK_LIBS.
ALWAYSLINK = True


def prelude():
    """The head of the BUILD file: loads, package(), codegen macros, filegroup.

    A function rather than a constant because the rust_crate_srcs glob is
    DERIVED (see the emit_cargo_bazel import above), and a constant is exactly
    what let it name two directories that upstream had deleted.
    """
    pkg = PKG_PREFIX.rstrip("/")
    globs = emit_cargo_bazel.package_crate_globs(pkg)
    excludes = emit_cargo_bazel.crate_src_glob_excludes(
        [g[:-len("/**")] for g in globs])
    extra = emit_cargo_bazel.PACKAGE_EXTRA_INPUTS.get(pkg, [])
    return '''load("@rules_cc//cc:defs.bzl", "cc_library")
load(":codegen.bzl", "libweb_codegen", "libweb_bindings_codegen")
load(":generated_srcs.bzl", "LIBWEB_GENERATED_SRCS", "LIBWEB_GENERATED_HDRS")
load(":export_header.bzl", "libweb_export_header")

package(default_visibility = ["//visibility:public"])

# LibWeb's generate_export_header output. It is generated HERE rather than in the
# root package because Bazel include dirs cannot escape a package: LibWeb is its
# own package, so its Export.h has to be an output of this package (at
# genroot/LibWeb/Export.h, with includes=["genroot"] -- includes=["../.."] is
# rejected by Bazel, and rightly so). Emitted by
# Meta/emit_export_headers_bazel.py --libweb.
libweb_export_header()

libweb_codegen()
libweb_bindings_codegen()

# The Rust crates that live INSIDE this package, exposed so the root package's
# cargo_ring() can declare them as cargo inputs. They are one cargo WORKSPACE
# with the crates at the repo root, but Bazel packages cut across it: glob() is
# package-relative, so the root package cannot see files under Libraries/LibWeb/
# at all. Hence a filegroup on this side of the boundary rather than a glob on
# that side -- the alternative (making the root package own these files) would
# mean deleting this package.
#
# The patterns are DERIVED from Cargo.toml (Meta/emit_cargo_bazel.py's
# crate_dirs(), the same list the root package globs), not written down. They
# used to be written down, and that broke the whole build: upstream consolidated
# libweb_css_rust and libweb_layout_rust into libweb_rust, so `CSS/Rust/**` and
# `Layout/Rust/**` matched nothing, and an allow_empty = False glob that matches
# nothing fails at LOADING time -- before any target exists to blame.
filegroup(
    name = "rust_crate_srcs",
    srcs = glob(%r, exclude = %r, allow_empty = False) + [
        # Non-Rust build-script inputs that live here too: libweb_rust's build.rs
        # GENERATES Rust from the CSS data files and reads the HTML name headers +
        # Entities.json. Taken from the reference build's cargo depfiles, so the
        # list is measured rather than predicted.
%s
    ],
)
''' % (globs, excludes,
        "\n".join("        %r," % x for x in extra))


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
    """See emit_build_bazel.vcpkg_available: the shim package is the source of
    truth for which external deps have a Bazel label."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import emit_build_bazel
    return emit_build_bazel.vcpkg_available()


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
                   ROOT + "/" + BUILD_REL.rstrip("/"), ROOT + "/" + BUILD_REL + "Libraries",
                   ROOT + "/" + BUILD_REL + "Services",
                   ROOT + "/" + BUILD_REL + "vcpkg_installed/x64-linux-dynamic/include"}
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

    deps, impl_deps, sysdeps, unknown = [], [], [], []
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
            rdeps, rimpl = rust_dep_labels(nm)
            deps.extend(rdeps)
            impl_deps.extend(rimpl)
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
            # Carried by the //Meta/vcpkg:<port> dep as system_includes; see
            # emit_build_bazel.py for why this is not a copt.
            pass
        elif i.startswith("/"):
            # Absolute system include root (/usr/include/libdrm). Bazel rejects a
            # path outside the execution root even as -isystem, so these live as
            # a global CPLUS_INCLUDE_PATH in .bazelrc -- same rule the root
            # emitter applies. It was already absent from the checked-in BUILD
            # file; this makes the emitter agree instead of drifting.
            pass
        else:
            copt_toks.append("-I" + i)

    deps.append("//:all_source_headers")
    out = [prelude()]
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
    # exclude= keeps a generated header that ALSO exists in the source tree
    # (a stale CMake copy, or one checked in) from being globbed as a source
    # hdr: the genrule output must win, or a consumer can compile against a
    # different header than the one Bazel generated.
    out.append('    hdrs = glob(["**/*.h"], exclude = LIBWEB_GENERATED_HDRS, '
               'allow_empty = True) + LIBWEB_GENERATED_HDRS,')
    out.append('    includes = [".."],')
    if ALWAYSLINK:
        out.append("    alwayslink = True,")
    if defs:
        out.append("    local_defines = %r," % defs)
    if copt_toks:
        out.append("    copts = [%s]," % ", ".join("%r" % x for x in copt_toks))
    # PRIVATE deps -- see rust_dep_labels() in emit_build_bazel.py for why an
    # unprefixed FFI include dir must not propagate.
    if impl_deps:
        out.append("    implementation_deps = [%s]," %
                   ", ".join("%r" % x for x in sorted(set(impl_deps))))
    out.append("    deps = [")
    for d in sorted(set(deps)):
        out.append(f"        {d!r},")
    out.append("    ],")
    out.append(")")
    print("\n".join(out))


if __name__ == "__main__":
    main()
