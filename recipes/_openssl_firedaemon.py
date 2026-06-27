"""Shared helper for the openssl-windows-* recipes (soldr#943).

Source: FireDaemon's OpenSSL mirror at https://download.firedaemon.com/
Single ZIP per release contains x64 + arm64 (and legacy x86) subdirs,
each with the same `bin/`, `lib/`, `include/` layout. We pick one
subdir per per-target-arch recipe.

Licensing: FireDaemon repackages the upstream OpenSSL project releases
under the same license terms as upstream OpenSSL (Apache 2.0 since 3.x).
They are a commercial vendor but the OpenSSL packages are free for any
use. Provenance is auditable via the upstream OpenSSL source they build.

The alternate source recommended by soldr#943 is slproweb's Win64OpenSSL
installer, but that's an NSIS self-extractor that needs `7z x` (i.e. a
p7zip-full apt install in the runner). FireDaemon's plain zip is easier
to handle in pure-Python urllib + zipfile and produces functionally
equivalent libraries (both build from upstream OpenSSL source with MSVC).

If FireDaemon's mirror is ever taken down, swap to slproweb and add a
`p7zip-full` apt install step + invoke `7z x` from `subprocess`.
"""

from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from pathlib import Path


# Pinned URL pattern. FireDaemon publishes openssl-<ver>.zip per release;
# version pinned at dispatch time via the `version` workflow input.
FIREDAEMON_URL_TPL = "https://download.firedaemon.com/FireDaemon-OpenSSL/openssl-{version}.zip"


def extract_arch(
    *,
    version: str,
    arch_dir: str,
    target_triple: str,
    build_folder: Path,
    output,
) -> dict:
    """Download the FireDaemon OpenSSL zip, extract `<arch_dir>/lib/`
    + `<arch_dir>/include/` (and `<arch_dir>/bin/` for the OpenSSL DLLs
    in case dynamic-linkage is preferred downstream) into
    `build_folder/package/`. Returns the meta dict to write to
    `meta.json` for ingest provenance.
    """
    url = FIREDAEMON_URL_TPL.format(version=version)
    output.info(f"fetching {url}")
    with urllib.request.urlopen(url, timeout=300) as resp:
        data = resp.read()
    output.info(f"downloaded {len(data)} bytes; extracting {arch_dir}/")

    out_root = build_folder / "package"
    out_root.mkdir(parents=True, exist_ok=True)

    prefix = f"{arch_dir}/"
    keep_subdirs = ("bin/", "lib/", "include/")
    extracted = 0

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not any(rel.startswith(p) for p in keep_subdirs):
                continue
            if info.is_dir():
                (out_root / rel).mkdir(parents=True, exist_ok=True)
                continue
            target = out_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src:
                target.write_bytes(src.read())
            extracted += 1

    if extracted == 0:
        raise RuntimeError(
            f"no files extracted for arch_dir={arch_dir!r}; "
            f"FireDaemon zip layout may have changed."
        )
    output.info(f"extracted {extracted} files into package/")

    meta = {
        "openssl_version": version,
        "target_triple": target_triple,
        "source_url": url,
        "arch_dir": arch_dir,
    }
    (build_folder / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta
