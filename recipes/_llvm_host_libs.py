"""Host runtime libraries for the managed LLVM bundle's lld (zackees/soldr#3774).

``zackees/clang-tool-chain-bins``' LLVM 21.1.5 ``lld`` NEEDs ``libxml2.so.2``
and ``libz.so.1`` and carries ``RUNPATH $ORIGIN/../lib``. Hosts without
``libxml2.so.2`` (ubuntu-26.04, Debian trixie: soname ``.16``) cannot run
it. This bundle builds both libraries from pinned sources inside
manylinux2014 (glibc 2.17) so soldr can place them in the LLVM ``lib/``.

Bundle layout::

    lib/libxml2.so.2
    lib/libz.so.1
    meta.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

PINNED_VERSIONS = ("libxml2-2.13.9-zlib-1.3.1",)
LIBXML2_VERSION = "2.13.9"
ZLIB_VERSION = "1.3.1"
SOURCES = {
    "libxml2": (
        "https://download.gnome.org/sources/libxml2/2.13/libxml2-2.13.9.tar.xz",
        "a2c9ae7b770da34860050c309f903221c67830c86e4a7e760692b803df95143a",
    ),
    "zlib": (
        "https://zlib.net/fossils/zlib-1.3.1.tar.gz",
        "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23",
    ),
}
MANYLINUX2014_IMAGES = {
    "linux-x64-gnu": (
        "quay.io/pypa/manylinux2014_x86_64:2025.10.10-1@"
        "sha256:9bc3d6107261c6d6bdd28512778d7b5c2d57f79f5baa5ad4a3533026e4a4237d"
    ),
    "linux-arm64-gnu": (
        "quay.io/pypa/manylinux2014_aarch64:2025.10.10-1@"
        "sha256:7b2ac44ef495149cd8e91323201e8707faa2d9c430a64e8483488c493fed1b95"
    ),
}
SHIPPED = ("libxml2.so.2", "libz.so.1")
# glibc core libraries plus what the bundle itself provides.
ALLOWED_NEEDED = frozenset(
    {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", *SHIPPED}
)
GLIBC_FLOOR = (2, 17)
_NEEDED_RE = re.compile(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]")
_SONAME_RE = re.compile(r"\(SONAME\)\s+Library soname: \[([^\]]+)\]")
_GLIBC_RE = re.compile(r"GLIBC_(\d+)\.(\d+)")


def docker_command(shape: str, build_root: Path, *, uid_gid: str) -> list[str]:
    image = MANYLINUX2014_IMAGES.get(shape)
    if image is None:
        raise ValueError(f"llvm-host-libs does not support {shape}")
    script = " && ".join(
        [
            f"cd /work/src/zlib-{ZLIB_VERSION}",
            "CFLAGS='-O2 -fPIC' ./configure --prefix=/work/stage --libdir=/work/stage/lib",
            'make -j"$(nproc)"',
            "make install",
            f"cd /work/src/libxml2-{LIBXML2_VERSION}",
            "./configure --prefix=/work/stage --libdir=/work/stage/lib"
            " --enable-shared --disable-static --without-python --without-lzma"
            " --without-icu --without-iconv --without-readline --without-debug"
            " --without-http --without-ftp --with-zlib=/work/stage CFLAGS='-O2'",
            'make -j"$(nproc)" libxml2.la',
            "cp -P .libs/libxml2.so* /work/stage/lib/",
        ]
    )
    return [
        "docker", "run", "--rm",
        "--user", uid_gid,
        "-v", f"{build_root}:/work",
        image,
        "bash", "-ec", script,
    ]


def parse_needed(dynamic: str) -> list[str]:
    return _NEEDED_RE.findall(dynamic)


def parse_soname(dynamic: str) -> str | None:
    match = _SONAME_RE.search(dynamic)
    return match.group(1) if match else None


def max_glibc(text: str) -> tuple[int, int] | None:
    versions = {(int(a), int(b)) for a, b in _GLIBC_RE.findall(text)}
    return max(versions) if versions else None


def unexpected_needed(needed: list[str]) -> list[str]:
    return [name for name in needed if name not in ALLOWED_NEEDED]


def _fetch(url: str, sha256: str, dest_dir: Path) -> Path:
    archive = dest_dir / Path(url).name
    if not archive.is_file():
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            archive.write_bytes(resp.read())
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != sha256:
        archive.unlink()
        raise RuntimeError(f"sha256 mismatch for {url}: expected {sha256}, got {actual}")
    return archive


def _readelf(args: list[str], path: Path) -> str:
    readelf = shutil.which("readelf")
    if not readelf:
        raise RuntimeError("host readelf is required")
    return subprocess.run([readelf, *args, str(path)], check=True, capture_output=True, text=True).stdout


def validate_package(package: Path) -> dict[str, object]:
    """Every shipped library: right soname, glibc <= 2.17, closed NEEDED set."""
    report: dict[str, object] = {}
    for name in SHIPPED:
        path = package / "lib" / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"package missing regular file lib/{name}")
        dynamic = _readelf(["-d"], path)
        if parse_soname(dynamic) != name:
            raise RuntimeError(f"lib/{name} has soname {parse_soname(dynamic)!r}")
        extra = unexpected_needed(parse_needed(dynamic))
        if extra:
            raise RuntimeError(f"lib/{name} needs libraries the bundle cannot provide: {extra}")
        measured = max_glibc(_readelf(["--version-info"], path))
        if measured and measured > GLIBC_FLOOR:
            raise RuntimeError(f"lib/{name} requires GLIBC_{measured[0]}.{measured[1]} > 2.17")
        report[name] = {"needed": parse_needed(dynamic), "max_glibc": measured and f"{measured[0]}.{measured[1]}"}
    return report


def build_bundle(*, version: str, shape: str, build_folder: Path, output) -> dict[str, object]:
    if version not in PINNED_VERSIONS:
        raise ValueError(f"unsupported version {version}; supported: {PINNED_VERSIONS}")
    if shape not in MANYLINUX2014_IMAGES:
        raise ValueError(f"unsupported shape {shape}")
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required to build against glibc 2.17")
    work = Path(build_folder).resolve()
    src = work / "src"
    for path in (src, work / "stage", work / "package"):
        if path.exists():
            shutil.rmtree(path)
    src.mkdir(parents=True)
    for url, sha256 in SOURCES.values():
        archive = _fetch(url, sha256, work)
        with tarfile.open(archive) as tf:
            tf.extractall(src)
    output.info(f"building libxml2 {LIBXML2_VERSION} + zlib {ZLIB_VERSION} for {shape}")
    subprocess.run(
        docker_command(shape, work, uid_gid=f"{os.getuid()}:{os.getgid()}"), check=True
    )
    lib = work / "package" / "lib"
    lib.mkdir(parents=True)
    for name in SHIPPED:
        shutil.copy2((work / "stage" / "lib" / name).resolve(), lib / name)
    for license_src, license_name in (
        (src / f"libxml2-{LIBXML2_VERSION}" / "Copyright", "LICENSE.libxml2"),
        (src / f"zlib-{ZLIB_VERSION}" / "LICENSE", "LICENSE.zlib"),
    ):
        if license_src.is_file():
            shutil.copy2(license_src, work / "package" / license_name)
    report = validate_package(work / "package")
    meta = {
        "tool": "llvm-host-libs",
        "version": version,
        "shape": shape,
        "sources": {name: {"url": url, "sha256": sha} for name, (url, sha) in SOURCES.items()},
        "image": MANYLINUX2014_IMAGES[shape],
        "libraries": report,
    }
    (work / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta
