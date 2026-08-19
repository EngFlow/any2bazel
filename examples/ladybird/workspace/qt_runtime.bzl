# Qt's RUNTIME half: the plugins, staged next to the binary, from the SAME Qt as
# the linked libraries.
#
# WHY THIS FILE EXISTS -- the bug it fixes (finding 40).
#
# rules_qt's `qt.local_repo` makes Qt's *link* half hermetic-ish: the cc_librarys
# under @qt point at one SDK, discovered through `qmake -query`, and Bazel links
# libQt6Core from there (via _solib_k8). Qt's *runtime* half was never wired up at
# all, and Qt does not take the hint: at QApplication construction it dlopens the
# QPA platform plugin (libqxcb.so) from a path baked into libQt6Core -- its build
# prefix -- or, when that prefix is empty, from the DIRECTORY OF THE EXECUTABLE.
# A Bazel binary's directory has no `platforms/`, so the search falls through to
# the compiled-in system path and Qt loads the DISTRO's plugin into a process
# whose Qt libraries came from the Bazel repo.
#
# Two different Qt builds in one process. What that does depends on which way the
# skew points, and both halves are reproduced (with a real X server):
#
#   plugin OLDER than libs  -> Qt's version gate rejects it:
#                              "Ignoring QPA plugin due to mismatching Qt
#                              versions 395520 394240" -> "no Qt platform plugin
#                              could be initialized", abort.
#   plugin NEWER than libs  -> the gate PASSES, the plugin loads, and it calls
#                              into a libQt6Core whose ABI it was not built
#                              against -> SIGSEGV in QXcbConnection::
#                              initializeScreens -> handleScreenAdded.
#
# The second one is the crash reported on Ubuntu 24.04 (aqt Qt 6.9.2 linked,
# distro plugins in /usr/lib/x86_64-linux-gnu/qt6/plugins). On a machine where
# the SDK and the distro happen to be the same version it works -- BY ACCIDENT,
# which is how it survived this long here (`QT_DEBUG_PLUGINS=1` on this box shows
# the same wrong scan, loading /usr's libqxcb.so into @qt's libQt6Core; the two
# are both 6.10.2, so nothing breaks).
#
# HOW IT IS FIXED.
#
#   1. `qt_plugins` (below) is a repository rule that reads @qt's OWN generated
#      qtconf.bzl -- not a hand-written path -- and symlinks every plugin under
#      that SDK's QT_INSTALL_PLUGINS into a repo, one filegroup per plugin type.
#      Deriving the path from @qt is the whole point: the plugins cannot come
#      from a different Qt than the libraries, because both names come from one
#      `qmake -query`.
#   2. `qt_plugin_tree` re-declares those files as outputs of the ROOT package at
#      `plugins/<type>/<plugin>.so`, so they land next to the binary in bazel-bin
#      (and, as data, in the runfiles tree too).
#   3. `qt_conf` writes the four-line qt.conf that points Qt at them:
#
#          [Paths]
#          Prefix = .
#          Plugins = plugins
#
#      Qt reads qt.conf from the directory of the *resolved* executable (it uses
#      /proc/self/exe, so running through a symlink -- `bazel run`, or the
#      runfiles tree's own symlink to bazel-bin -- still finds it). `Prefix = .`
#      is what makes ONE file correct in BOTH layouts: bazel-bin/ladybird sees
#      bazel-bin/plugins, and runfiles/_main/ladybird resolves to the same place.
#
# Setting Prefix also REPLACES the compiled-in prefix, so /usr's plugin directory
# is not merely outranked, it is never scanned. Verified by removal: with an empty
# directory bind-mounted over the host plugin dir, the binary still starts.
#
# The staged files are SYMLINKS to the SDK's plugins on purpose. A plugin needs Qt
# libraries the binary does not link (libqxcb.so needs libQt6XcbQpa.so.6), and
# aqt's plugins carry `RUNPATH $ORIGIN/../../lib`; $ORIGIN is resolved from the
# object's real path, so a symlinked plugin finds its own SDK's private libs with
# no rpath work and no LD_LIBRARY_PATH. Copying them would break exactly that --
# also checked, and it fails the way the report describes.

# The version floor Ladybird's own UI/Qt/CMakeLists.txt declares.
_QT_FLOOR = (6, 9)

