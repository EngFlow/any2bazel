#!/usr/bin/env python3
"""Run every test in tests/, and fail if a test FILE contributed nothing.

Why this exists rather than a list of commands in the README: for most of this
repo's life the tests were ten hand-run files, and the README listed six of them.
Three of the ten (`test_emit_cargo.py`, `test_emit_vcpkg.py`,
`test_vcpkg_plumbing.py`) had no `if __name__ == "__main__"` block at all, so
running them imported the module, defined 46 test functions, called none of them,
and exited 0. There is no pytest in this environment, so nothing else called them
either.

That is the same bug as `glob(..., allow_empty = True)` over a directory that is
not there, and as the `Build/full` shim packages in the Ladybird example (case
study finding 35): **a check that cannot fail is indistinguishable from one that
is not needed.** The lesson is not "add a runner to each file" -- that is what was
missing, but a per-file runner is exactly what nobody notices the absence of. The
lesson is that the suite needs ONE thing that knows how many test files there are
and how many tests each one contributed, so a file going silent is a FAILURE
rather than a smaller number nobody was counting.

Hence the two rules enforced here, in this order:

  1. Every `tests/test_*.py` is imported. Import failure is a failure, not a skip
     -- a module that cannot be imported reported zero tests before.
  2. A file that yields **zero** tests is a failure. This is the guard that the
     original bug would have tripped, and it costs one comparison.

Then the tests run, in one process, with one exit code.

Usage:  python3 tests/run_all.py [-v] [name-substring ...]
"""

import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def test_files():
    """Every test module, DISCOVERED rather than listed.

    A hand-kept list is one more thing that can silently omit an entry, which is
    how the README came to name six of the ten files.
    """
    return sorted(f for f in os.listdir(HERE)
                  if f.startswith("test_") and f.endswith(".py"))


def load(fn):
    path = os.path.join(HERE, fn)
    spec = importlib.util.spec_from_file_location(fn[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tests_in(mod):
    """The module's test callables, in source order.

    Source order, not alphabetical: these files are written to be read top to
    bottom (the fixture first, then the contract it pins), so a failure list in
    file order is the one that matches the reader's mental model.
    """
    fns = [(name, obj) for name, obj in vars(mod).items()
           if name.startswith("test_") and callable(obj)
           and getattr(obj, "__module__", None) == mod.__name__]
    return sorted(fns, key=lambda nf: getattr(nf[1], "__code__").co_firstlineno)


def main(argv):
    verbose = "-v" in argv
    filters = [a for a in argv if not a.startswith("-")]

    files = test_files()
    if not files:
        # The discovery equivalent of allow_empty = False: an empty tests/ means
        # the glob is wrong or the layout moved, never that there is nothing to do.
        print("FAIL: no tests/test_*.py files found at all")
        return 1

    total = passed = 0
    silent, broken, failures = [], [], []
    for fn in files:
        try:
            mod = load(fn)
        except Exception:
            # An unimportable module used to report zero tests and (with no
            # runner) exit 0. It is a failure.
            broken.append(fn)
            print("ERROR %s could not be imported" % fn)
            traceback.print_exc()
            continue
        cases = tests_in(mod)
        if not cases:
            # THE guard this file exists for.
            silent.append(fn)
            print("FAIL  %s defines no tests" % fn)
            continue
        selected = [(n, f) for n, f in cases
                    if not filters or any(s in n or s in fn for s in filters)]
        if not selected:
            continue
        n_fail = 0
        for name, fn_ in selected:
            total += 1
            try:
                fn_()
                passed += 1
                if verbose:
                    print("  PASS %s::%s" % (fn, name))
            except Exception:
                n_fail += 1
                failures.append("%s::%s" % (fn, name))
                print("  FAIL %s::%s" % (fn, name))
                traceback.print_exc()
        print("%-24s %d/%d" % (fn, len(selected) - n_fail, len(selected)))

    print()
    print("%d/%d tests passed across %d files" % (passed, total, len(files)))
    if failures:
        print("failed: %s" % ", ".join(failures))
    if silent:
        print("files defining NO tests (this is the bug finding 35 is about): %s"
              % ", ".join(silent))
    if broken:
        print("files that could not be imported: %s" % ", ".join(broken))
    return 1 if (failures or silent or broken) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
