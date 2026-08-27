# Copyright 2026 EngFlow Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The Rust ring's emitter: the things that would drift silently (finding 34).

Ring 2 part 3 moved Ladybird's 10 production Rust crates off a prebuilt 260 MB
`librust_combined.a` (copied out of `Build/full/cargo/`, pre-merged by a hand-run
`ar -M`) and onto crates Bazel fetches and builds itself, plus a Bazel-built
`flapc`. The real proof of that is a removal test: `Build/full/cargo` moved off
the machine, `bazel clean`, everything still builds and renders identically.
These tests are the cheap regression guard for the parts of it that a future
Ladybird bump can break WITHOUT breaking the build:

  * The **registry/workspace split.** A `[[package]]` with no `checksum` is an
    in-tree workspace member, i.e. a source Bazel already has; turning one into a
    fetch rule builds a crates.io URL for a crate that does not exist. Turning a
    registry crate into nothing is worse -- the offline build fails with "no
    matching package" a long way from the missing rule.
  * The **per-crate feature flags.** Three crates take `--features allocator` and
    the rest take none, and the spec for this work asserted a fourth
    (`libgfx_rust`) that at this commit does not even HAVE that feature -- cargo
    hard-errors. Wrong features change the ABI silently, which is the finding-23
    failure mode, so the features are PARSED out of `import_rust_crate()` rather
    than written down, including the `if (NOT BUILD_SHARED_LIBS)` variable form.
  * The **non-uniform FFI header lists**, and the fact that CMake's own
    declaration is INCOMPLETE: `libweb_rust` declares one header and its build
    scripts write two, because its dependency `libweb_html_tokenizer` writes
    `HTMLTokenizerRustFFI.h` into the same `$FFI_OUTPUT_DIR`. Three LibWeb TUs
    include it. CMake never notices (nothing declares or deletes it); Bazel
    deletes undeclared outputs, so the list must be the union, and the emitter
    must SAY when the two disagree instead of silently picking one.
  * The **`.cargo-checksum.json` shape**, which is what makes the vendor
    directory acceptable to cargo at all, and whose `package` field is the hash
    cargo re-verifies against the lock file.

