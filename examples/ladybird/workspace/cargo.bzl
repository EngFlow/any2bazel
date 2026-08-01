"""Ladybird's Rust crates, built by Bazel from crates Bazel fetched.

Ring 2 part 3, and the last blocker: before this, the 10 production crates were a
prebuilt 260 MB `librust_combined.a` copied out of `Build/full/cargo/` and `flapc`
was the reference build's binary, so `git clone && bazel build //:ladybird`
needed a CMake build first. Now nothing here names `Build/full`.

The three-layer shape is the same one finding 33 established for vcpkg, because
the problem is the same shape -- a foreign build system owns a recipe we do not
want to reimplement:

  fetching   -> module level (cargo_crates.bzl, generated from Cargo.lock)
  building   -> an ordinary, sandboxed, cacheable build ACTION (cargo_crate),
                deliberately not a repository_rule
  consuming  -> a rule returning CcInfo (cargo_lib), exactly as vcpkg_lib does

Where it differs from vcpkg is worth stating, because it is the interesting part:

  * **No capture was needed.** vcpkg's URLs are computed by CMake programs at
    run time, so pinning them required instrumenting a real vcpkg run.
    `Cargo.lock` already carries a sha256 for all 154 registry crates and
    crates.io's URL is a pure function of (name, version), so the pin is
    *already in the repo* and the emitter needs no cargo, no network and no CMake.
  * **The build action is genuinely hermetic and it is checkable.** cargo runs
    `--offline --locked` against a vendor directory assembled from Bazel's own
    fetched crates, with a sandbox-local CARGO_HOME, and the sandbox has no
    network (block-network). A crate missing from the vendor dir is a hard error
    ("no matching package named `yuv` found"), which is the x-block-origin
    equivalent -- verified by deleting one.
  * **One action yields the archive AND the FFI headers**, because cbindgen is a
    build script: each crate's build.rs writes its header into $FFI_OUTPUT_DIR.
    There is no second tool to wire, and no generated header shimmed out of
    CMake's tree.
"""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

# ---------------------------------------------------------------------------
# The toolchain, pinned.
#
# rust-toolchain.toml says channel "1.96.1", which is a pin we have to honour
# rather than "whatever rustc is on PATH" -- a crate built by an arbitrary
# compiler is not a reproducible input, and rustc's codegen changes between
# releases. rustup is the usual way to honour it and is exactly what we do not
# want inside a build action (it fetches at run time). So the three components
# are three http_archives, hashes from static.rust-lang.org's own published
# .sha256 files, assembled into ONE sysroot by a build action:
#
#   rustc      -> bin/rustc + lib/ (incl. libLLVM)
#   cargo      -> bin/cargo
#   rust-std   -> lib/rustlib/<triple>/lib  (the std cargo links against)
#
# They must end up in one tree because rustc locates its sysroot relative to
# argv[0]: bin/rustc has to sit next to lib/rustlib/<triple>. That is the whole
# reason for rust_sysroot below, and it is why this needs no rustup at all.
# ---------------------------------------------------------------------------
RUST_CHANNEL = "1.96.1"
RUST_TRIPLE = "x86_64-unknown-linux-gnu"

RUST_COMPONENTS = {
    "rustc": "3545a0efad2355ecb0a3b9ac02efee96e27f1f9d24b7ce2fc3f279b2efb0d923",
    "cargo": "ecc53a3c49fab5ab8c9301b3bbc8fb1dff9be6c65287add3f57a0fe8fddfea9e",
    "rust-std": "1bf4fde5048cca33e6ea00c7471281ed96d792f6923141e3db45072743a1afae",
}

# NOTE: no `exclude_directories = 0` here, unlike the crate filegroups. Globbing
# directories AS WELL as their contents makes the sandbox try to stage both, and
# it fails with a bare "File exists" on the first one (bin/) -- a failure whose
# message names the path but not the cause. Files alone are enough: the sysroot
# action reconstructs the directory structure from the file paths.
_TOOLCHAIN_BUILD = """filegroup(
    name = "tree",
    srcs = glob(["**"]),
    visibility = ["//visibility:public"],
)
"""

# Each component tarball wraps its payload in one directory whose name is not
# the component's (`rust-std`'s payload is `rust-std-<triple>/`), alongside an
# install.sh we deliberately do not run. strip_prefix reaches straight through
# to the payload, so each fetched repo's ROOT is already sysroot-shaped and
# merging them is a copy of three directories -- no per-component special-casing
# in the rule.
_RUST_PAYLOAD = {
    "rustc": "rustc",
    "cargo": "cargo",
    "rust-std": "rust-std-" + RUST_TRIPLE,
}

def rust_toolchain_archives():
    """One http_archive per toolchain component. Called from a module extension."""
    for component, sha in RUST_COMPONENTS.items():
        http_archive(
            name = rust_component_repo(component),
            urls = ["https://static.rust-lang.org/dist/%s-%s-%s.tar.xz" %
                    (component, RUST_CHANNEL, RUST_TRIPLE)],
            sha256 = sha,
            strip_prefix = "%s-%s-%s/%s" % (
                component,
                RUST_CHANNEL,
                RUST_TRIPLE,
                _RUST_PAYLOAD[component],
            ),
            build_file_content = _TOOLCHAIN_BUILD,
        )

def rust_component_repo(component):
    """The repo name for one toolchain component (also needed by MODULE.bazel)."""
    return "rust_%s_%s" % (component.replace("-", "_"), RUST_CHANNEL.replace(".", "_"))

def _rust_sysroot_impl(ctx):
    """Merge the three component trees into one rustc-shaped sysroot.

    A directory output, like vcpkg_tree and for the same reason: ~15k files whose
    names nothing outside this rule needs, and cargo takes a path, not a list.
    """
    out = ctx.actions.declare_directory(ctx.label.name)
    inputs = []
    roots = []
    for target in ctx.attr.components:
        files = target.files.to_list()
        if not files:
            fail("toolchain component %s provided no files" % target.label)
        inputs.extend(files)

        # Every file in the repo shares the repo root, and strip_prefix already
        # made that root the payload -- so the root is all this needs, taken from
        # the artifacts rather than reconstructed from the (normalized, and
        # therefore not invertible) repo name.
        roots.append(target.label.workspace_root)

    ctx.actions.run_shell(
        outputs = [out],
        inputs = depset(inputs),
        # Hard links, not copies: ~600 MB across the three components, and the
        # inputs are read-only in Bazel's output base, so copying is pure cost.
        #
        # -L (dereference) is load-bearing, not a nicety. Bazel stages inputs as
        # SYMLINKS into its content-addressed fetch cache, and `cp -al` without -L
        # copies the symlinks -- so bin/rustc in the merged tree still points at
        # .../cache/repos/v1/contents/<hash>. rustc computes its sysroot by
        # resolving argv[0], lands back in the cache directory, and reports
        # "can't find crate for `std` ... the x86_64-unknown-linux-gnu target may
        # not be installed" -- a message that sends you looking for a missing
        # rust-std component that is in fact right there. -L makes bin/rustc a
        # real file in the merged tree, so its sysroot is the merged tree.
        command = "set -e; for p in %s; do cp -alL \"$p\"/. %s/ 2>/dev/null || cp -RL \"$p\"/. %s/; done" % (
            " ".join(roots),
            out.path,
            out.path,
        ),
        mnemonic = "RustSysroot",
        progress_message = "Assembling the pinned Rust %s sysroot" % RUST_CHANNEL,
    )
    return [DefaultInfo(files = depset([out]))]

rust_sysroot = rule(
    implementation = _rust_sysroot_impl,
    doc = """rustc + cargo + rust-std merged into one tree.

    They have to share a tree because rustc finds its own sysroot relative to
    argv[0]: bin/rustc must sit next to lib/rustlib/<triple>. That is also why
    this needs no rustup -- the pin from rust-toolchain.toml is honoured by
    fetching exactly those three versioned components.""",
    attrs = {
        "components": attr.label_list(
            allow_files = True,
            mandatory = True,
            doc = "The three fetched component trees.",
        ),
    },
)

# ---------------------------------------------------------------------------
# Building a crate: an ordinary action, NOT a repository rule.
#
# The same argument as vcpkg_tree (finding 28), and it matters more here because
# there are 11 of these: a repository_rule runs at loading time, outside the
# action graph -- no sandbox, no remote cache, no dependency on the source files
# it actually reads. As a build action, each crate is sandboxed, its inputs are
# declared (the crate sources, the vendor crates, the sysroot), and a change to
# one crate's .rs files rebuilds one crate.
# ---------------------------------------------------------------------------
CargoCrateInfo = provider(
    doc = "One built Rust staticlib plus the FFI headers its build script wrote.",
    fields = {
        "archive": "The lib<crate>.a File.",
        "headers": "The generated FFI header Files.",
        "include_dirs": "Dirs to put on the include path for those headers.",
    },
)

def _crate_index(ctx):
    """"<name> <version> <sha256> <dir>" per line, for the vendor staging.

    Written by Bazel, so the mapping from crate to directory is part of the
    action's declared inputs rather than ambient state -- the same reason the
    vcpkg distfile index is a file and not an environment variable.
    """
    index = ctx.actions.declare_file(ctx.label.name + ".crate-index")
    lines = []
    inputs = []
    for key, target in ctx.attr.crates.items():
        files = target.files.to_list()
        if not files:
            fail("crate %s provided no files" % key)
        inputs.extend(files)

        # The crate's unpacked root is the repo's own root. Taken from the
        # artifacts' workspace_root rather than reconstructed from the repo name,
        # which is normalized (- and . -> _) and therefore not invertible.
        root = files[0].owner.workspace_root
        parts = key.split(" ")
        if len(parts) != 3:
            fail("crate key must be '<name> <version> <sha256>', got %r" % key)
        lines.append("%s %s %s %s" % (parts[0], parts[1], parts[2], root))
    ctx.actions.write(index, "\n".join(sorted(lines)) + "\n")
    return index, inputs

def _cargo_crate_impl(ctx):
    triple = ctx.attr.triple
    archive = ctx.actions.declare_file(
        "%s/%s/release/lib%s.a" % (ctx.label.name, triple, ctx.attr.crate),
    )

    # The FFI headers, declared as outputs. The prefix reproduces CMake's include
    # spelling: CMake sets FFI_OUTPUT_DIR to the consuming library's binary dir
    # (Build/full/Libraries/LibWeb) and -IBuild/full/Libraries makes
    # <LibWeb/RustFFI.h> resolve, while a per-target -I<that dir> makes the bare
    # <RustFFI.h> resolve too. Both spellings are in the tree, so both include
    # dirs are provided.
    ffi_root = "%s/ffi" % ctx.label.name
    headers = [
        ctx.actions.declare_file("%s/%s/%s" % (ffi_root, ctx.attr.ffi_prefix, h))
        for h in ctx.attr.ffi_headers
    ]

    index, crate_inputs = _crate_index(ctx)
    sysroot = ctx.file.sysroot

    ffi_out = "%s/%s/%s" % (archive.root.path, ctx.label.package, ffi_root)
    if ctx.attr.ffi_prefix:
        ffi_out += "/" + ctx.attr.ffi_prefix

    ctx.actions.run(
        outputs = [archive] + headers,
        inputs = depset(
            [index, sysroot, ctx.file._vendor] + ctx.files.srcs + crate_inputs,
        ),
        executable = ctx.executable._build,
        arguments = [
            ctx.attr.crate,
            ctx.attr.manifest,
            ",".join(ctx.attr.crate_features),
            sysroot.path,
            index.path,
            archive.path,
            ffi_out,
        ] + ctx.attr.ffi_headers,
        mnemonic = "CargoCrate",
        progress_message = "Building Rust crate %s (offline)" % ctx.attr.crate,
        execution_requirements = {
            # The hermeticity claim, made enforceable: no network in the sandbox,
            # so `--offline` is not the only thing standing between this action
            # and crates.io. A crate missing from the vendor dir fails the
            # action instead of being downloaded.
            "block-network": "1",
        },
        env = {
            # cargo shells out to cc/ar for crates with C build scripts; take
            # them from the environment the rest of the build uses rather than
            # letting cargo sniff. (No crate here has one today; `cc` is a
            # transitive dep of several, so this is one upstream bump away.)
            "CC": "cc",
            "CXX": "c++",
            "AR": "ar",
            "CARGO_VENDOR_LIB": ctx.file._vendor.path,
        },
        use_default_shell_env = True,
    )
    include_dirs = [
        "%s/%s/%s" % (archive.root.path, ctx.label.package, ffi_root),
    ]
    if ctx.attr.ffi_prefix:
        include_dirs.append(include_dirs[0] + "/" + ctx.attr.ffi_prefix)
    return [
        DefaultInfo(files = depset([archive] + headers)),
        CargoCrateInfo(
            archive = archive,
            headers = headers,
            include_dirs = include_dirs,
        ),
    ]

