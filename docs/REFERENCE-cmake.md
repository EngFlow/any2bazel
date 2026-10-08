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
- Generated code: `configure_file`, `add_custom_command`, protoc or other
  codegen steps.
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

The project flags passed to `cmake` define the platform/options the Bazel
aquery must mirror.

## CMake-specific comparison notes

- **Target names** in `target_map` and `exclude_targets` keys are CMake target
  names (e.g. `"bssl": ":bssl"`).
- **`dep_map`**: CMake spells external link deps as an archive basename
  (`Catch2Main`) or an imported target (`OpenSSL::SSL`), while Bazel spells them
  `catch2_main` / `ssl`.
- **Link flags**: CMake repeats compile/codegen flags (`-fvisibility=hidden`,
  `-fno-common`) on the link line, where they're benign, while Bazel doesn't.
  This is the common `ignore.link_flags` case.

## Tests

For [test diffing](../SKILL.md#diff-tests), configure CMake **without**
`-D..._BUILD_TESTING=OFF` (or the project's equivalent option) so the
reference model includes test targets.
