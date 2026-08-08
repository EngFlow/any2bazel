# The Python packages a vcpkg PORTFILE installs with pip, pinned as wheels.
#
# Hand-written, unlike the other four vcpkg .bzl files, and the reason is worth
# stating: this is not something the asset capture can produce. The 76 distfiles
# came from instrumenting vcpkg's own downloader (finding 28), and
# `--x-asset-sources=x-script+x-block-origin` covers everything that goes through
# it. `pip install` does not: the angle overlay-port calls
# x_vcpkg_get_python_packages, which shells out to pip inside a venv, and vcpkg's
# asset cache never sees the request. So the capture cannot report it, the pin
# cannot cover it, and x-block-origin cannot block it.
#
# That is exactly how it went unnoticed (finding 36): the vcpkg action runs
# `no-sandbox` with `use_default_shell_env = True`, so it inherited this sandbox's
# HTTP_PROXY and pip quietly reached PyPI for months, under a rule whose
# `requires-network: "0"` is only a scheduling hint Bazel does not enforce.
#
# pip has a supported offline mode -- PIP_NO_INDEX + PIP_FIND_LINKS -- so the fix
# needs no patch to the portfile: fetch the wheel by URL with a hash, put it in a
# find-links directory, and tell pip it may not use an index. Then a wheel missing
# from the pin is a hard error rather than a download, which is the same property
# x-block-origin gives the other 76.
load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_file")

# name -> (url, integrity). The URL is files.pythonhosted.org's content-addressed
# path, so it is immutable for a given (name, version) -- unlike `pip install ply`,
# which resolves against whatever PyPI serves today.
VCPKG_PYTHON_WHEELS = {
    # angle/portfile.cmake:86 -> x_vcpkg_get_python_packages(PACKAGES ply).
    # ply is pure-python (py2.py3-none-any), so one wheel serves every platform.
    "ply": (
        "https://files.pythonhosted.org/packages/a3/58/35da89ee790598a0700ea49b2a66594140f44dec458c07e8e3d4979137fc/ply-3.11-py2.py3-none-any.whl",
        "sha256-CW+bg1C2Xr0v0TRrEkUu/luWB/dIKBP/ylDCJyKoB84=",
    ),
}

def vcpkg_python_wheels():
    for name, (url, integrity) in VCPKG_PYTHON_WHEELS.items():
        http_file(
            name = "vcpkg_pywheel_" + name,
            urls = [url],
            downloaded_file_path = url.rsplit("/", 1)[-1],
            integrity = integrity,
        )
