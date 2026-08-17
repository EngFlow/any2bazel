#!/usr/bin/env python3
"""Fetch the four vcpkg_from_git archives, WITHOUT running CMake or vcpkg.

WHY THIS EXISTS
---------------
Four of vcpkg's inputs are not distfiles. skia and angle pull sub-dependencies
with `vcpkg_from_git`, which -- unlike every other fetch vcpkg makes -- bypasses
the asset cache entirely (`x-asset-sources` never sees it, so
`Meta/vcpkg_capture_assets.sh` cannot record it and `x-block-origin` does not
govern it). What vcpkg_from_git *does* honour is a pre-placed
`downloads/<PORT>-<REF>.tar.gz`, and that is the hook this uses.

For a long time those four tarballs came from a directory on the author's
machine, staged by a `cp ... 2>/dev/null || true` that could not fail (finding
36). They were in fact a copy of `Build/vcpkg/downloads/`, i.e. of vcpkg's own
cache -- which means the only way to get them was to have run CMake. This script
is the answer to "how do you get them WITHOUT running CMake":

It takes the LIST of archives from the committed pin (VCPKG_GIT_ARCHIVES in
vcpkg_git_archives.bzl), resolves each one's clone URL out of the portfiles, then
for each: `git clone` + `git -c core.autocrlf=false archive <ref>` -- byte-for-byte
what vcpkg_from_git does internally -- and **verifies the result against the pinned
SHA512, failing on any mismatch**. So the pin stops being trusted: the hashes came
from vcpkg, and reproducing them from scratch with git is the proof that git and
vcpkg agree. Needs the vcpkg checkout (`Meta/ladybird.py vcpkg`, ~70s) for the
portfiles, and nothing else.

WHY NOT A REPO RULE: these are `git archive` output, so there is no URL for
`http_file`; a repository_rule could shell out to git, but it would then be an
un-cacheable, single-threaded network fetch at load time, and the same eight
lines of git. Keep it as a prefetch that Bazel *verifies* (vcpkg_build.sh
hard-fails in 4s when the tarballs are absent), rather than machinery that hides
a clone inside analysis.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

LADYBIRD_ROOT = Path(os.environ.get("LADYBIRD_ROOT", Path(__file__).resolve().parent.parent))

# WHICH tarballs are needed is NOT decided here -- it is read from the committed
# pin (VCPKG_GIT_ARCHIVES), because that list came from vcpkg itself. This module
# only answers "given this archive name, what URL do I clone?", by scanning
# portfiles for the literal URL/REF arguments.
#
# That split is the whole correctness argument, and it is the second thing I got
# wrong here. A first version *derived* the list by parsing skia's and angle's
# portfiles: it found 8 for skia (against 4 truly fetched) and missed libyuv
# entirely. Both errors have the same cause -- `declare_external_from_git` only
# DECLARES, and `get_externals(${required_externals})` picks from it under
# feature/platform `if()`s, so the set is decided by CMake evaluation, not by the
# text -- and libyuv is a separate port whose portfile calls vcpkg_from_git
# directly. Statically deciding the SET is therefore unsound; statically
# resolving a NAME to a URL is not.


def all_portfiles(vcpkg_root: Path) -> list[Path]:
    """Overlay first: --overlay-ports shadows the checkout's ports/."""
    overlay = sorted((LADYBIRD_ROOT / "Meta" / "CMake" / "vcpkg" / "overlay-ports").glob("*/portfile.cmake"))
    builtin = sorted((vcpkg_root / "ports").glob("*/portfile.cmake"))
    shadowed = {p.parent.name for p in overlay}
    return overlay + [p for p in builtin if p.parent.name not in shadowed]


# Three syntaxes reach vcpkg_from_git, all with literal arguments:
#   declare_external_from_git(name URL "..." REF "...")   -- skia
#   checkout_in_path("<path>" "<url>" "<ref>")            -- angle
#   vcpkg_from_git(URL <url> REF <ref>)                   -- libyuv, directly
_PATTERNS = (
    re.compile(r'declare_external_from_git\s*\(\s*\w+\s+URL\s+"(?P<url>[^"]+)"\s+REF\s+"(?P<ref>[^"]+)"'),
    re.compile(r'checkout_in_path\s*\(\s*"[^"]+"\s+"(?P<url>[^"]+)"\s+"(?P<ref>[^"]+)"\s*\)'),
    re.compile(r'vcpkg_from_git\s*\((?P<body>[^)]*)\)'),
)
_FROM_GIT_URL = re.compile(r'URL\s+"?(?P<url>[^"\s]+)"?')
_FROM_GIT_REF = re.compile(r'\bREF\s+"?(?P<ref>[^"\s]+)"?')
_SET_RE = re.compile(r'set\s*\(\s*(?P<var>\w+)\s+"?(?P<val>[0-9a-f]{40})"?\s*\)')


