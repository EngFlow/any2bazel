"""-fPIE on a generated cc_binary is a crash, not a nit: the qApp copy relocation.

Case study finding 41. CMake puts -fPIE on every executable target
(CMAKE_POSITION_INDEPENDENT_CODE + an exe), the capture recorded it faithfully,
and the generator copied it into `copts` on each generated cc_binary. Bazel
appends per-target copts AFTER the .bazelrc's --copt=-fPIC, and for GCC the LAST
of -fPIC/-fPIE wins -- so the UI/Qt objects were compiled -fPIE while every
library around them was -fPIC.

Under -fPIE, GCC may reference extern data DIRECTLY (PC-relative) rather than
through the GOT, and the linker then materialises the definition inside the
executable with an R_X86_64_COPY relocation. Against a Qt built with
`reduce_relocations` -- every official/aqt SDK; Debian's is built without it --
that is fatal for QCoreApplication::self:

  * libQt6Core accesses `self` PC-relative (no reloc against it at all): its OWN
    BSS copy is the one QApplication's constructor writes.
  * libQt6Gui reads the same symbol through the GOT (R_X86_64_GLOB_DAT), and the
    copy relocation has repointed that GOT slot at the EXECUTABLE's BSS.

So qApp is set in one place and read in another, which is still null, and the
first signal emitted through it segfaults: QGuiApplication::screenAdded from
QWindowSystemInterface::handleScreenAdded, inside doActivate, on
`mov 0x8(%rdi),%rbx` with rdi = 0. Reported as a SIGSEGV in
QXcbConnection::initializeScreens; reproduced identically under the offscreen QPA
plugin, which is what proved it was not a plugin problem at all.

Qt's own headers diagnose this ("-fPIE is not sufficient ... Compile your code
with -fPIC and without -fPIE") but only when __PIC__ is unset -- and Bazel passes
BOTH flags, so __PIC__ is defined and the #error never fires. The build was clean
and the binary was broken, which is exactly the class of defect a generator test
has to carry.

Measured, before and after, on the six generated executables: 39
R_X86_64_COPY relocations (including QCoreApplication::self) before, 0 after, and
the GUI starts against the aqt 6.9.2 SDK where it previously died for both the
xcb and the offscreen plugin. -Wl,-z,nocopyreloc is NOT an alternative: it turns
the same defect into a link error ("causes overflow in R_X86_64_PC32").
"""

import os
import re

_HERE = os.path.dirname(__file__)
_WS = os.path.join(_HERE, "..", "examples", "ladybird", "workspace")


def _read(rel):
    with open(os.path.join(_WS, rel)) as f:
        return f.read()


def test_no_generated_target_carries_fpie():
    """The generated BUILD file must not put -fPIE in any copts.

    This is the regression that shipped: seven cc_binary targets (the six services
    plus ladybird) each with `copts = ['-fPIE']`.
    """
    build = _read("BUILD.bazel")
    assert "-fPIE" not in build, \
        "-fPIE in the generated BUILD file: qApp gets a copy relocation (finding 41)"


def test_the_emitter_drops_fpie_rather_than_the_checked_in_file_being_edited():
    """The fix has to live in the generator, or the next regeneration undoes it.

    BUILD.bazel is generated output; hand-editing it is how a fix survives exactly
    one commit. The emitter carries an explicit drop list.
    """
    emit = _read("Meta/emit_build_bazel.py")
    assert "DROPPED_TARGET_FLAGS" in emit
    dropped = emit.split("DROPPED_TARGET_FLAGS = ", 1)[1].split("\n", 1)[0]
    assert "-fPIE" in dropped
    # And it must actually be consulted in the flag loop, not merely defined.
    body = emit.split("def target_flags(", 1)[1].split("\ndef ", 1)[0]
    assert "DROPPED_TARGET_FLAGS" in body


def test_the_drop_is_explained_where_it_happens():
    """A bare `if x == "-fPIE": continue` reads like a style preference.

    The next person to see CMake pass -fPIE will put it back unless the comment
    says what breaks. Require the mechanism (copy relocation) and the symptom
    (qApp / QCoreApplication::self) to be named at the drop site.
    """
    emit = _read("Meta/emit_build_bazel.py")
    head = emit.split("DROPPED_TARGET_FLAGS = ", 1)[0]
    note = head[-4000:]
    for token in ("copy relocation", "R_X86_64_COPY", "QCoreApplication::self",
                  "reduce_relocations", "-fPIC"):
        assert token in note, "the -fPIE drop must explain %r" % token


def test_global_fpic_is_still_passed():
    """Dropping -fPIE only works because -fPIC is global.

    If the .bazelrc's --copt=-fPIC ever goes away, the objects become whatever
    the host GCC defaults to (Ubuntu: -fPIE), and the copy relocation returns by
    another road.
    """
    rc = _read("bazelrc.txt")
    assert re.search(r"^build --copt=-fPIC$", rc, re.M), \
        "global --copt=-fPIC is what makes dropping -fPIE correct"


def test_every_generated_executable_is_covered():
    """Not just //:ladybird: the services link Qt-adjacent libraries too.

    The measurement was taken on all six; the guard should be the same set, so a
    new cc_binary that picks up -fPIE cannot slip through by not being ladybird.
    """
    build = _read("BUILD.bazel")
    names = re.findall(r"cc_binary\(\s*\n\s*name = '([^']+)'", build)
    for expected in ("ladybird", "WebContent", "RequestServer", "ImageDecoder",
                     "Compositor", "WebWorker"):
        assert expected in names, "expected a cc_binary for %s" % expected
    # Every one of them, and any future one, must be -fPIE free -- which the
    # whole-file assertion above already covers, so this is the inventory that
    # keeps that assertion meaningful.
    assert len(names) >= 6


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print("ok", t.__name__)
    print("%d passed" % len(TESTS))
