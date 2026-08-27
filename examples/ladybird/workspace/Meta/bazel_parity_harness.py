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

"""Ring 1b parity harness: enumerate EVERY CUSTOM_COMMAND in
Build/full/build.ninja, re-run the generator ones into a scratch mirror, and
byte-diff each produced file against the CMake-materialized file in Build/full.

Usage: bazel_parity_harness.py [--seed N] [--list-bucket covered|excluded|unhandled]

Every ninja CUSTOM_COMMAND lands in exactly one of three buckets:

  covered    -- a code generator we re-run and byte-compare (the parity check).
  excluded   -- deliberately out of scope, each with a stated reason below.
  UNHANDLED  -- neither. A non-zero UNHANDLED count is a FAILURE (exit 1).

Why the accounting exists: the harness used to select commands with the single
substring test `'Meta/Generators/' in cmd` -- the *same* test the emitter
(Meta/emit_root_codegen_bazel.py) used to decide what to Bazel-ify. One filter
applied twice means the checker cannot catch what the emitter drops, so four
real generators (TIFFGenerator.py, two glslangValidator shader headers, and the
chained generate_interpreter_layout -> flapc pair) were neither Bazel-ified nor
compared, and the run still reported "1,402/1,402 identical". The denominator
was the emitter's assumption rather than the build's actual generator set.
Enumerating all commands and forcing each into a named bucket makes a generator
nobody Bazel-ified show up as a visible number instead of an absence.

--seed sets PYTHONHASHSEED for every generator. A generator whose output depends
on set/dict iteration order will match the reference under some seeds and not
others, so a single run (which inherits the seed CMake happened to use) can't
prove reproducibility -- sweep several seeds. This is how the
generate_libweb_bindings.py dictionary-ordering bug was found, and re-running
with a few seeds is what keeps it fixed.
"""
import os, re, subprocess, sys, shutil, filecmp

