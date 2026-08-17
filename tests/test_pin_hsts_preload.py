#!/usr/bin/env python3
"""Tests for examples/ladybird/workspace/Meta/pin_hsts_preload.py.

The script pins Chromium's HSTS preload table DOWNSTREAM: upstream's CMake
downloads it from `main` (unversioned) and we cannot change that, so Bazel fetches
one immutable commit URL with a sha256 instead. What is worth testing is not the
HTTP (that needs the network) but the three properties that make such a pin honest,
each of which was a way to get it wrong:

  1. the sha256 written out is MEASURED from the bytes fetched, never passed in;
  2. `--expect-same-as` is a real parity guard: if the pinned bytes differ from the
     file the other build system already downloaded, the script must refuse to
     write rather than emit a pin that silently changes the generated table (the
     concrete failure it exists to prevent: a Chromium *release tag* serves a file
     generating 168,593 entries against `main`'s ~94,600);
  3. `--check` reports without writing, so re-pinning is a deliberate act.

Plus the trivial-but-load-bearing one: the emitted file must be valid Starlark-ish
text carrying the commit, the hash and an immutable (commit-pinned, not `main`) URL.
"""

import importlib.util
import io
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "examples" / "ladybird" / "workspace" / "Meta" / "pin_hsts_preload.py"
PINNED_BZL = REPO / "examples" / "ladybird" / "workspace" / "hsts_preload.bzl"

# A commit sha and a payload standing in for the 10 MB table.
COMMIT = "3d75766484199c1fbefd269a4b168cccdb36fbca"
BLOB = b'{"entries": [{"name": "example.test", "policy": "bulk-18-weeks"}]}\n'


class expect_exit:
    """Minimal assertRaises(SystemExit) -- this repo's tests are plain functions."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        assert exc_type is SystemExit, "expected SystemExit, got %r" % (exc_type,)
        self.exception = exc
        return True


def load(responses):
    """Import the script with its single network entry point stubbed.

    `responses` maps a substring of the URL -> bytes to return, so a test can serve
    the commits API and the raw file differently without knowing the URL shapes.
    """
    spec = importlib.util.spec_from_file_location("pin_hsts_%d" % id(responses), SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        for needle, payload in responses.items():
            if needle in url:
                return payload
        raise AssertionError("unstubbed URL: %s" % url)

    mod._get = fake_get
    mod.calls = calls
    return mod


def run(mod, argv):
    """Run main() capturing stdout, the way the caller redirects it into the .bzl."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = mod.main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


def _commit_object():
    return {
        "sha": COMMIT,
        "commit": {
            "message": "[HSTS] Update bulk entries\n\nbody text",
            "committer": {"date": "2026-07-24T21:31:29Z"},
        },
    }


def _responses():
    """Stubs for the three endpoints the script touches.

    The two GitHub URLs differ only after `commits`: `commits?path=...` LISTS
    (a JSON array) while `commits/<sha>` DESCRIBES one (a JSON object). Keying the
    stubs on that distinction is deliberate -- an earlier version of this fixture
    served the array for both and the --commit path failed with a TypeError, which
    is exactly the confusion a reader of the script could make too.
    """
    import json
    return {
        "commits?path=": json.dumps([_commit_object()]).encode(),
        "commits/" + COMMIT: json.dumps(_commit_object()).encode(),
        "raw.githubusercontent.com": BLOB,
    }


def test_hash_is_measured_not_asserted():
    """The emitted sha256 must be of the bytes actually fetched."""
    import hashlib

    mod = load(_responses())
    rc, out, _ = run(mod, [])
    assert rc == 0
    assert hashlib.sha256(BLOB).hexdigest() in out, "the pin must carry the MEASURED hash"


def test_pins_a_commit_url_not_main():
    """A pin to `main` is not a pin: the URL must carry the full commit sha."""
    mod = load(_responses())
    _, out, _ = run(mod, [])
    assert 'HSTS_PRELOAD_COMMIT = "%s"' % COMMIT in out
    assert "/main/net/http/" not in out, "the emitted URL must not track main"
    assert "net/http/transport_security_state_static.json" in out


def test_emitted_file_declares_the_http_file_and_extension():
    """The output has to be usable: an http_file inside a module extension."""
    mod = load(_responses())
    _, out, _ = run(mod, [])
    for expected in ("http_file(", 'name = "hsts_preload_json"', "sha256 = HSTS_PRELOAD_SHA256",
                     "hsts_preload = module_extension("):
        assert expected in out, "emitted file is missing %r" % expected


def test_records_what_it_pinned():
    """A reviewer needs the subject+date of the commit, not just a sha."""
    mod = load(_responses())
    _, out, _ = run(mod, [])
    assert "[HSTS] Update bulk entries" in out
    assert "2026-07-24" in out


def test_explicit_commit_is_described_not_guessed():
    """--commit must still look up the subject/date, so the pin stays self-documenting."""
    mod = load(_responses())
    _, out, _ = run(mod, ["--commit", COMMIT])
    assert "[HSTS] Update bulk entries" in out
    assert any(("commits/" + COMMIT) in u for u in mod.calls), \
        "--commit should still describe the commit it pinned"


def test_expect_same_as_accepts_identical_bytes():
    """The parity guard passes when the pinned bytes equal the file CMake downloaded."""
    mod = load(_responses())
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        f.write(BLOB)
        f.flush()
        rc, out, err = run(mod, ["--expect-same-as", f.name])
    assert rc == 0
    assert "parity OK" in err
    assert "HSTS_PRELOAD_SHA256" in out


def test_expect_same_as_refuses_different_bytes():
    """The case this guard exists for: pinning a revision that changes the table."""
    mod = load(_responses())
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        f.write(BLOB + b"an extra entry, i.e. a different table\n")
        f.flush()
        with expect_exit() as e:
            run(mod, ["--expect-same-as", f.name])
    msg = str(e.exception)
    assert "PARITY" in msg
    assert "differ" in msg
    # and it must say what to do about it, not just that it failed
    assert "--commit" in msg


def test_expect_same_as_writes_nothing_on_failure():
    """A refused pin must not emit a partial .bzl the caller would redirect into place."""
    mod = load(_responses())
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        f.write(b"totally different\n")
        f.flush()
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            with expect_exit():
                mod.main(["--expect-same-as", f.name])
        finally:
            sys.stdout, sys.stderr = old
    assert "http_file(" not in out.getvalue(), "must not emit a pin it refused"


def test_check_reports_without_writing():
    """--check is the dry run: the pin is reported on stderr, stdout stays empty."""
    mod = load(_responses())
    rc, out, err = run(mod, ["--check"])
    assert rc == 0
    assert out == "", "--check must write no .bzl"
    assert COMMIT[:12] in err and "sha256" in err


def test_no_commits_for_the_path_is_an_error():
    """If GitHub returns nothing, fail loudly rather than pin the empty string."""
    mod = load({"commits?path=": b"[]", "raw.githubusercontent.com": BLOB})
    with expect_exit() as e:
        run(mod, [])
    assert "no commits" in str(e.exception)


def test_committed_pin_matches_the_scripts_own_template():
    """The committed hsts_preload.bzl must be what this script would write.

    Guards the hand-edit: hsts_preload.bzl carries a long comment explaining the
    pin, and the temptation is to tweak it in place until it drifts from the
    generator. Rendering the template with the committed values must reproduce the
    committed file exactly.
    """
    text = PINNED_BZL.read_text()
    import re

    commit = re.search(r'HSTS_PRELOAD_COMMIT = "([0-9a-f]{40})"', text).group(1)
    sha256 = re.search(r'HSTS_PRELOAD_SHA256 = "([0-9a-f]{64})"', text).group(1)
    subject = re.search(r'# The pinned commit: "(.*)" \((\d{4}-\d\d-\d\d)\)\.', text)
    size = int(re.search(r'reference build \(([\d,]+) bytes', text).group(1).replace(",", ""))

    mod = load(_responses())
    rendered = mod.TEMPLATE.format(
        commit=commit, sha256=sha256, size=size,
        subject=subject.group(1), date=subject.group(2),
    )
    assert rendered == text, "hsts_preload.bzl has drifted from Meta/pin_hsts_preload.py"


def test_codegen_genrule_consumes_the_pinned_file():
    """The pin is only useful if the generator actually reads it.

    codegen_root.bzl is generated, so this asserts the wiring survived a
    regeneration: gen_HSTSPreloadData must take @hsts_preload_json//file and must
    NOT reference the CMake configure's download path under Build/caches.
    """
    bzl = (REPO / "examples" / "ladybird" / "workspace" / "codegen_root.bzl").read_text()
    rule = bzl.split("name = 'gen_HSTSPreloadData'", 1)[1].split("native.genrule(", 1)[0]
    assert "@hsts_preload_json//file" in rule
    assert "Build/caches/HSTSPreload" not in bzl, \
        "the unpinned CMake download path must be gone from the generated file"


def test_module_bazel_names_the_repo():
    """bzlmod requires every extension-created repo in a use_repo, or it is invisible."""
    mod_bazel = (REPO / "examples" / "ladybird" / "workspace" / "MODULE.bazel").read_text()
    assert 'use_extension("//:hsts_preload.bzl", "hsts_preload")' in mod_bazel
    assert 'use_repo(hsts, "hsts_preload_json")' in mod_bazel