# The Qt MODULES //:ladybird links, and the Debian/Ubuntu package that ships each
# one. Checked here because rules_qt's qt.local_repo DERIVES its cc_library targets
# by listing the host's Qt lib directory: a module the host does not have is simply
# not declared, and the failure is Bazel's generic missing-target error naming a
# generated BUILD file in the output base --
#
#   ERROR: .../external/rules_qt++qt+qt/BUILD.bazel: no such target
#   '@@rules_qt++qt+qt//:QtPositioning': target 'QtPositioning' not declared in
#   package '' ... and referenced by '//:ladybird'
#
# -- which says nothing about Qt, nothing about apt, and points at a file the
# reader did not write and cannot fix. Ulf hit exactly this. (Same class as
# finding 38: a host probe whose absence is reported as a bug in your code.)
#
# The floor check right below this was already the right idea and had the wrong
# scope: it asked "is the SDK new enough" and never "does the SDK have the parts we
# link". Both are properties of the discovered SDK, so both belong here.
#
# Kept as a LIST OF NAMES, not derived from BUILD.bazel: the emitter writes the
# `@qt//:Qt*` deps, so deriving this from the same source would only prove the
# generator agrees with itself. This is the independent statement of what the build
# needs, and the test asserts the two match -- which is what catches a NEW Qt
# module appearing in a future repin without its preflight entry.
#
# Each entry names the Debian package AND the aqt module, because WHICH ONE IS THE
# RIGHT ADVICE DEPENDS ON THE SDK, and getting that wrong is worse than saying
# nothing. Ulf builds against Qt 6.9.2 in a venv while his system Qt is 6.4.2: for
# him `apt install qt6-positioning-dev` drops libQt6Positioning.so into
# /usr/lib/x86_64-linux-gnu, which is NOT the lib directory the discovered SDK
# reports, so the module stays missing, the error is unchanged, and the reader
# reasonably concludes the advice was wrong -- because it was. The message picks the
# form that matches the SDK it actually found (see _install_hint).
_QT_MODULES = {
    "QtCore": ("qt6-base-dev", "qtbase"),
    "QtGui": ("qt6-base-dev", "qtbase"),
    "QtWidgets": ("qt6-base-dev", "qtbase"),
    # UI/Qt/CMakeLists.txt:8 -- REQUIRED on non-Apple since the 71fb301a repin
    # (it was OPTIONAL before), for GeolocationProviderQt.cpp.
    "QtPositioning": ("qt6-positioning-dev", "qtpositioning"),
}

# ---------------------------------------------------------------------------
# The repo: @qt's plugins, as Bazel files.
# ---------------------------------------------------------------------------

_BUILD_HEADER = """# GENERATED by qt_runtime.bzl (qt_plugins). Do not edit.
load("@rules_cc//cc:defs.bzl", "cc_import", "cc_library")

package(default_visibility = ["//visibility:public"])
"""

# The OTHER half of "no LD_LIBRARY_PATH": the PRIVATE libraries an SDK bundles.
#
# rules_qt's cc_librarys stage libQt6*.so into _solib_k8 and nothing else. An
# official Qt SDK also bundles its own ICU -- aqt 6.9.2's libQt6Core needs
# libicui18n.so.73, which exists in the SDK's lib/ and nowhere else on a machine
# whose distro ICU is 78 -- so the binary died in the loader before main() and
# `LD_LIBRARY_PATH=<sdk>/lib` was the workaround. A workaround a human has to
# remember is a bug that has been rounded down to a habit.
#
# WHY AN RPATH ON THE BINARY CANNOT FIX THIS, which took three reductions to
# believe. libQt6Core resolves its own ICU through `RUNPATH $ORIGIN`, and $ORIGIN is
# the directory the loader OPENED the object by -- which is Bazel's solib dir, not
# the SDK. (`ldd` on the same symlink resolves ICU happily, because ldd's $ORIGIN is
# the realpath's dir; that near-miss is what made this look like a path problem.)
# Adding the SDK dir to OUR rpath does not help either, and the reason is a glibc
# rule worth writing down: DT_RUNPATH is consulted only for an object's own direct
# dependencies, and while DT_RPATH IS inherited by transitive loads, an
# intermediate object that has a DT_RUNPATH **of its own** blocks the inherited
# DT_RPATH entirely. libQt6Core has one. Measured on three generated .so files, all
# four combinations, before believing it.
#
# So the fix is not a search path at all: make the SDK's private libraries real
# link inputs, so BAZEL stages them into a solib dir and the binary's OWN runpath
# resolves them. Ours is the runpath glibc will consult, because they are now our
# direct dependencies. Verified with the loader trace, then end to end.
#
# The list is DERIVED, not written down: the intersection of "libraries the SDK's
# Qt modules declare in DT_NEEDED" with "libraries the SDK ships beside them". For
# aqt 6.9.2 that is exactly libicui18n/libicuuc/libicudata .so.73; for a distro Qt
# it is EMPTY (a distro's ICU is a distro package, already on the default search
# path) and this target degenerates to nothing, which is the correct answer rather
# than a special case.
_RUNTIME_LIB_IMPORT = """
cc_import(
    name = "{name}",
    shared_library = "{lib}",
)
"""

_RUNTIME_LIBS_EMPTY = """
# This Qt bundles no private libraries of its own (a distro Qt: its ICU is a distro
# package, already on the loader's default search path).
cc_library(
    name = "runtime_libs",
)
"""

_RUNTIME_LIBS_GROUP = """
cc_library(
    name = "runtime_libs",
    deps = [{deps}],
    # --no-as-needed: the BINARY does not reference an ICU 73 symbol (it has its own
    # ICU 78 from vcpkg), so the linker would drop the DT_NEEDED as unused and we
    # would be back to libQt6Core searching a directory that has no ICU in it.
    linkopts = ["-Wl,--no-as-needed"],
)
"""

def _parse_qtconf(content):
    """Reads @qt's generated qtconf.bzl as data: KEY="value" lines."""
    values = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        raw = raw.strip()
        if not raw.startswith("\"") or not raw.endswith("\""):
            continue
        values[key.strip()] = raw[1:-1]
    return values

# Directories that ARE the loader's default search path. A Qt whose libraries live
# in one of these is a distro Qt: everything it depends on is already findable, and
# there is nothing to stage. Getting this wrong is not a small mistake -- the first
# version of this derivation asked "which non-Qt .so files sit beside libQt6Core",
# which for a distro Qt is /usr/lib/x86_64-linux-gnu, i.e. it proposed to link the
# ENTIRE system library directory into the binary (56 cc_imports, ld-linux among
# them). It is a system directory, so it "worked"; that is exactly the kind of
# accident this whole finding is about.
_SYSTEM_LIB_DIRS = [
    "/lib",
    "/lib64",
    "/lib/x86_64-linux-gnu",
    "/lib/aarch64-linux-gnu",
    "/usr/lib",
    "/usr/lib64",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/usr/local/lib",
]

def _sdk_private_libs(repository_ctx, libs_root):
    """The SDK's bundled non-Qt libraries that its OWN Qt modules depend on.

    Derived, not listed: read DT_NEEDED out of the SDK's libQt6*.so with objdump and
    intersect it with the non-Qt .so files sitting in the same directory. For an
    official SDK that yields its bundled ICU; for a distro Qt it yields nothing,
    because the lib dir IS a system dir (see _SYSTEM_LIB_DIRS) and everything in it
    is already on the loader's default search path.
    """
    if libs_root.rstrip("/") in _SYSTEM_LIB_DIRS:
        return []

    root = repository_ctx.path(libs_root)
    if not root.exists:
        return []

    # What the directory ships that is not Qt itself, keyed by soname-ish basename.
    present = {}
    for entry in root.readdir():
        name = str(entry.basename)
        if name.startswith("libQt") or ".so" not in name:
            continue
        present[name] = "lib/{}".format(name)

    if not present:
        return []

    qt_modules = [
        "{}/{}".format(libs_root, str(e.basename))
        for e in root.readdir()
        if str(e.basename).startswith("libQt") and ".so." in str(e.basename)
    ]
    if not qt_modules:
        return []

    # objdump is in binutils, i.e. present anywhere a C++ toolchain is. If it is
    # not, say so rather than silently returning "no private libraries" -- that
    # would look exactly like a distro Qt and reintroduce the LD_LIBRARY_PATH need
    # with no diagnostic (finding 35: a check that cannot fail must not look like a
    # check that passed).
    result = repository_ctx.execute(["objdump", "-p"] + qt_modules)
    if result.return_code != 0:
        fail(("qt_plugins: cannot read DT_NEEDED from the Qt libraries in {d}: " +
              "objdump failed ({e}). objdump comes with binutils; it is needed here " +
              "to discover the private libraries an SDK bundles beside Qt (an " +
              "official SDK ships its own ICU), because without them the binary " +
              "cannot start without LD_LIBRARY_PATH.").format(
            d = libs_root,
            e = result.stderr.strip(),
        ))

    needed = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("NEEDED"):
            continue
        # Starlark's split() has no whitespace-default: give it one explicitly and
        # drop the empty fields the double spaces in objdump's output produce.
        fields = [f for f in line.split(" ") if f]
        if len(fields) < 2:
            continue
        soname = fields[-1]
        if soname in present:
            needed[soname] = present[soname]

    # Symlink each one in, and return the (label-safe) target names.
    out = []
    for soname in sorted(needed.keys()):
        repository_ctx.symlink(
            repository_ctx.path("{}/{}".format(libs_root, soname)),
            needed[soname],
        )
        out.append(needed[soname])
    return out

def _is_system_qt(libs_dir):
    """Is the discovered SDK the DISTRO's Qt, or a self-contained one?

    Decides which install instruction can possibly work. A distro Qt's modules are
    apt packages; a self-contained SDK (aqt/venv/official installer, or a Nix or
    Homebrew prefix) has its own lib directory, and apt would install into
    /usr/lib/... where that SDK never looks.

    Keyed on the LIB DIRECTORY, never on the prefix string. Two reasons, and the
    second one is a test catching me: `qmake -query` can report a prefix like /usr
    while the libraries live elsewhere, so the lib dir is the thing both
    qt.local_repo and the module probe actually read -- AND a list of "prefixes that
    mean distro" would be another hardcoded host path, the exact thing this file
    exists to remove (test_plugins_come_from_the_same_sdk_as_the_libraries forbids
    absolute /usr literals outside _SYSTEM_LIB_DIRS, and rightly failed on my first
    version of this).

    _SYSTEM_LIB_DIRS is already the list of "directories that are the loader's
    default search path", derived for the private-library staging; a Qt whose libs
    are in one of them is a distro Qt by the same definition.
    """
    return libs_dir.rstrip("/") in _SYSTEM_LIB_DIRS

def _install_hint(missing, libs_dir):
    """The instruction that fits the SDK we found -- not a guess between two.

    Both forms are shown when the SDK is self-contained, because we cannot know
    HOW it was built (aqt, the official installer, Nix, a distro-Qt venv that only
    wraps the tools), and a reader who is told only "apt install" will do it, see
    no change, and lose trust in the message. Naming the lib directory we probed is
    what lets them check the claim themselves.
    """
    debs = sorted({_QT_MODULES[m][0]: True for m in missing}.keys())
    aqts = sorted({_QT_MODULES[m][1]: True for m in missing}.keys())
    if _is_system_qt(libs_dir):
        return ("  This looks like your DISTRIBUTION's Qt, so on Debian/Ubuntu:\n\n" +
                "      sudo apt install {debs}\n").format(debs = " ".join(debs))
    return (
        "  This is a SELF-CONTAINED Qt (its libraries live in {libs}, not in a\n" +
        "  system directory), so `apt install` CANNOT fix it: apt installs into\n" +
        "  /usr/lib/..., which this SDK never looks in, and you would get this same\n" +
        "  error again. Add the module to THIS SDK instead. With aqt:\n\n" +
        "      aqtinstall ... --modules {aqts}\n" +
        "      # or: aqt install-qt linux desktop <version> --modules {aqts}\n\n" +
        "  With the official online installer, tick the module under your Qt version.\n" +
        "  Alternatively point qt.local_repo's `paths` in MODULE.bazel at a Qt that\n" +
        "  has the module -- but see the note below: it must be ONE Qt, not a mix.\n"
    ).format(libs = libs_dir or "?", aqts = " ".join(aqts))

