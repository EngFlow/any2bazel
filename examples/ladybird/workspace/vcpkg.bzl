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
            [index] + distfile_inputs +
            ctx.files.vcpkg_tree + ctx.files.source_root,
        ),
        executable = ctx.executable._build,
        arguments = [
            out.path,
            index.path,
            ctx.attr.vcpkg_root,
            ctx.attr.source_dir,
            ctx.attr.triplet,
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
