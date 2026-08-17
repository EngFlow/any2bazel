#!/usr/bin/env python3
"""Emit Bazel rules that make Bazel own fetching every vcpkg distfile.

Ring 2, part 1. See docs/CASE-ladybird-migration.md findings 23/24 for why this
shape and not the BCR: vcpkg stays the *recipe* (it encodes the patches,
configure flags and feature sets Ladybird pins), while Bazel takes over
fetching, hashing and sandboxing.

What it emits (two files, from one pass):

  vcpkg_distfiles.bzl   one http_file per upstream tarball, hash lifted from the
                        portfile, hex SHA512 -> Bazel SRI `integrity`.
  vcpkg_index.bzl       the sha512 -> label index the asset-cache script reads,
                        so vcpkg can resolve a download *by hash* from files
                        Bazel already fetched.

Three things here are load-bearing and were each found by testing, not reasoning
(finding 23):

  * Hashes must come from the **baseline-resolved** portfile, not from `ports/`
    tip. For 8 of Ladybird's 47 pins the pin is OLDER than the tip of `ports/`
    (`ports/zlib` describes 1.3.2 while Ladybird wants 1.3.1), so reading the tip
    yields a hash for the wrong version and the fetch fails a checksum. We
    resolve version -> `git-tree` through the `versions/` DB and `git cat-file`
    the historical portfile out of the vcpkg checkout.
  * The manifest must be used **verbatim**. A hand-written subset loses feature
    selections on transitive deps (`libpng[apng]` alone is 20 exported symbols),
    so this script never reconstructs a dependency list -- it only reads.
  * Three ports resolve no `versions/` entry, and they are exactly Ladybird's
    **overlay-ports**, whose portfiles live in the Ladybird tree. Those are read
    from the overlay directory instead.

**Static parsing gets 54 of 81 distfiles and then hits a wall.** Portfiles are
CMake *programs*, not manifests: curl derives `${curl_version}` from the version,
angle carries its own `${ANGLE_COMMIT}`, libpsl computes `${short_hash}`,
vcpkg-tool-gn assembles `${download_urls}` per platform. Expanding those needs a
CMake interpreter -- that is, it needs to be vcpkg. So the authoritative input is
a *capture* (Meta/vcpkg_capture_assets.sh) that records the fully-expanded
{url} {sha512} {filename} for every download by acting as vcpkg's asset cache,
the same instrument-don't-predict tactic as scripts/npm_instrument. Pass the
capture with --assets and this emitter is exact; without it, it falls back to
static parsing and REPORTS the shortfall rather than pretending to be complete.

Usage:
  emit_vcpkg_bazel.py --assets assets.tsv --distfiles > vcpkg_distfiles.bzl
  emit_vcpkg_bazel.py --assets assets.tsv --index     > vcpkg_index.bzl
  emit_vcpkg_bazel.py --report    # what resolved, what did not, what is unexpanded
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.environ.get(
    "LADYBIRD_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VCPKG = os.environ.get("VCPKG_ROOT", os.path.join(ROOT, "Build/vcpkg"))
OVERLAY = os.path.join(ROOT, "Meta/CMake/vcpkg/overlay-ports")


class VcpkgUnavailable(Exception):
    """No usable vcpkg checkout, so the static portfile parse cannot run."""


def git(*args):
    """Run git in the vcpkg checkout; '' on failure (missing object, etc.)."""
    try:
        return subprocess.run(("git", "-C", VCPKG) + args, capture_output=True,
                              text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return ""


def manifest():
    """Ladybird's vcpkg.json, read (never reconstructed)."""
    with open(os.path.join(ROOT, "vcpkg.json")) as f:
        return json.load(f)


def overrides():
    """port -> pinned version string, from the manifest's `overrides` list."""
    out = {}
    for o in manifest().get("overrides", []):
        v = o["version"]
        if "port-version" in o:
            v = "%s#%d" % (v, o["port-version"])
        out[o["name"]] = v
    return out


def baseline_versions():
    """port -> version, from the pinned vcpkg baseline (versions/baseline.json).

    Needed because the manifest's 45 `overrides` cover only the ports Ladybird
    names directly. The dependency *closure* is much larger (77 ports install,
    81 distfiles download): zstd, libtiff, openh264, opus, theora, ogg, vorbis,
    libvpx, libyuv, lcms, ngtcp2/nghttp3, xz, libidn2, libunistring, icu, and
    vcpkg's own provisioned tooling (cmake, ninja, meson, gn, patchelf, pkgconf,
    gperf, automake) all arrive transitively. Emitting only the overridden ports
    yielded 39 of 81 distfiles -- a measurement that looked plausible and was
    less than half the truth, caught only by diffing against what vcpkg really
    downloaded. Transitive deps take the baseline version unless overridden.
    """
    with open(os.path.join(VCPKG, "versions/baseline.json")) as f:
        base = json.load(f)["default"]
    out = {}
    for port, e in base.items():
        v = e["baseline"]
        if e.get("port-version"):
            v = "%s#%d" % (v, e["port-version"])
        out[port] = v
    return out


def closure_ports():
    """Every port in the resolved dependency closure, with its pinned version.

    The closure comes from `vcpkg depend-info`, i.e. vcpkg's OWN resolver run
    against Ladybird's manifest and overlay-ports -- deliberately not a
    re-implementation. Re-deriving the graph is how feature selections on
    transitive deps get lost (`libpng[apng]`, finding 23), and the resolver is
    the one component whose answer must match vcpkg exactly.
    """
    exe = os.path.join(VCPKG, "vcpkg")
    try:
        out = subprocess.run(
            [exe, "depend-info", "--format=list",
             "--x-manifest-root=" + ROOT, "--overlay-ports=" + OVERLAY],
            capture_output=True, text=True, check=True, cwd=ROOT)
        # depend-info prints the graph on STDERR, not stdout (stdout stays
        # empty). Read both so this does not silently resolve to nothing.
        out = (out.stdout or "") + (out.stderr or "")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        # Raise rather than sys.exit: whether this is fatal is the CALLER's call.
        # Without a capture it is fatal, because falling back to the manifest's
        # overrides alone silently UNDERCOUNTS the closure (39 of 81 distfiles),
        # which is worse than failing (finding 28). With a capture it is not fatal
        # at all -- the static parse is only a cross-check there.
        raise VcpkgUnavailable(detail.strip())
    ports = set()
    for line in out.splitlines():
        name, _, deps = line.partition(":")
        # "curl[brotli, ssl]" -> "curl"; features are the resolver's business.
        for tok in [name] + [d.strip() for d in deps.split(",")]:
            tok = tok.strip()
            if tok:
                ports.add(re.sub(r'\[.*', '', tok))
    ov, bl = overrides(), baseline_versions()
    return {p: ov.get(p, bl.get(p)) for p in sorted(ports) if ov.get(p, bl.get(p))}


def pinned_versions():
    return closure_ports()


def versions_db_path(port):
    """versions/<first-letter>-/<port>.json, vcpkg's version database layout."""
    return os.path.join(VCPKG, "versions", port[0] + "-", port + ".json")


def git_tree_for(port, version):
    """The `git-tree` of the portfile revision this pin resolves to.

    A pin may or may not carry an explicit #port-version. Match on the version
    string, preferring an exact "version#port-version" match and otherwise the
    highest port-version for that version -- which is what vcpkg itself does.
    """
    p = versions_db_path(port)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        entries = json.load(f).get("versions", [])
    base, _, pv = version.partition("#")
    best = None
    for e in entries:
        # vcpkg has FOUR version keys and a port picks exactly one by scheme:
        # `version` (relaxed), `version-semver`, `version-string` (literal) and
        # `version-date` (YYYY-MM-DD). Reading only the first two silently
        # resolved nothing for every date-versioned port -- egl-registry,
        # opengl-registry, libedit and all eight vcpkg-* tooling ports -- and a
        # port that resolves to no tree just vanishes from the output. Absence
        # is invisible unless something counts it, which is why this emitter
        # reports unresolved ports rather than only what it found.
        ev = (e.get("version") or e.get("version-semver")
              or e.get("version-string") or e.get("version-date"))
        if ev != base:
            continue
        epv = e.get("port-version", 0)
        if pv and epv != int(pv):
            continue
        if best is None or epv > best[0]:
            best = (epv, e["git-tree"])
    return best[1] if best else None


# A portfile's fetch calls. The vcpkg_from_* wrappers all funnel into
# vcpkg_download_distfile, so what we need from each is (filename, URL, SHA512).
RE_FROM_GITHUB = re.compile(r'vcpkg_from_github\s*\((.*?)\)', re.S)
RE_FROM_GITLAB = re.compile(r'vcpkg_from_gitlab\s*\((.*?)\)', re.S)
RE_FROM_SOURCEFORGE = re.compile(r'vcpkg_from_sourceforge\s*\((.*?)\)', re.S)
RE_DOWNLOAD = re.compile(r'vcpkg_download_distfile\s*\((.*?)\)', re.S)
RE_FROM_GIT = re.compile(r'vcpkg_from_git\s*\((.*?)\)', re.S)


def _kw(block, key):
    m = re.search(key + r'\s+([^\s\)]+)', block)
    if not m:
        return None
    return m.group(1).strip('"')


def expand(s, version, port):
    """Expand the CMake variables portfiles use inside URLs and filenames.

    Portfiles are CMake, so they interpolate: icu's distfile is
    `icu4c-${VERSION}-sources.tgz` and its URL embeds `release-${VERSION}`.
    Taking those strings literally produces a filename containing the characters
    `${VERSION}` -- which does not match anything vcpkg downloads, so the
    distfile is silently absent from the index rather than wrong. Only the
    variables vcpkg guarantees in portfile scope are expanded here; anything
    still containing `${` after this is reported unexpanded rather than guessed.
    """
    base = version.partition("#")[0]
    s = s.replace("${VERSION}", base).replace("${PORT}", port)
    # A few ports use the underscore/dash-separated forms of the version.
    s = s.replace("${VERSION_MAJOR}", base.split(".")[0])
    return s.replace("@VERSION@", base)


def distfiles_in_portfile(text, port, version):
    """[(name, url, sha512)] for every distfile a portfile fetches.

    Only the shapes Ladybird's closure actually uses are handled (github 48x,
    download_distfile 13x, gitlab 4x, sourceforge 3x, git 2x -- counted, not
    assumed). Anything else must surface in the unresolved report rather than be
    guessed at: a wrong URL is a checksum failure at build time, but a wrong
    *hash* would be a trust downgrade.
    """
    out = []
    E = lambda s: expand(s, version, port)
    for block in RE_FROM_GITHUB.findall(text):
        repo, ref, sha = (_kw(block, "REPO"), _kw(block, "REF"),
                          _kw(block, "SHA512"))
        if not (repo and ref and sha):
            continue
        repo, ref = E(repo), E(ref)
        owner, _, name = repo.partition("/")
        out.append(("%s-%s-%s.tar.gz" % (owner, name, ref),
                    "https://github.com/%s/archive/%s.tar.gz" % (repo, ref),
                    sha))
    for block in RE_FROM_GITLAB.findall(text):
        url, repo, ref, sha = (_kw(block, "GITLAB_URL"), _kw(block, "REPO"),
                               _kw(block, "REF"), _kw(block, "SHA512"))
        if not (repo and ref and sha):
            continue
        host = E(url or "https://gitlab.com")
        repo, ref = E(repo), E(ref)
        # vcpkg names the gitlab archive <org>-<repo>-<ref>.tar.gz, flattening
        # the repo path's slashes the same way it does for github.
        out.append((repo.replace("/", "-") + "-" + ref + ".tar.gz",
                    "%s/%s/-/archive/%s/%s-%s.tar.gz"
                    % (host, repo, ref, repo.split("/")[-1], ref), sha))
    for block in RE_FROM_SOURCEFORGE.findall(text):
        repo, fn, sha = (_kw(block, "REPO"), _kw(block, "FILENAME"),
                         _kw(block, "SHA512"))
        if not (repo and fn and sha):
            continue
        repo, fn = E(repo), E(fn)
        out.append((fn, "https://downloads.sourceforge.net/project/%s/%s"
                    % (repo, fn), sha))
    for block in RE_DOWNLOAD.findall(text):
        urls, fn, sha = (_kw(block, "URLS"), _kw(block, "FILENAME"),
                         _kw(block, "SHA512"))
        if not (urls and sha):
            continue
        urls = E(urls)
        out.append((E(fn) if fn else os.path.basename(urls), urls, sha))
    return out


def git_externals_in_portfile(text):
    """vcpkg_from_git calls: these need a PRE-PLACED archive, not a fetch.

    Asset caching only covers vcpkg_download_distfile; vcpkg_from_git shells out
    to `git fetch`, which no asset source intercepts. It does honour an existing
    downloads/${PORT}-${REF}.tar.gz, so these are reported so the build action
    can stage them (finding 23).
    """
    out = []
    for block in RE_FROM_GIT.findall(text):
        url, ref = _kw(block, "URL"), _kw(block, "REF")
        if url and ref:
            out.append((url, ref))
    return out


def portfile_text(port, version):
    """The portfile.cmake this pin resolves to, and where it came from."""
    tree = git_tree_for(port, version)
    if tree:
        txt = git("cat-file", "-p", "%s:portfile.cmake" % tree)
        if txt:
            return txt, "versions-db"
    # Overlay ports carry their own portfile in the Ladybird tree; they have no
    # versions/ entry by construction (they are not in the curated registry).
    p = os.path.join(OVERLAY, port, "portfile.cmake")
    if os.path.exists(p):
        with open(p) as f:
            return f.read(), "overlay"
    return None, "unresolved"


def sri(sha512_hex):
    """hex SHA512 -> Bazel SRI. This is why no re-hashing is needed: vcpkg's own
    published hash becomes Bazel's integrity attribute unchanged."""
    return "sha512-" + base64.b64encode(bytes.fromhex(sha512_hex)).decode()


def bazel_name(filename, sha512_hex):
    """A Bazel repo name that is unique per DISTFILE, not per filename.

    Two distinct distfiles can share a basename (vcpkg's downloads/ is flat and
    it disambiguates with a hash tag for exactly this reason), and a repo-name
    collision in MODULE.bazel is a silent wrong-content bug: the second
    http_file just loses. So the identity in the name is the hash."""
    return "vcpkg_%s_%s" % (
        re.sub(r'[^A-Za-z0-9_]', '_', filename), sha512_hex[:12])


def canonical_filename(dst, sha512_hex):
    """vcpkg's temp download path -> the filename the portfile actually asked for.

    vcpkg mangles the name it hands x-script in TWO ways, and both have to be
    undone here (finding 30). Neither is guesswork; both are in
    scripts/cmake/vcpkg_download_distfile.cmake.

      1. An in-flight download goes to "<final>.<pid>.part", renamed on success.

      2. If a file already exists at "<final>" whose hash does NOT match the
         expected SHA512, vcpkg does not overwrite it -- it splices the first 8
         hex chars of the expected hash in before the extension and retries there
         (`string(SUBSTRING "${arg_SHA512}" 0 8 hash)`, line 80). So one failed
         earlier attempt that left a 0-byte file behind renames every subsequent
         request for that distfile: "giflib-6.1.3.tar.gz" came through as
         "giflib-6-fb1d6319.1.3.tar.gz". Note where the tag lands: CMake's
         "extension" is everything from the FIRST dot, so it is spliced after
         "giflib-6" -- mid-version, not before ".tar.gz".

    Undoing (2) needs the hash, which is why this takes the sha512.
    """
    name = os.path.basename(dst)
    name = re.sub(r'\.\d+\.part$', '', name)
    return name.replace("-" + sha512_hex[:8].lower(), "", 1)


def load_capture(path):
    """Read a capture TSV (url, sha512, dst) -> {sha512: (url, filename, ...)}.

    This is the exact, authoritative path: every tuple came from vcpkg itself
    resolving a real download, so there are no unexpanded variables and no
    guessed URL shapes. `port` is unknown per row (the asset cache does not see
    it), which only affects the comment on the emitted rule.

    Keyed by SHA512, deliberately. Keying by filename (the first version of this)
    was wrong twice over: vcpkg's mangling (see canonical_filename) makes one
    distfile arrive under two names, so a filename-keyed map emitted 83 rules for
    79 distinct files -- 4 phantom http_files, each of which would fetch a URL and
    then fail its own integrity check. And what this feeds is a sha->label index,
    so the filename was never the identity in the first place.
    """
    out = {}
    with open(path, "rb") as f:
        for raw in f.read().split(b"\n"):
            # A capture killed mid-write can leave a hole of NULs at the head of
            # the file: it is append-only across restarts, and the O_APPEND
            # offset survives a truncation the writes do not.
            line = raw.lstrip(b"\x00").strip()
            if not line:
                continue
            parts = line.decode("utf-8", "replace").split("\t")
            if len(parts) != 3:
                continue
            url, sha, dst = parts
            if not re.fullmatch(r'[0-9a-fA-F]{128}', sha):
                continue
            sha = sha.lower()
            out[sha] = (url, canonical_filename(dst, sha), "captured", "capture")
    return out


# The host tools vcpkg fetches for ITSELF (cmake, ninja, ...), as opposed to the
# distfiles it fetches for ports. Values are (url, sha512, filename).
#
# WHY THIS IS NOT PART OF THE CAPTURE. The capture records what vcpkg actually
# downloaded on the capturing machine, and vcpkg does not download a tool it can
# already find: `vcpkg_find_acquire_program` probes the host first. My machine has
# /usr/bin/ninja at exactly 1.13.2 -- the version vcpkg wants -- so ninja was never
# fetched, never captured, and never pinned; a machine WITHOUT it got
# `distfile MISSING FROM INDEX ... ninja-linux.zip` followed by x-block-origin
# correctly refusing the network. cmake made it into the pin only by luck: the host
# cmake is 4.2.3 against the required 4.4.0, so that one WAS downloaded.
#
# So the capture is the wrong instrument for this class: what it observes depends on
# what the capturing machine happened to have installed. The right source is vcpkg's
# own tool metadata -- scripts/vcpkg-tools.json, versioned inside the checkout at the
# baseline commit, carrying url + sha512 + archive name for every tool on every
# platform. That is a PIN, not an observation, so it is complete regardless of what
# is installed anywhere.
# The tools this build's ports can actually ask for. vcpkg-tools.json also pins
# dotnet, node, powershell-core, azcopy, gsutil, coscli and nuget -- ~400 MB of
# downloads for a build that never invokes any of them (only the unrelated
# vbs-enclave-tooling-codegen port does, and it is not in this closure). Pinning
# those would trade one failure mode for a slower, larger version of the same
# hermeticity story, so the set is scoped and the scoping is stated: BUILD_TOOLS is
# what vcpkg needs to configure and build a port at all.
#
# cmake and ninja are needed by scripts/detect_compiler -- which runs before any
# port does, for every triplet -- so they are not optional for anyone.
BUILD_TOOLS = ("cmake", "ninja")


