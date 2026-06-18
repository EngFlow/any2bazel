# TODO: configure-time generation — Bazel side + differ

## What "configure-time generation" is (and isn't)

CMake `configure_file(src dst [@ONLY])` runs during `cmake` **configure**
(string templating), leaving **no node in the build/action graph**. It is a
**distinct concept** from build-time generation (`add_custom_command(OUTPUT …)`
/ codegen tools like protoc), which *does* live in the action graph. The two
must not be merged in the model:

| | configure-time | build-time |
|---|---|---|
| CMake source | `configure_file` (only in `--trace`) | action graph (codemodel / aquery) |
| When it runs | `cmake` configure | build |
| Bazel counterpart | **repository/workspace rule** (or `expand_template`) | **genrule** |
| IR type | `ConfiguredFile` (model.configured_files) | not yet modeled |

## Done (CMake → IR, single pass)

- `model.ConfiguredFile` — output name/path + template + options +
  `is_compile_input`. Configure-time only; deliberately separate from any
  future build-time-generation type.
- `extract_configure.parse_configure_trace()` — parses a
  `cmake --trace-format=json-v1` stream, keeps project-owned `configure_file`
  events (drops CMake-internal toolchain/CPack templates), flags
  `is_compile_input` for header/source outputs landing on an include dir.
- `extract_cmake.extract(build, repo_root, trace_path=…)` — single CMake pass
  emits both the File API reply and the trace; the extractor reads both and
  records `configured_files`. Validated on zlib (zconf.h flagged compile-input;
  zlib.pc / install .cmake / test fixtures benign).
- Serialized in the model JSON; `tests/test_configure.py`.

## NOT done — the work remaining

1. **Bazel-side extraction.** Populate `configured_files` from the Bazel build.
   A configure_file output may be either committed (Bazel uses the in-tree file)
   or produced by a repository rule / `expand_template`. We need to recognize
   the Bazel counterpart of each CMake `ConfiguredFile` and record it
   symmetrically. Open question: where does the Bazel side expose this? (repo
   rules don't show in aquery the way actions do.) Likely needs a separate
   Bazel-side probe, analogous to the cmake trace.

2. **The differ.** Compare `configured_files` across sides by **CONTENT**
   (read `output_path` on each side, byte-compare; escalate to LLM judgment;
   record normalization rules in `cmake2bazel.json`). Only `is_compile_input`
   files block parity; benign outputs (.pc, install .cmake) are reported, not
   enforced. The content check is filesystem-dependent — degrade gracefully
   ("output not on disk → report, never silently pass"), like the include-order
   verifier (see FUTURE-include-order-collision-check.md).

3. **Generation.** Emit the Bazel repository rule / `expand_template` that
   reproduces each configure-time output, so a fresh migration actually builds.

## Note for whoever picks this up

The committed-fallback case (zlib: a usable `zconf.h` is in the tree, so the
Bazel build just uses it) is benign and already migrates — the content check
would merely *report* that CMake's generated `zconf.h` and the committed one
differ by a few `#cmakedefine` lines (HAVE_UNISTD_H etc.). The hard cases are
projects whose configure_file output is NOT committed.