cargo_crate = rule(
    implementation = _cargo_crate_impl,
    doc = """Build one cargo staticlib crate offline, from Bazel-fetched crates.

    Declares BOTH outputs cargo produces: the archive and the FFI headers its
    build script writes. Declaring the headers is what makes them usable -- an
    undeclared output is deleted by Bazel, which is how a header CMake never
    declared (HTMLTokenizerRustFFI.h) turned up.""",
    attrs = {
        "crate": attr.string(mandatory = True, doc = "The cargo package name."),
        "manifest": attr.string(
            mandatory = True,
            doc = "Path to the crate's Cargo.toml, relative to the source root.",
        ),
        # NOT "features": that is a Bazel BUILT-IN attribute (the C++ feature
        # configuration), and a rule may not override it. The collision is worth
        # a comment because the failure ("built-in attributes cannot be
        # overridden") names the attribute but not the concept.
        "crate_features": attr.string_list(
            doc = """Cargo features, from import_rust_crate(... FEATURES ...).

            Per crate and NOT uniform: three crates take `allocator` and the rest
            take none, and at this commit libgfx_rust does not even HAVE that
            feature (cargo hard-errors). Wrong features change the ABI silently,
            which is the finding-23 failure mode.""",
        ),
        "ffi_headers": attr.string_list(
            doc = "Header paths the crate's build script writes, relative to its prefix.",
        ),
        "ffi_prefix": attr.string(
            doc = "Subdirectory the headers are staged under (e.g. LibWeb), for the include spelling.",
        ),
        "srcs": attr.label_list(
            allow_files = True,
            doc = """Everything cargo reads: the workspace manifests + lock, the
            crate sources, and the non-Rust inputs the build scripts read
            (Bytecode.def, the LibWeb CSS JSON files, TagNames.h ...). Those last
            ones are real inputs -- libweb_css_rust's build script generates Rust
            from Keywords.json -- and they are declared here rather than reached
            for, so editing Properties.json rebuilds the crate.""",
        ),
        "crates": attr.string_keyed_label_dict(
            allow_files = True,
            doc = "\"<name> <version> <sha256>\" -> the fetched crate tree.",
        ),
        "sysroot": attr.label(
            allow_single_file = True,
            mandatory = True,
            doc = "The assembled Rust sysroot (rust_sysroot).",
        ),
        "triple": attr.string(default = RUST_TRIPLE),
        "_build": attr.label(
            default = "//Meta:cargo_build",
            executable = True,
            cfg = "exec",
        ),
        # The shared staging script the driver SOURCES. Passed as a declared
        # input and located by path rather than found next to $0: an sh_binary's
        # runfiles live under <name>.runfiles/, not beside the wrapper Bazel
        # actually execs, so `dirname $0` finds nothing inside the sandbox --
        # which is a failure that only shows up sandboxed.
        "_vendor": attr.label(
            default = "//Meta:cargo_vendor.sh",
            allow_single_file = True,
        ),
    },
)