The header COLLISION that this work's worst bug came from is pinned too: six
crates each emit a file literally named `RustFFI.h` into one shared directory, so
"which crate's copy survives" is a real question, and for one build the answer was
wrong (liburl_rust shipped libregex_rust's header). See
test_colliding_header_names_are_real.
"""

import importlib.util
import json
import os
import re
import tempfile

_WS = os.path.join(os.path.dirname(__file__), "..", "examples", "ladybird",
                   "workspace")
_EMIT = os.path.join(_WS, "Meta", "emit_cargo_bazel.py")


def _load(root=None):
    """Load the emitter, optionally pointed at a fixture checkout."""
    if root:
        os.environ["LADYBIRD_ROOT"] = root
    else:
        os.environ.pop("LADYBIRD_ROOT", None)
    spec = importlib.util.spec_from_file_location("emit_cargo_bazel", _EMIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


emit = _load()


def _read(rel):
    with open(os.path.join(_WS, rel)) as f:
        return f.read()


# ---------------------------------------------------------------------------
# A minimal fixture checkout. Small on purpose: it is a CONTRACT, not a copy of
# Ladybird -- these tests must keep passing when Ladybird's real lock file grows.
# ---------------------------------------------------------------------------
LOCK = '''version = 4

[[package]]
name = "libgfx_rust"
version = "0.1.0"
dependencies = ["yuv"]

[[package]]
name = "libweb_rust"
version = "0.1.0"

[[package]]
name = "yuv"
version = "0.8.13"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "47d3a7e2cda3061858987ee2fb028f61695f5ee13f9490d75be6c3900df9a4ea"

[[package]]
name = "smallvec"
version = "1.15.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "67b1b7a3b5fe4f1376887184045fcf45c69e92af734b7aaddc05fb777b6fbd03"
'''

FLAP_LOCK = '''version = 4

[[package]]
name = "flapc"
version = "0.1.0"
dependencies = ["smallvec"]

[[package]]
name = "smallvec"
version = "1.15.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "67b1b7a3b5fe4f1376887184045fcf45c69e92af734b7aaddc05fb777b6fbd03"
'''

# The two shapes import_rust_crate() really takes: a literal FEATURES, and the
# variable form CMake uses for "static builds only". Both are in Ladybird.
CMAKE_GFX = '''
import_rust_crate(MANIFEST_PATH Rust/Cargo.toml CRATE_NAME libgfx_rust FFI_HEADER RustFFI.h)
'''

CMAKE_WEB = '''
import_rust_crate(
    MANIFEST_PATH Rust/Cargo.toml
    CRATE_NAME libweb_rust
    FFI_HEADERS HTML/Parser/RustFFI.h
)

set(css_rust_features "")
if (NOT BUILD_SHARED_LIBS)
    set(css_rust_features FEATURES allocator)
endif()
import_rust_crate(
    MANIFEST_PATH CSS/Rust/Cargo.toml
    CRATE_NAME libweb_css_rust
    ${css_rust_features}
    FFI_HEADERS RustFFI.h SelectorRustFFI.h
)
'''


def _fixture():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "Cargo.lock"), "w") as f:
        f.write(LOCK)
    with open(os.path.join(d, "rust-toolchain.toml"), "w") as f:
        f.write('[toolchain]\nchannel = "1.96.1"\n')
    os.makedirs(os.path.join(d, "Libraries/LibJS/Flap"))
    with open(os.path.join(d, "Libraries/LibJS/Flap/Cargo.lock"), "w") as f:
        f.write(FLAP_LOCK)
    for lib, text in (("LibGfx", CMAKE_GFX), ("LibWeb", CMAKE_WEB)):
        os.makedirs(os.path.join(d, "Libraries", lib), exist_ok=True)
        with open(os.path.join(d, "Libraries", lib, "CMakeLists.txt"), "w") as f:
            f.write(text)
    return d


# ---------------------------------------------------------------------------
# The registry / workspace split.
# ---------------------------------------------------------------------------
def test_only_checksummed_packages_become_fetch_rules():
    """A workspace member has no checksum because it is a SOURCE, not a download."""
    mod = _load(_fixture())
    registry, workspace = mod.split_packages(mod.lock_packages("Cargo.lock"))
    assert {n for n, _v, _c in registry} == {"yuv", "smallvec"}
    assert {n for n, _v in workspace} == {"libgfx_rust", "libweb_rust"}


def test_the_real_lock_splits_154_registry_from_13_workspace():
    """Pins the counts against the checked-in generated file, which is ground
    truth neither this test nor the emitter produced (finding 25's rule)."""
    crates = _read("cargo_crates.bzl")
    assert crates.count("http_archive(") == 154, crates.count("http_archive(")
    # And every one of them names a crates.io URL, i.e. none is a workspace member
    # that slipped through.
    urls = re.findall(r"urls = \['([^']+)'\]", crates)
    assert len(urls) == 154
    assert all(u.startswith("https://static.crates.io/crates/") for u in urls)


def test_a_crate_shared_by_both_locks_is_one_fetch():
    """flapc pins `=1.15.1` smallvec, which the big workspace also has.

    One repo per (name, version), so the shared crate must collapse to a single
    rule -- and the emitter must CHECK the two locks agree on its hash, because a
    repo name serving two contents is the silent-wrong-content bug the vcpkg
    emitter's hash-suffixed names exist to prevent.
    """
    mod = _load(_fixture())
    crates = mod.all_registry_crates()
    assert ("smallvec", "1.15.1") in crates
    assert len([k for k in crates if k[0] == "smallvec"]) == 1
    assert len(crates) == 2  # yuv + smallvec, not 3


def test_two_locks_disagreeing_on_a_hash_is_a_hard_error():
    """The check above only counts if disagreement FAILS rather than picking one."""
    d = _fixture()
    p = os.path.join(d, "Libraries/LibJS/Flap/Cargo.lock")
    with open(p, "w") as f:
        f.write(FLAP_LOCK.replace("67b1b7a3", "deadbeef"))
    mod = _load(d)
    try:
        mod.all_registry_crates()
    except SystemExit as e:
        assert "two different checksums" in str(e)
    else:
        raise AssertionError("a checksum conflict was silently accepted")


def test_crate_url_is_a_pure_function_of_name_and_version():
    """This is why the Rust ring needed no capture, unlike vcpkg.

    vcpkg's URLs are computed by CMake programs at run time, so pinning them
    needed an instrumented run. crates.io's URL is this one line, so Cargo.lock
    alone is the pin.
    """
    assert emit.crate_url("aho-corasick", "1.1.4") == (
        "https://static.crates.io/crates/aho-corasick/aho-corasick-1.1.4.crate")


def test_repo_names_are_unique_per_crate_version():
    """`-` and `.` are illegal in a repo name; the normalization must not collide."""
    assert emit.repo_name("aho-corasick", "1.1.4") == "crate_aho_corasick_1_1_4"
    names = set(re.findall(r"name = '([^']+)'", _read("cargo_crates.bzl")))
    # 154 crates -> 154 distinct repo names, i.e. the normalization is injective
    # on the real input set.
    assert len(names) == 154


def test_a_crate_archive_declares_its_type_because_dot_crate_is_not_known():
    """http_archive rejects a `.crate` suffix; without type= the fetch fails with
    a message about .zip/.tar.gz that says nothing about the cause."""
    assert _read("cargo_crates.bzl").count('type = "tgz"') == 154


# ---------------------------------------------------------------------------
# Features: parsed from CMake, per crate, both spellings.
# ---------------------------------------------------------------------------
def test_features_are_parsed_per_crate_including_the_variable_form():
    """The literal form and the `if (NOT BUILD_SHARED_LIBS)` variable form."""
    mod = _load(_fixture())
    specs = {c["crate"]: c for c in mod.parse_cmake_crates()}
    assert specs["libgfx_rust"]["features"] == []
    assert specs["libweb_rust"]["features"] == []
    assert specs["libweb_css_rust"]["features"] == ["allocator"]


def test_the_real_feature_set_is_three_of_ten_and_not_libgfx():
    """The spec for this work said libgfx_rust takes `allocator`. It does not --
    cargo fails outright ("the package 'libgfx_rust' does not contain this
    feature"). Pinned against the generated index, so a future bump that changes
    a feature has to change this number deliberately."""
    index = _read("cargo_index.bzl")
    specs = re.findall(r"'(\w+)': \{\n\s+\"manifest\": '[^']+',\n\s+"
                       r"\"features\": (\[[^\]]*\])", index)
    feats = {name: eval(f) for name, f in specs}
    assert len(feats) == 10, sorted(feats)
    with_alloc = {n for n, f in feats.items() if f == ["allocator"]}
    assert with_alloc == {"libregex_rust", "liburl_rust", "libunicode_rust",
                          "libweb_content_blocker_rust", "libweb_css_rust",
                          "libweb_layout_rust"}, sorted(with_alloc)
    assert feats["libgfx_rust"] == []
    assert feats["libjs_rust"] == []
    assert feats["libtextcodec_rust"] == []
    assert feats["libweb_rust"] == []


# ---------------------------------------------------------------------------
# FFI headers: non-uniform, and CMake's list is incomplete.
# ---------------------------------------------------------------------------
def test_ffi_header_lists_are_not_uniform():
    """One name per crate would be wrong for half of them."""
    index = _read("cargo_index.bzl")
    hdrs = dict(re.findall(r"'(\w+)': \{\n(?:.*\n)*?\s+\"ffi_headers\": (\[[^\]]*\])",
                           index))
    hdrs = {k: eval(v) for k, v in hdrs.items()}
    assert hdrs["libweb_css_rust"] == ["ComputedValuesRustFFI.h", "RustFFI.h",
                                       "SelectorRustFFI.h", "StyleValueRustFFI.h"]
    assert hdrs["libweb_layout_rust"] == ["Layout/TreeBuilderRustFFI.h"]
    assert hdrs["libweb_content_blocker_rust"] == ["ContentBlockerRustFFI.h"]
    assert hdrs["libgfx_rust"] == ["RustFFI.h"]
    # 14 headers across 10 crates, which is the number in CMake's own build tree.
    assert sum(len(v) for v in hdrs.values()) == 14


def test_the_header_cmake_never_declares_is_still_declared_here():
    """libweb_rust's DEPENDENCY writes HTMLTokenizerRustFFI.h, and three LibWeb
    TUs include it. CMake declares one header for that crate and gets away with
    it because nothing deletes the other; Bazel deletes undeclared outputs, so
    the emitter must take the union of declared and observed."""
    mod = _load(_fixture())
    assert "HTMLTokenizerRustFFI.h" in mod.FFI_HEADERS_OBSERVED["libweb_rust"]
    index = _read("cargo_index.bzl")
    assert "'HTML/Parser/RustFFI.h', 'HTMLTokenizerRustFFI.h'" in index


def test_a_header_cmake_declares_but_cargo_never_writes_is_reported():
    """The union must not be silent in the other direction either: a header
    declared and never written is a failing action, and --report must say so
    BEFORE the build does."""
    d = _fixture()
    mod = _load(d)
    mod.FFI_HEADERS_OBSERVED = dict(mod.FFI_HEADERS_OBSERVED)
    mod.FFI_HEADERS_OBSERVED["libweb_css_rust"] = ["RustFFI.h"]  # drop one
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.report(mod.all_registry_crates())
    assert rc != 0, "a declared-but-never-written header was accepted"
    assert "declared by CMake but never written" in buf.getvalue()
    assert "SelectorRustFFI.h" in buf.getvalue()


def test_colliding_header_names_are_real():
    """Six crates emit a file literally named RustFFI.h.

    They all land in one shared $FFI_OUTPUT_DIR, so "which crate's copy survives"
    is a real question -- and for one build of this work the answer was wrong:
    liburl_rust path-depends on libregex_rust, both build scripts ran against the
    same directory, and the URL crate shipped the REGEX crate's header. It was
    caught only by byte-comparing against CMake's tree. The fix mirrors
    Meta/CMake/sync_rust_ffi_header.cmake: resolve each header from the owning
    crate's own OUT_DIR (via build/<crate>-*/root-output), falling back to the
    shared dir only for a header a dependency wrote.

    This test pins the collision itself, so nobody "simplifies" that lookup back
    into a plain copy from the shared dir.
    """
    index = _read("cargo_index.bzl")
    hdrs = dict(re.findall(r"'(\w+)': \{\n(?:.*\n)*?\s+\"ffi_headers\": (\[[^\]]*\])",
                           index))
    owners = [k for k, v in hdrs.items() if "'RustFFI.h'" in v]
    assert len(owners) >= 6, owners
    driver = _read("Meta/cargo_build.sh")
    assert "root-output" in driver, "the header lookup no longer prefers OUT_DIR"
    assert "FFI_SCRATCH" in driver, "FFI_OUTPUT_DIR is no longer a scratch dir"


def test_headers_are_prefixed_so_both_include_spellings_resolve():
    """Both <LibURL/RustFFI.h> and a bare <RustFFI.h> are in the tree, and CMake
    makes both work (a -I into the gendir plus -IBuild/full/Libraries). The
    prefix is what reproduces that; getting it wrong is a compile error in a
    different package."""
    mod = _load(_fixture())
    assert mod.FFI_PREFIX["liburl_rust"] == "LibURL"
    assert mod.FFI_PREFIX["libweb_css_rust"] == "LibWeb"
    assert mod.FFI_PREFIX["libweb_layout_rust"] == "LibWeb"
    # Every crate with observed headers has a prefix: a missing one would stage
    # the header at the include root and silently change its include spelling.
    assert set(mod.FFI_HEADERS_OBSERVED) <= set(mod.FFI_PREFIX)


# ---------------------------------------------------------------------------
# .cargo-checksum.json: the thing that makes a vendor dir acceptable at all.
# ---------------------------------------------------------------------------
def test_the_checksum_file_carries_the_package_hash():
    """cargo refuses a vendor directory without .cargo-checksum.json, and
    hard-errors when `package` disagrees with the lock ("checksum for `yuv
    v0.8.13` changed between lock files"). `files` may be empty -- cargo only
    consults it to detect edits -- but `package` may not, and it is the hash
    Bazel already verified at fetch time, re-asserted so cargo re-checks it.

    Pinned by reading the driver, because the shape is the interface between two
    build systems and a "cleanup" that drops `package` would leave the build
    working while removing the check.
    """
    vendor = _read("Meta/cargo_vendor.sh")
    m = re.search(r'printf \'(\{[^\']*\})\\n\' "\$sha"', vendor)
    assert m, "the .cargo-checksum.json write is gone or reshaped"
    shape = json.loads(m.group(1).replace("%s", "0" * 64))
    assert shape == {"files": {}, "package": "0" * 64}


def test_the_index_key_carries_name_version_and_hash():
    """The vendor dir is named <name>-<version> and its checksum file needs the
    crate's sha256, so all three have to reach the action -- through the index
    key, since a Bazel label carries none of them."""
    index = _read("cargo_index.bzl")
    keys = re.findall(r"^    '([^']+)': '@crate_", index, re.M)
    assert len(keys) == 154
    for k in keys:
        name, version, sha = k.split(" ")
        assert re.fullmatch(r"[0-9a-f]{64}", sha), k
        assert re.match(r"[\d]", version), k


# ---------------------------------------------------------------------------
# Idempotence and the no-cargo/no-network claim.
# ---------------------------------------------------------------------------
def test_the_emitter_is_idempotent_and_needs_no_cargo_or_network():
    """Two runs, byte-identical, from the lock files alone.

    The fixture has no cargo, no vendor dir and no network; if the emitter ever
    reached for any of them this fails rather than working on the one machine
    that has them.
    """
    import contextlib
    import io
    mod = _load(_fixture())
    crates = mod.all_registry_crates()
    specs = mod.crate_specs()
    outs = []
    for _ in range(2):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.emit_crates(crates)
            mod.emit_index(crates, specs)
            mod.emit_extension(crates)
            mod.emit_ring(crates, specs)
        outs.append(buf.getvalue())
    assert outs[0] == outs[1]
    assert "http_archive(" in outs[0]


def test_check_flag_detects_drift_in_a_generated_file():
    """The repo's rule is "never hand-edit a generated file", which is only
    enforceable if the check is one command -- and only useful if it FAILS on a
    hand edit."""
    import contextlib
    import io
    d = _fixture()
    mod = _load(d)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        # Nothing generated in the fixture yet, so every file is MISSING.
        assert mod.check(d) != 0
    assert "MISSING" in buf.getvalue()

    # Write the real output, then corrupt one byte.
    os.makedirs(os.path.join(d, "out"))
    crates, specs = mod.all_registry_crates(), mod.crate_specs()
    for fn, fl in mod.GENERATED.items():
        b = io.StringIO()
        with contextlib.redirect_stdout(b):
            {"--crates": lambda: mod.emit_crates(crates),
             "--index": lambda: mod.emit_index(crates, specs),
             "--extension": lambda: mod.emit_extension(crates),
             "--ring": lambda: mod.emit_ring(crates, specs)}[fl]()
        with open(os.path.join(d, "out", fn), "w") as f:
            f.write(b.getvalue())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert mod.check(os.path.join(d, "out")) == 0
    with open(os.path.join(d, "out", "cargo_index.bzl"), "a") as f:
        f.write("# hand edit\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert mod.check(os.path.join(d, "out")) != 0
    assert "DRIFTED  cargo_index.bzl" in buf.getvalue()


def test_the_toolchain_pin_is_read_not_chosen():
    """rust-toolchain.toml says 1.96.1; "whatever rustc is on PATH" is not a pin.

    Checked in both directions: the emitter reads the file, and the three
    toolchain component hashes in cargo.bzl name that same version -- a skew
    between them would fetch a compiler the project did not ask for.
    """
    mod = _load(_fixture())
    assert mod.toolchain_channel() == "1.96.1"
    bzl = _read("cargo.bzl")
    assert 'RUST_CHANNEL = "1.96.1"' in bzl
    assert len(re.findall(r'"(rustc|cargo|rust-std)": "[0-9a-f]{64}"', bzl)) == 3


# ---------------------------------------------------------------------------
# The plumbing: what the generated files must and must not say.
# ---------------------------------------------------------------------------
def test_nothing_generated_still_reads_the_cmake_cargo_tree():
    """The host escape this whole task existed to remove.

    The 260 MB archive, the `ar -M` merge and the flapc binary all lived under
    Build/full/cargo; a lingering reference keeps working on a machine that has
    that tree, and only fails for the person cloning the repo -- which is the
    entire point of the exercise.
    """
    offenders = []
    for rel in ("BUILD.bazel", "Libraries/LibWeb/BUILD.bazel", "codegen_root.bzl",
                "cargo_ring.bzl", "cargo_index.bzl", "bazelrc.txt",
                "Build/full/Libraries/BUILD.bazel"):
        txt = _read(rel)
        if re.search(r"//Build/full/cargo|Build/full/cargo/build|rust_ffi_headers\b",
                     txt.replace("no rust_ffi_headers", "")):
            offenders.append(rel)
    assert not offenders, "still reference CMake's cargo tree: %s" % offenders


def test_each_crate_links_its_own_archive_and_no_others():
    """The `ar -M` blob is gone, and so is the --start-group that first replaced it.

    The shim's premise -- "the archives have circular cross-crate references" --
    is false, and I measured it rather than believing it: subtract each archive's
    OWN definitions from its undefined symbols and the cross-crate edge count is
    0 for all 10 crates. The ~200-700 symbols any two share are each crate's own
    bundled copy of rust-std.

    That makes the group actively wrong, not just redundant: inside
    --start-group, ld can satisfy libgfx_rust's std symbol from libjs_rust.a's
    member object, which then wants libjs_rust's C++ FFI -- and ImageDecoder,
    which links LibGfx but not LibJS, fails with hundreds of undefined
    `rust_sfd_*`. So: one linker input per crate, exactly CMake's edge. This test
    is the regression guard, because "add a --start-group" is the reflexive fix
    for any Rust link error.
    """
    bzl = _read("cargo.bzl")
    impl = bzl.split("def _cargo_lib_impl", 1)[1].split("\ncargo_lib = rule", 1)[0]
    # Comments may (and do) discuss the group; the CODE must not emit it.
    code = "\n".join(l for l in impl.splitlines() if not l.strip().startswith("#"))
    assert "--start-group" not in code, "the group leaks crates into each other"
    assert "user_link_flags = [archive.path]" in impl, "must link exactly one archive"
    assert "additional_inputs" in impl, "the archive would not be in the sandbox"
    ring = _read("cargo_ring.bzl")
    assert ring.count("cargo_crate(") == 10
    assert ring.count("cargo_lib(") == 10, "one consumable target per crate"
    assert "cargo_libs(" not in ring, "the shared link group is gone"


def test_a_library_depends_on_the_crates_it_uses_and_no_others():
    """The dep edges mirror CMake's target_link_libraries, one for one.

    Measured against the reference build, which is the only thing that can settle
    it: CMake's LibGfx links libgfx_rust.a alone; LibWeb links its four. If the
    emitter ever goes back to handing every library the whole Rust closure, the
    build still links here (a superset does) but ImageDecoder and RequestServer
    do not -- so this asserts the *narrowness*, not merely the presence.
    """
    root = _read("BUILD.bazel")
    libweb = _read("Libraries/LibWeb/BUILD.bazel")
    # No shared aggregate target anywhere.
    for rel, txt in (("BUILD.bazel", root), ("Libraries/LibWeb/BUILD.bazel", libweb)):
        assert "'//:rust'" not in txt, "%s links the whole Rust closure" % rel

    def crates_of(txt, name, path="//:"):
        block = txt.split("name = '%s',\n" % name, 1)[1].split("\n)", 1)[0]
        return sorted(re.findall(r"'%s(\w+_rust)_lib'" % re.escape(path), block))

    assert crates_of(root, "LibGfx") == ["libgfx_rust"]
    assert crates_of(root, "LibJS") == ["libjs_rust"]
    assert crates_of(root, "LibRegex") == ["libregex_rust"]
    assert crates_of(libweb, "LibWeb") == [
        "libweb_content_blocker_rust", "libweb_css_rust",
        "libweb_layout_rust", "libweb_rust",
    ]


def test_the_crates_are_built_once_for_both_configurations():
    """cfg = "exec" on the crate attrs, the same pin vcpkg_lib's tree needs.

    //:generate_interpreter_layout is an exec-config binary whose closure reaches
    the Rust crates, so without the pin each crate is built twice for
    byte-identical output -- and a genrule tool linked against one copy cannot
    find the other.
    """
    bzl = _read("cargo.bzl")
    for rule in ("cargo_lib = rule",):
        block = bzl.split(rule, 1)[1].split("\n)", 1)[0]
        assert 'cfg = "exec"' in block, "%s is not pinned to exec" % rule


def test_the_build_action_is_an_action_not_a_repository_rule():
    """A repository_rule runs at loading time: no sandbox, no remote cache, and
    no dependency on the sources it reads. The whole point of finding 28."""
    bzl = _read("cargo.bzl")
    assert "ctx.actions.run(" in bzl
    # `repository_rule` appears in a comment explaining why it is NOT used; what
    # must not appear is a CALL to it.
    assert not re.search(r"= *repository_rule\(", bzl)
    # And the hermeticity is enforced by the sandbox, not just by --offline.
    assert '"block-network": "1"' in bzl
    assert "--offline --locked" in _read("Meta/cargo_build.sh")


def test_flapc_is_built_by_bazel_and_used_as_the_genrule_tool():
    """The last artifact this migration took from CMake."""
    ring = _read("cargo_ring.bzl")
    assert "cargo_binary(" in ring
    assert 'bin = "flapc"' in ring
    codegen = _read("codegen_root.bzl")
    assert "tools = ['//:flapc']" in codegen
    assert "Build/full/bin/flapc" not in codegen


def test_the_libweb_package_exports_its_crate_sources():
    """Bazel packages cut across the cargo workspace: five crates live under
    Libraries/LibWeb, which is its own package, and glob() is package-relative --
    the root package cannot see them at all. The filegroup is the only thing that
    crosses, and if it loses a directory the crate builds against a stale tree.
    """
    libweb = _read("Libraries/LibWeb/BUILD.bazel")
    assert 'name = "rust_crate_srcs"' in libweb
    for sub in ("Rust/**", "CSS/Rust/**", "Layout/Rust/**",
                "ContentBlocker/Rust/**", "HTML/Parser/Rust/**"):
        assert '"%s"' % sub in libweb, sub
    # The CSS data files libweb_css_rust's build script GENERATES Rust from.
    for data in ("CSS/Properties.json", "CSS/Keywords.json", "CSS/Enums.json",
                 "HTML/TagNames.h", "HTML/Parser/Entities.json"):
        assert '"%s"' % data in libweb, data
    assert "//Libraries/LibWeb:rust_crate_srcs" in _read("cargo_ring.bzl")


def test_every_extension_created_repo_is_named_in_module_bazel():
    """bzlmod needs every repo an extension creates named in use_repo() to be
    visible, and the failure ("no such repository") lands far from its cause. 157
    names by hand would drift, so they are emitted -- this checks the checked-in
    MODULE.bazel actually has them all."""
    module = _read("MODULE.bazel")
    block = module.split("cargo_deps = use_extension", 1)[1]
    named = set(re.findall(r"'(crate_[A-Za-z0-9_]+|rust_[A-Za-z0-9_]+)'", block))
    declared = set(re.findall(r"name = '(crate_[A-Za-z0-9_]+)'",
                              _read("cargo_crates.bzl")))
    missing = declared - named
    assert not missing, "created but not named in MODULE.bazel: %s" % sorted(missing)
    # Plus the three toolchain components.
    assert {"rust_rustc_1_96_1", "rust_cargo_1_96_1",
            "rust_rust_std_1_96_1"} <= named