def tool_distfiles(triplet_os="linux", arches=(None, "x64", "amd64"),
                   want=BUILD_TOOLS, vcpkg=None):
    """vcpkg's own host tools for one platform, read from its tool metadata.

    Keyed by sha512 like every other distfile, so these merge straight into the
    index the asset-cache script resolves through.
    """
    path = os.path.join(vcpkg or VCPKG, "scripts", "vcpkg-tools.json")
    if not os.path.exists(path):
        raise VcpkgUnavailable("no %s (needed for vcpkg's own tool pins)" % path)
    with open(path) as f:
        meta = json.load(f)
    out = {}
    for t in meta.get("tools", []):
        if want is not None and t.get("name") not in want:
            continue
        if t.get("os") != triplet_os or t.get("arch") not in arches:
            continue
        url, sha = t.get("url"), t.get("sha512")
        if not url or not sha:
            continue          # a tool vcpkg expects from the system, not a download
        # vcpkg stores the archive under `archive` when it differs from the URL's
        # basename (ninja-linux.zip -> ninja-linux-1.13.2.zip): the asset cache is
        # asked for the name vcpkg will look for, so prefer it.
        name = t.get("archive") or url.rsplit("/", 1)[-1]
        out[sha.lower()] = (url, name, "vcpkg-tool:" + t.get("name", "?"), "tools.json")
    return out


# The tool pins are COMMITTED next to the asset capture, in the same 3-column TSV
# format, and that is not redundancy -- it is what keeps the emitter's central
# promise true: the committed pin regenerates every Bazel file with no vcpkg
# checkout, no CMake and no network. Deriving the tools from
# scripts/vcpkg-tools.json at emit time would have quietly made a vcpkg checkout a
# requirement again (two tests caught exactly that). So the derivation is a
# separate, deliberate step -- `--capture-tools` -- and its output is reviewed and
# committed like any other pin.
TOOLS_TSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "vcpkg_tool_assets.tsv")


def load_tool_pins(path=None):
    """The committed tool pins -> {sha512: (url, filename, port, source)}."""
    path = path or TOOLS_TSV
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            url, sha, name = parts
            if not re.fullmatch(r'[0-9a-fA-F]{128}', sha):
                continue
            out[sha.lower()] = (url, name, "vcpkg-tool", "tools.tsv")
    return out


# ---------------------------------------------------------------------------
# The third class of input: tools vcpkg CANNOT download at all.
#
# Finding 38 pinned the tools vcpkg fetches for itself (cmake, ninja) after ninja
# went unpinned because the capturing machine already had it. The same blind spot
# has a strictly worse case behind it, and nasm is it: on Linux
# `vcpkg_find_acquire_program(NASM)` has NO download_urls -- the Windows branch
# has three URLs and a sha512, the Linux branch has an apt package name and
# nothing else. So there is no pin to add. vcpkg probes the host, does not find
# it, and stops:
#
#   CMake Error at scripts/cmake/vcpkg_find_acquire_program.cmake:201 (message):
#     Could not find nasm.  Please install it via your package manager
#
# ~20 minutes into `bazel build //:vcpkg_installed`, from inside libvpx, naming a
# scratch path. Six ports in this closure ask for it (dav1d, ffmpeg,
# libjpeg-turbo, libvpx, openh264, openssl), and the machine this was developed on
# has nasm, perl, pkg-config and python3 all preinstalled -- which is exactly why
# neither the capture NOR the tools.json pin could ever have revealed this.
#
# These are HOST PREREQUISITES, and calling them that is the point: they are not a
# hermeticity gap we can close by pinning a URL, they are the current, honest
# boundary of the port. Two things follow, and both are implemented here:
#
#   1. The set is DERIVED from vcpkg's own scripts (which programs the closure's
#      portfiles invoke, and which of those have no Linux download), not from a
#      hand-written list that rots at the next baseline bump.
#   2. It is CHECKED BEFORE the build, all at once, by name, with the ports that
#      need each one -- so a machine missing three of them learns all three in one
#      second, instead of one per 20-minute build.
#
# The real fix -- pinning these as Bazel-fetched binaries so the build needs no
# host tools at all -- is the same open work as glslangValidator, and is filed
# rather than pretended.
HOST_TOOLS_TSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "vcpkg_host_tools.tsv")


def _acquire_script(program, vcpkg=None):
    p = os.path.join(vcpkg or VCPKG, "scripts", "cmake",
                     "vcpkg_find_acquire_program(%s).cmake" % program)
    return p if os.path.exists(p) else None


# A condition that selects Windows. `MSVC` counts: an
# `ASM_COMPILER_ID STREQUAL "MSVC"` branch is Windows-only in practice, and that
# branch is the ONLY thing in this closure that asks for CLANG -- read naively it
# reports clang as a Linux prerequisite, which it is not.
_WIN_TOKEN = re.compile(
    r'\b(CMAKE_HOST_WIN32|VCPKG_HOST_IS_WINDOWS|VCPKG_TARGET_IS_WINDOWS|WIN32|MSVC|'
    r'VCPKG_TARGET_IS_UWP|VCPKG_TARGET_IS_MINGW)\b')


def _classify(cond):
    """'win' / 'notwin' / None for a CMake if-condition."""
    if not _WIN_TOKEN.search(cond):
        return None
    return "notwin" if re.match(r'\s*NOT\b', cond) else "win"


def strip_windows_blocks(text):
    """CMake source -> only the lines reachable on a non-Windows host.

    Both halves of this analysis need it, which is why it is one function. When
    reading an acquire-script, every download URL lives in the Windows branch, so
    NOT skipping it is exactly the mistake that hides the gap. When reading a
    portfile, a `vcpkg_find_acquire_program` call inside a Windows branch is not a
    prerequisite here at all.

    `else()` is the subtle case and is handled rather than guessed: the else of
    `if(NOT VCPKG_TARGET_IS_WINDOWS)` IS the Windows branch (dav1d's is written
    exactly that way), so each if-chain remembers whether any of its conditions
    was a negated Windows test and suppresses its else accordingly.
    """
    out, stack = [], []
    for raw in text.splitlines():
        s = raw.strip()
        m = re.match(r'if\s*\((.*)', s, re.S)
        if m:
            cls = _classify(m.group(1))
            stack.append({"suppressed": cls == "win", "saw_notwin": cls == "notwin"})
            continue
        m = re.match(r'elseif\s*\((.*)', s, re.S)
        if m and stack:
            cls = _classify(m.group(1))
            stack[-1]["suppressed"] = cls == "win"
            stack[-1]["saw_notwin"] = stack[-1]["saw_notwin"] or cls == "notwin"
            continue
        if re.match(r'else\s*\(', s) and stack:
            stack[-1]["suppressed"] = stack[-1]["saw_notwin"]
            continue
        if re.match(r'endif\b', s):
            if stack:
                stack.pop()
            continue
        if not any(f["suppressed"] for f in stack):
            out.append(raw)
    return "\n".join(out)


