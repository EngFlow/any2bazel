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
