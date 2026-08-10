#!/usr/bin/env python3
"""Emit Bazel genrules for the non-LibWeb generator CUSTOM_COMMANDs.

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

BEYOND THE PYTHON GENERATORS. The Python-generator path above keys on the
`Meta/Generators/` substring. Three generator families in this build are real
code generators that the substring misses, so they used to be neither
Bazel-ified nor parity-checked (their outputs were shimmed out of CMake's build
tree via `exports_files`). They differ enough in rule *shape* that widening one
filter cannot express them, so each gets a small explicit section:

  * TIFF (`Libraries/LibGfx/TIFFGenerator.py`) -- a Python generator that lives
    outside Meta/Generators and writes two named files into an -o DIRECTORY
    rather than to named outputs. Handled by the generic python path with a
    widened script search plus directory-output handling.
  * glslang (`glslangValidator -V --vn ... -o <header> <shader>`) -- not Python
    at all; a host binary at an absolute path (like the `python3` the existing
    genrules already invoke absolutely).
  * flapc + generate_interpreter_layout -- two CHAINED tools this build makes
    itself, the second consuming the first's output. In Bazel these want
    `tools = [...]` on the genrule so the tool is built in the same graph and is
    a declared input. `generate_interpreter_layout` is a C++ program
    (cc_binary); `flapc` is a Rust crate built by cargo, which is Ring 2
    territory -- see FLAPC_NOTE below for what is emitted and why.

Usage: emit_root_codegen_bazel.py > codegen_root.bzl
"""
import re, os, sys, shlex

ROOT = os.environ.get("LADYBIRD_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FULL = ROOT + "/Build/full"
LIBWEB_CD = FULL + "/Libraries/LibWeb"
VCPKG_PKG = "//:vcpkg_installed_exec"

# A python generator command is one that runs a .py under Meta/Generators (the
# bulk) or one of the generators that live next to the library they serve.
PY_GENERATOR_RE = re.compile(r'(Meta/Generators/\S+\.py|Libraries/\S+Generator\.py)')


def _blocks():
    txt = open(FULL + "/build.ninja").read()
    for b in re.split(r'\nbuild ', txt):
        if ': CUSTOM_COMMAND' not in b:
            continue
        m = re.search(r'\n  COMMAND = (.*)', b)
        if not m:
            continue
        cmd = m.group(1)
        cdm = re.match(r'\s*cd (\S+) &&', cmd)
        cd = cdm.group(1) if cdm else FULL
        declared = [os.path.normpath(d) for d in
                    b.split(': CUSTOM_COMMAND', 1)[1].split('\n', 1)[0].split()]
        # Ninja edge OUTPUTS are the part before the rule name; everything after
        # a '|' there is an implicit output (CMake repeats each with its
        # ${cmake_ninja_workdir} prefix, so keep only the plain ones).
        head = b.split(': CUSTOM_COMMAND', 1)[0].split('|', 1)[0]
        produced = [os.path.normpath(os.path.join(FULL, o)) for o in head.split()]
        yield cmd, cd, declared, produced


def parse():
    """The Python-generator commands owned by the root package."""
    rules = []
    for cmd, cd, declared, produced in _blocks():
        if not PY_GENERATOR_RE.search(cmd):
            continue
        if cd == LIBWEB_CD and 'Meta/Generators/' in cmd:
            continue                     # owned by //Libraries/LibWeb
        py = next((s for s in cmd.split(' && ')
                   if 'python3' in s and PY_GENERATOR_RE.search(s)), None)
        if not py:
            continue
        rules.append((py.strip(), cd, declared, produced))
    return rules


def parse_glslang():
    """The glslangValidator shader-header commands.

    build.ninja wraps each in CMake's run_quiet.cmake log wrapper, so read the
    tool invocation out of the wrapper's argv rather than assuming position.
    """
    out = []
    for cmd, cd, _declared, _produced in _blocks():
        if 'glslangValidator' not in cmd:
            continue
        toks = shlex.split(cmd.split('&&', 1)[1] if '&&' in cmd else cmd)
        i = next(i for i, t in enumerate(toks) if t.endswith('glslangValidator'))
        argv = toks[i:]
        tool = argv[0]
        vn = argv[argv.index('--vn') + 1]
        header = argv[argv.index('-o') + 1]
        shader = argv[-1]
        out.append(dict(
            tool=tool, vn=vn,
            out=os.path.relpath(os.path.normpath(
                header if header.startswith('/') else os.path.join(cd, header)), FULL),
            src=os.path.relpath(shader, ROOT),
        ))
    return sorted(out, key=lambda d: d['out'])


def parse_flap():
    """The two chained self-built-tool commands: layout.conf, then the .S.

    Returns (layout, flap) dicts, either possibly None if this build has no
    Flap interpreter (non-x86_64/aarch64 hosts do not define FLAP_ARCH).
    """
    layout = flap = None
    for cmd, cd, _declared, _produced in _blocks():
        if re.search(r'bin/generate_interpreter_layout', cmd):
            m = re.search(r'generate_interpreter_layout\s*>\s*(\S+)', cmd)
            conf = m.group(1).strip('"')
            layout = dict(out=os.path.relpath(os.path.normpath(
                conf if conf.startswith('/') else os.path.join(cd, conf)), FULL))
        elif re.search(r'bin/flapc --arch', cmd):
            # NB: the cargo command that BUILDS flapc also mentions bin/flapc
            # (it copies the cargo output there), so match on the invocation's
            # own first flag rather than the tool path alone.
            toks = shlex.split(cmd.split('&&', 1)[1] if '&&' in cmd else cmd)
            i = next(i for i, t in enumerate(toks) if t.endswith('bin/flapc'))
            argv = toks[i + 1:]
            VALUED = ('arch', 'object-format', 'constants', 'bytecode-def',
                      'input', 'output')
            d, extra, k = {}, [], None
            for t in argv:
                if t.startswith('--'):
                    k = t[2:]
                    if k not in VALUED:
                        extra.append(t)   # a valueless flag (--enable-assertions)
                        k = None
                    continue
                if k:
                    d[k] = t
                    k = None
            def _rel_full(p):
                return os.path.relpath(os.path.normpath(
                    p if p.startswith('/') else os.path.join(cd, p)), FULL)
            flap = dict(
                arch=d['arch'], object_format=d['object-format'],
                out=_rel_full(d['output']),
                constants=_rel_full(d['constants']),
                bytecode_def=os.path.relpath(d['bytecode-def'], ROOT),
                dsl=os.path.relpath(d['input'], ROOT),
                extra=extra,
            )
    return layout, flap


# Bazel packages that are NOT the root package: a source inside one must be
# referenced as //pkg:path, not as a root-package-relative path.
SUBPACKAGES = ("Libraries/LibWeb", "Meta")

# Generator inputs that are NOT in the repo: CMake downloads them at configure
# time into the build tree, so a Bazel-only clone has no file at the path the
# ninja command line names. Each maps to the label of a pinned fetch.
#
# There is exactly one, and it is the HSTS preload table:
# hsts_preload.cmake downloads Chromium's `main` -- unversioned -- so the path
# below only exists if someone ran a CMake configure, and its CONTENT depends on
# the day they ran it. hsts_preload.bzl pins a commit + sha256 downstream (the
# upstream unpinned fetch is filed as a bug we cannot fix from here), so the
# genrule takes the pinned file instead of the configure's leftovers. Mapping it
# here rather than post-editing codegen_root.bzl keeps the emitter the single
# source of truth for that file.
DOWNLOADED_INPUTS = {
    'Build/caches/HSTSPreload/transport_security_state_static.json':
        '@hsts_preload_json//file',
}


def _label(rel):
    """Root-relative repo path -> a label the root package can reference."""
    if rel in DOWNLOADED_INPUTS:
        return DOWNLOADED_INPUTS[rel]
    for pkg in SUBPACKAGES:
        if rel == pkg or rel.startswith(pkg + '/'):
            return '//%s:%s' % (pkg, os.path.relpath(rel, pkg))
    return rel


def convert(py, cd, declared=(), produced=()):
    script_path = PY_GENERATOR_RE.search(py).group(1)
    script = script_path.split('Generators/')[-1] if 'Meta/Generators/' in script_path \
        else script_path
    tail = shlex.split(py.split(script_path, 1)[1])
    outs, srcs, argv = [], [], []
    dir_out = None
    for t in tail:
        if t.endswith('.tmp'):
            # Resolve against the ninja cd dir, then rebase onto the repo root.
            absout = t[:-4] if t.startswith('/') else os.path.join(cd, t[:-4])
            rel = os.path.relpath(os.path.normpath(absout), FULL)
            outs.append(rel)
            argv.append('$(location %s)' % rel)
        elif t.startswith(ROOT + '/') and not t.startswith(FULL):
            rel = _label(os.path.relpath(t, ROOT))
            srcs.append(rel)
            argv.append('$(location %s)' % rel)
        elif t.startswith(FULL):
            # An output DIRECTORY under the build tree (TIFFGenerator.py -o).
            # The generator writes its own filenames into it, so the outs come
            # from the CMake edge and the argv gets a directory, not a file.
            dir_out = os.path.relpath(os.path.normpath(t), FULL)
            argv.append('@@DIROUT@@')
        else:
            argv.append(shlex.quote(t) if ' ' in t else t)
    if dir_out is not None:
        # A generator that writes a set of NAMED files into an -o DIRECTORY
        # rather than taking one output path per file (TIFFGenerator.py writes
        # TIFFMetadata.h + TIFFTagHandler.cpp into its -o dir). Bazel genrules
        # must declare every out, so the file list cannot be read off the
        # command line -- take it from the ninja edge's OUTPUTS.
        outs = sorted(os.path.relpath(p, FULL) for p in produced
                      if p.startswith(FULL + '/'))
        # $(RULEDIR) is the genrule's output dir; the outs are declared relative
        # to the package, so pass the dirname of the first out under it.
        argv = [a if a != '@@DIROUT@@'
                else '$(RULEDIR)/%s' % os.path.dirname(outs[0]) for a in argv]
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
    return name, dict(script=script, outs=outs, srcs=srcs, args=' '.join(argv),
                      mkdir=(os.path.dirname(outs[0]) if dir_out is not None else None))


def emit_header_roots(all_outs):
    """Header-root cc_librarys over the generated headers.

    The generated headers are included as <Services-relative> or
    <Libraries-relative> paths (e.g. <WebContent/WebContentClientEndpoint.h>,
    <LibJS/Bytecode/Op.h>, <LibGfx/ImageFormats/TIFFMetadata.h>), matching
    CMake's -IServices -ILibraries. Bazel needs the genfiles equivalents as
    include roots, or consumers silently fall back to the CMake copies under
    Build/full -- which is the bug this whole ring is about. `includes` is
    relative to the package, and Bazel adds both the source and genfiles
    variants of each.
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


def emit_glslang(shaders):
    """genrule per SPIR-V shader header + one bare-name include root.

    glslangValidator is a HOST tool invoked by absolute path, exactly as the
    Python genrules above invoke /usr/bin/python3: it is not built from source
    here (building glslang would be its own dependency ring). The two headers
    are included by BARE name (<WebContentViewLinuxFragShader.h>), so the
    include root is the directory that holds them, not a source root.
    """
    if not shaders:
        return []
    print('    # glslangValidator (host tool, absolute path -- same escape as')
    print('    # python3 above) compiles the Vulkan shaders to SPIR-V and emits')
    print('    # them as C arrays named by --vn.')
    outs = []
    for s in shaders:
        name = 'gen_' + re.sub(r'[^A-Za-z0-9]', '_',
                               os.path.splitext(os.path.basename(s['out']))[0])
        print('    native.genrule(')
        print('        name = %r,' % name)
        print('        srcs = [%r],' % s['src'])
        print('        outs = [%r],' % s['out'])
        print('        cmd = "%s -V --vn %s -o $(location %s) $(location %s)",'
              % (s['tool'], s['vn'], s['out'], s['src']))
        print('    )')
        outs.append(s['out'])
    print('    cc_library(')
    print('        name = %r,' % 'generated_shader_headers')
    print('        hdrs = %r,' % [':' + o for o in outs])
    # The headers are included by bare name, so the include root is their dir.
    print('        includes = [%r],' % os.path.dirname(outs[0]))
    print('    )')
    return outs


# ---------------------------------------------------------------------------
# FLAPC_NOTE
#
# The interpreter .S needs two tools the build makes itself:
#
#   generate_interpreter_layout  -- C++ (Interpreter/GenerateLayout.cpp, links
#       AK, compiled with -Dprivate=public -Dprotected=public so offsetof() can
#       see private members). A plain cc_binary; Bazel builds it host-side and
#       the genrule runs it via tools=[].
#   flapc -- a RUST crate (Libraries/LibJS/Flap, its own cargo workspace, 51 .rs
#       files, depends on the in-tree bytecode_def crate and on smallvec from
#       crates.io). Bazel BUILDS it now (//:flapc, a cargo_binary): its lock has
#       exactly 3 packages and the one registry crate is the same smallvec 1.15.1
#       the big workspace already fetches -- same version, same checksum, checked
#       by the emitter -- so the Rust ring covers it with no extra machinery.
#       Nothing in this chain comes out of CMake's tree any more.
# ---------------------------------------------------------------------------
def emit_flap(layout, flap):
    if not (layout and flap):
        return []
    print('    # Two chained self-built tools. generate_interpreter_layout is')
    print('    # built by Bazel (//:generate_interpreter_layout) and emits the')
    print('    # struct offsets flapc needs; flapc then compiles the Flap DSL to')
    print('    # the interpreter assembly. tools=[] makes each a declared,')
    print('    # hashed input, so the .S is Bazel output rather than a shim over')
    print('    # CMake\'s build tree. flapc is Bazel-built too now (//:flapc,')
    print('    # a cargo_binary from its own 3-package lock), so neither tool')
    print('    # nor output in this chain comes from the reference build.')
    print('    native.genrule(')
    print('        name = %r,' % 'gen_interpreter_layout')
    print('        outs = [%r],' % layout['out'])
    print('        tools = ["//:generate_interpreter_layout"],')
    # The tool links the vcpkg .so shims and is RUN during the build, so its
    # runtime libs are real inputs of this action. srcs (not tools) because they
    # are data for the exec-config binary, staged at their execroot paths so the
    # relative rpath in .bazelrc resolves inside the sandbox.
    print('        srcs = [%r],' % VCPKG_PKG)
    print('        cmd = "$(location //:generate_interpreter_layout) > $@",')
    print('    )')
    extra = (' ' + ' '.join(flap['extra'])) if flap['extra'] else ''
    print('    native.genrule(')
    print('        name = %r,' % 'gen_interpreter_asm')
    print('        srcs = [%r, %r, %r],'
          % (flap['dsl'], flap['bytecode_def'], ':' + layout['out']))
    print('        outs = [%r],' % flap['out'])
    print('        tools = [%r],' % FLAPC_TOOL)
    print('        cmd = "$(location %s) --arch %s --object-format %s '
          '--constants $(location %s) --bytecode-def $(location %s) '
          '--input $(location %s) --output $@%s",'
          % (FLAPC_TOOL, flap['arch'], flap['object_format'],
             ':' + layout['out'], flap['bytecode_def'], flap['dsl'], extra))
    print('    )')
    return [flap['out']]


# flapc, BUILT BY BAZEL (//:flapc -- a cargo_binary, see cargo.bzl and
# cargo_ring.bzl). It used to be the reference cargo build's binary, the last
# artifact this migration took from CMake, because Libraries/LibJS/Flap is a Rust
# crate and Rust was a separate ring. That ring has landed, so both tools in this
# chain and both of their outputs are now Bazel's own.
FLAPC_TOOL = '//:flapc'


def main():
    rules = parse()
    shaders = parse_glslang()
    layout, flap = parse_flap()
    print('# AUTO-GENERATED by Meta/emit_root_codegen_bazel.py — do not edit.')
    # native.cc_library was removed from Bazel; the rule must be loaded.
    print('load("@rules_cc//cc:defs.bzl", "cc_library")')
    print('# %d Python-generator genrules for the root package,' % len(rules))
    print('# %d glslang shader headers, %d self-built-tool genrules'
          % (len(shaders), 2 if (layout and flap) else 0))
    print('# (byte-parity: Meta/bazel_parity_harness.py).\n')
    print('def root_codegen():')
    seen = set()
    all_outs = []
    for py, cd, declared, produced in rules:
        name, d = convert(py, cd, declared, produced)
        if name in seen:
            continue
        seen.add(name)
        script_ref = ('Meta/Generators/%s' % d['script']) if not d['script'].startswith('Libraries/') \
            else d['script']
        mk = ('mkdir -p $(RULEDIR)/%s && ' % d['mkdir']) if d['mkdir'] else ''
        srcs = list(d['srcs'])
        if d['script'].startswith('Libraries/'):
            # A generator that lives outside Meta/ is not in //Meta:generators;
            # it must be a declared src of its own genrule.
            srcs.append(d['script'])
        print('    native.genrule(')
        print('        name = %r,' % name)
        print('        srcs = %r + ["//Meta:generators"],' % srcs)
        print('        outs = %r,' % d['outs'])
        print('        cmd = "%sPYTHONHASHSEED=0 python3 %s %s",'
              % (mk, script_ref, d['args']))
        print('    )')
        all_outs.extend(d['outs'])
    all_outs.extend(emit_flap(layout, flap))
    emit_header_roots(all_outs)
    emit_glslang(shaders)


if __name__ == '__main__':
    main()
