#!/usr/bin/env python3
"""Generate the hierarchical release manifest for soldr-toolchain.

This repo's root tree is a public release-asset catalogue::

    /                              # repo root (main branch)
    ├── manifest.json              # top-level index: tools -> subdir
    ├── zccache/manifest.json      # one per tool: assets + URLs + sha
    ├── crgx/manifest.json
    ├── cargo-chef/manifest.json
    ├── cargo-zigbuild/manifest.json
    ├── cargo-xwin/manifest.json
    └── deps/                      # vendored, non-GitHub-Releases sources
        └── mac/
            ├── manifest.json
            └── sdk.tar.zstd

A nightly workflow (``.github/workflows/refresh-manifest.yml``) re-runs
this script and commits the diff — so per-tool files only change when
the upstream release actually changes. Consumers fetch via the public
``raw.githubusercontent.com`` / ``media.githubusercontent.com`` CDN,
which is NOT subject to the GitHub Releases API rate-limit that was
triggering 403s across parallel matrix jobs on consumers.

Auth: ``$GITHUB_TOKEN`` (workflow-provided) raises the API rate limit
from the unauthenticated 60 req/hour to 5000 req/hour. The script
back-offs on 403/429 with exponential delay.

Pinned versions: ``--repo-root`` should point at a checkout of the
consumer repo (``zackees/soldr``) so per-tool pins can be read directly
out of its Rust source constants. This keeps the manifest from drifting
from what the consumer would fetch at runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Ordering note: GitHub's `/releases?per_page=N` endpoint already
# returns releases sorted by `published_at` descending (newest-first),
# so we don't parse versions client-side — we just trust the API order
# and break ties on `published_at` when merging old + new entries.

REPO_ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL_FILENAME = "manifest.json"
PER_TOOL_FILENAME = "manifest.json"
SCHEMA_VERSION = 5


def read_constant(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf'{re.escape(name)}\s*:\s*&str\s*=\s*"([^"]+)"', text)
    if not m:
        raise RuntimeError(f"could not find {name} in {path}")
    return m.group(1)


def gh_request(url: str, token: str | None) -> Any:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "soldr-toolchain-manifest-builder")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    last_exc: Exception | None = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in (403, 429):
                wait = int(exc.headers.get("Retry-After") or 2 ** attempt)
                print(
                    f"  github API {exc.code} for {url}; sleeping {wait}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as exc:
            last_exc = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"github API failed after retries: {last_exc}")


def list_releases(owner: str, repo: str, token: str | None) -> list[dict[str, Any]]:
    """Fetch the most recent 100 releases for a repo.

    GitHub returns them sorted by `published_at` descending (newest
    first). 100 is the max `per_page` for this endpoint and covers
    every tool we currently track.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=100"
    return gh_request(url, token)


def derive_platform_key(filename: str) -> str | None:
    """Map an upstream asset filename to a normalized
    ``<os>-<arch>[-<extra>]`` key, or None if the filename isn't a
    runnable platform binary (sha256 sums, installers, source tarballs,
    dist manifests, debug bundles, etc.).

    Modern short arch names (npm/Node.js convention):
      os    ∈ { linux, darwin, windows }
      arch  ∈ { x64, arm64, universal2 }
      extra ∈ { gnu, musl, msvc, gnullvm, … }  (optional)

    32-bit lanes (i686 / armv7) are intentionally not surfaced.
    """
    name = filename.lower()

    if not (
        name.endswith(".tar.gz")
        or name.endswith(".tgz")
        or name.endswith(".tar.xz")
        or name.endswith(".txz")
        or name.endswith(".tar.bz2")
        or name.endswith(".zip")
    ):
        return None
    if "source" in name or "dist-manifest" in name or "installer" in name:
        return None
    if "-debug" in name or ".debug" in name or "-sym" in name or ".pdb" in name:
        return None

    if "apple-darwin" in name or "-macos-" in name or ".macos." in name:
        os_key = "darwin"
    elif "windows" in name:
        os_key = "windows"
    elif "linux" in name:
        os_key = "linux"
    else:
        return None

    if "universal2" in name:
        arch = "universal2"
    elif "x86_64" in name or "windows-x64" in name or "amd64" in name:
        arch = "x64"
    elif "aarch64" in name or "arm64" in name:
        arch = "arm64"
    else:
        return None

    extra: str | None = None
    if "musleabihf" in name:
        extra = "musleabihf"
    elif "musleabi" in name:
        extra = "musleabi"
    elif "musl" in name:
        extra = "musl"
    elif "gnullvm" in name:
        extra = "gnullvm"
    elif "-gnu" in name or ".gnu." in name:
        extra = "gnu"
    elif "msvc" in name:
        extra = "msvc"
    elif os_key == "windows":
        extra = "msvc"

    if extra is not None:
        return f"{os_key}-{arch}-{extra}"
    return f"{os_key}-{arch}"