def _resolve(value: str, variables: dict) -> str | None:
    """Expand ${VAR} against `set(VAR <sha>)` in the same portfile.

    Only 40-hex values are collected, so this cannot silently expand to a branch
    name: vcpkg_from_git REQUIRES a commit SHA (it rev-parses and compares), and
    a ref that is not one should surface here rather than later.
    """
    m = re.fullmatch(r"\$\{(\w+)\}", value)
    if m:
        return variables.get(m.group(1))
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def refs_in_portfile(path: Path) -> dict:
    """-> {(port, ref): url} for every literal vcpkg_from_git call in one portfile."""
    text = path.read_text()
    port = path.parent.name
    variables = {m.group("var"): m.group("val") for m in _SET_RE.finditer(text)}
    out = {}
    for pat in _PATTERNS:
        for m in pat.finditer(text):
            if "body" in m.groupdict() and m.groupdict().get("body") is not None:
                body = m.group("body")
                mu, mr = _FROM_GIT_URL.search(body), _FROM_GIT_REF.search(body)
                if not (mu and mr):
                    continue
                url, raw_ref = mu.group("url"), mr.group("ref")
            else:
                url, raw_ref = m.group("url"), m.group("ref")
            ref = _resolve(raw_ref, variables)
            if ref:
                # vcpkg names the archive after the PORT, not the dependency:
                # DOWNLOADS/${PORT}-${sanitized_ref}.tar.gz in vcpkg_from_git.cmake.
                out[f"{port}-{ref}.tar.gz"] = url
    return out


def resolve_urls(vcpkg_root: Path, wanted: list) -> dict:
    """Find the clone URL for each WANTED archive name. Every name must resolve."""
    index: dict = {}
    for pf in all_portfiles(vcpkg_root):
        for name, url in refs_in_portfile(pf).items():
            index.setdefault(name, url)
    missing = [n for n in wanted if n not in index]
    if missing:
        raise SystemExit(
            "fetch_vcpkg_git_archives: no vcpkg_from_git call found for:\n  "
            + "\n  ".join(missing)
            + "\n(the pin lists it, but no portfile in the checkout or overlay declares it --\n"
            " the checkout may be at the wrong baseline)"
        )
    return {n: index[n] for n in wanted}


def committed_hashes() -> dict[str, str]:
    bzl = LADYBIRD_ROOT / "vcpkg_git_archives.bzl"
    if not bzl.is_file():
        return {}
    return dict(re.findall(r"'([^']+\.tar\.gz)':\s*'([0-9a-f]{128})'", bzl.read_text()))


def fetch(vcpkg_root: Path, out_dir: Path) -> int:
    """Reproduce each pinned tarball with git clone + git archive, verify, install."""
    pinned = committed_hashes()
    if not pinned:
        raise SystemExit(
            "fetch_vcpkg_git_archives: no pinned archives found in vcpkg_git_archives.bzl\n"
            "  That file IS the list of what to fetch; regenerate the pin first with\n"
            "  Meta/vcpkg_capture_git_archives.sh (vcpkg install --only-downloads)."
        )
    urls = resolve_urls(vcpkg_root, sorted(pinned))
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = []

    for name, want in sorted(pinned.items()):
        ref = name.rsplit("-", 1)[1][: -len(".tar.gz")]
        dest = out_dir / name
        if dest.is_file() and sha512(dest) == want:
            print(f"  ok (cached)  {name}")
            continue
        with tempfile.TemporaryDirectory(prefix="vcpkg-git-archive-") as tmp:
            repo = Path(tmp) / "repo"
            print(f"  cloning      {urls[name]}")
            # A full clone: the pinned ref is usually not the tip, and a shallow
            # clone cannot archive an arbitrary commit -- the same reason vcpkg
            # needs full history for its own registry.
            subprocess.run(["git", "clone", "--quiet", urls[name], str(repo)], check=True)
            tmp_out = Path(tmp) / name
            # `-c core.autocrlf=false` is not optional: it is what
            # vcpkg_from_git.cmake passes, and it changes the bytes of any file
            # git would otherwise translate.
            subprocess.run(
                ["git", "-c", "core.autocrlf=false", "archive", ref, "-o", str(tmp_out)],
                cwd=repo,
                check=True,
            )
            got = sha512(tmp_out)
            if got != want:
                failures.append(f"{name}: sha512 {got[:16]}... != pinned {want[:16]}...")
                print(f"  MISMATCH     {name}")
                continue
            tmp_out.replace(dest)
            print(f"  verified     {name}")

    for f in failures:
        print(f"fetch_vcpkg_git_archives: {f}", file=sys.stderr)
    if failures:
        return 1
    print(f"{len(pinned)}/{len(pinned)} git-sourced externals present and verified in {out_dir}")
    return 0


def sha512(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vcpkg-root", type=Path, default=LADYBIRD_ROOT / "Build" / "vcpkg")
    ap.add_argument(
        "--out",
        type=Path,
        default=LADYBIRD_ROOT / "Meta" / "CMake" / "vcpkg" / "git-archives",
        help="where the tarballs are written (the directory Meta/vcpkg_build.sh stages from)",
    )
    args = ap.parse_args()

    if not args.vcpkg_root.is_dir():
        raise SystemExit(
            f"fetch_vcpkg_git_archives: no vcpkg checkout at {args.vcpkg_root}\n"
            "  Get one with:  python3 Meta/ladybird.py vcpkg   (no CMake configure needed)"
        )
    return fetch(args.vcpkg_root, args.out)


if __name__ == "__main__":
    sys.exit(main())
