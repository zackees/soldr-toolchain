#!/usr/bin/env python3
"""Convert soldr-toolchain's v5 asset tree to the unified manifest.json v1
schema (https://github.com/zackees/manifest.json).

Reads the v5 tree from --src (the working copy of the `assets` branch)
and writes a fresh v1 tree to --dest, with:
  - top-level manifest.json: kind=Index
  - <tool>/manifest.json:    kind=Catalog
  - index.html landing page (a small static site for GitHub Pages)
  - asset-index.json copied verbatim for backward compat / debugging

The script is pure data transformation — no network calls — so it runs
cross-platform on stock python3.

Platform-key normalization (npm-style flat -> orthogonal tuple):
  linux-x64         -> {os: linux,   arch: x86_64}
  linux-x64-gnu     -> {os: linux,   arch: x86_64, libc: glibc}
  linux-x64-musl    -> {os: linux,   arch: x86_64, libc: musl}
  linux-arm64-*     -> {os: linux,   arch: aarch64, libc?: ...}
  darwin-x64        -> {os: darwin,  arch: x86_64}
  darwin-arm64      -> {os: darwin,  arch: aarch64}
  darwin-universal2 -> {os: darwin,  arch: universal2}
  windows-x64-msvc  -> {os: windows, arch: x86_64,  abi: msvc}
  windows-x64-gnu   -> {os: windows, arch: x86_64,  abi: gnullvm}
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

V1_SCHEMA_VERSION = 1
ONLINE_BASE = "https://zackees.github.io/soldr-toolchain"
# Canonical pointer for IDE auto-validation and document self-verification.
# Pinned to /v1/ so consumers stay valid forever as the schema evolves.
SCHEMA_URL = "https://zackees.github.io/manifest.json/v1/manifest.schema.json"

# v5 short-arch -> v1 canonical arch
ARCH_MAP: dict[str, str] = {
    "x64": "x86_64",
    "arm64": "aarch64",
    "universal2": "universal2",
}

# v5 extra -> (v1 field, v1 value)
EXTRA_MAP: dict[str, tuple[str, str]] = {
    "gnu":     ("libc", "glibc"),
    "musl":    ("libc", "musl"),
    "msvc":    ("abi",  "msvc"),
    "gnullvm": ("abi",  "gnullvm"),
}


def split_platform_key(key: str) -> dict[str, str] | None:
    """`linux-x64-musl` -> `{os: linux, arch: x86_64, libc: musl}`.

    When the v5 key omits the extra ("linux-x64"), the npm convention is
    glibc on linux — so we make that explicit. Without this, a producer
    that ships both `linux-x64` and `linux-x64-musl` would have its bare
    entry shadow-match every musl query (the resolver's "missing field =
    wildcard" semantics would treat them as equally valid). Forcing the
    default to glibc preserves producer intent.
    """
    parts = key.split("-")
    if len(parts) < 2:
        return None
    os_name = parts[0]
    arch_short = parts[1]
    if arch_short not in ARCH_MAP:
        return None
    out: dict[str, str] = {"os": os_name, "arch": ARCH_MAP[arch_short]}
    if len(parts) >= 3:
        # Re-join in case extra itself has a dash (e.g. "gnueabihf")
        extra = "-".join(parts[2:])
        mapping = EXTRA_MAP.get(extra)
        if mapping:
            out[mapping[0]] = mapping[1]
        else:
            # Treat unrecognized extras as a free-form ABI tag.
            out["abi"] = extra
    elif os_name == "linux":
        out["libc"] = "glibc"
    return out


def index_assets_by_filename(asset_index: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Build a (owner, repo, tag) -> {filename -> entry} lookup table."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in asset_index.get("entries", []):
        key = (entry["owner"], entry["repo"], entry["tag"])
        out.setdefault(key, {})[entry["asset"]] = entry
    return out


def convert_per_tool_release(
    v5_release: dict[str, Any],
    asset_lookup: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    owner = v5_release.get("owner", "")
    repo = v5_release.get("repo", "")
    tag = v5_release.get("tag", "")
    by_filename = asset_lookup.get((owner, repo, tag), {})

    platforms_out: list[dict[str, Any]] = []
    for pkey, pval in (v5_release.get("platforms") or {}).items():
        platform_tuple = split_platform_key(pkey)
        if platform_tuple is None:
            # Skip unparseable keys but log to stderr.
            print(f"  skip {owner}/{repo}@{tag}: cannot parse platform key {pkey!r}",
                  file=sys.stderr)
            continue
        filename = pval.get("filename") or ""
        sha256 = (by_filename.get(filename) or {}).get("sha256", "")
        url = pval.get("url") or ""
        asset_out: dict[str, Any] = {
            "filename":   filename,
            "size_bytes": int(pval.get("size") or 0),
            "urls":       [url] if url else [],
        }
        if sha256:
            asset_out["sha256"] = sha256
        platforms_out.append({
            "platform": platform_tuple,
            "asset":    asset_out,
        })

    # Use the upstream `tag` as the v1 version so it round-trips against the
    # v5 top-level `latest` / `pinned` pointers (which also use the tag).
    # `version` (normalized, no "v" prefix) is dropped — channels always
    # resolve through the same string the producer keyed everything by.
    release_out: dict[str, Any] = {
        "schema_version":     V1_SCHEMA_VERSION,
        "version":            tag or v5_release.get("version") or "",
        "published_at":       v5_release.get("published_at", "") or "",
        "min_client_version": 1,
        "platforms":          platforms_out,
    }
    # Source fallback: GitHub repo + tag, when the release came from GH.
    if owner and repo and tag and owner != "vendored":
        release_out["source"] = {
            "vcs":         "git",
            "repo_url":    f"https://github.com/{owner}/{repo}",
            "ref":         tag,
            "archive_url": f"https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.tar.gz",
        }
    return release_out


def convert_tool_catalog(
    tool_name: str,
    v5_releases: list[dict[str, Any]],
    asset_lookup: dict[tuple[str, str, str], dict[str, Any]],
    top_level: dict[str, Any],
) -> dict[str, Any]:
    releases_v1 = [convert_per_tool_release(r, asset_lookup) for r in v5_releases]
    tool_meta = (top_level.get("tools") or {}).get(tool_name, {})
    latest = tool_meta.get("latest", "")
    pinned = tool_meta.get("pinned") or ""
    channels: dict[str, str] = {}
    if latest:
        channels["latest-stable"] = latest
        channels["stable"] = latest
    if pinned and pinned not in channels.values():
        channels["pinned"] = pinned
    return {
        "$schema":        SCHEMA_URL,
        "kind":           "Catalog",
        "schema_version": V1_SCHEMA_VERSION,
        "tool":           tool_name,
        "online_url":     f"{ONLINE_BASE}/{tool_name}/manifest.json",
        "channels":       channels,
        "releases":       releases_v1,
    }


def _kind_hint_for(tool_name: str, meta: dict[str, Any]) -> str:
    """Map an entry to a coarse-grained category surfaced via the v1
    ToolEntry.kind_hint field. Free-form; informational only.

    apple-sdk is a cross-compile sysroot (a resource bundle, not an
    invocable binary). Everything else we currently track is a tool the
    consumer actually runs.
    """
    if meta.get("kind") == "vendored-sdk" or tool_name == "apple-sdk":
        return "sysroot"
    return "tool"


def convert_index(
    v5_top_level: dict[str, Any],
    catalog_sha_lookup: dict[str, tuple[str, int]],
) -> dict[str, Any]:
    tools_out: dict[str, Any] = {}
    for tool_name, meta in (v5_top_level.get("tools") or {}).items():
        sha, size = catalog_sha_lookup.get(tool_name, ("", 0))
        descriptor: dict[str, Any] = {
            "url":        f"{tool_name}/manifest.json",
            "size_bytes": size,
            "media_type": "application/vnd.manifest.v1+json",
        }
        if sha:
            descriptor["sha256"] = sha
        owner = meta.get("owner", "")
        repo = meta.get("repo", "")
        summary = f"{owner}/{repo}" if owner and owner != "vendored" else repo
        tools_out[tool_name] = {
            "descriptor": descriptor,
            "summary":    summary,
            "kind_hint":  _kind_hint_for(tool_name, meta),
        }
    return {
        "$schema":        SCHEMA_URL,
        "kind":           "Index",
        "schema_version": V1_SCHEMA_VERSION,
        "tools":          tools_out,
    }


def write_json(path: Path, doc: dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    data = text.encode("utf-8")
    path.write_bytes(data)
    return data


def hash_sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src",  type=Path, required=True, help="v5 tree root (assets branch checkout)")
    p.add_argument("--dest", type=Path, required=True, help="output dir for v1 tree")
    args = p.parse_args()

    top_level_v5 = json.loads((args.src / "manifest.json").read_text(encoding="utf-8"))
    asset_index = json.loads((args.src / "asset-index.json").read_text(encoding="utf-8"))
    asset_lookup = index_assets_by_filename(asset_index)

    args.dest.mkdir(parents=True, exist_ok=True)

    # Per-tool Catalogs (write first so we can hash them for the Index).
    catalog_sha: dict[str, tuple[str, int]] = {}
    for tool_name in sorted((top_level_v5.get("tools") or {}).keys()):
        per_tool_path = args.src / tool_name / "manifest.json"
        if not per_tool_path.exists():
            print(f"  warn: {per_tool_path} missing, skipping {tool_name}", file=sys.stderr)
            continue
        v5_releases = json.loads(per_tool_path.read_text(encoding="utf-8"))
        if isinstance(v5_releases, dict):
            # apple-sdk and other vendored single-release manifests come as a list anyway,
            # but defensively accept dict too.
            v5_releases = [v5_releases]
        catalog = convert_tool_catalog(tool_name, v5_releases, asset_lookup, top_level_v5)
        data = write_json(args.dest / tool_name / "manifest.json", catalog)
        catalog_sha[tool_name] = (hash_sha256(data), len(data))
        print(f"  wrote {tool_name}/manifest.json ({len(data)} bytes, {len(catalog['releases'])} releases)")

    # Top-level Index.
    index = convert_index(top_level_v5, catalog_sha)
    data = write_json(args.dest / "manifest.json", index)
    print(f"  wrote manifest.json ({len(data)} bytes, {len(index['tools'])} tools)")

    # Copy asset-index.json verbatim for backward compat / debugging.
    # Skip when src and dest point at the same file (in-place regeneration).
    src_asset_index = args.src / "asset-index.json"
    dst_asset_index = args.dest / "asset-index.json"
    if src_asset_index.exists() and src_asset_index.resolve() != dst_asset_index.resolve():
        shutil.copy2(src_asset_index, dst_asset_index)

    return 0


if __name__ == "__main__":
    sys.exit(main())
