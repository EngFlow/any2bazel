#!/usr/bin/env python3
"""Emit Bazel genrules for the non-LibWeb Python-generator CUSTOM_COMMANDs.

Companion to emit_codegen_bazel.py (which handles the //Libraries/LibWeb
package). Everything else -- the IPC endpoints under Services/* and
Libraries/LibWebView|LibRequests|LibImageDecoderClient, LibJS's Bytecode/Op,
LibHTTP's HSTS table, the Compositor WebGL replayer -- lives in the ROOT Bazel
package, so all of it lands in one .bzl.

Two things differ from the LibWeb emitter:

  * Output paths in build.ninja are relative to the ninja `cd` directory (a
    subdir of Build/full), not to the package. CMake also picks a `cd` that has
    nothing to do with where the output lands: WebContentClientEndpoint.h is
    generated with `cd Build/full/Libraries/LibWebView` but written to
    Services/WebContent/. We resolve outputs against the cd dir and then rebase
    onto the repo root, which is the actual Bazel package.
  * srcs are repo-root-relative rather than package-relative.

As in the LibWeb emitter, srcs = union(command line, the ninja edge's declared
deps): a generator may read inputs that never appear on its command line.

Usage: emit_root_codegen_bazel.py > codegen_root.bzl
"""
import re, os, sys, shlex

ROOT = os.environ.get("LADYBIRD_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FULL = ROOT + "/Build/full"
LIBWEB_CD = FULL + "/Libraries/LibWeb"


def parse():
    txt = open(FULL + "/build.ninja").read()
    rules = []
    for b in re.split(r'\nbuild ', txt):
        if ': CUSTOM_COMMAND' not in b:
            continue
        m = re.search(r'\n  COMMAND = (.*)', b)
        if not m or 'Meta/Generators/' not in m.group(1):
            continue
        cmd = m.group(1)
        cd = re.match(r'\s*cd (\S+) &&', cmd).group(1)
        if cd == LIBWEB_CD:
            continue                     # owned by //Libraries/LibWeb
        py = next((s for s in cmd.split(' && ')
                   if 'python3' in s and 'Generators/' in s), None)
        if not py:
            continue
        declared = [os.path.normpath(d) for d in
                    b.split(': CUSTOM_COMMAND', 1)[1].split('\n', 1)[0].split()]
        rules.append((py.strip(), cd, declared))
    return rules


# Bazel packages that are NOT the root package: a source inside one must be
# referenced as //pkg:path, not as a root-package-relative path.
SUBPACKAGES = ("Libraries/LibWeb", "Meta")


def _label(rel):
    """Root-relative repo path -> a label the root package can reference."""
    for pkg in SUBPACKAGES:
        if rel == pkg or rel.startswith(pkg + '/'):
            return '//%s:%s' % (pkg, os.path.relpath(rel, pkg))
    return rel


def convert(py, cd, declared=()):
    script = re.search(r'Generators/(\S+\.py)', py).group(1)
    tail = shlex.split(py.split(script, 1)[1])
    outs, srcs, argv = [], [], []
    for t in tail:
        if t.endswith('.tmp'):
            # Resolve against the ninja cd dir, then rebase onto the repo root.
            absout = t[:-4] if t.startswith('/') else os.path.join(cd, t[:-4])
            rel = os.path.relpath(os.path.normpath(absout), FULL)
            outs.append(rel)
            argv.append('$(location %s)' % rel)
        elif t.startswith(ROOT + '/'):
            rel = _label(os.path.relpath(t, ROOT))
            srcs.append(rel)
            argv.append('$(location %s)' % rel)
        else:
            argv.append(shlex.quote(t) if ' ' in t else t)
    for d in (os.path.normpath(x) for x in declared):
        if not d.startswith(ROOT + '/') or d.endswith('.py'):
            continue                     # generators come via //Meta:generators
        rel = os.path.relpath(d, ROOT)
        if rel.startswith('Build/'):
            continue                     # build-dir artifacts, not sources
        rel = _label(rel)
        if rel not in srcs:
            srcs.append(rel)
    base = os.path.splitext(os.path.basename(outs[0]))[0] if outs else script
    name = 'gen_' + re.sub(r'[^A-Za-z0-9]', '_', base)
    return name, dict(script=script, outs=outs, srcs=srcs, args=' '.join(argv))


def emit_header_roots(all_outs):
    """Header-root cc_librarys over the generated headers.

    The generated headers are included as <Services-relative> or
    <Libraries-relative> paths (e.g. <WebContent/WebContentClientEndpoint.h>,
    <LibJS/Bytecode/Op.h>), matching CMake's -IServices -ILibraries. Bazel needs
    the genfiles equivalents as include roots, or consumers silently fall back to
    the CMake copies under Build/full -- which is the bug this whole ring is
    about. `includes` is relative to the package, and Bazel adds both the source
    and genfiles variants of each.
    """
    for root in ("Libraries", "Services"):
        hdrs = sorted(o for o in all_outs
                      if o.endswith('.h') and o.startswith(root + '/'))
        if not hdrs:
            continue
        print('    cc_library(')
        print('        name = %r,' % ('generated_%s_headers' % root.lower()))
        print('        hdrs = %r,' % [':' + h for h in hdrs])
        print('        includes = [%r],' % root)
        print('    )')


def main():
    rules = parse()
    print('# AUTO-GENERATED by Meta/emit_root_codegen_bazel.py — do not edit.')
    # native.cc_library was removed from Bazel; the rule must be loaded.
    print('load("@rules_cc//cc:defs.bzl", "cc_library")')
    print('# %d Python-generator genrules for the root package' % len(rules))
    print('# (byte-parity: Meta/bazel_parity_harness.py).\n')
    print('def root_codegen():')
    seen = set()
    all_outs = []
    for py, cd, declared in rules:
        name, d = convert(py, cd, declared)
        if name in seen:
            continue
        seen.add(name)
        print('    native.genrule(')
        print('        name = %r,' % name)
        print('        srcs = %r + ["//Meta:generators"],' % d['srcs'])
        print('        outs = %r,' % d['outs'])
        print('        cmd = "PYTHONHASHSEED=0 python3 Meta/Generators/%s %s",'
              % (d['script'], d['args']))
        print('    )')
        all_outs.extend(d['outs'])
    emit_header_roots(all_outs)


if __name__ == '__main__':
    main()
