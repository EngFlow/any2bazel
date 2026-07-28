#!/usr/bin/env python3
"""Ring 1b parity harness: extract every Python-generator CUSTOM_COMMAND from
Build/full/build.ninja, re-run it into a scratch mirror, and byte-diff each
produced file against the CMake-materialized file in Build/full.

Usage: bazel_parity_harness.py [--seed N]

--seed sets PYTHONHASHSEED for every generator. A generator whose output depends
on set/dict iteration order will match the reference under some seeds and not
others, so a single run (which inherits the seed CMake happened to use) can't
prove reproducibility -- sweep several seeds. This is how the
generate_libweb_bindings.py dictionary-ordering bug was found, and re-running
with a few seeds is what keeps it fixed.
"""
import os, re, subprocess, sys, shutil, filecmp

ROOT = "/home/ubuntu/ladybird-work"
FULL = os.path.join(ROOT, "Build/full")
SCRATCH = "/tmp/parity_out"

def extract_commands():
    txt = open(os.path.join(FULL, "build.ninja")).read()
    cmds = re.findall(r'\n  COMMAND = (.*)', txt)
    return [c for c in cmds if 'Meta/Generators/' in c]

def cd_of(cmd):
    m = re.match(r'\s*cd (\S+) &&', cmd)
    return m.group(1) if m else FULL

def outputs_of(cmd):
    return re.findall(r'copy_if_different \S+\.tmp (\S+)', cmd)

def main():
    seed = None
    if "--seed" in sys.argv:
        seed = sys.argv[sys.argv.index("--seed") + 1]
    env = dict(os.environ)
    if seed is not None:
        env["PYTHONHASHSEED"] = seed
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    shutil.copytree(FULL, SCRATCH, symlinks=True,
                    ignore=shutil.ignore_patterns('*.o', '*.a', '*.so', 'CMakeFiles', 'bin', 'lib'))
    cmds = extract_commands()
    total = identical = mismatch = errored = 0
    problems = []
    for cmd in cmds:
        rc = subprocess.run(cmd.replace(FULL, SCRATCH), shell=True, cwd=SCRATCH,
                            capture_output=True, text=True, env=env)
        cwd = cd_of(cmd).replace(FULL, SCRATCH)
        outs = outputs_of(cmd)
        cdreal = cd_of(cmd)
        if rc.returncode != 0:
            errored += 1
            s = re.search(r'Meta/Generators/(\S+\.py)', cmd)
            problems.append(("ERR", s.group(1) if s else "?", rc.stderr[-300:]))
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
    print(f"generator commands: {len(cmds)}  errored: {errored}"
          + (f"  PYTHONHASHSEED={seed}" if seed is not None else "  (inherited seed)"))
    print(f"output files checked: {total}  identical: {identical}  mismatch: {mismatch}")
    for kind, name, detail in problems[:50]:
        print(f"  {kind:8} {name}")
        if detail: print("           ", detail.replace(chr(10),' ')[:200])

if __name__ == "__main__":
    main()
