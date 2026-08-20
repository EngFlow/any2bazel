"""The documented run recipe must not stage a COPY of the build's own outputs.

Ladybird's UI finds its helper processes through
`WebView::get_paths_for_helper_process()`, which searches, in order:

    <prefix>/libexec/<name>     <-- first
    <prefix>/bin/<name>
    <application dir>/<name>
    ./<name>

Under Bazel the services are already siblings of `ladybird` in `bazel-bin`, i.e.
the build output IS on that chain. The README's run recipe nevertheless used to
`cp` them into `$BIN/libexec/`, which puts a second copy on the chain AHEAD of the
real one -- a cache with no invalidation, in a tree whose whole point is that
Bazel decides what is stale.

It cost a day. After the 71fb301a repin the fresh UI kept talking to WebContent
binaries left in `bazel-out/k8-fastbuild/libexec/` by a staging run from the
PREVIOUS pin, six weeks earlier. Upstream had inserted IPC messages, so every
message id past the insertion point had shifted by one: the endpoint magic
matched (right endpoint) and the payload did not parse, giving

    Failed to parse IPC message:
      Local endpoint error: Can't read past the end of the stream memory
      Peer endpoint error: Endpoint magic number mismatch, not my message!

~14,000 times, while all 20 generated `*Endpoint.h` were byte-identical to
CMake's -- so every check aimed at the code generator said the build was fine,
because it was. The failing artifact was not built by the build.

The fix is to delete the staging step, not to refresh it: verified by removal,
with no `libexec/` anywhere, `--headless=text` and `--headless=layout-tree` are
byte-identical to the CMake reference at the same pin.

These tests guard the recipe, because the recipe is the interface: it is what
Ulf runs, and a stale binary it silently prefers is indistinguishable from a
miscompile.
"""

import os
import re

_HERE = os.path.dirname(__file__)
_EXAMPLE = os.path.join(_HERE, "..", "examples", "ladybird")


def _readme():
    with open(os.path.join(_EXAMPLE, "README.md")) as f:
        return f.read()


def _run_recipe_shell():
    """The ```sh block of the run recipe -- the part a reader copy-pastes.

    Keyed off the resource-root assignment rather than a heading, so reordering
    the prose does not silently make these tests vacuous.
    """
    blocks = re.findall(r"```sh\n(.*?)```", _readme(), re.S)
    hits = [b for b in blocks if 'share/Lagom' in b and 'bazel info bazel-bin' in b]
    assert len(hits) == 1, \
        f"expected exactly one run recipe block, found {len(hits)}"
    return hits[0]


def test_the_recipe_does_not_copy_services_into_libexec():
    """The assertion that would have failed while the browser was broken.

    Any copy INTO a libexec directory is the bug, whatever it is spelled with --
    cp, install, ln -s -- because the destination is searched before bin/.
    """
    sh = _run_recipe_shell()
    for line in sh.splitlines():
        code = line.split("#", 1)[0]
        if not code.strip():
            continue
        assert not re.search(r"(cp|install|ln)\s.*libexec", code), \
            ("the run recipe stages a copy of the build's outputs into libexec, "
             "which Ladybird searches BEFORE bin/ -- so the copy shadows the "
             f"build and can go stale: {line.strip()}")


def test_the_recipe_removes_a_libexec_left_by_an_older_recipe():
    """Deleting the step is not enough: the directory it made still shadows.

    Anyone who ran the previous recipe has one on disk, and it keeps winning
    forever -- it is not an output of any target, so no `bazel clean` removes it
    and no rebuild refreshes it. The recipe has to actively clear it.
    """
    sh = _run_recipe_shell()
    assert re.search(r"rm -rf\s+[^\n]*libexec", sh), \
        "the recipe must delete a libexec/ left behind by the older recipe"


def test_the_shadowing_lookup_order_is_written_down():
    """Why no staging is needed is the non-obvious part; state it or lose it.

    Without the lookup order in the text, "don't stage into libexec" reads like
    a style preference and the next person helpfully re-adds it.
    """
    # Collapse whitespace first: the README is hard-wrapped at 80 columns, so
    # where the line breaks fall is arbitrary and must not decide the assertion.
    readme = re.sub(r"\s+", " ", _readme())
    assert "get_paths_for_helper_process" in readme, \
        "the function that defines the search order is not named"
    assert re.search(r"libexec.{0,80}\bbefore\b.{0,40}bin", readme), \
        "the README does not say libexec is searched BEFORE bin"
    # And the symptom, so the next person greps the error and lands here rather
    # than re-auditing the IPC code generator (which was innocent).
    assert "Endpoint magic number mismatch" in readme, \
        "the IPC symptom this produces is not documented"


def test_the_resource_root_recipe_survives_a_stale_share_symlink():
    """Same class, second instance: `share` was a symlink into CMake's tree.

    An older recipe pointed `<bindir>/../share` at `Build/full/share`. Once that
    build directory moved, `mkdir -p` on a path under it failed -- reported as
    "File exists" for a path that does not exist, which reads like a bug in
    mkdir. `rm -rf` does not remove a dangling symlink's target problem; only
    removing the LINK does.
    """
    sh = _run_recipe_shell()
    assert re.search(r"rm -f\s+\"?\$\(dirname\s+\"?\$BIN\"?\)\"?/share", sh), \
        "the recipe does not clear a `share` symlink left pointing into CMake's tree"


def test_the_diagnostic_checks_for_a_shadowing_libexec_before_it_checks_qt():
    """The stale-libexec failure must be ruled out FIRST, because it mimics Qt.

    It has now bitten twice, and both times it arrived disguised: a SIGILL or
    `VERIFICATION FAILED` with a Qt-flavoured backtrace (QApplicationPrivate,
    QEventDispatcherGlib, libQt6Core frames), preceded by `Endpoint magic number
    mismatch, not my message!` on every IPC message. Nothing in that picture says
    "you are running binaries from a previous build" -- but the PATHS in the
    backtrace do: `ladybird` from bazel-out/.../bin/ and `Compositor` from
    bazel-out/.../libexec/.

    So the Qt diagnostic checks it before any Qt question, or it confidently
    investigates the wrong subsystem. (Todo 4a93a257: for a "built fine, behaves
    wrong" bug, ask what is EXECUTING before auditing what produced it.)
    """
    script = os.path.join(_EXAMPLE, "qt_runtime_diagnose.sh")
    assert os.path.isfile(script), "the Qt runtime diagnostic is missing"
    with open(script) as f:
        t = f.read()
    assert "libexec" in t, \
        "the diagnostic never checks for a shadowing libexec, the likelier cause"
    # Before the Qt sections: a diagnostic that asks about Qt first sends the
    # reader into the wrong subsystem, which is exactly what happened.
    libexec_at = t.index("libexec")
    qt_at = min(t.index("MODULE.bazel"), t.index("qtconf.bzl"))
    assert libexec_at < qt_at, \
        ("the libexec check must come BEFORE the Qt checks: it mimics a Qt crash "
         "and is the more common cause")
    # It must name the lookup order, or "delete libexec" is a superstition.
    assert re.search(r"libexec.{0,120}\bFIRST\b", t, re.S | re.I), \
        "the diagnostic does not say libexec is searched FIRST (why the copy wins)"
    # And it must print what each service RESOLVES to, which is the datum the
    # backtrace carried and no build check produces.
    assert "SHADOWS" in t, \
        "the diagnostic does not report which copy of each service would run"
    for svc in ("Compositor", "WebContent", "RequestServer"):
        assert svc in t, f"the resolution check does not cover {svc}"


def test_the_diagnostic_unsets_ld_library_path_when_it_runs_the_binary():
    """A run with LD_LIBRARY_PATH set cannot tell you whether you need it.

    Ulf's run.sh sets LD_LIBRARY_PATH=~/Qt/6.9.2/gcc_64/lib, which masks BOTH Qt
    runtime failures (the missing bundled ICU, and the wrong-SDK QPA plugin). The
    diagnostic exists to say which one fired, so it must run the binary WITHOUT it.
    """
    script = os.path.join(_EXAMPLE, "qt_runtime_diagnose.sh")
    with open(script) as f:
        t = f.read()
    assert "env -u LD_LIBRARY_PATH" in t, \
        ("the diagnostic must run the binary with LD_LIBRARY_PATH unset -- with it "
         "set, both failures disappear and the run proves nothing")


