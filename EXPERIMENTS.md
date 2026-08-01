# Experiments

Which Bazel rulesets these migrations used, at which versions, and where rules
were written by hand instead: [docs/BAZEL-RULES.md](docs/BAZEL-RULES.md).

## CMake Experiments

### Ladybird (cmake + vcpkg + cargo -> Bazel)

- Source: https://github.com/LadybirdBrowser/ladybird
- Generated BUILD files; the workspace overlay is in
  [`examples/ladybird/`](examples/ladybird/README.md), the 34 findings in
  [`docs/CASE-ladybird-migration.md`](docs/CASE-ladybird-migration.md)
- `git clone && bazel build //:ladybird` builds the browser: **Bazel owns the
  whole dependency closure**, with no CMake build and no network in any action —
  the 77 vcpkg ports (fetched from a captured asset pin) and the 10 Rust crates +
  `flapc` (154 crates.io archives resolved from `Cargo.lock`) are Bazel targets
- All 51 code generators run under Bazel with output byte-identical to CMake's
  (1,408/1,408 files); the 6 binaries render `--headless=text` and
  `--headless=layout-tree` byte-identically to the CMake reference
- C++23, and the largest subject here: 34 libraries, ~3.7k TUs, LibWeb alone
  ~1,961 compile inputs — which is what surfaced the extractor's depSet OOM
- Generated custom Bazel rules, all because the recipe was worth keeping and the
  ecosystem ruleset would have replaced it: `vcpkg_tree`/`vcpkg_lib` (vcpkg as an
  ordinary action under `x-block-origin`, not `rules_foreign_cc`), and
  `rust_sysroot`/`cargo_crate`/`cargo_lib`/`cargo_binary` (offline cargo, not
  `rules_rust`/`crate_universe`)
- Qt via `kklochkov/rules_qt` — the one place a ruleset *was* adopted

### BoringSSL (cmake <-> Bazel)

- Source: https://github.com/google/boringssl
- Verified build equivalence
- Discovered targets missing from Bazel build (`bssl_shim`, see
  https://github.com/ulfjack/boringssl/tree/bazel-missing-ssl-shim)

### Abseil-cpp (cmake <-> Bazel)

- Source: https://github.com/abseil/abseil-cpp
- Verified build equivalence
- CMake emits -std=gnu++17 (GNU extensions on); Bazel uses -std=c++17

### RE2 (cmake <-> Bazel)

- Source: https://github.com/google/re2
- Verified build equivalence
- CMake emits -std=gnu++17 (GNU extensions on); Bazel uses -std=c++17

### FMT (cmake -> Bazel)

- Source: https://github.com/fmtlib/fmt
- Generated BUILD files

### Spdlog (cmake -> Bazel)

- Source: https://github.com/gabime/spdlog
- Generated BUILD files

### TinyXML2 (cmake -> Bazel)

- Source: https://github.com/leethomason/tinyxml2
- Generated BUILD files
- `CMAKE_CXX_VISIBILITY_PRESET=hidden` and `VISIBILITY_INLINES_HIDDEN=YES`

### Zlib (cmake -> Bazel)

- Source: https://github.com/madler/zlib
- Generated BUILD files
- `-fPIC` in cmake

## Maven Experiments

### Guava (Maven -> Bazel)

- Source: https://github.com/google/guava
- Generated BUILD files
- Maven compiles `module-info.java` with `-source=9 -target=9`, everything else with `-source=8 -target=8`
- However, Maven uses a locally installed JDK (e.g., Jdk 25)
- Maven dependencies: JSpecify, Error Prone Annotations, J2objc Annotations, Guava FailureAccess
- Bazel build uses `rules_jvm_external`

## Other Experiments

### VSCode (Custom -> Bazel)

- Source: https://github.com/microsoft/vscode
- Generated BUILD files, see https://github.com/ulfjack/vscode/tree/vscode-with-bazel
- Generated custom Bazel rules:
  - `ts_program`
  - `bundle_target_name`
  - `esbuild_bundle`
  - `vscode_app`
  - `nodegyp_module`
