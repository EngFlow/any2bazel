"""The runner's own guards, exercised rather than asserted.

run_all.py exists because three test files silently ran zero tests (case study
finding 35). Its whole value is three guards -- a file with no tests fails, a file
that will not import fails, a failing test fails -- and a guard that has never
been seen to fire is exactly the thing this repo keeps getting caught by. So each
one is triggered here against a throwaway tests/ directory.

Note what is NOT done: this does not import run_all and inspect it. It runs it as
a subprocess and checks the EXIT CODE, because the exit code is what a commit gate
or CI reads, and an exit code is precisely what the original bug got wrong (0 while
running nothing).
"""

import os
import shutil
import subprocess
import sys
import tempfile

_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_all.py")


def _run(files):
    """Run the runner over a temp tests/ dir containing exactly `files`.

    A temp dir rather than the real one so a guard can be triggered without a
    file that breaks the actual suite ever existing on disk.
    """
    d = tempfile.mkdtemp()
    try:
        runner = os.path.join(d, "run_all.py")
        shutil.copy(_RUNNER, runner)
        for name, text in files.items():
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        p = subprocess.run([sys.executable, runner], capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr
    finally:
        shutil.rmtree(d)


def test_a_passing_file_passes_and_reports_its_count():
    rc, out = _run({"test_ok.py": "def test_a(): pass\ndef test_b(): pass\n"})
    assert rc == 0, out
    assert "2/2 tests passed across 1 files" in out


def test_a_file_that_defines_no_tests_is_a_failure():
    """THE bug: 46 test functions defined in files nobody ran, exit code 0.

    A module with no test callables is not "nothing to do" -- it is a file whose
    contents stopped being reachable, which is what happened here for three files
    and what `allow_empty = True` did for the Build/full shims.
    """
    rc, out = _run({"test_silent.py": "def helper(): pass\n"})
    assert rc != 0, out
    assert "defines no tests" in out


def test_a_module_that_cannot_be_imported_is_a_failure_not_a_skip():
    """An unimportable module reported zero tests before, which looked like zero
    failures. Import errors are results, not absences."""
    rc, out = _run({"test_broken.py": "import nonexistent_xyz\ndef test_a(): pass\n"})
    assert rc != 0, out
    assert "could not be imported" in out


def test_a_failing_test_fails_the_run_and_is_named():
    rc, out = _run({"test_bad.py": "def test_a(): assert False, 'boom'\n",
                    "test_ok.py": "def test_b(): pass\n"})
    assert rc != 0, out
    assert "test_bad.py::test_a" in out
    # The other file still ran: one bad file must not mask the rest.
    assert "1/2 tests passed" in out


def test_an_empty_tests_directory_is_a_failure():
    """The discovery equivalent of allow_empty = False. If the glob matches
    nothing, the glob is wrong -- it never means there is nothing to test."""
    rc, out = _run({})
    assert rc != 0, out
    assert "no tests/test_*.py files found" in out


def test_helpers_and_imported_functions_are_not_counted_as_tests():
    """A `test_`-prefixed name imported FROM another module is not this file's
    test; counting it would double-count and, worse, make a file look non-silent
    because of somebody else's tests."""
    rc, out = _run({
        "test_one.py": "def test_a(): pass\n",
        "test_two.py": "from test_one import test_a\ndef test_b(): pass\n",
    })
    assert rc == 0, out
    assert "2/2 tests passed across 2 files" in out


def test_the_real_suite_is_discovered_whole():
    """Every test file in this repo is reached by discovery -- the check that the
    README's hand-kept list of six filenames could not make.

    This calls `test_files()` directly instead of running the runner on the real
    tests/ directory, and the reason is a trap worth recording: this file IS in
    that directory, so a subprocess here would run the runner, which would run
    this test, which would run the runner... The first version of this test hung
    the suite. Discovery is the thing being claimed, and `test_files()` is
    discovery, so the direct call is also the more precise assertion.
    """
    sys.path.insert(0, os.path.dirname(_RUNNER))
    try:
        import run_all
    finally:
        sys.path.pop(0)
    here = os.path.dirname(os.path.abspath(__file__))
    on_disk = sorted(f for f in os.listdir(here)
                     if f.startswith("test_") and f.endswith(".py"))
    assert run_all.test_files() == on_disk
    assert os.path.basename(__file__) in on_disk, "the runner's own tests"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