ROOT = os.environ.get("LADYBIRD_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FULL = os.path.join(ROOT, "Build/full")
SCRATCH = os.environ.get("PARITY_SCRATCH", os.path.join(ROOT, "Build/parity_out"))

# ---------------------------------------------------------------------------
# Bucket (a): generators we re-run and byte-compare.
#
# Each entry is (substring or regex, label). A command matching any of these is
# a generator this harness owns. The list is deliberately a list of *tools*, not
# one catch-all substring, so adding a generator to the build without adding it
# here shows up as UNHANDLED rather than as silence.
# ---------------------------------------------------------------------------
COVERED = [
    (r'Meta/Generators/',              'python generator (Meta/Generators)'),
    (r'Libraries/LibGfx/TIFFGenerator\.py', 'TIFFGenerator.py (LibGfx)'),
    (r'glslangValidator',              'glslangValidator (SPIR-V shader header)'),
    # Anchored on the tool being INVOKED (start of command, or after `&&`), not
    # merely named: the cargo command that BUILDS flapc also mentions bin/flapc,
    # as the destination of a copy_if_different. An unanchored 'bin/flapc' put
    # that cargo build in this bucket, where it ran (a full cargo rebuild) and
    # then reported NO_OUTS -- a generator-shaped false positive. Compiling flapc
    # is a Rust build, which the EXCLUDED list below owns.
    (r'(?:^|&& )\S*bin/generate_interpreter_layout\b',
                                       'generate_interpreter_layout (self-built)'),
    (r'(?:^|&& )\S*bin/flapc\b',       'flapc (self-built)'),
]

# ---------------------------------------------------------------------------
# Bucket (b): deliberately excluded, with the reason. Ordered: the first match
# wins, and COVERED is tested before this list.
# ---------------------------------------------------------------------------
EXCLUDED = [
    (r'\bcpack\b',            'packaging (cpack), not a build input'),
    (r'\bctest\b',            'test driver (ctest), not a build input'),
    (r'check_style|lint|clang-format|/lint-',
                              'lint/style check, produces no build input'),
    (r'cmake -E echo|cmake -E true|cmake -E touch',
                              'no-op/echo bookkeeping step'),
    (r'copy_if_different',    'resource staging, tracked as the runfiles gap'),
    (r'--interactive-dialog|ccmake|cmake-gui',
                              'interactive-dialog stub, never runs in a build'),
    (r'\bcargo\b|rustc',      'Rust crate build -- a separate future ring'),
    (r'cmake -E remove|cmake -E rm ',
                              'temp-file cleanup for another command'),
    (r'cmake -E make_directory|cmake -E copy_directory',
                              'directory staging, tracked as the runfiles gap'),
    (r'-P .*run_quiet\.cmake.*(?!glslang)',
                              'log wrapper around a command counted separately'),
    # CMake's own per-directory bookkeeping. Ninja emits these four rules in
    # EVERY one of the ~80 build directories, which is why they dominate the
    # count: 4 x directories, none of them producing a build input.
    (r'cmake --regenerate-during-build',
                              'CMake re-running itself when CMakeLists change; '
                              'Bazel loads BUILD files every invocation'),
    (r'-P cmake_install\.cmake',
                              'install/strip step (cmake --install), not a build '
                              'input; Bazel packaging is a separate concern'),
    (r'cmake -E env \S*LADYBIRD_SOURCE_DIR=\S+ (\S*bin/Ladybird|gdb)\b',
                              'ninja "run"/"debug" convenience target, not a '
                              'build step'),
    (r'cmake -E copy \S+\.py \S+',
                              'staging a test script into bin/ as an executable; '
                              'test scaffolding, and tracked as the runfiles gap'),
    (r'cmake -E cmake_autorcc\b',
                              'Qt AUTORCC. Bazel runs rcc ITSELF (rules_qt '
                              'qt_cc_rcc, see //:qt_rcc), so the resource .cpp is '
                              'Bazel output; it is not byte-compared here because '
                              'CMake and rules_qt name the generated symbols after '
                              'their own respective output paths'),
]


def classify(cmd):
    """-> ('covered'|'excluded'|'unhandled', label/reason)."""
    for pat, label in COVERED:
        if re.search(pat, cmd):
            return 'covered', label
    for pat, reason in EXCLUDED:
        if re.search(pat, cmd):
            return 'excluded', reason
    return 'unhandled', ''


def extract_commands():
    """Every CUSTOM_COMMAND in build.ninja, de-duplicated, order preserved."""
    txt = open(os.path.join(FULL, "build.ninja")).read()
    cmds, seen = [], set()
    for b in re.split(r'\nbuild ', txt):
        if ': CUSTOM_COMMAND' not in b:
            continue
        m = re.search(r'\n  COMMAND = (.*)', b)
        if not m:
            continue
        c = m.group(1)
        if c in seen:
            continue
        seen.add(c)
        cmds.append(c)
    return cmds


def cd_of(cmd):
    m = re.match(r'\s*cd (\S+) &&', cmd)
    return m.group(1) if m else FULL


def outputs_of(cmd):
    """Paths this generator command produces, relative to its cwd (or absolute).

    Most generators emit via CMake's `copy_if_different <tmp> <dest>`, so the
    destinations are readable straight off the command line. A few write a whole
    output *directory* instead (`generate_libweb_bindings.py -o Bindings`, which
    alone emits ~1,300 files; TIFFGenerator.py -o writes two named files into
    one). Those were silently unchecked -- the regex found no outputs, so the
    command contributed 0 comparisons and the run still said "identical" --
    which is exactly how a nondeterminism bug in that generator could hide from
    this harness. Enumerate such directories instead.

    Two non-Python generators name their output with -o/--output directly
    (glslangValidator -o <header>, flapc --output <asm>) and one writes to
    stdout via a shell redirect (generate_interpreter_layout > layout.conf).
    """
    outs = re.findall(r'copy_if_different \S+\.tmp (\S+)', cmd)
    if outs:
        return outs
    # flapc: --output <file>. generate_interpreter_layout: > <file>.
    outs += re.findall(r'--output (\S+)', cmd)
    outs += re.findall(r'>\s*(\S+\.conf)', cmd)
    if outs:
        return outs
    for out in re.findall(r' -o (\S+)', cmd):
        base = os.path.join(cd_of(cmd), out)
        if os.path.isfile(base):
            outs.append(out)          # glslangValidator -o <header>
            continue
        if not os.path.isdir(base):
            continue
        for dirpath, _, files in os.walk(base):
            for f in files:
                # .d depfiles embed absolute paths, so they name the scratch
                # mirror rather than Build/full and can never compare equal;
                # they are build plumbing, not generated source. Skip them.
                if f.endswith(('.h', '.cpp')):
                    outs.append(os.path.join(dirpath, f))
    return outs


def label_of(cmd):
    """Short human name for a command, for problem reporting."""
    for pat in (r'Meta/Generators/(\S+\.py)', r'(TIFFGenerator\.py)',
                r'(glslangValidator)', r'bin/(generate_interpreter_layout)',
                r'bin/(flapc)'):
        m = re.search(pat, cmd)
        if m:
            return m.group(1)
    return cmd[:60]


def main():
    seed = None
    if "--seed" in sys.argv:
        seed = sys.argv[sys.argv.index("--seed") + 1]
    env = dict(os.environ)
    if seed is not None:
        env["PYTHONHASHSEED"] = seed

    all_cmds = extract_commands()
    buckets = {'covered': [], 'excluded': [], 'unhandled': []}
    reasons = {}
    for c in all_cmds:
        kind, why = classify(c)
        buckets[kind].append(c)
        if kind == 'excluded':
            reasons.setdefault(why, 0)
            reasons[why] += 1

    if "--list-bucket" in sys.argv:
        which = sys.argv[sys.argv.index("--list-bucket") + 1]
        for c in buckets[which]:
            print(c)
        return 0

    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    shutil.copytree(FULL, SCRATCH, symlinks=True,
                    ignore=shutil.ignore_patterns('*.o', '*.a', '*.so', 'CMakeFiles', 'lib'))
    # `bin/` is normally pruned (it is build output, not a generator input), but
    # the two self-built tools LIVE there: flapc and generate_interpreter_layout
    # are compiled by this build and then run as generators. Keep them.
    for tool in ("flapc", "generate_interpreter_layout"):
        src = os.path.join(FULL, "bin", tool)
        if os.path.exists(src):
            dst = os.path.join(SCRATCH, "bin", tool)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

    total = identical = mismatch = errored = 0
    problems = []
    for cmd in buckets['covered']:
        rc = subprocess.run(cmd.replace(FULL, SCRATCH), shell=True, cwd=SCRATCH,
                            capture_output=True, text=True, env=env)
        cwd = cd_of(cmd).replace(FULL, SCRATCH)
        outs = outputs_of(cmd)
        cdreal = cd_of(cmd)
        if rc.returncode != 0:
            errored += 1
            problems.append(("ERR", label_of(cmd), rc.stderr[-300:]))
            continue
        if not outs:
            problems.append(("NO_OUTS", label_of(cmd),
                             "command produced no comparable output path"))
            continue
        for o in outs:
            total += 1
            sf = o.replace(FULL, SCRATCH) if o.startswith('/') else os.path.normpath(os.path.join(cwd, o))
            rf = o if o.startswith('/') else os.path.normpath(os.path.join(cdreal, o))
            if not os.path.exists(sf):
                mismatch += 1; problems.append(("MISSING", o, "")); continue
            if not os.path.exists(rf):
                problems.append(("NO_REF", o, "")); continue
            if filecmp.cmp(sf, rf, shallow=False):
                identical += 1
            else:
                mismatch += 1; problems.append(("DIFF", o, ""))

    n_cov, n_exc, n_unh = (len(buckets[k]) for k in ('covered', 'excluded', 'unhandled'))
    print(f"ninja CUSTOM_COMMANDs: {len(all_cmds)}")
    print(f"  covered (re-run + byte-compared): {n_cov}")
    print(f"  excluded (deliberate, see reasons): {n_exc}")
    print(f"  UNHANDLED: {n_unh}")
    for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"      excluded x{n:<4} {why}")
    print(f"generator commands run: {n_cov}  errored: {errored}"
          + (f"  PYTHONHASHSEED={seed}" if seed is not None else "  (inherited seed)"))
    print(f"output files checked: {total}  identical: {identical}  mismatch: {mismatch}")
    for kind, name, detail in problems[:50]:
        print(f"  {kind:8} {name}")
        if detail: print("           ", detail.replace(chr(10), ' ')[:200])
    if n_unh:
        print(f"\nFAILURE: {n_unh} CUSTOM_COMMAND(s) are neither covered nor "
              f"deliberately excluded. Run with --list-bucket unhandled to see "
              f"them; then either Bazel-ify + cover them, or add them to "
              f"EXCLUDED with a reason.")
        for c in buckets['unhandled'][:20]:
            print(f"  UNHANDLED {c[:220]}")
        return 1
    if mismatch or errored:
        print(f"\nFAILURE: {mismatch} mismatching file(s), {errored} errored command(s).")
        return 1
    print("\nOK: every CUSTOM_COMMAND accounted for; every generated file "
          "byte-identical to CMake's.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
