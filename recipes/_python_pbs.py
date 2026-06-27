"""Shared helpers for the python-* PBS sysroot recipes (soldr#931 / #932 / #933).

Each per-target recipe (recipes/python-windows-x64/, python-darwin-arm64/, …)
is a thin shim that imports `extract_sysroot` from this module and passes
its own `TARGET_TRIPLE` constant. Same code shape, different download URL
+ slightly different in-archive layout (Windows uses `install/libs/`,
Unix uses `install/lib/`).

Sourced from [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone)
releases. The `PBS_TAGS` table below pins each supported Python version to
a tested release tag; bump when a newer PBS release ships and the recipes
should pick it up.

The PBS naming convention is:

    cpython-<py_version>+<pbs_tag>-<triple>-install_only.tar.gz

Inside the tarball:

    python/install/include/...           Python.h + CAPI headers (all platforms)
    python/install/libs/python3.lib      Windows (with `s`) — stable ABI import
    python/install/libs/python313.lib    Windows — versioned import
    python/install/lib/libpython3.13.so  Linux gnu
    python/install/lib/libpython3.13.dylib  Darwin

The extractor whitelists just `include/` + the per-platform `lib/`-style
directory, dropping the interpreter binary + the `Lib/` (stdlib Python
source) tree so the resulting catalogue blob is small.
"""

from __future__ import annotations

import io
import json
import tarfile
import urllib.request
from pathlib import Path


PBS_TAGS = {
    "3.13.0": "20241016",
    "3.12.7": "20241016",
    "3.11.10": "20241016",
    "3.10.15": "20241016",
}


def supported_versions() -> tuple[str, ...]:
    return tuple(sorted(PBS_TAGS.keys()))


def extract_sysroot(
    *,
    py_version: str,
    target_triple: str,
    build_folder: Path,
    output,
) -> dict:
    """Fetch the PBS archive for ``(py_version, target_triple)`` and
    extract just `include/` + `lib/` (or `libs/` on Windows) into
    ``build_folder/package/``. Returns the meta-dict to be written to
    ``meta.json`` for ingest provenance."""
    pbs_tag = PBS_TAGS.get(py_version)
    if pbs_tag is None:
        raise ValueError(
            f"unsupported python version {py_version}; "
            f"supported: {sorted(PBS_TAGS.keys())}"
        )
    archive_name = f"cpython-{py_version}+{pbs_tag}-{target_triple}-install_only.tar.gz"
    url = (
        f"https://github.com/astral-sh/python-build-standalone/releases/"
        f"download/{pbs_tag}/{archive_name}"
    )
    output.info(f"fetching {url}")
    with urllib.request.urlopen(url, timeout=180) as resp:
        data = resp.read()
    output.info(f"downloaded {len(data)} bytes; extracting sysroot subset")

    out_root = build_folder / "package"
    out_root.mkdir(parents=True, exist_ok=True)

    # Whitelist the dirs we care about. Windows libs/ → lib/ (for
    # consistent soldr-side ergonomics — the consumer always looks
    # under `lib/`).
    whitelist = ("libs/", "lib/", "include/")
    prefix = "python/install/"

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf:
            name = member.name
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not any(rel.startswith(p) for p in whitelist):
                continue
            if rel.startswith("libs/"):
                rel = "lib/" + rel[len("libs/"):]
            target = out_root / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            buf = tf.extractfile(member)
            if buf is None:
                continue
            target.write_bytes(buf.read())

    meta = {
        "python_version": py_version,
        "pbs_tag": pbs_tag,
        "target_triple": target_triple,
        "source_url": url,
    }
    (build_folder / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta
