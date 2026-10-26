# Reference build: CMake

Supporting context for migrating **from** CMake. The migration process itself
lives in [SKILL.md](../SKILL.md); this file only covers what is unique to CMake
as the reference build.

**Status: mature** — the validated path.

## Detecting it

`CMakeLists.txt` files throughout the repo.

## Scope traps

When [confirming scope](../SKILL.md#confirm-scope), inspect `CMakeLists.txt`
for these and surface them before proceeding:
- Build-time generation with no clear Bazel mapping: a sub-build the CMake
  model does not show (autotools, a vendored fork's own build) whose outputs
  the binaries link. Other codegen (`add_custom_command`, protoc) is yours to
  migrate, and so is `configure_file`: see
  [Configure-time generated files](#configure-time-generated-files).
- `find_package` of non-system libraries. There is no automatic
  `find_package` → bzlmod resolution; each one must be mapped by hand.

## Extracting the reference model

The CMake side is the **File API codemodel-v2**, not `compile_commands.json`
(which lacks link info). It exposes both compile and link actions.

A single CMake pass emits both the File API reply and a `--trace` (the latter
is the only place `configure_file()` outputs are visible — they leave no node in
the build graph). The optional 4th arg feeds the trace to the extractor, which
records configure-time generated files in `configured_files`.
```bash
mkdir -p <build>/.cmake/api/v1/query
touch     <build>/.cmake/api/v1/query/codemodel-v2
cmake -S <src> -B <build> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    --trace-expand --trace-format=json-v1 2> <build>/trace.jsonl   # + project flags
python3 "$SKILL_DIR/scripts/extract_cmake.py" <build> <repo_root> model.cmake.json <build>/trace.jsonl
```
> Configure-time generated compile inputs (e.g. CMake's `configure_file` output
> `zconf.h`) are recorded but **not yet diffed** — the Bazel-side extraction and
> the content differ are TODO (see
> [TODO-configure-time-generation.md](TODO-configure-time-generation.md)). This
> is distinct from build-time codegen (genrules), which is also unmodeled.
> `diff.py` lists them in a `configured_files_not_compared` warning (see
> [Diff against reference build](../SKILL.md#diff)).

The project flags passed to `cmake` define the platform/options the Bazel
aquery must mirror.

## Writing the BUILD files

What is specific to CMake when
[creating the BUILD files](../SKILL.md#create-build-files).

### Build type

`--compilation_mode` is not a translation of `CMAKE_BUILD_TYPE`: Release is
`-O3 -DNDEBUG` where Bazel's `opt` is `-O2 -DNDEBUG`, and Debug is `-g` where
Bazel's default `fastbuild` has neither. A project may set its own default
build type in `CMakeLists.txt` even when none is passed. Read the reference's
level (`levels` in `diff.json`, or `CMAKE_<LANG>_FLAGS_<CONFIG>` in the cache)
and pin it in `.bazelrc`, e.g. `build -c opt` plus `build --copt=-O3`. `opt`
also turns assertions off where the reference keeps them (`ndebug` in
`diff.json`); `build --copt=-UNDEBUG` undoes the toolchain's `-DNDEBUG`, not
one in a rule's `copts`.

### Configure-time generated files

For `configure_file` and probe headers, prefer a Starlark `configure_file` rule
over the `.in` template, so the configuration stays derived. Checking in a
probe's result is acceptable for one platform if the file says so and you
verify it, file by file, against the configure output it came from. Never
point Bazel at CMake's build directory: that will not rebuild from the
repository, which is worse than migrating less.

## CMake-specific comparison notes

- **Target names** in `target_map` and `exclude_targets` keys are CMake target
  names (e.g. `"bssl": ":bssl"`).
- **`dep_map`**: CMake spells external link deps as an archive basename
  (`Catch2Main`) or an imported target (`OpenSSL::SSL`), while Bazel spells them
  `catch2_main` / `ssl`.
- **Link flags**: CMake repeats compile/codegen flags (`-fvisibility=hidden`,
  `-fno-common`) on the link line, where they're benign, while Bazel doesn't.
  The differ reports warnings, `-D`/`-U` and the common front-end `-f` flags
  as `link_compile_only_flags`; any other benign one is an `ignore.link_flags`
  case.

## Tests

For [test diffing](../SKILL.md#diff-tests), configure CMake **without**
`-D..._BUILD_TESTING=OFF` (or the project's equivalent option) so the
reference model includes test targets.
