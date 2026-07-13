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
import copy
import json
import sys
from pathlib import Path
from typing import Any

V1_SCHEMA_VERSION = 1
ONLINE_BASE = "https://zackees.github.io/soldr-toolchain"
# Canonical pointer for IDE auto-validation and document self-verification.
# Pinned to /v1/ so consumers stay valid forever as the schema evolves.
SCHEMA_URL = "https://zackees.github.io/manifest.json/v1/manifest.schema.json"
LOCAL_SUPPORT_TOOLS = {"cargo-chef", "crgx", "cargo-binstall", "cargo-nextest"}
LOCAL_SUPPORT_URL_MARKER = "zackees/soldr-toolchain/assets/"

# v5 short-arch -> v1 canonical arch
ARCH_MAP: dict[str, str] = {
    "x64": "x86_64",
    "arm64": "aarch64",
    "universal2": "universal2",
}

# v5 extra -> (v1 field, v1 value)
EXTRA_MAP: dict[str, tuple[str, str]] = {
    "gnu": ("libc", "glibc"),
    "musl": ("libc", "musl"),
    "msvc": ("abi", "msvc"),
    "gnullvm": ("abi", "gnullvm"),
}


def _collapse_darwin_universal2(
    platforms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect identical assets shipped under both `darwin/x86_64` and
    `darwin/aarch64` and collapse them into a single `darwin/universal2`
    entry.

    Two entries are considered identical if they have the same filename,
    URL, AND sha256 (or both lack sha256 — the URL+filename match is
    sufficient when sha is missing). This matches the apple-sdk pattern
    in soldr-toolchain where a fat Mach-O is duplicated under each
    concrete darwin arch.

    Other multi-arch fan-out (e.g. linux/x86_64+aarch64 with same bytes)
    is left untouched — `universal2` is an Apple-specific convention.
    """
    by_key: dict[tuple[str, str, str], list[int]] = {}
    for i, p in enumerate(platforms):
        plat = p.get("platform", {}) or {}
        asset = p.get("asset", {}) or {}
        if plat.get("os") != "darwin":
            continue
        arch = plat.get("arch", "")
        if arch not in ("x86_64", "aarch64"):
            continue
        # Compare on the identity of the asset itself.
        key = (
            asset.get("filename", ""),
            (asset.get("urls") or [""])[0],
            asset.get("sha256", ""),
        )
        by_key.setdefault(key, []).append(i)

    collapse_indices: set[int] = set()
    for indices in by_key.values():
        if len(indices) < 2:
            continue
        # Confirm the two entries cover BOTH darwin arches (not e.g.
        # x86_64 listed twice with different variants).
        archs = {platforms[i]["platform"].get("arch", "") for i in indices}
        if archs != {"x86_64", "aarch64"}:
            continue
        collapse_indices.update(indices)

    if not collapse_indices:
        return platforms

    out: list[dict[str, Any]] = []
    universal_emitted: set[tuple[str, str, str]] = set()
    for i, p in enumerate(platforms):
        if i not in collapse_indices:
            out.append(p)
            continue
        plat = p["platform"]
        asset = p["asset"]
        key = (
            asset.get("filename", ""),
            (asset.get("urls") or [""])[0],
            asset.get("sha256", ""),
        )
        if key in universal_emitted:
            continue
        universal_emitted.add(key)
        # Drop arch -> universal2; preserve other platform fields (os_version, etc.)
        new_plat = dict(plat)
        new_plat["arch"] = "universal2"
        out.append({"platform": new_plat, "asset": asset})
    return out


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


def index_assets_by_filename(
    asset_index: dict[str, Any],
) -> dict[tuple[str, str, str], dict[str, Any]]:
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
            print(
                f"  skip {owner}/{repo}@{tag}: cannot parse platform key {pkey!r}",
                file=sys.stderr,
            )
            continue
        filename = pval.get("filename") or ""
        # Prefer the sha embedded in the v5 platform entry (populated
        # from GitHub's per-asset `digest` field by build_manifest.py
        # since the universal2 / api-digest change). Fall back to the
        # asset-index for legacy data and locally-hosted blobs.
        sha256 = pval.get("sha256") or (by_filename.get(filename) or {}).get(
            "sha256", ""
        )
        url = pval.get("url") or ""
        asset_out: dict[str, Any] = {
            "filename": filename,
            "size_bytes": int(pval.get("size") or 0),
            "urls": [url] if url else [],
        }
        if sha256:
            asset_out["sha256"] = sha256
        platforms_out.append(
            {
                "platform": platform_tuple,
                "asset": asset_out,
            }
        )

    platforms_out = _collapse_darwin_universal2(platforms_out)

    # Use the upstream `tag` as the v1 version by default so it round-trips against the
    # v5 top-level `latest` / `pinned` pointers (which also use the tag).
    # `version` (normalized, no "v" prefix) is dropped — channels always
    # resolve through the same string the producer keyed everything by.
    release_out: dict[str, Any] = {
        "schema_version": V1_SCHEMA_VERSION,
        "version": v5_release.get("catalog_version")
        or tag
        or v5_release.get("version")
        or "",
        "published_at": v5_release.get("published_at", "") or "",
        "min_client_version": 1,
        "platforms": platforms_out,
    }
    # Source fallback: GitHub repo + tag, when the release came from GH.
    if owner and repo and tag and owner != "vendored":
        release_out["source"] = {
            "vcs": "git",
            "repo_url": f"https://github.com/{owner}/{repo}",
            "ref": tag,
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
    versions_by_tag = {
        release.get("tag"): release.get("catalog_version") or release.get("tag")
        for release in v5_releases
        if release.get("tag")
    }
    latest_tag = tool_meta.get("latest", "")
    pinned_tag = tool_meta.get("pinned") or ""
    latest = versions_by_tag.get(latest_tag, latest_tag)
    pinned = versions_by_tag.get(pinned_tag, pinned_tag)
    channels: dict[str, str] = {}
    if latest:
        channels["latest-stable"] = latest
        channels["stable"] = latest
    if pinned and pinned not in channels.values():
        channels["pinned"] = pinned
    return {
        "$schema": SCHEMA_URL,
        "kind": "Catalog",
        "schema_version": V1_SCHEMA_VERSION,
        "tool": tool_name,
        "online_url": f"{ONLINE_BASE}/{tool_name}/manifest.json",
        "channels": channels,
        "releases": releases_v1,
    }


def _platform_identity(platform_entry: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    platform = platform_entry.get("platform") or {}
    return tuple(sorted((str(k), str(v)) for k, v in platform.items()))


def _is_local_support_asset(platform_entry: dict[str, Any]) -> bool:
    asset = platform_entry.get("asset") or {}
    urls = asset.get("urls") or []
    return any(LOCAL_SUPPORT_URL_MARKER in str(url) for url in urls)


def _sort_platforms(platforms: list[dict[str, Any]]) -> None:
    platforms.sort(
        key=lambda p: (
            p.get("platform", {}).get("os", ""),
            p.get("platform", {}).get("arch", ""),
            p.get("platform", {}).get("libc", ""),
            p.get("platform", {}).get("abi", ""),
        )
    )


def merge_local_support_assets(
    dest: Path,
    tool_name: str,
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Preserve locally hosted support bundles across v5 refreshes.

    cargo-chef and crgx are both upstream GitHub tools and soldr release
    support binaries. The v5 refresh can rebuild their Catalogs from the
    upstream releases, but the support bundles are produced by this repo's
    Forge jobs under the soldr-toolchain assets branch. Keep only those
    local entries from the existing v1 Catalog and merge them into the
    freshly converted Catalog.
    """
    if tool_name not in LOCAL_SUPPORT_TOOLS:
        return catalog

    existing_path = dest / tool_name / "manifest.json"
    if not existing_path.is_file():
        return catalog

    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return catalog
    if existing.get("kind") != "Catalog":
        return catalog

    releases = catalog.setdefault("releases", [])
    releases_by_version = {
        r.get("version"): r
        for r in releases
        if isinstance(r, dict) and r.get("version")
    }
    preserved_count = 0

    for old_release in existing.get("releases", []) or []:
        if not isinstance(old_release, dict):
            continue
        local_platforms = [
            copy.deepcopy(p)
            for p in (old_release.get("platforms") or [])
            if isinstance(p, dict) and _is_local_support_asset(p)
        ]
        if not local_platforms:
            continue

        version = old_release.get("version")
        if not version:
            continue
        release = releases_by_version.get(version)
        if release is None:
            release = {
                k: copy.deepcopy(v) for k, v in old_release.items() if k != "platforms"
            }
            release.setdefault("schema_version", V1_SCHEMA_VERSION)
            release.setdefault("published_at", "")
            release.setdefault("min_client_version", 1)
            release["platforms"] = []
            releases.append(release)
            releases_by_version[version] = release

        local_keys = {_platform_identity(p) for p in local_platforms}
        current_platforms = [
            p
            for p in (release.get("platforms") or [])
            if _platform_identity(p) not in local_keys
        ]
        current_platforms.extend(local_platforms)
        _sort_platforms(current_platforms)
        release["platforms"] = current_platforms
        preserved_count += len(local_platforms)

    if preserved_count:
        existing_channels = existing.get("channels") or {}
        channels = catalog.setdefault("channels", {})
        for name, version in existing_channels.items():
            channels.setdefault(name, version)
        print(
            f"  preserved {preserved_count} local support platform(s) for {tool_name}",
            file=sys.stderr,
        )

    return catalog


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
            "url": f"{tool_name}/manifest.json",
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
            "summary": summary,
            "kind_hint": _kind_hint_for(tool_name, meta),
        }
    return {
        "$schema": SCHEMA_URL,
        "kind": "Index",
        "schema_version": V1_SCHEMA_VERSION,
        "tools": tools_out,
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


def _preserve_vendored_entries(
    dest: Path,
    v5_tool_names: set[str],
) -> dict[str, dict[str, Any]]:
    """Read the EXISTING v1 Index in `dest` (if any) and return entries
    for tools that aren't in the v5 source — those are vendored
    (apple-sdk, xwin-cache, ...). The converter shouldn't touch them.

    Returns a {tool_name: ToolEntry} dict. Re-computes descriptor
    sha256/size from the on-disk catalog file so any manual edit to a
    vendored catalog is reflected without a separate manual step.
    """
    import hashlib

    index_path = dest / "manifest.json"
    if not index_path.is_file():
        return {}
    try:
        existing = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if existing.get("kind") != "Index":
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, entry in (existing.get("tools") or {}).items():
        if name in v5_tool_names:
            continue  # GH-derived; the converter is rebuilding this
        # vendored: preserve, but refresh descriptor from on-disk catalog
        desc = dict((entry.get("descriptor") or {}))
        rel_url = desc.get("url", "")
        if rel_url and not rel_url.startswith(("http://", "https://")):
            cat_path = dest / rel_url
            if cat_path.is_file():
                blob = cat_path.read_bytes()
                desc["sha256"] = hashlib.sha256(blob).hexdigest()
                desc["size_bytes"] = len(blob)
            else:
                print(
                    f"  warn: vendored {name} catalog missing: {cat_path}",
                    file=sys.stderr,
                )
                continue
        out[name] = {
            **entry,
            "descriptor": desc,
        }
        print(f"  preserving vendored tool: {name}", file=sys.stderr)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--src", type=Path, required=True, help="v5 tree root (assets branch checkout)"
    )
    p.add_argument("--dest", type=Path, required=True, help="output dir for v1 tree")
    args = p.parse_args()

    top_level_v5 = json.loads((args.src / "manifest.json").read_text(encoding="utf-8"))
    asset_index = json.loads(
        (args.src / "asset-index.json").read_text(encoding="utf-8")
    )
    asset_lookup = index_assets_by_filename(asset_index)

    args.dest.mkdir(parents=True, exist_ok=True)

    v5_tool_names = set((top_level_v5.get("tools") or {}).keys())

    # Per-tool Catalogs (write first so we can hash them for the Index).
    catalog_sha: dict[str, tuple[str, int]] = {}
    for tool_name in sorted(v5_tool_names):
        per_tool_path = args.src / tool_name / "manifest.json"
        if not per_tool_path.exists():
            print(
                f"  warn: {per_tool_path} missing, skipping {tool_name}",
                file=sys.stderr,
            )
            continue
        v5_releases = json.loads(per_tool_path.read_text(encoding="utf-8"))
        if isinstance(v5_releases, dict):
            # apple-sdk and other vendored single-release manifests come as a list anyway,
            # but defensively accept dict too.
            v5_releases = [v5_releases]
        catalog = convert_tool_catalog(
            tool_name, v5_releases, asset_lookup, top_level_v5
        )
        catalog = merge_local_support_assets(args.dest, tool_name, catalog)
        data = write_json(args.dest / tool_name / "manifest.json", catalog)
        catalog_sha[tool_name] = (hash_sha256(data), len(data))
        print(
            f"  wrote {tool_name}/manifest.json ({len(data)} bytes, {len(catalog['releases'])} releases)"
        )

    # Preserve vendored entries from the destination tree (apple-sdk,
    # xwin-cache, etc.) — those live independently of the v5 pipeline.
    vendored_entries = _preserve_vendored_entries(args.dest, v5_tool_names)

    # Top-level Index — start from v5-derived entries, then merge in vendored.
    index = convert_index(top_level_v5, catalog_sha)
    for name, entry in vendored_entries.items():
        index["tools"][name] = entry
    # Re-sort tools for stable output.
    index["tools"] = dict(sorted(index["tools"].items()))
    data = write_json(args.dest / "manifest.json", index)
    print(
        f"  wrote manifest.json ({len(data)} bytes, {len(index['tools'])} tools, "
        f"{len(vendored_entries)} vendored preserved)"
    )

    # asset-index.json is no longer copied here. After the v1 migration
    # its attribution columns (owner, repo, tag) reflect v1 reality —
    # which the v5-source file does NOT know about. The workflow now
    # runs build_asset_index.py separately AGAINST the v1 tree (--dest)
    # so attribution is always self-consistent.

    return 0


if __name__ == "__main__":
    sys.exit(main())
