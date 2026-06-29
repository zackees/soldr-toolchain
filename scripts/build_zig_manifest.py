#!/usr/bin/env python3
"""Generate ``zig/manifest.json`` directly in v1 catalogue format.

Unlike ``build_manifest.py`` (which queries GitHub Releases for the
five tracked tools), zig is distributed via ziglang.org's own
infrastructure — there is no GitHub Release to consume. Upstream
publishes a canonical JSON index at
``https://ziglang.org/download/index.json`` that pins ``shasum``,
``size``, and ``tarball`` URL per ``<version> × <platform>``.

This script consumes that index and produces a v1 ``zig/manifest.json``
matching the existing schema (compatible with ``cargo-zigbuild``'s
per-tool catalog written by the v5 → v1 converter).

The producer writes ONLY ``<output-dir>/zig/manifest.json``. The
top-level ``manifest.json`` Index is left to ``convert_v5_to_v1.py``,
whose ``_preserve_vendored_entries`` pass automatically re-computes
the descriptor sha256 + size from the on-disk catalog content — so
the Index stays consistent without a separate step here.

Workflow integration: run this BEFORE ``convert_v5_to_v1.py`` so the
converter's vendored-entry pass sees the fresh content.

Used by ``zackees/soldr`` for the ``aarch64-unknown-linux-musl``
release lane via ``cargo zigbuild``. See soldr-toolchain#32 for the
backstory.

Runtime deps: stdlib only (urllib, hashlib, json, argparse). Matches
the producer policy in CLAUDE.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

SCHEMA_URL = "https://zackees.github.io/manifest.json/v1/manifest.schema.json"
ONLINE_BASE = "https://zackees.github.io/soldr-toolchain"
TOOL_NAME = "zig"
ZIG_INDEX_URL = "https://ziglang.org/download/index.json"
V1_SCHEMA_VERSION = 1

# Map ziglang.org's `<arch>-<os>` platform keys to the v1 catalogue
# schema. Platforms not in this map are intentionally skipped — the
# catalogue surfaces the same six lanes as cargo-zigbuild does, no
# 32-bit / freebsd / netbsd / riscv64 / loongarch / s390x / powerpc
# (matching the schema rule documented in build_manifest.py:
#   "32-bit lanes (i686 / armv7) are intentionally not surfaced").
PLATFORM_MAP: dict[str, dict[str, str]] = {
    "x86_64-linux":   {"os": "linux",   "arch": "x86_64"},
    "aarch64-linux":  {"os": "linux",   "arch": "aarch64"},
    "x86_64-macos":   {"os": "darwin",  "arch": "x86_64"},
    "aarch64-macos":  {"os": "darwin",  "arch": "aarch64"},
    "x86_64-windows": {"os": "windows", "arch": "x86_64"},
    "aarch64-windows":{"os": "windows", "arch": "aarch64"},
}

# Channels published in the manifest. Always include latest-stable; the
# pinned channel falls back to the same value (consumers that resolve
# either get the same result).
DEFAULT_KEEP_N_STABLE = 5


def fetch_index(url: str = ZIG_INDEX_URL) -> dict[str, Any]:
    """Pull the canonical zig release index from ziglang.org."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "soldr-toolchain-zig-manifest-builder")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise RuntimeError(
            f"failed to fetch zig index from {url}: {exc}"
        ) from exc


def is_stable_version(version: str) -> bool:
    """True when ``version`` is a stable release (no ``master`` / no pre-tag).

    zig's index publishes:
      * ``master`` — the rolling dev tip; skip
      * ``<x.y.z>`` — stable; keep
      * ``<x.y.z>-dev.<n>+<sha>`` — pre-release; skip
    """
    if version == "master":
        return False
    if "-" in version:
        return False
    parts = version.split(".")
    if len(parts) != 3:
        return False
    return all(p.isdigit() for p in parts)


def parse_version_tuple(version: str) -> tuple[int, int, int]:
    """Sort key for stable versions. Pre-checked by ``is_stable_version``."""
    a, b, c = version.split(".")
    return (int(a), int(b), int(c))


def select_stable_versions(
    index: dict[str, Any],
    keep_n: int = DEFAULT_KEEP_N_STABLE,
) -> list[str]:
    """Pick the N most-recent stable versions, newest-first."""
    stables = [v for v in index.keys() if is_stable_version(v)]
    stables.sort(key=parse_version_tuple, reverse=True)
    return stables[:keep_n]


def derive_published_at(entry: dict[str, Any]) -> str:
    """Convert zig's ``date`` field (``YYYY-MM-DD``) to ISO-8601 UTC.

    Falls back to empty string when ``date`` is missing or malformed —
    matching how v5→v1 conversion handles missing `published_at`.
    """
    date = entry.get("date", "")
    if not date:
        return ""
    # ziglang.org publishes day-precision; pad to midnight UTC so the
    # ISO timestamp is parseable as an `Instant` in consumer code.
    if "T" not in date:
        return f"{date}T00:00:00Z"
    return date


def build_release_entry(
    version: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Render one zig release (one version × N platforms) in v1 shape.

    Skips platforms not in ``PLATFORM_MAP``. If NO platforms survive the
    filter, returns a release with an empty ``platforms`` list — the
    caller should drop it.
    """
    platforms: list[dict[str, Any]] = []
    for upstream_key, plat in PLATFORM_MAP.items():
        info = entry.get(upstream_key)
        if not isinstance(info, dict):
            continue
        tarball = info.get("tarball", "")
        sha256 = info.get("shasum", "")
        size_raw = info.get("size", "0")
        try:
            size = int(size_raw)
        except (TypeError, ValueError):
            size = 0
        if not tarball:
            # Defensive: ziglang.org's index has shipped every
            # documented platform for every stable release we use, but
            # if a future release omits one we skip rather than emit
            # a half-broken entry.
            print(
                f"  zig {version}: missing tarball for {upstream_key}; skipping",
                file=sys.stderr,
            )
            continue
        filename = tarball.rsplit("/", 1)[-1]
        asset_out: dict[str, Any] = {
            "filename":   filename,
            "size_bytes": size,
            "urls":       [tarball],
        }
        if sha256:
            asset_out["sha256"] = sha256
        platforms.append({
            "platform": plat,
            "asset":    asset_out,
        })
    return {
        "schema_version":     V1_SCHEMA_VERSION,
        "version":            version,
        "published_at":       derive_published_at(entry),
        "min_client_version": 1,
        "platforms":          platforms,
        "source": {
            "kind":      "upstream-direct",
            "vendor":    "ziglang.org",
            "index_url": ZIG_INDEX_URL,
        },
    }


def build_catalog(
    index: dict[str, Any],
    keep_n: int = DEFAULT_KEEP_N_STABLE,
) -> dict[str, Any]:
    """Render the full zig per-tool catalog (v1 ``Catalog`` document)."""
    versions = select_stable_versions(index, keep_n=keep_n)
    if not versions:
        raise RuntimeError(
            "no stable zig versions found in index — refusing to write "
            "an empty manifest"
        )
    releases: list[dict[str, Any]] = []
    for v in versions:
        entry = index.get(v)
        if not isinstance(entry, dict):
            continue
        release = build_release_entry(v, entry)
        if not release["platforms"]:
            print(
                f"  zig {v}: no surfaced platforms; dropping release",
                file=sys.stderr,
            )
            continue
        releases.append(release)
    if not releases:
        raise RuntimeError(
            "all candidate zig releases were dropped during filtering — "
            "refusing to write an empty manifest"
        )
    latest = releases[0]["version"]
    return {
        "$schema":        SCHEMA_URL,
        "kind":           "Catalog",
        "schema_version": V1_SCHEMA_VERSION,
        "tool":           TOOL_NAME,
        "online_url":     f"{ONLINE_BASE}/{TOOL_NAME}/manifest.json",
        "channels":       {
            "latest-stable": latest,
            "stable":        latest,
        },
        "releases":       releases,
    }


def write_if_changed(path: Path, doc: dict[str, Any]) -> bool:
    """Write ``doc`` as pretty JSON to ``path``. Returns True iff the
    file changed. Matches build_manifest.py's idempotency semantics so
    the nightly workflow's ``git diff --cached --quiet`` correctly
    detects no-op runs.
    """
    new_text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    new_bytes = new_text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            old_bytes = path.read_bytes()
        except OSError:
            old_bytes = b""
        if old_bytes == new_bytes:
            return False
    path.write_bytes(new_bytes)
    return True


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help=(
            "Root of the assets-branch checkout. The script writes "
            "<output-dir>/zig/manifest.json. The top-level "
            "manifest.json is left for convert_v5_to_v1.py to update."
        ),
    )
    p.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP_N_STABLE,
        help=(
            f"Number of most-recent stable releases to include "
            f"(default: {DEFAULT_KEEP_N_STABLE})."
        ),
    )
    p.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help=(
            "Optional local index.json fixture to consume instead of "
            "fetching from ziglang.org. Used by unit tests."
        ),
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.fixture is not None:
        index = json.loads(args.fixture.read_text(encoding="utf-8"))
    else:
        print(f"fetching zig release index from {ZIG_INDEX_URL}", file=sys.stderr)
        index = fetch_index()

    catalog = build_catalog(index, keep_n=args.keep)
    output_path = args.output_dir / TOOL_NAME / "manifest.json"
    changed = write_if_changed(output_path, catalog)
    if changed:
        print(
            f"  wrote {output_path} "
            f"({len(catalog['releases'])} releases, channel latest-stable="
            f"{catalog['channels']['latest-stable']})",
            file=sys.stderr,
        )
    else:
        print(f"  unchanged: {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
