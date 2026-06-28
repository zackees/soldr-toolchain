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

Inside the tarball (verified 2026-06-28 against PBS 20241016 archives):

    python/include/...                 Python.h + CAPI headers (all platforms)
    python/libs/python3.lib            Windows (with `s`) — stable ABI import
    python/libs/python313.lib          Windows — versioned import
    python/lib/libpython3.13.so        Linux gnu
    python/lib/libpython3.13.dylib     Darwin

Important: top-level prefix is `python/`, **not** `python/install/`.
Earlier versions of this helper assumed a `python/install/<...>` layout
which was wrong for `install_only.tar.gz` archives — that path is only
present in the `full` archive variants. With the wrong prefix the
recipe silently extracted nothing and forge's "package payload below
10240 bytes" gate caught it. Fixed by dropping the bogus `install/`
component.

The extractor whitelists `include/` + the per-platform `lib/`-style
directory (filtered to keep only `libpython*` libs on Unix, dropping
the stdlib tree), so the resulting catalogue blob is small.
"""

from __future__ import annotations

import io
import json
import tarfile
import urllib.request
from pathlib import Path


PBS_TAGS = {
    # 20260623 is the first PBS release that ships
    # aarch64-pc-windows-msvc + aarch64-unknown-linux-musl, both of
    # which the soldr#1006 Lane 1/2 win + arm64 musl recipes need.
    # 3.13.14 is the most-recent 3.13.x in that release; new
    # dispatches should prefer these entries.
    "3.13.14": "20260623",
    "3.12.13": "20260623",
    "3.11.15": "20260623",
    "3.10.20": "20260623",
    # Legacy 20241016 entries kept for backwards-compat with any
    # external dispatch still pinned to the older versions. These
    # tags don't ship win-arm64 / linux-arm64-musl.
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

    # Whitelist the dirs we care about. The PBS `install_only`
    # archives have `python/` as the top-level prefix (NOT
    # `python/install/` — that's the `full` archive variant).
    # We keep:
    #   include/  (all platforms)
    #   libs/     (Windows-only — the MSVC import libs)
    #   lib/libpython*  (Unix — explicitly excludes the python3.13/
    #                    stdlib tree which lives under `lib/` too)
    # Everything else (bin/, DLLs/, Lib/, share/, the interpreter
    # itself, the full stdlib tree) is dropped.
    prefix = "python/"

    def _keep(rel: str) -> str | None:
        """Return the destination relative path, or None to skip."""
        if rel.startswith("include/"):
            return rel
        if rel.startswith("libs/"):
            # libs/ → lib/ for soldr-side ergonomics (consumer always
            # looks under `lib/`).
            return "lib/" + rel[len("libs/"):]
        if rel.startswith("lib/lib"):
            return rel
        return None

    extracted_count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf:
            name = member.name
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            dest_rel = _keep(rel)
            if dest_rel is None:
                continue
            target = out_root / dest_rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            buf = tf.extractfile(member)
            if buf is None:
                continue
            target.write_bytes(buf.read())
            extracted_count += 1

    output.info(f"extracted {extracted_count} files into package/")
    if extracted_count == 0:
        raise RuntimeError(
            f"no files extracted from PBS archive for triple={target_triple}; "
            "tarball layout may have changed (expected `python/include/` + "
            "`python/libs/` on Windows or `python/lib/libpython*` on Unix)."
        )

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
