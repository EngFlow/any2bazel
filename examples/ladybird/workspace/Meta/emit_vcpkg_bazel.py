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
        print("        urls = [%r]," % url)
        print("        downloaded_file_path = %r," % name)
        print("        integrity = %r,  # %s" % (sri(sha), port))
        print("    )")


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
    else:
        return report(distfiles, externals, unresolved, unexpanded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
