"""Shared helper for the mingw-w64-gcc prebuilt recipe.

The first supported shape is ``windows-x64-gnu``: a Windows x64
MinGW-w64 GCC toolchain for Rust's ``x86_64-pc-windows-gnu`` target.

Source archive: WinLibs' standalone MinGW-w64 + GCC zip. WinLibs ships
matching .7z and .zip files; this recipe deliberately consumes the zip
so forge can extract with Python's standard library and does not depend
on a host 7-Zip binary.
"""

from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


PINNED_VERSIONS = ("15.3.0posix-14.0.0-msvcrt-r1",)

SHAPE_ASSETS = {
    "windows-x64-gnu": {
        "asset": "winlibs-x86_64-posix-seh-gcc-15.3.0-mingw-w64msvcrt-14.0.0-r1.zip",
        "thread_model": "posix",
        "exception_model": "seh",
        "runtime": "msvcrt",
        "gcc_version": "15.3.0",
        "mingw_w64_version": "14.0.0",
    },
}

REQUIRED_PATHS = (
    "bin/gcc.exe",
    "bin/g++.exe",
    "bin/ar.exe",
    "bin/ranlib.exe",
    "bin/ld.exe",
    "bin/windres.exe",
    "x86_64-w64-mingw32/include",
    "x86_64-w64-mingw32/lib",
    "lib/gcc/x86_64-w64-mingw32",
)


def supported_shapes() -> tuple[str, ...]:
    return tuple(sorted(SHAPE_ASSETS.keys()))


def extract_bundle(
    *,
    version: str,
    shape: str,
    build_folder: Path,
    output,
) -> dict:
    """Fetch and repackage the WinLibs archive for ``shape``.

    The upstream zip wraps everything under ``mingw64/``. The catalogue
    bundle strips that prefix so consumers see the standard layout:
    ``bin/gcc.exe``, ``include/``, ``lib/``, ``libexec/``, and the
    ``x86_64-w64-mingw32/`` target sysroot at package root.
    """

    cfg = SHAPE_ASSETS.get(shape)
    if cfg is None:
        raise ValueError(
            f"unsupported mingw-w64-gcc shape {shape}; supported: {supported_shapes()}"
        )
    if version not in PINNED_VERSIONS:
        raise ValueError(
            f"unsupported mingw-w64-gcc version {version}; supported: {PINNED_VERSIONS}"
        )

    asset_name = cfg["asset"]
    url = (
        "https://github.com/brechtsanders/winlibs_mingw/releases/download/"
        f"{version}/{asset_name}"
    )
    output.info(f"fetching {url}")
    with urllib.request.urlopen(url, timeout=600) as resp:
        data = resp.read()
    output.info(f"downloaded {len(data)} bytes; extracting mingw64/ payload")

    out_root = build_folder / "package"
    out_root.mkdir(parents=True, exist_ok=True)
    extracted_count = _extract_zip_payload(data, out_root)
    output.info(f"extracted {extracted_count} files into package/")
    if extracted_count == 0:
        raise RuntimeError(
            f"no files extracted from {asset_name}; expected a mingw64/ archive root"
        )

    _validate_package(out_root, shape)

    meta = {
        "tool": "mingw-w64-gcc",
        "version": version,
        "shape": shape,
        "asset_name": asset_name,
        "source_url": url,
        "upstream": "brechtsanders/winlibs_mingw",
        "gcc_version": cfg["gcc_version"],
        "mingw_w64_version": cfg["mingw_w64_version"],
        "thread_model": cfg["thread_model"],
        "exception_model": cfg["exception_model"],
        "runtime": cfg["runtime"],
    }
    (build_folder / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta


def _extract_zip_payload(data: bytes, out_root: Path) -> int:
    extracted_count = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith("mingw64/"):
                continue
            rel = name[len("mingw64/") :]
            if not rel:
                continue
            rel_path = PurePosixPath(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise RuntimeError(f"unsafe path in {name!r}")
            target = out_root.joinpath(*rel_path.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            if rel.startswith("bin/") or rel.startswith("libexec/"):
                target.chmod(0o755)
            extracted_count += 1
    return extracted_count


def _validate_package(out_root: Path, shape: str) -> None:
    missing = [rel for rel in REQUIRED_PATHS if not (out_root / rel).exists()]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"mingw-w64-gcc bundle for {shape} is missing required paths: {joined}"
        )