def _version_tuple(version):
    parts = version.split(".")
    nums = []
    for p in parts[:3]:
        if not p.isdigit():
            return None
        nums.append(int(p))
    if len(nums) < 2:
        return None
    return nums

def _qt_plugins_impl(repository_ctx):
    # @qt writes this file from `qmake -query`; reading it as DATA (rather than
    # loading it, which a repository rule cannot do) is what ties the plugins to
    # the same SDK as the libraries.
    qtconf_label = Label("@qt//:qtconf.bzl")
    values = _parse_qtconf(repository_ctx.read(qtconf_label))

    version = values.get("QT_VERSION", "")
    plugins_root = values.get("QT_INSTALL_PLUGINS", "")
    if not version or not plugins_root:
        fail("qt_plugins: @qt//:qtconf.bzl has no QT_VERSION/QT_INSTALL_PLUGINS. " +
             "rules_qt's qt.local_repo generates it from `qmake -query`; if the keys " +
             "moved, this rule has to follow them.")

    # Preflight the version FLOOR Ladybird's own CMake declares, in the place that
    # can still say something useful about it. UI/Qt/CMakeLists.txt has
    # `find_package(Qt6 6.9 REQUIRED COMPONENTS Core Widgets)`; CMake refuses an
    # older Qt with a clear message, and until this check existed the Bazel build
    # just compiled against whatever qmake was first on PATH and failed later --
    # at moc time, at link time, or (finding 40) not at all until the GUI crashed.
    have = _version_tuple(version)
    if have == None:
        fail("qt_plugins: cannot parse QT_VERSION {!r} from @qt".format(version))
    if (have[0], have[1]) < (_QT_FLOOR[0], _QT_FLOOR[1]):
        fail(("qt_plugins: Qt {have} is too old.\n\n" +
              "  Ladybird requires Qt >= {floor} (UI/Qt/CMakeLists.txt:\n" +
              "  find_package(Qt6 {floor} REQUIRED COMPONENTS Core Widgets)).\n" +
              "  The SDK rules_qt discovered is {have} at {prefix}.\n\n" +
              "  Point qt.local_repo's `paths` in MODULE.bazel at a newer Qt, or\n" +
              "  install one (Debian/Ubuntu: qt6-base-dev >= {floor}; otherwise an\n" +
              "  official Qt SDK). Do NOT mix: the plugins are taken from this same\n" +
              "  SDK, and a mixed pair is the crash finding 40 is about.").format(
            have = version,
            floor = "{}.{}".format(_QT_FLOOR[0], _QT_FLOOR[1]),
            prefix = values.get("QT_INSTALL_PREFIX", "?"),
        ))

    # Preflight the MODULES, for the reason spelled out at _QT_MODULES: a module the
    # host Qt lacks is never declared by qt.local_repo, and Bazel then blames a
    # generated BUILD file in the output base for a missing apt package.
    #
    # Checked against the SDK's own lib directory (the same input qt.local_repo
    # derives its targets from) rather than by asking @qt for the target: a
    # repository rule cannot query another repo's targets, and reading the libs is
    # what makes the answer agree with what qt.local_repo will do.
    libs_dir = values.get("QT_INSTALL_LIBS", "")
    if libs_dir:
        libs_path = repository_ctx.path(libs_dir)
        present = {}
        if libs_path.exists:
            for f in libs_path.readdir():
                b = str(f.basename)
                # libQt6Positioning.so / .so.6 / .so.6.10.2 all mean "present";
                # _create_lib_name in qt_local_repo.bzl takes the same first field.
                if b.startswith("libQt{}".format(have[0])) and ".so" in b:
                    present["Qt" + b.split(".")[0][len("libQt%d" % have[0]):]] = True
        missing = [m for m in sorted(_QT_MODULES) if m not in present]
        if missing:
            prefix = values.get("QT_INSTALL_PREFIX", "?")
            fail((
                "qt_plugins: the Qt this build discovered is missing {n} module(s)\n" +
                "  that //:ladybird links.\n\n" +
                "  Qt {v}\n    prefix: {prefix}\n    libraries: {libs}\n" +
                "  (that is the SDK `qmake -query` reported, i.e. whatever\n" +
                "   qt.local_repo's `paths` in MODULE.bazel points at)\n\n" +
                "  Missing:\n\n{list}\n\n" +
                "{hint}\n" +
                "  Without this check Bazel reports the same problem as\n" +
                "    no such target '@@rules_qt++qt+qt//:Qt<Module>'\n" +
                "  naming a GENERATED BUILD file in your output base, because rules_qt's\n" +
                "  qt.local_repo derives its cc_library targets by listing the library\n" +
                "  directory above -- a module you do not have is simply never declared.\n\n" +
                "  Do NOT satisfy this by installing the module for a DIFFERENT Qt than the\n" +
                "  one named above: the plugins are taken from this same SDK, and mixing two\n" +
                "  Qt builds in one process is the crash this file's header is about.\n\n" +
                "  (If you installed it just now, Bazel may have the old @qt cached:\n" +
                "   `bazel sync --configure` or `bazel clean --expunge` re-runs the probe.)"
            ).format(
                prefix = prefix,
                libs = libs_dir,
                v = version,
                n = len(missing),
                list = "\n".join(["      " + m for m in missing]),
                hint = _install_hint(missing, libs_dir),
            ))

    root = repository_ctx.path(plugins_root)
    if not root.exists:
        fail(("qt_plugins: Qt {v} reports its plugins live in\n    {p}\n" +
              "but that directory does not exist. A Qt with no QPA plugin cannot open a\n" +
              "window: install the platform plugins (Ubuntu: qt6-base-dev pulls\n" +
              "libqt6gui6, which ships plugins/platforms/libqxcb.so).").format(
            v = version,
            p = plugins_root,
        ))

    # Every plugin type the SDK ships, symlinked file by file. Deliberately NOT a
    # hand-picked list of the four types Ladybird happens to need today: a list is
    # a thing that drifts, symlinks cost nothing, and the failure mode of a
    # missing plugin type (no input method, no native file dialog, no icons) is
    # the sort of thing nobody notices for a month.
    groups = {}
    for type_dir in sorted([str(p.basename) for p in root.readdir()]):
        src_dir = repository_ctx.path("{}/{}".format(plugins_root, type_dir))
        if not src_dir.is_dir:
            continue
        files = []
        for plugin in sorted([str(p.basename) for p in src_dir.readdir()]):
            if not plugin.endswith(".so"):
                continue
            repository_ctx.symlink(
                repository_ctx.path("{}/{}/{}".format(plugins_root, type_dir, plugin)),
                "plugins/{}/{}".format(type_dir, plugin),
            )
            files.append("plugins/{}/{}".format(type_dir, plugin))
        if files:
            groups[type_dir] = files

    if "platforms" not in groups:
        fail(("qt_plugins: {p} has no `platforms/` directory, so there is no QPA " +
              "plugin to load and the GUI cannot start.").format(p = plugins_root))

    # The private libraries the SDK's Qt modules need and the SDK itself ships.
    private_libs = _sdk_private_libs(repository_ctx, values.get("QT_INSTALL_LIBS", ""))

    lines = [_BUILD_HEADER]
    if private_libs:
        names = []
        for lib in private_libs:
            target = lib[len("lib/"):].replace(".", "_")
            names.append(target)
            lines.append(_RUNTIME_LIB_IMPORT.format(name = target, lib = lib))
        lines.append(_RUNTIME_LIBS_GROUP.format(
            deps = ", ".join(["\":{}\"".format(n) for n in names]),
        ))
    else:
        # A distro Qt: nothing to stage, and an empty target so the dep edge in
        # BUILD.bazel is the same on every machine (finding 35 -- the alternative is
        # a label that exists on some hosts and not others).
        lines.append(_RUNTIME_LIBS_EMPTY)
    for type_dir in sorted(groups.keys()):
        lines.append("filegroup(\n    name = \"{}\",\n    srcs = [\n{}    ],\n)\n".format(
            type_dir,
            "".join(["        \"{}\",\n".format(f) for f in groups[type_dir]]),
        ))
    lines.append("filegroup(\n    name = \"plugins\",\n    srcs = [\n{}    ],\n)\n".format(
        "".join(["        \":{}\",\n".format(t) for t in sorted(groups.keys())]),
    ))
    repository_ctx.file("BUILD.bazel", "\n".join(lines))

    # The version the plugins ARE, recorded next to them so a consumer (and the
    # provenance test) can compare it against the Qt the binary linked instead of
    # trusting that they match.
    repository_ctx.file("qt_plugins.bzl", "\n".join([
        "# GENERATED by qt_runtime.bzl (qt_plugins). Do not edit.",
        "QT_PLUGINS_VERSION = \"{}\"".format(version),
        "QT_PLUGINS_SOURCE = \"{}\"".format(plugins_root),
        "QT_PLUGIN_TYPES = {}".format(str(sorted(groups.keys()))),
        "",
    ]))

qt_plugins = repository_rule(
    implementation = _qt_plugins_impl,
    doc = """Exposes the Qt plugins of the SDK rules_qt discovered as Bazel files.

Reads @qt's generated `qtconf.bzl` for `QT_INSTALL_PLUGINS`, so the plugins are
by construction from the same Qt as the `@qt//:Qt*` libraries the binary links --
see the header of qt_runtime.bzl for what happens when they are not.""",
    # local: the SDK is a host path, exactly like rules_qt's own qt.local_repo,
    # so this must be re-evaluated rather than cached across host changes.
    local = True,
)

def _qt_runtime_ext_impl(module_ctx):
    qt_plugins(name = "qt_plugins")
    return module_ctx.extension_metadata(root_module_direct_deps = ["qt_plugins"], root_module_direct_dev_deps = [])

qt_runtime = module_extension(implementation = _qt_runtime_ext_impl)

# ---------------------------------------------------------------------------
# Staging: the plugins as outputs of the package that holds the binary.
# ---------------------------------------------------------------------------

def _strip_to_plugins(short_path):
    """`plugins/platforms/libqxcb.so` out of a runfiles-relative path.

    The path looks like `../qt_plugins/plugins/platforms/libqxcb.so`, so the match
    has to be on the SEPARATED component `/plugins/` -- searching for `plugins/`
    matches inside the repository name `qt_plugins/` and stages everything one
    directory too deep (which is what it did, silently, until qt.conf pointed at
    an empty tree).
    """
    if short_path.startswith("plugins/"):
        return short_path
    idx = short_path.find("/plugins/")
    if idx == -1:
        fail("qt_plugin_tree: {} is not under a plugins/ directory".format(short_path))
    return short_path[idx + 1:]