def _cargo_binary_impl(ctx):
    """A cargo `--bin` crate, e.g. flapc.

    Same action, different cargo subcommand, and the output is executable so a
    genrule can name it in `tools`. flapc's own 3-package workspace gets the
    identical treatment: its lock file has exactly one registry crate (smallvec,
    pinned `=1.15.1`, the same version and checksum as the big workspace's), so
    it needs no separate machinery at all.
    """
    out = ctx.actions.declare_file(ctx.label.name)
    index, crate_inputs = _crate_index(ctx)
    sysroot = ctx.file.sysroot
    ctx.actions.run(
        outputs = [out],
        inputs = depset([index, sysroot, ctx.file._vendor] + ctx.files.srcs +
                        crate_inputs),
        executable = ctx.executable._build,
        arguments = [
            ctx.attr.crate,
            ctx.attr.manifest,
            ",".join(ctx.attr.crate_features),
            sysroot.path,
            index.path,
            out.path,
            ctx.attr.bin,
        ],
        mnemonic = "CargoBinary",
        progress_message = "Building Rust binary %s (offline)" % ctx.attr.bin,
        execution_requirements = {"block-network": "1"},
        env = {
            "CC": "cc",
            "CXX": "c++",
            "AR": "ar",
            "CARGO_VENDOR_LIB": ctx.file._vendor.path,
        },
        use_default_shell_env = True,
    )
    return [DefaultInfo(
        files = depset([out]),
        executable = out,
        runfiles = ctx.runfiles(files = [out]),
    )]

cargo_binary = rule(
    implementation = _cargo_binary_impl,
    executable = True,
    doc = "Build a cargo binary crate offline (flapc), runnable as a genrule tool.",
    attrs = {
        "crate": attr.string(mandatory = True),
        "bin": attr.string(mandatory = True, doc = "The --bin name."),
        "manifest": attr.string(mandatory = True),
        "crate_features": attr.string_list(),
        "srcs": attr.label_list(allow_files = True),
        "crates": attr.string_keyed_label_dict(allow_files = True),
        "sysroot": attr.label(allow_single_file = True, mandatory = True),
        "_build": attr.label(
            default = "//Meta:cargo_binary_build",
            executable = True,
            cfg = "exec",
        ),
        "_vendor": attr.label(
            default = "//Meta:cargo_vendor.sh",
            allow_single_file = True,
        ),
    },
)

# ---------------------------------------------------------------------------
# Consuming the crates: ONE target per crate, carrying that crate's headers AND
# that crate's archive. The dep edge in BUILD.bazel is then exactly CMake's
# target_link_libraries edge, one for one.
#
# This is the second design here, and the first one was wrong in a way worth
# recording, because the wrongness was inherited from the shim it replaced.
#
# The old shim pre-merged all 10 archives with `ar -M` into one 260 MB blob and
# aliased every crate name to it, on the stated grounds that the archives have
# "circular cross-crate symbol references" that no command-line ordering can
# resolve. So the first version of this rule kept that premise and only moved it
# into the graph: all 10 archives in one linker input bracketed by
# -Wl,--start-group/--end-group, which makes ld re-scan until nothing new
# resolves. It links, and it retires the `ar -M` step, so it looked right.
#
# It is not right, and the premise is false. Measured, not reasoned: of each
# crate's undefined symbols, the ones another crate defines are ALSO defined in
# the crate's OWN archive -- every one of them. Subtract each archive's own
# definitions and the cross-crate edge count is **0 for all 10 crates in both
# directions**; what remains (176-274 per crate) is libc/libgcc/pthread. The
# "cycle" was never Ladybird's Rust code calling across crates. It is that each
# staticlib bundles its own copy of rust-std, compiler-builtins and alloc, so any
# two archives share ~200-700 std symbols -- symmetric by construction, which is
# exactly what a real dependency is not.
#
# And --start-group over the whole set is not merely unnecessary, it BREAKS the
# build. Inside a group ld may satisfy a crate's std symbol from a DIFFERENT
# crate's member object, pulling that object in; that object then wants its own
# crate's C++ FFI functions, which live in a Ladybird library the target never
# linked. Concretely: ImageDecoder and RequestServer link LibGfx but not LibJS,
# and failed with hundreds of undefined `rust_sfd_*`/`script_gdi_*` -- symbols
# defined in Libraries/LibJS/RustIntegration.cpp, dragged in because
# libjs_rust.a happened to be the group member that defined a std symbol
# libgfx_rust.a also needed. The archives are not a cycle; they are ten
# independent units that must stay independent, and the group was making them
# leak into each other.
#
# Per-crate it is, then -- which is also what CMake does (each Lagom library
# links its own crate's .a and no others), so the parity check had the answer all
# along. Headers ride on the same target: a TU that includes
# <LibWeb/SelectorRustFFI.h> depends on the crate that generates it. That is the
# split finding 33 made for the vcpkg include dirs, and the same reason: the
# include path and the link input ride on the dep edge instead of being a global
# every TU gets whether it asked or not.
# ---------------------------------------------------------------------------
def _cargo_lib_impl(ctx):
    info = ctx.attr.crate[CargoCrateInfo]
    archive = info.archive
    return [
        DefaultInfo(files = depset([archive] + info.headers)),
        CcInfo(
            compilation_context = cc_common.create_compilation_context(
                headers = depset(info.headers),
                # -isystem, not -I: these are cbindgen output, and Ladybird's
                # -Werror should not fire inside generated code (the same reason
                # the vcpkg headers are -isystem).
                system_includes = depset(info.include_dirs),
            ),
            linking_context = cc_common.create_linking_context(
                linker_inputs = depset([cc_common.create_linker_input(
                    owner = ctx.label,
                    # ONE archive, named by path, with NO --start-group. The
                    # group is what leaked one crate's objects into another
                    # target's link (see the block comment above); a lone archive
                    # is scanned once, and everything Ladybird's Rust code needs
                    # from std is already inside it.
                    user_link_flags = [archive.path],
                    # The archive is named by PATH in user_link_flags, so this is
                    # what puts it in the sandbox -- the same load-bearing role
                    # additional_inputs plays for the vcpkg tree.
                    additional_inputs = depset([archive]),
                )]),
            ),
        ),
    ]

cargo_lib = rule(
    implementation = _cargo_lib_impl,
    doc = """One crate: its archive to link, its generated FFI headers to include.

    One target per crate, mirroring CMake's per-library edge, so a library links
    the crate it uses and no others -- and so a TU that includes
    <LibWeb/SelectorRustFFI.h> has to depend on the crate that generates it.""",
    attrs = {
        "crate": attr.label(
            providers = [CargoCrateInfo],
            mandatory = True,
            # Pinned to exec in BOTH configurations, exactly as vcpkg_lib's tree
            # is (finding 33) and for the same reason: cargo picks its own
            # profile and never sees Bazel's C++ flags, so the crate is not
            # configuration-dependent -- but its output PATH is, and a
            # target-config copy plus an exec-config copy is the same cargo build
            # done twice for byte-identical output.
            # //:generate_interpreter_layout is an exec-config binary in a
            # dependency closure that reaches these, so both configurations
            # really do arrive here.
            cfg = "exec",
        ),
    },
)