def test_ladybird_declares_the_services_it_spawns_as_data():
    """`bazel build //:ladybird` must rebuild the services too.

    The services are found by PATH at runtime (get_paths_for_helper_process), not
    linked, so nothing in the build graph said the browser needs them: a build of
    //:ladybird alone left whatever WebContent happened to be in bazel-bin from a
    previous build. Ulf hit it in its most confusing form -- ladybird dated Aug 20
    beside a WebContent dated Aug 11, a browser from this pin talking to a service
    from the previous one. Upstream had inserted ~3 IPC messages between the pins,
    shifting every id after them, so every message failed to decode with

        Local endpoint error: Can't read past the end of the stream memory
        Peer endpoint error: Endpoint magic number mismatch, not my message!

    which reads like a codegen or ABI bug and is nothing of the kind. (The magic
    0xffa5367a is AK::string_hash("WebContentServer"), i.e. the CORRECT endpoint;
    the message NUMBERING is what disagreed -- 7/7 against the old pin, 0/7 against
    the new one.) His fix: declare them.

    `data`, not `deps`: separate processes, not link inputs -- the relationship
    LibWasm already has to cranelift-compiler.
    """
    build = os.path.join(_EXAMPLE, "workspace", "BUILD.bazel")
    with open(build) as f:
        text = f.read()
    # The ladybird cc_binary block.
    i = text.index("name = 'ladybird'")
    block = text[i:text.index("\n)", i)]
    m = re.search(r"data = \[([^\]]*)\]", block)
    assert m, "//:ladybird declares no data at all"
    data = m.group(1)
    for svc in ("Compositor", "ImageDecoder", "RequestServer", "WebContent", "WebWorker"):
        assert f"':{svc}'" in data, (
            f"//:ladybird does not declare :{svc} in data, so `bazel build "
            "//:ladybird` can leave a STALE copy of it in bazel-bin and the IPC "
            "message ids will not match")
    # deps would be wrong: they are processes, not libraries to link.
    deps = re.search(r"deps = \[(.*?)\]", block, re.S)
    if deps:
        assert "':WebContent'" not in deps.group(1), \
            "the services must be data (spawned processes), not deps (link inputs)"


def test_the_spawned_service_list_is_derived_from_ladybirds_own_source():
    """A hand-kept list of services is one new service away from the same bug.

    Upstream ADDS services (Compositor is new since the previous pin), and the
    names are already written down in the place the runtime lookup uses them: the
    string literals passed to launch_server_process<> in HelperProcess.cpp. So the
    emitter reads them from there, and fails loudly if it parses none -- an empty
    list would silently re-emit the bug it exists to prevent.
    """
    emitter = os.path.join(_EXAMPLE, "workspace", "Meta", "emit_build_bazel.py")
    with open(emitter) as f:
        text = f.read()
    assert "def spawned_services" in text, "the emitter has no spawned_services()"
    assert "HelperProcess.cpp" in text, \
        "the service list is not read from HelperProcess.cpp (hand-listed?)"
    assert "launch_server_process" in text, \
        "the service names must come from the launch_server_process<> call sites"
    fn = text[text.index("def spawned_services"):]
    fn = fn[:fn.index("\ndef ")]
    # No literal roster inside the function: that is the thing being avoided.
    for svc in ("WebContent", "RequestServer", "ImageDecoder", "WebWorker"):
        assert f'"{svc}"' not in fn and f"'{svc}'" not in fn, \
            f"spawned_services() hardcodes {svc} instead of deriving it"
    # An empty parse must be fatal, not an empty list.
    assert "sys.exit" in fn, \
        ("spawned_services() must FAIL when it parses no service names -- returning "
         "[] would silently rebuild //:ladybird without its services again")
