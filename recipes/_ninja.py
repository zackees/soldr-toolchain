"""Shared helpers for the ninja-* prebuilt-repackage recipes.

Each per-shape recipe (recipes/ninja-windows-x64/, ninja-darwin-arm64/,
...) is a thin shim that imports `extract_bundle` from this module and
passes its own `SHAPE` constant. Same code shape as
recipes/_python_pbs.py / recipes/_cmake.py: download an upstream
prebuilt archive, repackage into `build_folder/package/`, write
`meta.json` for ingest provenance.

Sourced from official [ninja-build/ninja](https://github.com/ninja-build/ninja)
release archives. `PINNED_VERSIONS` pins the versions we have verified
the asset names for (checked 2026-07-01 against the v1.13.2 release via
`gh release view v1.13.2 --repo ninja-build/ninja --json assets`); bump
the tuple when a newer ninja should be dispatchable.

Upstream asset naming (v1.13.2, verified — note: version-independent
filenames, the version only appears in the release tag):

    ninja-win.zip           windows-x64
    ninja-winarm64.zip      windows-arm64
    ninja-mac.zip           darwin-x64 AND darwin-arm64 (universal binary)
    ninja-linux.zip         linux-x64-gnu
    ninja-linux-aarch64.zip linux-arm64-gnu

No musl shapes: ninja's upstream Linux binaries link glibc.

Each zip contains a single flat `ninja` / `ninja.exe` binary at the
archive root; we repackage it as `bin/ninja(.exe)` so consumers get
the same `bin/` layout as every other tool bundle in the catalogue.
"""

from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from pathlib import Path


PINNED_VERSIONS = ("1.13.2",)

# shape → upstream release asset name.
SHAPE_ASSETS = {
    "windows-x64": "ninja-win.zip",
    "windows-arm64": "ninja-winarm64.zip",
    # The macOS binary is universal — both darwin shapes repackage
    # the same upstream asset.
    "darwin-x64": "ninja-mac.zip",
    "darwin-arm64": "ninja-mac.zip",
    "linux-x64-gnu": "ninja-linux.zip",
    "linux-arm64-gnu": "ninja-linux-aarch64.zip",
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
    """Fetch the upstream ninja zip for ``(version, shape)`` and place
    the single binary at ``build_folder/package/bin/ninja(.exe)``.
    Returns the meta-dict written to ``meta.json`` for ingest
    provenance."""
    asset_name = SHAPE_ASSETS.get(shape)
    if asset_name is None:
        raise ValueError(
            f"unsupported ninja shape {shape}; supported: {supported_shapes()}"
        )
    url = (
        f"https://github.com/ninja-build/ninja/releases/download/"
        f"v{version}/{asset_name}"
    )
    output.info(f"fetching {url}")
    with urllib.request.urlopen(url, timeout=300) as resp:
        data = resp.read()
    output.info(f"downloaded {len(data)} bytes; repackaging as bin/ninja")

    binary_name = "ninja.exe" if shape.startswith("windows-") else "ninja"
    out_root = build_folder / "package"
    bin_dir = out_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    extracted = False
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # The upstream zips are flat (`ninja` / `ninja.exe` at the
            # root); tolerate a directory prefix in case that ever
            # changes.
            if Path(info.filename).name != binary_name:
                continue
            target = bin_dir / binary_name
            target.write_bytes(zf.read(info))
            target.chmod(0o755)
            extracted = True
            break

    if not extracted:
        raise RuntimeError(
            f"no {binary_name} found in {asset_name} for shape={shape}; "
            "upstream zip layout may have changed."
        )
    output.info(f"packaged bin/{binary_name}")

    meta = {
        "tool": "ninja",
        "ninja_version": version,
        "shape": shape,
        "asset_name": asset_name,
        "source_url": url,
    }
    (build_folder / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta
