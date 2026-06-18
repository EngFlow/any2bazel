# FUTURE: include search-order collision check

## Status
Deferred. The diff currently compares includes by **presence, not order**
(`_norm_includes` + a subset check in `diff.py`). This note records the proper
fix so it isn't lost.

## The problem
CMake and Bazel often list the same include roots in a **different relative
order**. Example (boringssl `crypto/test/abi_test.cc`, after `include_map`):

```
CMake : @gtest/googlemock/include, @gtest/googletest/include, include
Bazel : ..., include, ..., @gtest/googlemock/include, @gtest/googletest/include
```

All roots are present on both sides — only the order differs (CMake puts the
project's own `include` last; Bazel puts it earlier).

A strict subsequence (order-preserving) check flags this. But it is almost
always **benign**: include search order changes the build *only* if the same
header name is reachable from two roots whose order differs — then order decides
which file wins. If the roots hold disjoint header names, the reorder cannot
change resolution of any `#include`.

We previously enforced order and got false positives; we now do not enforce it,
which is a (small) blind spot in the other direction.

## The proper fix: prove the reorder harmless instead of ignoring it
For any pair of include roots whose relative order differs between the two
sides, enumerate the header files under each root (they are real directories on
disk) and check for a **cross-root header-name collision**:

- **No overlapping header name** → the reorder cannot change which file any
  `#include` resolves to → **pass, having proven it safe**.
- **Overlapping name across reordered roots** → a genuine shadowing risk →
  report as an `includes_diff` error.

This resolves the class with a real verdict rather than a suppression — the same
"verify equivalence, don't assert it" philosophy as the rest of the tool.

## Why it's deferred (the cost)
- **Filesystem access at diff time.** The diff is currently a pure
  function of the two JSON models and touches no filesystem. This check needs to
  glob the include roots. CMake roots (source tree, install prefix) are always
  present; Bazel's `external/` and `bazel-out/.../bin` roots exist after the
  build/fetch that `aquery` already required, but may be absent in a
  cleaned/CI checkout — so the check needs a graceful "roots not on disk →
  cannot verify → fall back to reporting" path.
- **Mapped tokens lose their path.** `include_map` rewrites e.g.
  `external/gtest+` → `@gtest`, discarding the real directory. The collision
  check must run on the **pre-map** paths (or the map must retain the source
  path alongside the token).

## Suggested shape
A standalone verifier (like `triage.py`), kept OUT of `diff.py` so the core diff
stays pure JSON→JSON:

```
python3 scripts/verify_include_order.py diff.json <cmake_repo> <bazel_execroot>
```

It re-reads the pre-map include lists, finds order-differing root pairs, globs
headers, and emits a verdict per TU: `order-safe` (disjoint) or
`order-collision: <header>` (real). The diff keeps reporting the order
difference as informational; the verifier upgrades/clears it.
