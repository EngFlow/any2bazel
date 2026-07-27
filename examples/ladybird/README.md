# Ladybird → Bazel: Ring 1b codegen artifacts

These are the codegen-parity artifacts from migrating the Ladybird browser
(CMake+vcpkg, C++23) to Bazel. See `../../docs/CASE-ladybird-migration.md` for
the full story. They run against a Ladybird checkout with a completed reference
build in `Build/full` (CMake File-API + `ninja` materialize all generated files
there), not against this repo.

## Files

- **`bazel_parity_harness.py`** — extracts every `Meta/Generators/*.py`
  `CUSTOM_COMMAND` from `Build/full/build.ninja`, re-runs it into a scratch
  mirror, and byte-diffs each produced `.h`/`.cpp`/`.idl` against the CMake
  build. Proves the generators are reproducible before wrapping them. Result on
  Ladybird: 71/71 single-generator outputs identical (the 1331-file bindings
  mega-command is verified separately, all identical).

- **`emit_codegen_bazel.py`** — parses `build.ninja` and emits a Bazel
  `genrule` per generator command: absolute source paths become
  package-relative `$(location …)`, CMake `*.tmp` outputs become genrule `outs`,
  quoted args are handled via `shlex`, and every command is pinned to
  `PYTHONHASHSEED=0` for hermetic, byte-stable output. Usage:
  `emit_codegen_bazel.py Libraries/LibWeb > codegen.bzl`.

- **`LibWeb.codegen.bzl`** — the emitted result for LibWeb: 27 genrules that
  reproduce all **1379** LibWeb generated files byte-for-byte under Bazel,
  including the single `generate_libweb_bindings.py` mega-genrule (661 IDL
  inputs → 1331 files). Loaded from a `Libraries/LibWeb/BUILD.bazel` that also
  needs a `//Meta:generators` filegroup staging the whole `Meta/Generators` +
  `Meta/Utils` Python tree (generators `sys.path.append` then import
  `Generators.*` / `Utils.*`).

## Two parity findings this surfaced

1. **Undeclared implicit input** — `generate_dom_tree.py` reads
   `HTML/MediaControls.css` via a `<link>` in its input `.html`; CMake never
   declared it, Bazel's sandbox caught the `FileNotFoundError`.
2. **`PYTHONHASHSEED` nondeterminism** — `generate_libweb_bindings.py` emitted
   one file's structs in set-iteration order; pinning `PYTHONHASHSEED=0`
   restores byte-parity with CMake and makes the actions cacheable.