def _has_non_windows_download(text):
    """Does this acquire-script offer a download vcpkg can use on Linux?

    A tool with no such download is one vcpkg can only take from the host.
    """
    body = strip_windows_blocks(text)
    # `z_use_vcpkg_fetch` delegates to `vcpkg fetch`, which IS covered by the
    # tools.json pin (that is how ninja is handled) -- so it is not a host
    # prerequisite even though it sets no download_urls itself.
    if "z_use_vcpkg_fetch" in body:
        return True
    return bool(re.search(r'set\s*\(\s*download_urls', body))


def _port_cmake_files(port, vcpkg=None):
    """Every .cmake/.json in a port that is reachable on a non-Windows host."""
    vc = vcpkg or VCPKG
    for base in (OVERLAY, os.path.join(vc, "ports")):
        d = os.path.join(base, port)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            # A port can split itself by platform at the FILE level rather than
            # with an if(): openssl's portfile.cmake picks between
            # `unix/portfile.cmake` and `windows/portfile.cmake`, and the windows
            # one asks for CLANG and NASM at TOP LEVEL, guarded by nothing this
            # file can see. Skipping windows/ directories is therefore not
            # cosmetic -- without it openssl reports clang as a Linux
            # prerequisite. Conservative by construction: a Windows-only
            # directory can only hold Windows-only requirements.
            rel = os.path.relpath(root, d).split(os.sep)
            if any(p.lower() in ("windows", "win32", "uwp", "mingw") for p in rel):
                continue
            for fn in sorted(files):
                if fn.endswith((".cmake", ".json")):
                    yield os.path.join(root, fn)
        return


# Programs a port probes with a bare `find_program` and then hard-fails on. This
# is a SECOND mechanism, found the hard way after the first one shipped: gperf
# died on `autoconf autoconf-archive automake libtoolize` from
# vcpkg-make/vcpkg_make.cmake, which never calls vcpkg_find_acquire_program at
# all -- it calls find_program(AUTORECONF NAMES autoreconf) and raises
# FATAL_ERROR listing apt packages. And it does it from a HELPER port
# (vcpkg-make), so the error names gperf while the requirement lives somewhere
# gperf does not mention. Enumerating only the acquire-program calls would have
# missed every one of these, which is why the preflight scans for both.


# Which binary proves an apt package is installed. Only needed where the two
# names differ; anything absent is probed under its own name, and a package that
# ships no executable at all maps to "" and is reported as unprobeable.
_PKG_BINARY = {
    "autoconf": ["autoreconf", "autoconf"],
    "automake": ["aclocal", "automake"],
    "libtool": ["libtoolize", "glibtoolize"],
    "gettext": ["autopoint"],
    "gtk-doc-tools": ["gtkdocize"],
    "autoconf-archive": [],   # m4 macros, no binary
    "libltdl-dev": [],        # headers
    "pkg-config": ["pkg-config", "pkgconf"],
}


def _fatal_package_requirements(text):
    """Packages a file DEMANDS from the system package manager, and how to probe.

    Anchored on the text of the `message(FATAL_ERROR ...)` itself, not on the file
    containing a FATAL_ERROR somewhere. That distinction is the difference between
    a useful preflight and a noisy one: angle's portfile has both a FATAL_ERROR
    (about an unsupported architecture) and a WARNING recommending
    mesa-common-dev, and a file-level check staples them together and demands a
    package no build step actually requires. A WARNING is advice; only a
    FATAL_ERROR is a prerequisite.

    Returns {apt-package: [binaries that prove it, possibly empty]}.
    """
    out = {}
    for m in re.finditer(r'message\(\s*FATAL_ERROR\s+(.*?)\)\s*$',
                         strip_windows_blocks(text), re.S | re.M):
        msg = m.group(1)
        if not re.search(r'package manager|apt(?:-get)? install', msg):
            continue
        for am in re.finditer(r'apt(?:-get)? install ([^\n"\\]*)', msg):
            for pkg in am.group(1).split():
                if not re.fullmatch(r'[\w.+-]+', pkg) or pkg == "sudo":
                    continue
                out.setdefault(pkg, _PKG_BINARY.get(pkg, [pkg]))
    return out


def host_tool_requirements(ports=None, vcpkg=None):
    """-> {key: (binary, apt-package, [ports that need it], [alternatives])}.

    The programs the closure's portfiles need FROM THE HOST, by both mechanisms:
    a `vcpkg_find_acquire_program` for a tool with no Linux download, and a bare
    `find_program` in a file that hard-fails naming a package manager.

    This is a static parse and inherits its limits (a program named through a
    variable is invisible). Unlike the distfile parse there is no instrument that
    does better, because the failure only manifests on a machine that LACKS the
    tool -- so a capture on a machine that has everything sees nothing, which is
    precisely how nasm and autoconf each got to fail at minute 20. Under-reporting
    degrades to that same late error; it never invents a prerequisite.
    """
    vc = vcpkg or VCPKG
    if ports is None:
        ports = sorted(pinned_versions())
    out, asked = {}, {}
    for port in ports:
        for path in _port_cmake_files(port, vc):
            with open(path, errors="replace") as f:
                text = f.read()
            # Mechanism 1: only the calls REACHABLE on a non-Windows host. openssl
            # and vcpkg-make both ask for CLANG, but only inside an MSVC/Windows
            # branch -- counting those made clang a prerequisite of a Linux build,
            # which is a false alarm, and a preflight that cries wolf gets deleted.
            for m in re.finditer(
                    r'vcpkg_find_acquire_program\(\s*([A-Z0-9_]+)',
                    strip_windows_blocks(text)):
                asked.setdefault(m.group(1), set()).add(port)
            # Mechanism 2: a FATAL_ERROR naming system packages directly.
            for apt, binaries in _fatal_package_requirements(text).items():
                cur = out.setdefault(apt, [binaries[0] if binaries else "", apt,
                                           set(), binaries[1:]])
                cur[2].add(port)
    for program, users in asked.items():
        script = _acquire_script(program, vc)
        if not script:
            continue
        with open(script, errors="replace") as f:
            text = f.read()
        if _has_non_windows_download(text):
            continue
        # program_name from the NON-Windows branch: PYTHON3 sets `python` for
        # Windows and `python3` for everything else, and probing for `python` on
        # Linux asks for a binary that has not existed by default for years.
        body = strip_windows_blocks(text)
        name = re.search(r'set\(program_name\s+"?([\w.+-]+)', body)
        apt = re.search(r'set\(apt_package_name\s+"?([\w.+-]+)', body)
        binary = name.group(1) if name else program.lower()
        cur = out.setdefault(binary, [binary, apt.group(1) if apt else "",
                                      set(), []])
        cur[2] |= users
    return {k: (v[0], v[1], sorted(v[2]), v[3]) for k, v in out.items()}


def emit_host_tools(reqs):
    """Write the host-prerequisite TSV: program, binary, apt package, ports."""
    print("# Tools the build needs FROM THE HOST, because vcpkg cannot supply them")
    print("# on Linux at all. GENERATED by emit_vcpkg_bazel.py --host-tools.")
    print("#")
    print("# NOT the same class as vcpkg_tool_assets.tsv. Those are tools vcpkg")
    print("# fetches for ITSELF, and the fix there was a pin. There is no URL to")
    print("# pin for these, by either of the two mechanisms that produce them:")
    print("#")
    print("#   1. vcpkg_find_acquire_program(<X>) whose download URLs are ONLY")
    print("#      inside `if(CMAKE_HOST_WIN32)`. On Linux vcpkg probes the host and")
    print("#      hard-fails: 'Could not find nasm. Please install it via your")
    print("#      package manager'.")
    print("#   2. a message(FATAL_ERROR ...) naming apt packages outright, after a")
    print("#      bare find_program. gperf died this way on autoconf/automake/")
    print("#      libtool -- and the requirement lives in the vcpkg-make HELPER")
    print("#      port, so the error names gperf while the cause names neither.")
    print("#")
    print("# So this file does not pretend to close the gap: it NAMES the gap, and")
    print("# vcpkg_build.sh checks the whole list up front -- a machine missing")
    print("# three tools is told about three tools in one second, instead of one")
    print("# per 20-minute build (nasm surfaced from libvpx at minute ~20).")
    print("#")
    print("# ports-that-need-it is where the requirement is WRITTEN, which for")
    print("# vcpkg-make is not where it fails: every autotools port using it can.")
    print("#")
    print("# An empty binary means the package ships no executable to probe for")
    print("# (autoconf-archive is m4 macros, libltdl-dev is headers), so the")
    print("# preflight can only name it -- it cannot verify it.")
    print("#")
    print("# binary-or-alternatives\tapt-package\tports-that-need-it")
    for program in sorted(reqs):
        binary, apt, users, alts = reqs[program]
        names = "|".join([binary] + list(alts)) if binary else "-"
        print("%s\t%s\t%s" % (names, apt or program, ",".join(users)))


def load_host_tools(path=None):
    """The committed host-prerequisite list -> [(alternatives, apt, ports)].

    `alternatives` is empty for a package with no binary to probe.
    """
    path = path or HOST_TOOLS_TSV
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            names, apt, users = parts
            out.append(([n for n in names.split("|") if n and n != "-"], apt,
                        [p for p in users.split(",") if p]))
    return out


def emit_tool_pins(tools):
    """Write the tool pin TSV: url, sha512, the filename vcpkg looks for."""
    print("# vcpkg's OWN host tools, pinned from its scripts/vcpkg-tools.json at the")
    print("# builtin-baseline. GENERATED by emit_vcpkg_bazel.py --capture-tools.")
    print("#")
    print("# Separate from vcpkg_assets.tsv because the asset capture CANNOT see")
    print("# these: vcpkg_find_acquire_program probes the host first, so a tool the")
    print("# capturing machine already has is never downloaded and never captured.")
    print("# That is how ninja went unpinned -- the capturing machine had")
    print("# /usr/bin/ninja at exactly the required version -- while cmake was pinned")
    print("# only because the host's was too old. What a pin contains must not depend")
    print("# on what happens to be installed on one machine.")
    print("#")
    print("# url\tsha512\tfilename-vcpkg-looks-for")
    for sha, (url, name, port, _src) in sorted(tools.items(), key=lambda kv: kv[1][1]):
        print("%s\t%s\t%s" % (url, sha, name))


def collect():
    """-> (distfiles, git_externals, unresolved). Ports come from the manifest."""
    pins = pinned_versions()
    distfiles, externals, unresolved, unexpanded = {}, {}, [], []
    for port in sorted(pins):
        text, source = portfile_text(port, pins[port])
        if text is None:
            unresolved.append((port, pins[port]))
            continue
        for name, url, sha in distfiles_in_portfile(text, port, pins[port]):
            if "${" in name or "${" in url:
                unexpanded.append((port, name, url))
                continue
            # Keyed by sha512, same as the capture: one distfile, one key.
            distfiles[sha.lower()] = (url, name, port, source)
        ext = git_externals_in_portfile(text)
        if ext:
            externals[port] = ext
    return distfiles, externals, unresolved, unexpanded


def observed_git_archives(downloads_dir, distfiles):
    """The archives in a completed run's downloads/ that the asset cache never saw.

    vcpkg_from_git shells out to `git fetch` + `git archive`, which NO asset
    source intercepts, so these are invisible to the capture by construction --
    and equally invisible to the static parse, for the usual reason: skia reaches
    them through its own `declare_external_from_git` wrapper (10 of them) and
    angle's are behind `${URL}`/`${REF}`. Static parsing reported 2; the real
    number is 4. So identify them the same way as everything else here: by
    DIFFERENCE against what actually landed on disk, not by prediction.

    Returns (archives, byproducts): archives are what a hermetic build must
    pre-place at downloads/<PORT>-<REF>.tar.gz; byproducts are files vcpkg's
    downloads/ accumulates that are not inputs at all (parsetab.py is PLY's
    generated parse table, written by angle's build, not fetched).
    """
    have = set(distfiles)
    archives, byproducts = [], []
    for name in sorted(os.listdir(downloads_dir)):
        path = os.path.join(downloads_dir, name)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            continue
        h = hashlib.sha512(open(path, "rb").read()).hexdigest()
        if h in have:
            continue
        (archives if name.endswith(".tar.gz") else byproducts).append((name, h))
    return archives, byproducts


def emit_git_archives(archives):
    sys.stdout.write(HEADER)
    print('load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_file")')
    print()
    print("# %d git-sourced externals. vcpkg_from_git bypasses asset caching"
          % len(archives))
    print("# entirely, but DOES honour a pre-placed downloads/<PORT>-<REF>.tar.gz,")
    print("# which is what these become. The SHA512 is of the `git archive` output,")
    print("# so it is only stable because git archive is deterministic for a fixed")
    print("# ref -- verified by two independent runs producing identical bytes.")
    print()
    print("VCPKG_GIT_ARCHIVES = {")
    for name, sha in archives:
        print("    %r: %r," % (name, sha))
    print("}")


HEADER = "# AUTO-GENERATED by Meta/emit_vcpkg_bazel.py — do not edit.\n"


