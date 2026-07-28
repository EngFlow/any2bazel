# Case study: Dolphin → Bazel, and the problems the migration exposed

**Subject:** [dolphin-emu/dolphin](https://github.com/dolphin-emu/dolphin) at
`d742aa8`, ~900 C++ translation units, 11 first-party libraries, 48 vendored
externals, CMake → Bazel via the any2bazel action-graph differ.

**Outcome:** both configurations build green (`bazel build //...` and
`--define=enable_qt=true`), 1350 unit tests pass, all five binaries link and run,
and both parity diffs converge with 0 errors.

This document is **not** about that. It is about what the migration *found*. A
build migration is an unusually good static analysis: to express a build in a
system that enforces declared dependencies and sandboxes headers, you must first
discover every place the original build got away with not saying what it meant.
Dolphin is a well-maintained, 20-year-old codebase, and every finding below
predates the migration and is live in `master` today.

The order is roughly worst-first.

---

## 1. The dependency graph is one big cycle

Dolphin's `CMakeLists.txt` files declare a library graph that looks layered.
It isn't. Reading `target_link_libraries` on the Linux configuration and
computing strongly-connected components:

```
discio       PUBLIC core
core         PUBLIC audiocommon common discio inputcommon
                    videonull videoogl videosoftware
videocommon  PUBLIC core
videonull    PUBLIC common videocommon
videoogl     PUBLIC common videocommon
videosoftware PUBLIC common videocommon
```

`core → discio → core`. `core → videoogl → videocommon → core`. The declared
graph contains a **single 6-library SCC**:

> `{core, discio, videocommon, videonull, videoogl, videosoftware}`

These six targets are not a layering; they are one library wearing six names.
Nothing in the build enforces a direction between them, so nothing stops the next
`#include` from adding another edge — and the history shows exactly that
accumulation.

### It's worse than declared: `common` reaches up into `core`

Two files in the *bottom* library include the *middle* one:

```
Source/Core/Common/FatFsUtil.cpp:29        #include "Core/Config/MainSettings.h"
Source/Core/Common/TraversalClient.cpp:...  #include "Core/NetPlayProto.h"
```

`common`'s CMake target does **not** declare `core` as a dependency. It compiles
anyway, for two reasons that are both accidents rather than decisions:

1. The top-level `CMakeLists.txt` does `include_directories(Source/Core)` before
   anything else, so *every* TU in the tree can include *any* header in the tree.
   Include paths, not declared dependencies, are what actually make the build
   work.
2. The referenced symbol resolves at final link, from an archive `common` doesn't
   claim to need.

Add that real edge (`common → core`) to the graph and the SCC swallows almost
the whole project:

> **10 of 11 first-party libraries are in one cycle.** Only `videovulkan`
> stays out.

Bazel's sandbox surfaced this on the first real build attempt, as a hard error.
CMake never mentioned it in 20 years.

### The link line admits it

If the graph were a DAG, a topological link order would exist. It doesn't, so
CMake resolves the cycle by brute force — repeating archives on the link line
until the undefined references run out. From the generated `build.ninja` for
`dolphin-nogui`:

| archive | times on one link line |
|---|---|
| `libcore.a` | **4** |
| `libcommon.a` | 2 |
| `libdiscio.a` | 2 |

That repetition *is* the cycle, made physical. It's invisible in the CMake
sources, which is why it never gets fixed.

### The `traversal_server` tell

The sharpest illustration. `Source/Core/Common/CMakeLists.txt:375`:

```cmake
add_executable(traversal_server TraversalServer.cpp)
target_link_libraries(traversal_server PRIVATE common fmt::fmt)
```

A small utility that links `common` and nothing else. But `libcommon.a` has 22
undefined `Config::` symbols in it, including `FatFsUtil.cpp`'s reference to
`Config::MAIN_WII_SD_CARD_FILESIZE`, which lives in `core`. The link succeeds
only because a plain static archive is **demand-loaded**: the linker never pulls
`FatFsUtil.o` in, because nothing in `TraversalServer.cpp` needs it, so its
dangling reference is never resolved. Confirmed — no `FatFsUtil` symbols in the
shipped binary.

So `traversal_server` links today because of a linker implementation detail. Any
change that causes `FatFsUtil.o` to be pulled in — a new reference from another
`common` file, a switch to `--whole-archive`, LTO, a unity build — breaks it with
undefined references, and the error will point at `traversal_server`, which is
innocent. In Bazel this needed an explicit second, non-`alwayslink` view of
`common` to reproduce the demand-load semantics.

**Recommended fix (upstream, independent of Bazel):** move
`Config::MAIN_WII_SD_CARD_FILESIZE` and `NetPlayProto`'s shared declarations out
of `core` into `common` (or a new leaf `config` library). Two files, and the
bottom of the stack stops depending on the middle.

---

## 2. `PUBLIC` everywhere, so every include path is everyone's include path

Nearly every `target_link_libraries` in Dolphin is `PUBLIC`. In CMake, `PUBLIC`
means "my dependents inherit this, including my include directories." Chained
through the cycle above, the include set of every library becomes the union of
the include sets of all of them.

Measured on the CMake reference model, every `discio` compile action carries **14
include roots**:

```
$SRC/Externals/expr/include              $SRC/Externals/minizip-ng/minizip-ng
$SRC/Externals/glslang                   $SRC/Externals/picojson
$SRC/Externals/glslang/glslang           $SRC/Externals/hidapi/hidapi-src/hidapi
$SRC/Externals/glslang/glslang/Public    $SRC/Externals/libusb/libusb/libusb
$SRC/Externals/glslang/glslang/glslang/..  $SRC/Source/Core
$SRC/Externals/mbedtls/include           $SRC/build/Source/Core
/usr/include                             $SRC/build/include
```

`DiscIO/`'s entire external surface is **two headers**:

```
#include <fmt/format.h>
#include <mbedtls/md5.h>
```

It has zero references to glslang, hidapi, libusb, minizip or picojson — yet
carries include roots for all of them, including *four separate* glslang paths
(one of which doesn't exist; see §3). `dolphin-tool` inherits an almost identical
set. These are pure noise: they slow every compile, and they mean a stray
`#include <glslang/...>` in a disc-image parser would compile clean.

`videocommon` is `PUBLIC core`; `core` is `PUBLIC videonull videoogl
videosoftware`. So a video backend's headers are visible to the disc-image reader.
There is no encapsulation boundary anywhere in the project.

**Recommended fix:** the `PUBLIC`→`PRIVATE` audit is mechanical and can be done
incrementally in CMake alone — flip one target, build, see what breaks. Each
`PRIVATE` that sticks is a real interface narrowed.

---

## 3. An include path pointing at a directory that does not exist

594 compile actions in the reference model carry:

```
-isystem $SRC/Externals/glslang/glslang/Public
```

That directory **does not exist**. The real path is
`Externals/glslang/glslang/glslang/Public` (note the tripled component — glslang's
submodule nests its own name), and it does contain the expected
`ResourceLimits.h` / `ShaderLang.h`. The flag comes from
`Externals/glslang/CMakeLists.txt:16`:

```cmake
target_include_directories(glslang SYSTEM PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/glslang>
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/glslang/Public>   # <-- missing a level
```

Presumably dead since a glslang submodule bump changed the directory layout (I
can't date it — the working clone is shallow).

Distribution across targets, all inherited through `PUBLIC`:

| target | actions carrying the dead include path |
|---|---|
| `core` | 338 |
| `videocommon` | 110 |
| `glslang` | 41 |
| `discio` | 33 |
| `videovulkan` | 18 |
| `videosoftware` | 15 |
| `videoogl` | 13 |
| `uicommon` | 11 |
| `dolphin-tool` | 6 |
| `dolphin-nogui` | 5 |
| `videonull` | 4 |

It's harmless (a nonexistent include path is silently skipped) and it is also a
long-standing inaccuracy in the build description that nobody could see, because
nothing reports unused or unresolvable include paths. Notably, `dolphin-tool` — a
CLI disc utility — carries a broken path to a shader compiler's headers.

**Recommended fix:** add the missing path component. Then consider a CI check
that every include path resolves;
see `docs/FUTURE-include-order-collision-check.md` for the adjacent idea.

---

## 4. Vendored crypto is 4 years EOL, and the build may silently pick either version

`Externals/mbedtls` is **not** a submodule — it's a vendored source copy, pinned
at:

```
MBEDTLS_VERSION_STRING  "2.28.0"
```

mbedtls 2.28 is long past end-of-life. Meanwhile Dolphin's build defaults to
`USE_SYSTEM_LIBS=AUTO`: *"Use system if available, otherwise use bundled."* On
this machine the system mbedtls is **3.6.5**.

So the same source tree, on two developers' machines, compiles against crypto
libraries four years and one major version apart — chosen implicitly by what
happens to be installed. And the code cannot actually take the modern one:

```cpp
// Source/Core/Common/Crypto/SHA1.cpp:43
ASSERT(!mbedtls_sha1_starts_ret(&ctx));
```

The `_ret` suffixed functions were **removed in mbedtls 3.x**. Dolphin's SHA1 is
written exclusively against the 2.x API. During the migration this produced a real
compile error the moment the sandbox resolved headers to the system copy — which
is the useful version of this bug, because the alternative is `AUTO` quietly
finding a system 2.x somewhere and nobody noticing the EOL dependency at all.

This is the general hazard of `AUTO` system-library resolution: the build is not
a function of the source tree. 48 externals, each independently `AUTO`, is a large
matrix of configurations nobody tests.

**Recommended fix:** two separable things. (a) Port `SHA1.cpp` to the mbedtls 3.x
API and bump the vendored copy — it's a handful of call sites. (b) Default
`USE_SYSTEM_LIBS=OFF` for reproducibility, making system libs opt-in for distro
packagers who want them.

### The same bug, a second time: enet (found after this document was first written)

Worth recording because of *how* it was found — a reader tried the migration on
Ubuntu 24.04 and hit:

```
TraversalClient.cpp:186: error: 'ENET_SOCKOPT_TTL' was not declared in this
scope; did you mean 'ENET_SOCKOPT_ERROR'?
```

`ENET_SOCKOPT_TTL` was added in upstream enet **1.3.18**;
`Common/TraversalClient.cpp` uses it at two call sites. Ubuntu 24.04 LTS ships
**1.3.17**. CMake copes because its dependency carries the version floor
explicitly (`CMakeLists.txt:645`):

```cmake
dolphin_find_optional_system_library_pkgconfig(ENET libenet>=1.3.18 \
    enet::enet Externals/enet)
```

— use the system enet *only if* ≥ 1.3.18, else build the bundled submodule. My
Bazel translation reduced that to a bare `-lenet` linkopt, keeping the system
branch and silently discarding both the floor and the fallback. It was green on
my machine for exactly one reason: this box happens to ship 1.3.18.

Two lessons, both generalizable beyond Dolphin:

1. **For the codebase:** `AUTO` resolution doesn't just risk *version skew*
   (§4's mbedtls), it risks *unsatisfiable* system dependencies, and the version
   floors that encode this knowledge are scattered one-per-call through a
   900-line `CMakeLists.txt`. There are **25** `dolphin_find_optional_system_library*`
   calls, **12** carrying an explicit floor. Nothing collects them, and nothing
   tests the below-floor path. Measured on this Ubuntu 26.04 box:

   | dependency | floor | system version | margin |
   |---|---|---|---|
   | `zlib` | 1.3.1 | 1.3.1 | **exactly at the floor** |
   | `libenet` | 1.3.18 | 1.3.18 | **exactly at the floor** ← the bug |
   | `libxxhash` | 0.8.2 | 0.8.3 | thin |
   | `liblz4` | 1.8 | 1.10.0 | fine |
   | `libzstd` | 1.4.0 | 1.5.7 | fine |

   Two dependencies sit *exactly* on their floor. For **CMake** that is
   harmless — below the floor it just builds the bundled copy, which is the
   whole point of the construct. It is only fatal for a *translation* that
   dropped the fallback, which is why enet was reported first and why `zlib` is
   the next one to check.
2. **For a migration tool:** a version-gated dependency is a *conditional* in the
   build description, and the action-graph diff cannot see it. The diff compares
   one configured CMake build against one configured Bazel build — it faithfully
   reproduces the branch the reference host took and is structurally blind to the
   branch it didn't. `dolphin_find_optional_system_library` calls are a
   high-value thing for a migration to enumerate and preserve explicitly, rather
   than resolve once and bake in.

Fixed by adding `//Externals/enet:enet` (the bundled submodule) behind
`--define=bundled_enet=true`, which restores CMake's fallback branch.

---

## 5. AUTOMOC hides a missing entry in the source list

`Source/Core/DolphinQt/CMakeLists.txt` lists 205 headers. There are 168 headers
containing `Q_OBJECT` under `DolphinQt/`. Exactly one is **not** in the target's
source list:

```
Config/Mapping/IOWindow.h
```

(`IOWindow.cpp` is listed at line 162; the header is nowhere.)

CMake's AUTOMOC still processes it, because AUTOMOC scans the `#include`s of
compiled sources — and `IOWindow.cpp`, `MappingWidget.cpp` and
`MappingButton.cpp` all include it. So the moc output exists, the vtable and
`staticMetaObject` get generated, and the omission is completely invisible.

It stops being invisible the moment anything enumerates the target's sources
rather than following `#include` edges: my first Qt build derived the moc list
from `add_executable` and failed to link with undefined
`IOWindow::staticMetaObject` and `vtable for IOWindow`.

This is a small bug with a general moral: **AUTOMOC makes the source list
non-authoritative.** Any tool that trusts it — IDE indexers, coverage,
license scanners, static analysis, another build system — gets a subtly wrong
answer, and the build won't tell you.

**Recommended fix:** add the header. More usefully, a lint that every
`Q_OBJECT`-containing header appears in exactly one target's source list.

---

## 6. `ENABLE_QT` silently changes a library that isn't the GUI

`Source/Core/UICommon/CMakeLists.txt` gates, behind
`if(UNIX AND NOT APPLE AND NOT ANDROID AND ENABLE_QT)`:

- an extra source file, `DBusUtils.cpp`
- an extra define, `HAVE_QTDBUS=1`
- an extra dependency, `Qt6::DBus`

So `uicommon` — a library shared with the *headless* build — has a different
translation-unit set depending on whether the GUI is enabled. `dolphin-nogui`
built in a Qt-enabled tree is not the same binary as `dolphin-nogui` built in a
headless tree. The flag's name promises it only affects the Qt frontend.

Worse, `Qt6::DBus` is attached **`PUBLIC`**, so its interface defines
(`QT_CORE_LIB`, `QT_DBUS_LIB`, `QT_NO_DEBUG`) propagate to everything downstream
of `uicommon` — including `dolphin-tool`, a command-line disc utility that has no
Qt code in it whatsoever.

**Recommended fix:** put the D-Bus support in its own small target that only the
GUI links, so `uicommon` is configuration-independent; and make `Qt6::DBus`
`PRIVATE`.

---

## 7. Smaller things, noted in passing

- **Unreferenced glslang objects.** CMake compiles `ResourceLimits.cpp`,
  `resource_limits_c.cpp` and `stub.cpp`; Dolphin supplies its own resource
  limits (`VideoCommon/Spirv.cpp`), so all three objects are compiled and then
  dropped at link. Wasted build time, and a trap for anyone who assumes the
  glslang default limits are in play.
- **Per-target defines that are already global.** The reference model shows
  `-DNDEBUG` repeated on the *link* line, and a long tail of per-target flags
  duplicating `add_definitions()` from the top level.
- **A vendored external mutates the whole tree's warning flags.**
  `Externals/mbedtls/CMakeLists.txt` does `set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS}
  -Wall -Wextra -Wwrite-strings -Wformat=2 -Wvla -Wlogical-op -Wshadow …")` —
  mutating the *directory-inherited* flag variable rather than setting properties
  on its own targets. It's pulled in at `CMakeLists.txt:701`; `add_subdirectory(Source)`
  is at line 813. So the mutation lands before Dolphin's own code is configured,
  and those flags propagate downward. Verified from the reference model:
  `-Wlogical-op` (an mbedtls-chosen flag) appears on **all 17 targets** —
  `common`, `core`, `discio`, `videocommon`, `dolphin-tool`, `traversal_server`,
  everything. Dolphin's warning configuration is partly decided by a vendored
  crypto library, and moving one `dolphin_find_optional_system_library` call
  across line 813 would silently change the warnings on the entire project.
- **13 flag families had to be declared "ignorable"** for the diff to converge,
  almost all `-Wunused*` / `-Wmaybe-uninitialized` variants that differ per
  target for no discernible reason.
- **Directory-scoped `add_definitions` ordering.** Externals added to the build
  before the app-level `add_definitions()` calls silently miss those defines.
  Whether an external sees `HAVE_*` depends on its position in the
  `add_subdirectory` sequence — action at a distance via file ordering.

---

## What the migration is actually good at

Worth separating, because it shaped which bugs got found:

**The action-graph diff (compile parity) found none of §1.** It compares a
project-wide union of translation units and their flags, which is deliberately
dependency-agnostic and grouping-agnostic. That's what made it usable on a
codebase with a 10-library cycle — you can reach flag-level parity without
touching the layering. But it means "the diff converged" and "the code builds"
are different claims.

**Actually building found §1, §4, §5.** Three classes of bug are only visible
when a real compiler runs in a sandbox that enforces declared dependencies:

1. headers reachable via a global `-I` but not via any declared dependency,
2. version skew between a vendored copy and a system copy,
3. link-order and demand-load accidents.

The lesson for any2bazel: **parity is necessary but not sufficient, and the
migration should always end in a real build.** The diff proves you translated the
build faithfully; only the build proves the result works.

**And building on ONE host wasn't sufficient either.** The enet bug (§4's
addendum) survived a converged diff, a green `//...`, 1350 passing tests and a
running emulator — then failed immediately on someone else's distro. The diff
compares one *configured* build against another, so every host-dependent
conditional in the original build description (`find_library` fallbacks, version
floors, `check_function_exists`) is invisible to it by construction: it sees the
branch taken, never the branch that exists. A migration that ends at "green on my
machine" has verified one point in a configuration space the original build was
explicitly written to span.

**The forcing function is the point.** Every finding here is a place where CMake's
permissiveness — global include paths, non-enforced dependencies, `PUBLIC` by
default, demand-loaded archives, AUTOMOC's implicit source discovery — let an
inconsistency persist invisibly. None of them require Bazel to *fix*; §1's
symbol move, §2's `PRIVATE` audit, §3's dead flag, §4's API port, §5's missing
header and §6's target split are all ordinary CMake changes. The migration's real
output isn't the `BUILD.bazel` files. It's the list.

---

## Appendix: reproducing these findings

```sh
# §1  the cycle, from the declared graph
grep -A20 '^target_link_libraries' Source/Core/*/CMakeLists.txt

# §1  the undeclared common -> core edge
grep -rn '#include "Core/' Source/Core/Common/

# §1  archive repetition on the link line
grep -A3 '^build .*dolphin-nogui' build/build.ninja | tr ' ' '\n' \
  | grep -E 'lib(core|common|discio)\.a' | sort | uniq -c

# §1  traversal_server's dangling references
nm -C --undefined-only build/Source/Core/Common/libcommon.a | grep -c 'Config::'

# §2/§3  inherited and dead include roots, from the extracted model
python3 -c "import json,collections; m=json.load(open('model.cmake.json'));
c=collections.Counter()
for n,t in m['targets'].items():
  for a in t.get('actions',[]):
    if any('glslang/glslang/Public' in str(x) for x in a.get('arguments',[])): c[n]+=1
print(c)"
test -d Externals/glslang/glslang/Public || echo 'dead include path confirmed'

# §2  DiscIO's 14 inherited include roots vs. its 2 actual external headers
grep -rhoE '#include [<"][a-z0-9_-]+/[A-Za-z0-9_/.-]+[>"]' Source/Core/DiscIO/ | sort -u

# §4  the version skew
grep MBEDTLS_VERSION_STRING Externals/mbedtls/include/mbedtls/version.h \
                            /usr/include/mbedtls/build_info.h

# §4  every version-gated system dependency (each one a conditional the
#     action-graph diff cannot see)
grep -n 'dolphin_find_optional_system_library' CMakeLists.txt
grep -n 'ENET_SOCKOPT_TTL' /usr/include/enet/enet.h   # absent => needs bundled enet

# §5  Q_OBJECT headers missing from the source list
grep -rl Q_OBJECT Source/Core/DolphinQt/ | wc -l
grep -rn IOWindow.h Source/Core/DolphinQt/CMakeLists.txt   # empty
```

Full migration record: `BAZEL_MIGRATION.md` (plan),
`BAZEL_MIGRATION_REPORT.md` (per-stage findings),
`STAGE8_QT_DESIGN.md` (Qt codegen), `DOLPHIN_BAZEL_PATCH.md` (the generated
Bazel files) — all in the Dolphin tree as of the migration commit.
