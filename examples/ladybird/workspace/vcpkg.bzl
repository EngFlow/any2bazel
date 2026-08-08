# Ring 2: the vcpkg dependency tree as a Bazel build ACTION.
#
# Deliberately NOT a repository_rule. Repository rules run at load time,
# single-threaded, unsandboxed, with no remote execution, no remote cache and no
# fine-grained invalidation -- the wrong home for a 45-minute C++ build. So the
# split is by what each layer is good at:
#
#   fetching  -> module level (http_file per distfile, see vcpkg_distfiles.bzl).
#                That is what bzlmod is for: it pins the hashes and shares the
#                download cache.
#   building  -> an ordinary build action (this file), so it is sandboxed,
#                cacheable and remotable.
#
# And wrapping vcpkg at all is a stepping stone, not the destination: the
# on-mission answer for a converter is to consume vcpkg's *resolution*
# (`depend-info` + each port's .list manifest) and build the deps with Bazel's
# own toolchain. See docs/CASE-ladybird-migration.md findings 23/24/28.

load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

def _vcpkg_tree_impl(ctx):
    out = ctx.actions.declare_directory(ctx.attr.install_root)

    # The index the asset-cache script resolves through: "<sha512> <path>" per
    # line. Written by Bazel, so the mapping from hash to file is part of the
    # action's declared inputs rather than ambient state.
    index = ctx.actions.declare_file(ctx.label.name + ".index")
    lines = []
    for sha, f in ctx.attr.distfiles.items():
        files = f.files.to_list()
        if len(files) != 1:
            fail("distfile %s must provide exactly one file, got %d" %
                 (sha, len(files)))
        lines.append("%s %s" % (sha, files[0].path))
    ctx.actions.write(index, "\n".join(lines) + "\n")

    distfile_inputs = []
    for f in ctx.attr.distfiles.values():
        distfile_inputs.extend(f.files.to_list())

    ctx.actions.run(
        outputs = [out],
        inputs = depset(
            [index] + distfile_inputs + ctx.files.python_wheels +
            ctx.files.vcpkg_tree + ctx.files.source_root,
        ),
        executable = ctx.executable._build,
        arguments = [
            out.path,
            index.path,
            ctx.attr.vcpkg_root,
            ctx.attr.source_dir,
            ctx.attr.triplet,
            # The find-links dir for pip: a comma-joined list of the wheel paths,
            # empty when there are none (which is then a hard failure for any port
            # that pip-installs, rather than a silent fetch).
            ",".join([f.path for f in ctx.files.python_wheels]),
        ],
        mnemonic = "VcpkgInstall",
        progress_message = "Building %d vcpkg ports (no network)" % len(ctx.attr.distfiles),
        # vcpkg drives compilers, needs a real /tmp, and takes ~45 minutes; it is
        # not remotable as-is (it reads the host toolchain), so keep it local for
        # now and revisit when the deps are built by Bazel's own toolchain.
        execution_requirements = {
            "local": "1",
            "no-sandbox": "1",
            "requires-network": "0",
        },
        use_default_shell_env = True,
        # An opt-in, ABI-hash-keyed resume cache. Off by default: a run that
        # restores prebuilt archives proves nothing about building from source.
        # It is passed as an explicit env entry rather than inherited, so that
        # whether the cache is in play is visible in the action -- an interrupted
        # 45-minute build needs to be resumable (a sandbox restart killed this at
        # 57/76 ports, twice), but not invisibly so.
        env = {"VCPKG_BAZEL_CACHE": ctx.attr.cache_dir} if ctx.attr.cache_dir else {},
    )
    return [DefaultInfo(files = depset([out]))]

vcpkg_tree = rule(
    implementation = _vcpkg_tree_impl,
    doc = """Build the vcpkg dependency tree from Bazel-fetched distfiles.

    Every download is resolved by SHA512 out of `distfiles`; `x-block-origin`
    makes a miss a hard error rather than a silent network fetch, which is what
    makes the hermeticity claim checkable instead of aspirational.""",
    attrs = {
        "distfiles": attr.string_keyed_label_dict(
            allow_files = True,
            doc = "sha512 (hex, lowercase) -> the fetched file for it.",
        ),
        "vcpkg_tree": attr.label(
            allow_files = True,
            doc = "The vcpkg checkout (ports/ + versions/ + the vcpkg binary).",
        ),
        "source_root": attr.label(
            allow_files = True,
            doc = "Ladybird's manifest, overlay-ports and overlay-triplets.",
        ),
        "python_wheels": attr.label_list(
            allow_files = True,
            doc = """Wheels for the Python packages a PORTFILE pip-installs.

            Staged into a find-links dir and paired with PIP_NO_INDEX, so pip
            resolves offline from these files alone. Needed because pip does NOT go
            through vcpkg's asset cache, so `x-block-origin` never sees it and the
            distfile pin cannot cover it -- the angle port's `pip install ply` was
            reaching PyPI through an inherited HTTP_PROXY for months (finding 36).
            An empty list means pip has no index and no wheels, i.e. any port that
            asks for a package fails loudly instead of downloading one.""",
        ),
        "vcpkg_root": attr.string(doc = "Path of the vcpkg checkout."),
        "source_dir": attr.string(doc = "Path of the Ladybird source root."),
        "install_root": attr.string(
            default = "vcpkg_installed",
            doc = "Directory to produce, the vcpkg_installed/ equivalent.",
        ),
        "triplet": attr.string(default = "x64-linux-dynamic"),
        "cache_dir": attr.string(
            doc = """Optional absolute path for vcpkg's ABI-hash-keyed binary cache.

            Set it and an interrupted build resumes from the ports it already
            finished; leave it empty for the genuine from-source build. Safe to
            keep across interruptions for the same reason downloads/ is: the key
            is a digest of each port's source, features, triplet, toolchain and
            its dependencies' hashes, so it is content-addressed. The install tree
            and buildtrees are NOT, and are still discarded on every run.""",
        ),
        "_build": attr.label(
            default = "//Meta:vcpkg_build",
            executable = True,
            cfg = "exec",
        ),
    },
)

# ---------------------------------------------------------------------------
# Consuming the tree: why not cc_import.
#
# The 34 shims were `cc_import(shared_library = "lib/libfmt.so")`, which needs a
# FILE label -- and vcpkg_tree declares a DIRECTORY. So every shim still read the
# CMake reference tree, and the whole Bazel-built tree above was dead weight: a
# target you could build but not consume. That was the last thing between "we
# fetch and build the deps" and "you can clone this and build it".
#
# The fix is to stop pretending the tree is a set of files Bazel knows the names
# of, and hand the linker what it already understands: a search path. vcpkg_lib
# returns a CcInfo whose
#
#   compilation context = the tree as a header input + `-isystem <tree>/include`
#   linking context     = `-L<tree>/lib -l<name>` plus the TREE as
#                         additional_inputs
#
# additional_inputs is the load-bearing part: it makes the directory a declared
# input of every link that (transitively) depends on this lib, so the sandbox
# contains it and `-L` resolves. No per-file outputs to enumerate, and -- unlike
# cc_import -- no SONAME problem: cc_import stages libfmt.so into _solib_k8 under
# its FILE name while the loader asks for libfmt.so.12, whereas a search path
# into the real tree has every symlink and version in place, exactly as the
# dynamic linker expects.
#
# Still a wrapper around a foreign build, and still not what the destination
# looks like (deps built by Bazel's own toolchain, per finding 24/28). But the
# host escape is gone: nothing here names Build/full.
# ---------------------------------------------------------------------------

def _vcpkg_lib_impl(ctx):
    tree = ctx.file.tree

    # vcpkg installs into <install root>/<triplet>/, so the tree artifact has one
    # more level than the include/ + lib/ the compiler wants.
    root = tree.path + "/" + ctx.attr.triplet
    includes = [root + "/" + d for d in ctx.attr.include_dirs]

    # -l<name> for a shared lib, or the archive's path for a static one. Static
    # libs go on the link line by path rather than -l<name>: -l prefers the .so
    # when both exist in the same directory, and for woff2 only the .a exists.
    flags = ["-L" + root + "/lib"]
    for n in ctx.attr.shared:
        flags.append("-l" + n)
    for n in ctx.attr.static:
        flags.append(root + "/lib/lib" + n + ".a")

    # rpath so a binary Bazel RUNS during the build (a genrule tool) finds the
    # .so at its execroot-relative path -- the same reason the old .bazelrc had a
    # relative -rpath, except the path is now Bazel's own output tree.
    flags.append("-Wl,-rpath," + root + "/lib")

    # -rpath-link lets ld follow each .so's own DT_NEEDED (avif->libyuv,
    # cpptrace->libdwarf) without those becoming link inputs of ours.
    flags.append("-Wl,-rpath-link," + root + "/lib")

    return [
        DefaultInfo(files = depset([tree]), runfiles = ctx.runfiles(files = [tree])),
        CcInfo(
            compilation_context = cc_common.create_compilation_context(
                headers = depset([tree]),
                # system_includes, not includes: third-party headers must be
                # -isystem so Ladybird's -Werror does not fire inside them
                # (openssl/tls1.h alone trips -Wcast-qual).
                system_includes = depset(includes),
            ),
            linking_context = cc_common.create_linking_context(
                linker_inputs = depset([cc_common.create_linker_input(
                    owner = ctx.label,
                    user_link_flags = flags,
                    additional_inputs = depset([tree]),
                )]),
            ),
        ),
    ]

vcpkg_lib = rule(
    implementation = _vcpkg_lib_impl,
    doc = """A linkable dep backed by a directory-output vcpkg tree.

    One target per port-ish group; the C++ rules see an ordinary CcInfo, so
    consumers just put it in deps like any cc_library.""",
    attrs = {
        "tree": attr.label(
            allow_single_file = True,
            mandatory = True,
            # Pinned to the EXEC configuration on purpose, in BOTH configs.
            # vcpkg picks its own triplet and never sees Bazel's flags, so the
            # tree is not configuration-dependent -- but its output PATH is, and
            # a target-config copy plus an exec-config copy is the same
            # 45-minute build done twice for byte-identical output. Pinning it
            # gives one tree that both configs link against. The honest limit:
            # this is only sound because host == target here; a real
            # cross-compile has to plumb the triplet through anyway.
            cfg = "exec",
            doc = "The vcpkg_tree target (a single directory artifact).",
        ),
        "shared": attr.string_list(doc = "Library names to link as -l<name>."),
        "static": attr.string_list(
            doc = "Library names whose lib<name>.a is put on the link line by path.",
        ),
        "include_dirs": attr.string_list(
            default = ["include"],
            doc = "Dirs under <tree>/<triplet>/ added as -isystem.",
        ),
        "triplet": attr.string(
            default = "x64-linux-dynamic",
            doc = "The vcpkg triplet subdirectory inside the tree.",
        ),
    },
)

def _vcpkg_tree_for_exec_impl(ctx):
    """The tree, forced into the EXEC configuration.

    A genrule that RUNS an exec-config binary linked against these .so files
    needs the tree staged in its sandbox -- but as srcs it would arrive in the
    TARGET configuration, at a different bazel-out path than the one baked into
    that binary's rpath, and would build the 45-minute tree a SECOND time. This
    one-attribute rule with cfg = "exec" is the transition."""
    return [DefaultInfo(files = depset(ctx.files.tree))]

vcpkg_tree_for_exec = rule(
    implementation = _vcpkg_tree_for_exec_impl,
    attrs = {"tree": attr.label(allow_files = True, cfg = "exec", mandatory = True)},
)