def emit_distfiles(distfiles):
    sys.stdout.write(HEADER)
    print('load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_file")')
    print()
    print("# %d upstream distfiles. Each `integrity` is vcpkg's own published"
          % len(distfiles))
    print("# SHA512 from the baseline-resolved portfile, hex->base64; nothing is")
    print("# re-hashed here, so this is not a trust downgrade.")
    print()
    print("def vcpkg_distfiles():")
    for sha, (url, name, port, _src) in sorted(
            distfiles.items(), key=lambda kv: (kv[1][1], kv[0])):
        print("    http_file(")
        print("        name = %r," % bazel_name(name, sha))
        print("        urls = %r," % urls_for(url))
        print("        downloaded_file_path = %r," % name)
        print("        integrity = %r,  # %s" % (sri(sha), port))
        print("    )")


# Known-equivalent mirrors, keyed by URL prefix. This is the payoff predicted in
# finding 29: vcpkg's x-script asset hook is handed ONE url per attempt, so the
# multi-mirror redundancy portfiles encode never reaches it and a single 502 kills
# the fetch. `http_file` takes a LIST of urls and tries them in order, so moving
# fetching to Bazel does not just relocate the problem, it fixes it. (Observed,
# not hypothetical: ftpmirror.gnu.org 502'd during the capture AND again during
# the first Bazel fetch of all 76.)
#
# Safe because every URL is content-addressed by `integrity`: a mirror that serves
# the wrong bytes fails the hash, so the only thing a bad mirror can cost is time.
MIRRORS = {
    "https://ftpmirror.gnu.org/gnu/": [
        "https://ftp.gnu.org/gnu/",
        "https://www.mirrorservice.org/sites/ftp.gnu.org/gnu/",
        "https://mirrors.kernel.org/gnu/",
    ],
    "https://ftp.gnu.org/gnu/": [
        "https://ftpmirror.gnu.org/gnu/",
        "https://www.mirrorservice.org/sites/ftp.gnu.org/gnu/",
        "https://mirrors.kernel.org/gnu/",
    ],
    "https://www.mirrorservice.org/sites/ftp.gnu.org/gnu/": [
        "https://ftpmirror.gnu.org/gnu/",
        "https://ftp.gnu.org/gnu/",
        "https://mirrors.kernel.org/gnu/",
    ],
}


def urls_for(url):
    """One URL -> the list to hand http_file, primary first."""
    out = [url]
    for prefix, alts in MIRRORS.items():
        if url.startswith(prefix):
            rest = url[len(prefix):]
            out += [a + rest for a in alts]
            break
    return out


def emit_extension(distfiles):
    """The module extension that actually creates the repos.

    `http_file` is a *repository* rule, so under bzlmod it cannot be called from
    MODULE.bazel directly -- it has to be invoked from a module extension's
    implementation. That indirection is also what lets one `use_repo` name the 76
    repos without MODULE.bazel enumerating any URLs or hashes.
    """
    sys.stdout.write(HEADER)
    print('load(":vcpkg_distfiles.bzl", "vcpkg_distfiles")')
    # The pip-installed Python packages a PORTFILE asks for. Hand-written, not
    # captured -- pip does not go through vcpkg's asset cache, so the instrument
    # that produced the 76 distfiles cannot see them (finding 36). Loaded here so
    # they are created by the same extension and named by the same use_repo.
    print('load(":vcpkg_python_packages.bzl", "vcpkg_python_wheels")')
    print()
    print("def _vcpkg_deps_impl(_ctx):")
    print("    vcpkg_distfiles()")
    print("    vcpkg_python_wheels()")
    print()
    print("vcpkg_deps = module_extension(implementation = _vcpkg_deps_impl)")


def emit_use_repo(distfiles):
    """The `use_repo(vcpkg_deps, ...)` line MODULE.bazel needs.

    Emitted rather than hand-maintained because bzlmod requires every repo an
    extension creates to be named here to be visible, and a list of 76 names kept
    in sync by hand is a guaranteed drift (and the failure is a confusing
    "no such repository", far from its cause)."""
    names = sorted(bazel_name(name, sha)
                   for sha, (_u, name, _p, _s) in distfiles.items())
    # The pip wheels are created by the SAME extension, so they must be in the
    # same use_repo -- and they are NOT in the capture, because pip does not go
    # through vcpkg's asset cache (finding 36). Read them out of the hand-written
    # vcpkg_python_packages.bzl rather than restating them, so pasting this output
    # into MODULE.bazel cannot silently drop the wheel and leave a "no such
    # repository" a long way from its cause.
    names += ["vcpkg_pywheel_" + n for n in sorted(python_wheel_names())]
    print("use_repo(")
    print("    vcpkg_deps,")
    for n in names:
        print("    %r," % n)
    print(")")


def python_wheel_names():
    """The keys of VCPKG_PYTHON_WHEELS, parsed out of the .bzl that declares them.

    Parsed rather than duplicated: that file is the pin, and a second copy of the
    list here would be one more thing to drift (finding 23's rule, applied to the
    one vcpkg input no instrument can capture).
    """
    path = os.path.join(ROOT, "vcpkg_python_packages.bzl")
    if not os.path.exists(path):
        sys.stderr.write("WARNING: %s is missing; no pip wheels will be named, so "
                         "any port that pip-installs will fail\n" % path)
        return []
    with open(path) as f:
        body = f.read()
    block = body.split("VCPKG_PYTHON_WHEELS = {", 1)[1].split("\n}", 1)[0]
    return re.findall(r'^\s*"([A-Za-z0-9_.\-]+)":', block, re.M)


def emit_index(distfiles):
    sys.stdout.write(HEADER)
    print("# sha512 (hex) -> the label of the file Bazel fetched for it.")
    print("# The asset-cache script resolves vcpkg's downloads by hash through")
    print("# this map, so vcpkg never reaches the network (x-block-origin).")
    print()
    print("VCPKG_DISTFILE_INDEX = {")
    for sha, (_url, name, port, _src) in sorted(
            distfiles.items(), key=lambda kv: (kv[1][1], kv[0])):
        print("    %r: %r,  # %s" % (
            sha, "@%s//file:%s" % (bazel_name(name, sha), name), port))
    print("}")


def report(distfiles, externals, unresolved, unexpanded):
    pins = pinned_versions()
    by_source = {}
    for _n, (_u, _s, _p, src) in distfiles.items():
        by_source[src] = by_source.get(src, 0) + 1
    print("pinned ports (from vcpkg.json overrides): %d" % len(pins))
    print("distfiles resolved: %d" % len(distfiles))
    for src, n in sorted(by_source.items()):
        print("    via %-12s %d" % (src, n))
    print("ports needing pre-staged git archives: %d" % len(externals))
    for port, ext in sorted(externals.items()):
        for url, ref in ext:
            print("    %-16s %s @ %s" % (port, url, ref))
    print("unresolved ports: %d" % len(unresolved))
    for port, v in unresolved:
        print("    %-16s %s" % (port, v))
    print("distfiles with unexpanded CMake variables: %d" % len(unexpanded))
    for port, name, url in unexpanded:
        print("    %-16s %s  %s" % (port, name, url))
    return 1 if (unresolved or unexpanded) else 0


def main():
    # With --assets the capture is authoritative and the static parse is only a
    # cross-check, so a missing vcpkg checkout must NOT be fatal: the whole point
    # of committing the capture is that a consumer can regenerate the Bazel rules
    # from the pin alone, with no vcpkg, no CMake and no network. Without --assets
    # the static parse IS the output, and then failing loudly is right (finding
    # 28: refusing to silently undercount the closure).
    if "--assets" in sys.argv:
        try:
            distfiles, externals, unresolved, unexpanded = collect()
        except VcpkgUnavailable as e:
            sys.stderr.write(
                "note: no local vcpkg checkout, so the static cross-check is "
                "unavailable (%s).\n      The capture is authoritative; emitting "
                "from it alone.\n" % e)
            distfiles, externals, unresolved, unexpanded = {}, {}, [], []
    else:
        try:
            distfiles, externals, unresolved, unexpanded = collect()
        except VcpkgUnavailable as e:
            sys.stderr.write("error: vcpkg depend-info failed: %s\n" % e)
            sys.stderr.write(
                "Refusing to fall back to the manifest's overrides alone: that "
                "silently UNDERCOUNTS the closure (39 of 81 distfiles), which is "
                "worse than failing. Pass --assets <capture.tsv> to emit from the "
                "committed pin instead, which needs no vcpkg at all.\n")
            return 2
    if "--assets" in sys.argv:
        cap = load_capture(sys.argv[sys.argv.index("--assets") + 1])
        # The capture REPLACES the static parse rather than being unioned with it.
        # Unioning looks safer and is not: a portfile is a program, so the static
        # regex cannot see through the platform branches it is full of, and every
        # static-only row here turned out to be a Windows-only fetch
        # (libiconv/pthreads/dirent are behind `if(VCPKG_TARGET_IS_WINDOWS)`).
        # Those are not distfiles this build needs; emitting them adds downloads
        # that can break the build when an unrelated upstream URL rots, in
        # exchange for nothing. Meanwhile the union covered none of the 5 files
        # the capture genuinely misses -- the static parse misses those too. So
        # the instrument wins outright, and the shortfall is REPORTED.
        static_only = sorted(set(distfiles) - set(cap))
        sys.stderr.write("capture: %d distfiles (static parse had %d)\n"
                         % (len(cap), len(distfiles)))
        if static_only:
            sys.stderr.write(
                "  dropping %d static-only entries vcpkg never asked for on this "
                "platform:\n" % len(static_only))
            for sha in static_only:
                sys.stderr.write("    %-40s %s\n"
                                 % (distfiles[sha][1], distfiles[sha][2]))
        distfiles, unexpanded = cap, []
        # vcpkg's OWN tools are unioned in from their COMMITTED pin, not replaced
        # and not re-derived: they are a different class from port distfiles, and
        # the capture cannot see them at all (finding 38).
        tools = load_tool_pins()
        if not tools:
            sys.stderr.write(
                "error: no %s\n"
                "vcpkg's own host tools (cmake, ninja) are pinned there, because the\n"
                "capture cannot see a tool the capturing machine already had. Without\n"
                "them the index works only on machines that happen to have the same\n"
                "tools installed. Regenerate with:\n"
                "    emit_vcpkg_bazel.py --capture-tools > Meta/vcpkg_tool_assets.tsv\n"
                % TOOLS_TSV)
            return 2
        added = [sha for sha in tools if sha not in distfiles]
        sys.stderr.write("vcpkg host tools: %d pinned from %s (%d not in the capture)\n"
                         % (len(tools), os.path.basename(TOOLS_TSV), len(added)))
        for sha in sorted(added):
            sys.stderr.write("    added %-34s %s\n" % (tools[sha][1], tools[sha][2]))
        # Cross-check against vcpkg's metadata WHEN a checkout is at hand: a stale
        # committed pin (baseline bumped, tool version moved) is otherwise invisible
        # until someone without that tool tries to build.
        try:
            live = tool_distfiles()
        except VcpkgUnavailable:
            pass
        else:
            stale = set(live) - set(tools)
            gone = set(tools) - set(live)
            for sha in sorted(stale):
                sys.stderr.write(
                    "    WARNING: %s is in vcpkg-tools.json but NOT in the committed"
                    " pin -- re-run --capture-tools\n" % live[sha][1])
            for sha in sorted(gone):
                sys.stderr.write(
                    "    note: %s is pinned but no longer in vcpkg-tools.json\n"
                    % tools[sha][1])
        for sha, row in tools.items():
            distfiles.setdefault(sha, row)
    if "--capture-tools" in sys.argv:
        emit_tool_pins(tool_distfiles())
        return 0
    if "--host-tools" in sys.argv:
        emit_host_tools(host_tool_requirements())
        return 0
    if "--git-archives" in sys.argv:
        dl = sys.argv[sys.argv.index("--git-archives") + 1]
        archives, byproducts = observed_git_archives(dl, distfiles)
        # Cross-check the static parse against the observation, and say so when
        # it fell short -- the point of finding 25: a green check has to be
        # compared against something IT did not produce.
        static = set()
        for port, ext in externals.items():
            for _url, ref in ext:
                if "${" not in ref:
                    static.add("%s-%s.tar.gz" % (port, ref))
        observed = set(n for n, _h in archives)
        sys.stderr.write("git archives: %d observed, static parse predicted %d\n"
                         % (len(observed), len(static)))
        for n in sorted(observed - static):
            sys.stderr.write("    MISSED BY STATIC PARSE: %s\n" % n)
        for n in sorted(static - observed):
            sys.stderr.write("    predicted but never fetched: %s\n" % n)
        for n, _h in byproducts:
            sys.stderr.write("    byproduct, not an input: %s\n" % n)
        emit_git_archives(archives)
    elif "--distfiles" in sys.argv:
        emit_distfiles(distfiles)
    elif "--index" in sys.argv:
        emit_index(distfiles)
    elif "--extension" in sys.argv:
        emit_extension(distfiles)
    elif "--use-repo" in sys.argv:
        emit_use_repo(distfiles)
    else:
        return report(distfiles, externals, unresolved, unexpanded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
