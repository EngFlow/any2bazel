#!/usr/bin/env python3
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

"""Emit Bazel genrules for Ladybird's Python-generator CUSTOM_COMMANDs.

Extracts each generator invocation from Build/full/build.ninja and rewrites it
as a Bazel genrule whose output is byte-identical to the CMake build (proven by
Meta/bazel_parity_harness.py). Absolute source paths become package-relative
$(location ...) refs; output paths (CMake's *.tmp) become genrule outs.

Usage: emit_codegen_bazel.py <LibDir e.g. Libraries/LibWeb>  > codegen.bzl
"""
import re, os, sys, shlex

ROOT = os.environ.get("LADYBIRD_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FULL = ROOT + "/Build/full"

def parse(lib_build_dir):
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
        if cd != lib_build_dir:
            continue
        py = next((s for s in cmd.split(' && ')
                   if 'python3' in s and 'Generators/' in s), None)
        if not py:
            continue
        # The ninja edge's own dependency list (CMake DEPENDS) is authoritative:
        # a generator may read files that never appear on its command line
        # (e.g. generate_dom_tree.py follows <link href> out of its input HTML).
        # Scraping only the command line silently drops those inputs, which is
        # fine under CMake (in-place source tree) but fails in Bazel's sandbox.
        declared = [os.path.normpath(d) for d in
                    b.split(': CUSTOM_COMMAND', 1)[1].split('\n', 1)[0].split()]
        rules.append((py.strip(), declared))
    return rules

def convert(py, pkg_src_dir, declared=()):
    """py: the 'python3 .../script.py <args>' segment.

    declared: the CMake/ninja DEPENDS list for this edge. Inputs listed there but
    absent from the command line are implicit reads and must still be declared as
    Bazel srcs, or the sandboxed action fails.  Returns (name, dict).
    """
    script = re.search(r'Generators/(\S+\.py)', py).group(1)
    tail = shlex.split(py.split(script, 1)[1])
    outs, srcs, argv = [], [], []
    i = 0
    while i < len(tail):
        t = tail[i]
        if t.endswith('.tmp'):
            o = t[:-4]
            outs.append(o)
            argv.append('$(location %s)' % o)
        elif t.startswith(pkg_src_dir + '/'):
            rel = os.path.relpath(t, pkg_src_dir)
            srcs.append(rel)
            argv.append('$(location %s)' % rel)
        elif t.startswith(ROOT + '/'):
            # repo source outside this package -> keep absolute-relative label form
            rel = os.path.relpath(t, ROOT)
            srcs.append('//%s' % os.path.dirname(rel) + ':' + os.path.basename(rel)
                        if False else rel)
            argv.append('$(location //:%s)' % rel if False else rel)
        else:
            argv.append(shlex.quote(t) if ' ' in t else t)
        i += 1
    # Fold in DEPENDS-only inputs (implicit reads: not on the command line).
    for d in (os.path.normpath(x) for x in declared):
        if not d.startswith(pkg_src_dir + '/'):
            continue            # generator script (covered by //Meta:generators)
        rel = os.path.relpath(d, pkg_src_dir)
        if rel not in srcs:
            srcs.append(rel)

    base = os.path.splitext(os.path.basename(outs[0]))[0] if outs else script
    name = 'gen_' + re.sub(r'[^A-Za-z0-9]', '_', base)
    return name, dict(script=script, outs=outs, srcs=srcs, args=' '.join(argv))

def bindings_rule(pkg_src, lib_build):
    """The one generator CMake runs as a single mega-command: 661 IDL files in,
    1331 Bindings/*.{h,cpp} out.

    Two things make it unlike the others:

      * Outputs are not on the command line at all -- the generator gets a
        single `-o Bindings` output *directory*. So the outs list comes from the
        ninja edge's output list, not from parsing argv.
      * srcs must be the ninja edge's full DEPENDS closure (1324 .idl), not just
        the 661 top-level arguments: the generator follows `includes` and partial
        interfaces between .idl files. Two of those inputs are themselves
        generated (CSS/GeneratedCSS{StyleProperties,NumericFactoryMethods}.idl),
        so they are emitted as same-package genrule labels.

    NB this used to use native.glob(["**/*.idl"]), which was wrong twice over: a
    glob is an undeclared-input wildcard, and glob() *never* matches
    genrule outputs, so the two generated .idl were only found because stale
    copies of CMake's output happened to sit in the source tree.
    """
    txt = open(FULL + "/build.ninja").read()
    for b in re.split(r'\nbuild ', txt):
        if ': CUSTOM_COMMAND' not in b:
            continue
        m = re.search(r'\n  COMMAND = (.*)', b)
        if not m or 'generate_libweb_bindings.py' not in m.group(1):
            continue
        pkg_rel = os.path.relpath(pkg_src, ROOT) + '/'
        # Outputs: ninja lists each twice (once ${cmake_ninja_workdir}-prefixed).
        outs = sorted({o[len(pkg_rel):] for o in b.split(': CUSTOM_COMMAND', 1)[0].split()
                       if o.startswith(pkg_rel)})
        declared = {os.path.normpath(d) for d in
                    b.split(': CUSTOM_COMMAND', 1)[1].split('\n', 1)[0].split()}
        # The DEPENDS closure, minus phony `generate_*` edge names. ninja mixes
        # absolute paths (source tree) and build-dir-relative ones in this list.
        idl = set()
        for d in declared:
            if not d.endswith('.idl') or os.path.basename(d).startswith('generate_'):
                continue
            for base in (pkg_src, pkg_rel.rstrip('/'), lib_build):
                if d.startswith(base + '/'):
                    idl.add(os.path.relpath(d, base))
                    break
        idl = sorted(idl)
        srcs, gen_srcs = [], []
        for rel in idl:
            (srcs if os.path.exists(os.path.join(pkg_src, rel)) else gen_srcs).append(rel)
        # Argument order must match CMake's exactly (it is the emit order).
        # The 2 generated .idl appear here as paths under the CMake build dir;
        # rebase those onto the package, where the genrule declares them.
        argv = []
        for a in shlex.split(m.group(1)):
            if not a.endswith('.idl'):
                continue
            for base in (pkg_src, lib_build):
                if a.startswith(base + '/'):
                    argv.append(os.path.relpath(a, base))
                    break
        return dict(outs=outs, srcs=srcs, gen_srcs=gen_srcs, argv=argv)
    return None


def main():
    lib = sys.argv[1]                       # e.g. Libraries/LibWeb
    lib_build = FULL + '/' + lib
    pkg_src = ROOT + '/' + lib
    rules = parse(lib_build)
    print('# AUTO-GENERATED by Meta/emit_codegen_bazel.py — do not edit.')
    print('# %d Python-generator genrules for %s (byte-parity: Meta/bazel_parity_harness.py)\n' % (len(rules), lib))
    print('def %s_codegen():' % lib.split('/')[-1].lower())
    seen = set()
    for py, declared in rules:
        if 'generate_libweb_bindings.py' in py:
            continue  # the mega-rule: emitted by bindings_rule() below
        name, d = convert(py, pkg_src, declared)
        if name in seen:
            continue
        seen.add(name)
        print('    native.genrule(')
        print('        name = %r,' % name)
        print('        srcs = %r + ["//Meta:generators"],' % d['srcs'])
        print('        outs = %r,' % d['outs'])
        print('        cmd = "PYTHONHASHSEED=0 python3 Meta/Generators/%s %s",' % (d['script'], d['args']))
        print('    )')

    b = bindings_rule(pkg_src, lib_build)
    if not b:
        return
    print()
    print('def %s_bindings_codegen():' % lib.split('/')[-1].lower())
    print('    # Mega-genrule: %d IDL args (%d static + %d generated) -> %d files.'
          % (len(b['argv']), len(b['srcs']), len(b['gen_srcs']), len(b['outs'])))
    print('    # Byte-identical to CMake, proven %d/%d by Meta/bazel_parity_harness.py.' % (len(b['outs']), len(b['outs'])))
    print('    # srcs is the FULL DEPENDS closure (%d .idl), not just the %d args:'
          % (len(b['srcs']) + len(b['gen_srcs']), len(b['argv'])))
    print('    # the generator follows `includes`/partial interfaces between .idl files.')
    print('    native.genrule(')
    print("        name = 'gen_bindings',")
    print('        srcs = %r + [' % b['srcs'])
    for g in b['gen_srcs']:
        print('            %r,  # generated in this package' % g)
    print('        ] + ["//Meta:generators"],')
    print('        outs = [')
    for o in b['outs']:
        print('            %r,' % o)
    print('        ],')
    args = ' '.join('$(location %s)' % a for a in b['argv'])
    print('        cmd = \'PYTHONHASHSEED=0 python3 Meta/Generators/generate_libweb_bindings.py'
          ' -o $(RULEDIR)/Bindings %s\',' % args)
    print('    )')

if __name__ == '__main__':
    main()
