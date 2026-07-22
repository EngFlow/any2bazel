# Experiments

## CMake Experiments

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
