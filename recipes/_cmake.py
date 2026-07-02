"""Shared helpers for the cmake-* prebuilt-repackage recipes.

Each per-shape recipe (recipes/cmake-windows-x64/, cmake-darwin-arm64/,
...) is a thin shim that imports `extract_bundle` from this module and
passes its own `SHAPE` constant. Same code shape as
recipes/_python_pbs.py: download an upstream prebuilt archive, extract
a whitelisted subset into `build_folder/package/`, write `meta.json`
for ingest provenance.

Sourced from official [Kitware/CMake](https://github.com/Kitware/CMake)
release archives. `PINNED_VERSIONS` pins the versions we have verified
the asset names for (checked 2026-07-01 against the v4.3.4 release via
`gh release view v4.3.4 --repo Kitware/CMake --json assets`); bump the
tuple when a newer CMake should be dispatchable.

Upstream asset naming (v4.3.4, verified):

    cmake-<ver>-windows-x86_64.zip      windows-x64
    cmake-<ver>-windows-arm64.zip       windows-arm64
    cmake-<ver>-macos-universal.tar.gz  darwin-x64 AND darwin-arm64
                                        (universal binary; both shapes
                                        repackage the same archive)
    cmake-<ver>-linux-x86_64.tar.gz     linux-x64-gnu
    cmake-<ver>-linux-aarch64.tar.gz    linux-arm64-gnu

No musl shapes: Kitware's Linux binaries link glibc.

In-archive layout: every archive has a single top-level directory named
after the archive stem (e.g. `cmake-4.3.4-linux-x86_64/`). The macOS
archive additionally nests everything under `CMake.app/Contents/`,
which we strip so the package root is uniform across shapes.

What we keep (whitelist):

    bin/cmake(.exe), bin/ctest(.exe), bin/cpack(.exe)
    share/cmake-<major.minor>/...   the FULL module tree — cmake is
                                    useless without its Modules/

What we drop: doc/, man/, share/aclocal, share/bash-completion,
share/emacs, share/vim, bin/cmake-gui + bin/ccmake, and the macOS
.app scaffolding. Keeps the catalogue blob small.
"""

from __future__ import annotations

import io
import json
import tarfile
import urllib.request
import zipfile
from pathlib import Path


PINNED_VERSIONS = ("4.3.4",)

# shape → upstream release asset name template.
SHAPE_ASSETS = {
    "windows-x64": "cmake-{ver}-windows-x86_64.zip",
    "windows-arm64": "cmake-{ver}-windows-arm64.zip",
    # The macOS archive is a universal binary — both darwin shapes
    # repackage the same upstream asset.
    "darwin-x64": "cmake-{ver}-macos-universal.tar.gz",
    "darwin-arm64": "cmake-{ver}-macos-universal.tar.gz",
    "linux-x64-gnu": "cmake-{ver}-linux-x86_64.tar.gz",
    "linux-arm64-gnu": "cmake-{ver}-linux-aarch64.tar.gz",
}

_KEEP_BINARIES = frozenset(
    {
        "cmake",
        "cmake.exe",
        "ctest",
        "ctest.exe",
        "cpack",
        "cpack.exe",
    }
)


def supported_shapes() -> tuple[str, ...]:
    return tuple(sorted(SHAPE_ASSETS.keys()))


def _archive_prefix(asset_name: str, shape: str) -> str:
    """Top-level in-archive prefix to strip.

    All archives wrap their content in `<archive-stem>/`; the macOS
    universal archive additionally nests under `CMake.app/Contents/`.
    """
    stem = asset_name
    for suffix in (".tar.gz", ".zip"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if shape.startswith("darwin-"):
        return f"{stem}/CMake.app/Contents/"
    return f"{stem}/"


def _keep(rel: str, share_prefix: str) -> bool:
    if rel.startswith("bin/") and Path(rel).name in _KEEP_BINARIES:
        return True
    if rel.startswith(share_prefix):
        return True
    return False


def extract_bundle(
    *,
    version: str,
    shape: str,
    build_folder: Path,
    output,
) -> dict:
    """Fetch the Kitware prebuilt for ``(version, shape)`` and extract
    `bin/{cmake,ctest,cpack}` + the full `share/cmake-<maj.min>/` tree
    into ``build_folder/package/``. Returns the meta-dict written to
    ``meta.json`` for ingest provenance."""
    asset_tpl = SHAPE_ASSETS.get(shape)
    if asset_tpl is None:
        raise ValueError(
            f"unsupported cmake shape {shape}; supported: {supported_shapes()}"
        )
    asset_name = asset_tpl.format(ver=version)
    url = (
        f"https://github.com/Kitware/CMake/releases/download/"
        f"v{version}/{asset_name}"
    )
    output.info(f"fetching {url}")
    with urllib.request.urlopen(url, timeout=600) as resp:
        data = resp.read()
    output.info(f"downloaded {len(data)} bytes; extracting bundle subset")

    out_root = build_folder / "package"
    out_root.mkdir(parents=True, exist_ok=True)

    prefix = _archive_prefix(asset_name, shape)
    major_minor = ".".join(version.split(".")[:2])
    share_prefix = f"share/cmake-{major_minor}/"

    if asset_name.endswith(".zip"):
        extracted_count = _extract_zip(data, out_root, prefix, share_prefix)
    else:
        extracted_count = _extract_tar_gz(data, out_root, prefix, share_prefix)

    output.info(f"extracted {extracted_count} files into package/")
    if extracted_count == 0:
        raise RuntimeError(
            f"no files extracted from cmake archive for shape={shape}; "
            f"archive layout may have changed (expected prefix {prefix!r} "
            f"with bin/ + {share_prefix})."
        )

    exe_suffix = ".exe" if shape.startswith("windows-") else ""
    for binary in ("cmake", "ctest", "cpack"):
        expected = out_root / "bin" / f"{binary}{exe_suffix}"
        if not expected.is_file():
            raise RuntimeError(
                f"cmake bundle for shape={shape} is missing {expected.name} "
                "under bin/ — upstream archive layout may have changed."
            )
    modules_dir = out_root / share_prefix / "Modules"
    if not modules_dir.is_dir():
        raise RuntimeError(
            f"cmake bundle for shape={shape} is missing "
            f"{share_prefix}Modules/ — cmake is useless without its module "
            "tree; upstream archive layout may have changed."
        )

    meta = {
        "tool": "cmake",
        "cmake_version": version,
        "shape": shape,
        "asset_name": asset_name,
        "source_url": url,
    }
    (build_folder / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta


def _extract_zip(
    data: bytes, out_root: Path, prefix: str, share_prefix: str
) -> int:
    extracted_count = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not rel or not _keep(rel, share_prefix):
                continue
            target = out_root / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            # Windows zips carry no useful unix modes; mark bin/
            # entries executable so a cross-host consumer can run
            # them after extraction on a case-preserving FS.
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if rel.startswith("bin/") or (unix_mode & 0o111):
                target.chmod(0o755)
            extracted_count += 1
    return extracted_count


def _extract_tar_gz(
    data: bytes, out_root: Path, prefix: str, share_prefix: str
) -> int:
    extracted_count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf:
            name = member.name
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not rel or not _keep(rel, share_prefix):
                continue
            target = out_root / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.issym() or member.islnk():
                try:
                    if target.exists() or target.is_symlink():
                        target.unlink()
                    target.symlink_to(member.linkname)
                except OSError:
                    pass
                continue
            buf = tf.extractfile(member)
            if buf is None:
                continue
            target.write_bytes(buf.read())
            if member.mode & 0o111:
                target.chmod(0o755)
            extracted_count += 1
    return extracted_count
