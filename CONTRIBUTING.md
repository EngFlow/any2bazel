# Contributing to any2bazel

Contributions are welcome. This document explains how to contribute and which
checks must pass before a change can merge.

## EngFlow CLA

All contributions require a signed EngFlow Contributor License Agreement (CLA)
on file before they can be merged. If you have not signed one, a maintainer
will let you know how to do so when you send your first change.

## Discuss major changes first

For anything larger than a small fix, please reach out and discuss the change
with the maintainers before investing significant effort. Open an issue at
https://github.com/EngFlow/any2bazel describing the problem and your proposed
approach. This avoids duplicated work and changes that don't fit the project's
direction.

## Changes must be complete

Each change should be a complete solution:

* Changes to `scripts/` must include tests in `tests/`.
* Changes to a script's arguments, output, or behavior must update `SKILL.md`
  and any affected documentation in the same change.
* Bug fixes should include a test that fails without the fix.

Keep changes focused, smaller changes ensure faster reviews.

## Code style
Python code in this project generally follows [PEP
8](https://peps.python.org/pep-0008/). When in doubt, follow the style of the
surrounding code.

## Sending changes

Send changes as pull requests to https://github.com/EngFlow/any2bazel. Make
sure all presubmit requirements below pass.

## Presubmit requirements

### Python tests

CI runs the test suite with pytest in a fresh virtual environment. To
reproduce locally:

```bash
python -m venv .venv
.venv/bin/pip install pytest
.venv/bin/pytest tests/
```

### Copyright headers

All source files must begin with the Apache 2.0 license header.

```bash
bazel run //infra/internal/check_copyright_headers
```

> [!IMPORTANT]
> The check inspects files in `HEAD`, not the working tree. **Commit your
> changes before running it**, or new files will not be checked.