def _qt_plugin_tree_impl(ctx):
    outs = []
    for src in ctx.files.plugins:
        out = ctx.actions.declare_file(_strip_to_plugins(src.short_path))
        # A SYMLINK, not a copy: the plugin resolves its own private Qt libraries
        # through `RUNPATH $ORIGIN/../../lib`, and $ORIGIN follows the real path.
        ctx.actions.symlink(output = out, target_file = src)
        outs.append(out)
    return [DefaultInfo(
        files = depset(outs),
        runfiles = ctx.runfiles(files = outs),
    )]

qt_plugin_tree = rule(
    implementation = _qt_plugin_tree_impl,
    doc = """Stages @qt_plugins under `plugins/` in THIS package, so the files sit
next to the binary that qt.conf points at.""",
    attrs = {
        "plugins": attr.label_list(
            allow_files = True,
            doc = "Plugin files from @qt_plugins (`@qt_plugins//:plugins`).",
        ),
    },
)

def _qt_conf_impl(ctx):
    out = ctx.actions.declare_file(ctx.attr.filename)

    # `Prefix = .` -- relative to the directory of the resolved executable, which
    # is the one thing that is the same in bazel-bin and in the runfiles tree.
    ctx.actions.write(
        output = out,
        content = "\n".join([
            "# GENERATED by //:{} (qt_runtime.bzl). Points Qt at the plugins staged".format(ctx.label.name),
            "# beside the binary, so it cannot dlopen the host's plugins into Bazel's Qt.",
            "[Paths]",
            "Prefix = .",
            "Plugins = {}".format(ctx.attr.plugins_dir),
            "",
        ]),
    )
    return [DefaultInfo(files = depset([out]), runfiles = ctx.runfiles(files = [out]))]

qt_conf = rule(
    implementation = _qt_conf_impl,
    doc = """Writes the qt.conf that redirects Qt's plugin search into the staged tree.""",
    attrs = {
        "filename": attr.string(default = "qt.conf"),
        "plugins_dir": attr.string(default = "plugins"),
    },
)
