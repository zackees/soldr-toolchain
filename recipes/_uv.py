"""Shared helpers for the uv-* prebuilt-repackage recipes.

Each per-shape recipe (recipes/uv-windows-x64/, uv-darwin-arm64/, ...)
is a thin shim that imports `extract_bundle` from this module and
passes its own `SHAPE` constant. Same code shape as
recipes/_cmake.py / recipes/_ninja.py: download an upstream prebuilt
archive, repackage into `build_folder/package/`, write `meta.json`
for ingest provenance.

Sourced from official [astral-sh/uv](https://github.com/astral-sh/uv)
release archives. `PINNED_VERSIONS` pins the versions we have verified
the asset names for (checked 2026-07-01 against the 0.11.26 release via
`gh release view 0.11.26 --repo astral-sh/uv --json assets`); bump the
tuple when a newer uv should be dispatchable.

NOTE: uv release tags have NO `v` prefix (the tag is `0.11.26`, not
`v0.11.26`) — the download URL below reflects that.

Upstream asset naming (0.11.26, verified — version-independent
filenames, the version only appears in the release tag):

    uv-x86_64-pc-windows-msvc.zip        windows-x64
    uv-aarch64-pc-windows-msvc.zip       windows-arm64
    uv-x86_64-apple-darwin.tar.gz        darwin-x64
    uv-aarch64-apple-darwin.tar.gz       darwin-arm64
    uv-x86_64-unknown-linux-gnu.tar.gz   linux-x64-gnu
    uv-aarch64-unknown-linux-gnu.tar.gz  linux-arm64-gnu
    uv-x86_64-unknown-linux-musl.tar.gz  linux-x64-musl
    uv-aarch64-unknown-linux-musl.tar.gz linux-arm64-musl

All eight shapes: unlike cmake/ninja, uv ships musl Linux builds
upstream, so both musl shapes are included.

In-archive layout: the `.tar.gz` archives wrap their content in a
`uv-<triple>/` directory containing `uv` + `uvx`; the Windows zips are
flat (`uv.exe`, `uvw.exe`, `uvx.exe` at the root). We repackage as
`bin/uv(.exe)` plus `bin/uvx(.exe)` when present, so consumers get the
same `bin/` layout as every other tool bundle in the catalogue (the
`uvw.exe` windowed launcher is dropped).
"""

from __future__ import annotations

import io
import json
import tarfile
import urllib.request
import zipfile
from pathlib import Path


PINNED_VERSIONS = ("0.11.26",)

# shape → upstream release asset name.
SHAPE_ASSETS = {
    "windows-x64": "uv-x86_64-pc-windows-msvc.zip",
    "windows-arm64": "uv-aarch64-pc-windows-msvc.zip",
    "darwin-x64": "uv-x86_64-apple-darwin.tar.gz",
    "darwin-arm64": "uv-aarch64-apple-darwin.tar.gz",
    "linux-x64-gnu": "uv-x86_64-unknown-linux-gnu.tar.gz",
    "linux-arm64-gnu": "uv-aarch64-unknown-linux-gnu.tar.gz",
    "linux-x64-musl": "uv-x86_64-unknown-linux-musl.tar.gz",
    "linux-arm64-musl": "uv-aarch64-unknown-linux-musl.tar.gz",
}


def supported_shapes() -> tuple[str, ...]:
    return tuple(sorted(SHAPE_ASSETS.keys()))


def extract_bundle(
    *,
    version: str,
    shape: str,
    build_folder: Path,
    output,
) -> dict:
    """Fetch the upstream uv archive for ``(version, shape)`` and place
    the binaries at ``build_folder/package/bin/uv(.exe)`` (+ ``uvx``
    when the archive ships it). Returns the meta-dict written to
    ``meta.json`` for ingest provenance."""
    asset_name = SHAPE_ASSETS.get(shape)
    if asset_name is None:
        raise ValueError(
            f"unsupported uv shape {shape}; supported: {supported_shapes()}"
        )
    # NOTE: no `v` prefix on the release tag.
    url = (
        f"https://github.com/astral-sh/uv/releases/download/"
        f"{version}/{asset_name}"
    )
    output.info(f"fetching {url}")
    with urllib.request.urlopen(url, timeout=600) as resp:
        data = resp.read()
    output.info(f"downloaded {len(data)} bytes; repackaging as bin/uv")

    exe_suffix = ".exe" if shape.startswith("windows-") else ""
    # uv is mandatory; uvx is packaged when present (it is in every
    # 0.11.26 archive, but treat it as best-effort so an upstream
    # layout tweak doesn't brick the required binary).
    wanted = {f"uv{exe_suffix}", f"uvx{exe_suffix}"}
    out_root = build_folder / "package"
    bin_dir = out_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    if asset_name.endswith(".zip"):
        extracted = _extract_zip(data, bin_dir, wanted)
    else:
        extracted = _extract_tar_gz(data, bin_dir, wanted)

    if f"uv{exe_suffix}" not in extracted:
        raise RuntimeError(
            f"no uv{exe_suffix} found in {asset_name} for shape={shape}; "
            "upstream archive layout may have changed."
        )
    output.info(f"packaged {', '.join(f'bin/{name}' for name in sorted(extracted))}")

    meta = {
        "tool": "uv",
        "uv_version": version,
        "shape": shape,
        "asset_name": asset_name,
        "source_url": url,
    }
    (build_folder / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta


def _extract_zip(data: bytes, bin_dir: Path, wanted: set[str]) -> set[str]:
    """The Windows zips are flat (`uv.exe` / `uvx.exe` at the root);
    tolerate a directory prefix in case that ever changes."""
    extracted: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if name not in wanted:
                continue
            target = bin_dir / name
            target.write_bytes(zf.read(info))
            target.chmod(0o755)
            extracted.add(name)
    return extracted


def _extract_tar_gz(data: bytes, bin_dir: Path, wanted: set[str]) -> set[str]:
    """The tar.gz archives wrap the binaries in a `uv-<triple>/`
    directory; match on basename so the wrapper name never matters."""
    extracted: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf:
            if not member.isfile():
                continue
            name = Path(member.name).name
            if name not in wanted:
                continue
            buf = tf.extractfile(member)
            if buf is None:
                continue
            target = bin_dir / name
            target.write_bytes(buf.read())
            target.chmod(0o755)
            extracted.add(name)
    return extracted
