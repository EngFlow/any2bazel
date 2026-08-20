#!/usr/bin/env bash
# Diagnose a Qt runtime crash in the Bazel-built Ladybird, in one paste.
#
# WHY THIS EXISTS. Two unrelated failures present as "immediate crash", and
# LD_LIBRARY_PATH makes both go away -- which is exactly why reaching for it loses
# the information that says which one you had:
#
#   A. THE LIBRARIES. An official/aqt Qt bundles its own ICU (aqt 6.9.2's
#      libQt6Core needs libicui18n.so.73, which exists in the SDK's lib/ and
#      nowhere else on a machine whose distro ICU is 76+). Bazel links @qt's
#      libQt6Core out of a solib dir; libQt6Core resolves ICU through
#      `RUNPATH $ORIGIN`, and $ORIGIN is the directory the loader OPENED it by --
#      the solib dir, which has no ICU. Death before main().
#      Fixed by @qt_plugins//:runtime_libs: the SDK's private libs become real link
#      inputs, so BAZEL stages them and OUR runpath (the one glibc consults for our
#      direct deps) finds them.
#
#   B. THE PLUGINS. Qt dlopens the QPA plugin at QApplication construction from a
#      prefix baked into libQt6Core, or from the executable's directory. Load the
#      DISTRO's libqxcb.so into an SDK libQt6Core and you get SIGSEGV in
#      QXcbConnection::initializeScreens (or, if the plugin is older, a clean
#      "no Qt platform plugin could be initialized" abort).
#      Fixed by //:qt_conf + //:qt_plugins staged beside the binary.
#
# Both fixes are in the tree. This script checks whether each one actually FIRED
# for the Qt you have, because a fix that silently degrades to "no private
# libraries" is indistinguishable from a Qt that needs none (finding 35).
#
# Usage:   ./qt_runtime_diagnose.sh [/path/to/ladybird-tree]
# Reads only; runs nothing that can change the build.

set -uo pipefail
TREE="${1:-$PWD}"
cd "$TREE" || { echo "no such tree: $TREE" >&2; exit 1; }

say() { printf '\n=== %s\n' "$*"; }
BIN="bazel-bin/ladybird"

say "0. the tree"
echo "tree:   $TREE"
echo "commit: $(git rev-parse --short HEAD 2>/dev/null || echo '?')"

say "1. which Qt the BUILD was told to use (MODULE.bazel)"
sed -n 's|^[[:space:]]*paths = {"linux-x86_64": "\([^"]*\)".*|\1|p' MODULE.bazel | head -1

say "2. which Qt @qt actually DISCOVERED (its own generated qtconf.bzl)"
# The output base, asked of bazel rather than guessed.
OB="$(bazel info output_base 2>/dev/null)"
QTCONF="$(find "$OB/external" -maxdepth 3 -name qtconf.bzl 2>/dev/null | head -1)"
if [ -n "$QTCONF" ]; then
    grep -E '^(QT_VERSION|QT_INSTALL_PREFIX|QT_INSTALL_LIBS|QT_INSTALL_PLUGINS)' "$QTCONF"
    LIBS="$(sed -n 's|^QT_INSTALL_LIBS="\(.*\)"|\1|p' "$QTCONF")"
else
    echo "NOT FOUND -- @qt has not been fetched yet (build first)"
    LIBS=""
fi

say "3. did the PRIVATE-LIBRARY staging fire? (failure A)"
PLUG="$(find "$OB/external" -maxdepth 2 -name '*qt_plugins' -type d 2>/dev/null | head -1)"
if [ -z "$PLUG" ]; then
    echo "@qt_plugins not fetched yet"
elif grep -q 'name = "runtime_libs"' "$PLUG/BUILD.bazel" 2>/dev/null; then
    # grep -c prints one count PER FILE; with a glob that is "0\n0", which then
    # fails `[ ... -eq 0 ]` with "integer expected". Count lines instead.
    n="$(grep -h '^cc_import' "$PLUG/BUILD.bazel" 2>/dev/null | wc -l)"
    echo "runtime_libs exists with $n cc_import(s):"
    grep -A2 '^cc_import' "$PLUG/BUILD.bazel" 2>/dev/null | sed 's/^/    /'
    if [ "$n" -eq 0 ]; then
        cat <<'NOTE'
    -> EMPTY. That is CORRECT for a distro Qt (its ICU is a distro package,
       already on the loader's default search path) and WRONG for a
       self-contained SDK. Check section 4: if the SDK's lib dir has libicu*
       in it and this is empty, the derivation missed them -- that is the bug,
       not your machine.
NOTE
    fi
else
    echo "no runtime_libs target at all (an OLD @qt_plugins? bazel sync --configure)"
fi

say "4. what the SDK's lib dir actually ships (the input to that derivation)"
if [ -n "$LIBS" ] && [ -d "$LIBS" ]; then
    echo "$LIBS:"
    ls "$LIBS" | grep -vE '^libQt' | grep '\.so' | sed 's/^/    /' | head -20
    echo "  (non-Qt .so files above; those DT_NEEDED by a libQt6*.so are what must be staged)"
    echo "  ICU the Qt libs ask for:"
    objdump -p "$LIBS"/libQt6Core.so.6 2>/dev/null \
        | awk '/NEEDED/ && /icu/ {print "    " $2}' | sort -u
else
    echo "lib dir unknown or missing: '${LIBS:-}'"
fi

say "5. did the PLUGIN staging fire? (failure B)"
if [ -f bazel-bin/qt.conf ]; then
    sed 's/^/    /' bazel-bin/qt.conf
    echo "  plugins staged: $(find bazel-bin/plugins -name '*.so' 2>/dev/null | wc -l) .so"
    echo "  platform plugin -> $(readlink -f bazel-bin/plugins/platforms/libqxcb.so 2>/dev/null || echo MISSING)"
    echo "  (that path must be under the SAME SDK as section 2's QT_INSTALL_LIBS)"
else
    echo "bazel-bin/qt.conf MISSING -- Qt will scan the compiled-in prefix instead"
fi

say "6. what the BINARY resolves at load time (the actual answer)"
if [ -x "$BIN" ]; then
    if ldd "$BIN" 2>&1 | grep -q "not found"; then
        echo "UNRESOLVED libraries -- this is failure A:"
        ldd "$BIN" 2>&1 | grep "not found" | sed 's/^/    /'
    else
        echo "all libraries resolve. Where the interesting ones come from:"
        ldd "$BIN" 2>/dev/null | grep -iE 'icu|libQt6(Core|Gui|Widgets)' | sed 's/^/    /'
    fi
else
    echo "$BIN not built"
fi

say "7. run it, with NO LD_LIBRARY_PATH, and keep the first failure"
echo "(unset deliberately: with it set, both failures disappear and this says nothing)"
env -u LD_LIBRARY_PATH "$BIN" --version 2>&1 | head -5
rc=$?
echo "--version exit: $rc"

cat <<'EOF'

=== READ IT LIKE THIS
  section 6 shows "not found", or section 7 says "error while loading shared
  libraries"            -> failure A: the private-library staging. Section 3 is
                           empty while section 4 lists libicu*. Send me 3+4.
  section 7 crashes with a BACKTRACE through QXcbConnection / QGuiApplication,
  or says "no Qt platform plugin could be initialized"
                        -> failure B: the plugins. Compare section 5's resolved
                           libqxcb.so path against section 2's QT_INSTALL_LIBS;
                           if they are different SDKs, that is the bug.
  neither, and it still crashes -> not the Qt runtime at all; get the backtrace:
      gdb -q -batch -ex run -ex bt --args bazel-bin/ladybird
EOF