def build_release_entry(
    release: dict[str, Any],
    *,
    tool: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Render one GitHub release into the per-tool entry shape."""
    resolved_tag: str = release["tag_name"]
    version = resolved_tag[1:] if resolved_tag.startswith("v") else resolved_tag
    assets: dict[str, dict[str, Any]] = {}
    platforms: dict[str, dict[str, Any]] = {}
    for asset in release.get("assets", []):
        asset_name = asset["name"]
        # `digest` is a GitHub-provided "<algo>:<hex>" string (sha256 today).
        # Available on every release asset; lets us populate per-asset
        # sha256 without fetching a SHA256SUMS sidecar.
        digest_raw = asset.get("digest") or ""
        sha256 = ""
        if digest_raw.startswith("sha256:"):
            sha256 = digest_raw[len("sha256:"):].lower()
        entry = {
            "url": asset["browser_download_url"],
            "size": asset.get("size"),
            "content_type": asset.get("content_type"),
            "created_at": asset.get("created_at"),
            "updated_at": asset.get("updated_at"),
        }
        if sha256:
            entry["sha256"] = sha256
        assets[asset_name] = entry
        platform_key = derive_platform_key(asset_name)
        if platform_key is not None:
            plat = {
                "filename": asset_name,
                "url": entry["url"],
                "size": entry["size"],
            }
            if sha256:
                plat["sha256"] = sha256
            platforms.setdefault(platform_key, plat)
    entry: dict[str, Any] = {}
    if tool is not None:
        entry["tool"] = tool
    if owner is not None:
        entry["owner"] = owner
    if repo is not None:
        entry["repo"] = repo
    entry.update({
        "tag": resolved_tag,
        "version": version,
        "name": release.get("name"),
        "draft": release.get("draft"),
        "prerelease": release.get("prerelease"),
        "created_at": release.get("created_at"),
        "published_at": release.get("published_at"),
        "release_html_url": release.get("html_url"),
        "platforms": dict(sorted(platforms.items())),
        "assets": dict(sorted(assets.items())),
    })
    return entry


def load_existing_per_tool(path: Path) -> list[dict[str, Any]]:
    """Read a previously-written per-tool manifest. Handles v5 (flat
    array), v4 (dict with version-tagged keys), and v3/v2 (releases:
    wrapper). Returns empty list on missing/malformed files."""
    if not path.is_file():
        return []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return [e for e in parsed if isinstance(e, dict) and "tag" in e]
    if not isinstance(parsed, dict):
        return []
    nested = parsed.get("releases")
    if isinstance(nested, dict):
        return [e for e in nested.values() if isinstance(e, dict) and "tag" in e]
    legacy_metadata = {"schema_version", "name", "owner", "repo",
                       "pinned", "tracked_tags", "latest"}
    entries: list[dict[str, Any]] = []
    for key, value in parsed.items():
        if key in legacy_metadata:
            continue
        if isinstance(value, dict) and "tag" in value and "platforms" in value:
            entries.append(value)
    return entries


def build_merged_tool_releases(
    name: str,
    owner: str,
    repo: str,
    pinned_tag: str | None,
    token: str | None,
    existing: list[dict[str, Any]],
    *,
    list_releases_fn=list_releases,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch + merge releases. ``list_releases_fn`` is overridable so
    unit tests can drive the merge logic without hitting the network.
    """
    print(f"listing releases for {name} ({owner}/{repo})...", file=sys.stderr)
    fetched = list_releases_fn(owner, repo, token)

    by_tag: dict[str, dict[str, Any]] = {}
    for prior in existing:
        prior_tag = prior.get("tag")
        if prior_tag:
            prior.setdefault("tool", name)
            prior.setdefault("owner", owner)
            prior.setdefault("repo", repo)
            by_tag[prior_tag] = prior
    for release in fetched:
        entry = build_release_entry(release, tool=name, owner=owner, repo=repo)
        by_tag[entry["tag"]] = entry

    def _key(entry: dict[str, Any]) -> tuple[int, str, str]:
        published = entry.get("published_at") or ""
        return (1 if published else 0, published, entry.get("tag") or "")

    ordered = sorted(by_tag.values(), key=_key, reverse=True)
    latest_tag = ordered[0]["tag"] if ordered else None
    if pinned_tag is not None:
        for entry in ordered:
            entry["is_pinned"] = (entry.get("tag") == pinned_tag)
    return ordered, latest_tag


def preserve_vendored_top_level_entries(
    output_dir: Path,
    per_tool_index: dict[str, dict[str, Any]],
) -> None:
    """Re-add vendored / non-GitHub-Releases entries from the EXISTING
    root manifest.json into the in-progress index so the nightly
    refresh doesn't wipe them.

    The Apple SDK (``deps/mac/manifest.json``, indexed as ``apple-sdk``)
    is the canonical example — populated by a manual procedure and
    invisible to ``build_merged_tool_releases``. Entries whose ``path``
    no longer exists on disk are silently dropped.
    """
    top_path = output_dir / TOP_LEVEL_FILENAME
    if not top_path.is_file():
        return
    try:
        existing = json.loads(top_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    existing_tools = existing.get("tools") or {}
    for name, entry in existing_tools.items():
        if name in per_tool_index:
            continue
        path_ref = entry.get("path")
        if not path_ref:
            continue
        if not (output_dir / path_ref).is_file():
            print(
                f"  dropping stale vendored entry: {name} -> {path_ref} (file missing)",
                file=sys.stderr,
            )
            continue
        per_tool_index[name] = entry
        print(
            f"  preserving vendored entry: {name} -> {path_ref}",
            file=sys.stderr,
        )


def write_if_changed(path: Path, new_content: str) -> bool:
    """Write only if content differs. Returns True iff the file was
    rewritten — used by the nightly workflow to keep `git status` quiet
    when no upstream change occurred."""
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        if existing == new_content:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")
    return True


def load_pinned_versions(repo_root: Path) -> dict[str, str | None]:
    """Read the three pinned-version Rust constants out of a soldr checkout.

    Returns a mapping ``{tool_name: tag_or_None}`` for the tools that
    have a pin. Unpinned tools (``cargo-zigbuild``, ``cargo-xwin``)
    are omitted; the caller treats absence as "latest".
    """
    fetch_mod = repo_root / "crates" / "soldr-cli" / "src" / "fetch" / "mod.rs"
    known_tools = repo_root / "crates" / "soldr-cli" / "src" / "fetch" / "known_tools.rs"
    zccache_version = read_constant(fetch_mod, "MANAGED_ZCCACHE_VERSION")
    crgx_version = read_constant(fetch_mod, "MANAGED_CRGX_VERSION")
    cargo_chef_version = read_constant(known_tools, "CARGO_CHEF_PINNED_VERSION")
    return {
        "zccache": zccache_version,
        "crgx": f"v{crgx_version}",
        "cargo-chef": f"v{cargo_chef_version}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write the manifest tree into (default: cwd).",
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        help=(
            "Path to a soldr checkout used to read pinned version "
            "constants from crates/soldr-cli/src/fetch/."
        ),
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    pins = load_pinned_versions(repo_root)

    # (display_name, owner, repo, tag_or_None_for_latest).
    # cargo-zigbuild and cargo-xwin are unpinned in `known_tools`
    # (soldr resolves "latest" at fetch time), so we mirror that here.
    tools = [
        ("zccache",        "zackees",         "zccache",        pins["zccache"]),
        ("crgx",           "yfedoseev",       "crgx",           pins["crgx"]),
        ("cargo-chef",     "LukeMathWalker",  "cargo-chef",     pins["cargo-chef"]),
        ("cargo-zigbuild", "rust-cross",      "cargo-zigbuild", None),
        ("cargo-xwin",     "rust-cross",      "cargo-xwin",     None),
    ]

    token = os.environ.get("GITHUB_TOKEN") or None
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    per_tool_index: dict[str, dict[str, Any]] = {}
    changed_count = 0
    for name, owner, repo, pinned_tag in tools:
        tool_dir = output_dir / name
        tool_path = tool_dir / PER_TOOL_FILENAME
        existing = load_existing_per_tool(tool_path)
        entries, latest_tag = build_merged_tool_releases(
            name, owner, repo, pinned_tag, token, existing
        )
        per_tool_index[name] = {
            "path": f"{name}/{PER_TOOL_FILENAME}",
            "owner": owner,
            "repo": repo,
            "latest": latest_tag,
            "pinned": pinned_tag,
            "tracked_tags": [e.get("tag") for e in entries if e.get("tag")],
        }
        per_tool_payload = json.dumps(entries, indent=2) + "\n"
        if write_if_changed(tool_path, per_tool_payload):
            print(
                f"  wrote {tool_path} (latest={latest_tag}, "
                f"{len(entries)} tags total)",
                file=sys.stderr,
            )
            changed_count += 1
        else:
            print(f"  unchanged {tool_path}", file=sys.stderr)

    preserve_vendored_top_level_entries(output_dir, per_tool_index)

    top_manifest = {
        "schema_version": SCHEMA_VERSION,
        "tools": dict(sorted(per_tool_index.items())),
    }
    top_path = output_dir / TOP_LEVEL_FILENAME
    top_payload = json.dumps(top_manifest, indent=2) + "\n"
    top_changed = write_if_changed(top_path, top_payload)
    if top_changed:
        print(f"  wrote {top_path}", file=sys.stderr)
    print(
        f"manifest built: {len(tools)} tools, {changed_count} per-tool files updated, "
        f"top-level {'updated' if top_changed else 'unchanged'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
