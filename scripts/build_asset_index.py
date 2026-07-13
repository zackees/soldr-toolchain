#!/usr/bin/env python3
"""Generate ``asset-index.json`` for the soldr-toolchain ``assets`` branch.

The runtime resolver in soldr (``crates/soldr-cli/src/fetch/manifest_lookup.rs``)
consults a vendored, sha-bearing asset index hosted on the toolchain repo::

    https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/asset-index.json

The deployed parser (see ``ManifestIndex`` in that file) expects a
deliberately FLAT shape — one row per ``(owner, repo, tag, asset)``::

    {
      "entries": [
        {
          "owner":  "zackees",
          "repo":   "zccache",
          "tag":    "1.12.9",
          "asset":  "zccache-v1.12.9-x86_64-pc-windows-msvc.zip",
          "url":    "https://github.com/.../...zip",
          "sha256": "<64-char lowercase hex>"
        },
        ...
      ]
    }

GitHub-release rows are looked up by that four-field tuple. Locally
hosted platform bundles are distinguished by URL because they often
reuse filenames like ``bundle.tar.zst`` across platform directories.

This script walks the assets-branch tree and emits that JSON. Two
data sources contribute entries:

1. **Locally-hosted blobs under ``<tool>/<version>/<platform>/``.**
   Sha256 is computed directly from the file on disk (matches soldr's
   ``crates/soldr-cli/src/fetch/trust.rs::sha256_of`` exactly: raw
   bytes through SHA-256, lowercase hex). The companion
   ``<tool>/manifest.json`` carries the ``(owner, repo, tag)`` for
   each release; we look up the entry matching the on-disk version
   directory to attribute ownership. Variants for the same OS+arch
   live as flat siblings inside the platform folder (e.g. ``linux-x64/``
   contains both ``...-gnu.tar.gz`` and ``...-musl.tar.gz``).

2. **GitHub-released assets whose release ships a ``SHA256SUMS``
   asset.** Where the existing per-tool manifest lists ``SHA256SUMS``
   in its raw ``assets`` map, we fetch that single small file and
   parse it for per-asset hashes. Releases without a ``SHA256SUMS``
   skip silently — the resolver degrades to a cache miss + live
   GitHub Releases API fallback.

Determinism: entries are sorted ascending by
``(owner, repo, tag, asset, url)`` so the diff of ``asset-index.json``
between refreshes is reviewable. Locally-hosted bundles may legitimately
share the same filename under different platform directories, so the URL
is part of the identity for de-duplication.

Refreshed nightly in lockstep with ``build_manifest.py`` from
``.github/workflows/refresh-manifest.yml``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ASSET_INDEX_SCHEMA_VERSION = 5

SHA256SUMS_ASSET_NAME = "SHA256SUMS"
SHA256SUMS_SKIP_LINES = {"SHA256SUMS", "install.sh", "install.ps1"}
_LFS_POINTER_SHA_RE = re.compile(r"^oid sha256:([0-9a-f]{64})$", re.MULTILINE)


def sha256_of_file(path: Path) -> str:
    """SHA-256 of ``path``'s bytes, lowercase hex.

    Matches soldr's ``crates/soldr-cli/src/fetch/trust.rs::sha256_of``
    exactly: raw bytes through SHA-256, no header, no length prefix,
    hex-encoded in lowercase. If the checkout contains a Git LFS pointer
    instead of the materialized blob, return the pointer's object id; LFS
    object ids are SHA-256 hashes of the real blob bytes.
    """
    if path.stat().st_size <= 1024:
        try:
            head = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            head = ""
        if head.startswith("version https://git-lfs.github.com/spec/"):
            match = _LFS_POINTER_SHA_RE.search(head)
            if match:
                return match.group(1)

    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def http_get_text(url: str, *, timeout: float = 30.0) -> str | None:
    """Fetch ``url`` and return its body as UTF-8 text, or None on any
    failure. Callers treat None as "no SHA256SUMS for this release"."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "soldr-toolchain-asset-index-builder")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a ``SHA256SUMS`` body into ``{asset_filename: sha256_hex}``.

    Format (per ``sha256sum -b``)::

        <64-char hex>  <filename>
        <64-char hex>  ./<filename>

    Leading ``./`` stripped. Comments + blank lines ignored. Debug /
    symbol packages excluded (matches ``derive_platform_key``).
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, name = parts
        if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha.lower()):
            continue
        name = name.removeprefix("./").strip()
        if not name or name in SHA256SUMS_SKIP_LINES:
            continue
        lower = name.lower()
        if "-debug" in lower or ".debug" in lower or ".pdb" in lower or "-sym" in lower:
            continue
        out[name] = sha.lower()
    return out


def iter_local_blobs(manifest_root: Path) -> Iterable[Path]:
    """Yield every locally-hosted blob under the
    ``<tool>/<version>/<platform>/`` layout.

    The per-tool ``manifest.json`` at ``<tool>/manifest.json`` is metadata,
    NOT a blob, so it is skipped. Anything else inside
    ``<tool>/<anything>/...`` is treated as a blob and yielded.
    """
    for tool_dir in sorted(p for p in manifest_root.iterdir() if p.is_dir()):
        if tool_dir.name.startswith("."):
            continue
        for version_dir in sorted(p for p in tool_dir.iterdir() if p.is_dir()):
            for path in sorted(version_dir.rglob("*")):
                if path.is_file():
                    yield path


def _entry_sort_key(entry: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        entry.get("owner", ""),
        entry.get("repo", ""),
        entry.get("tag", ""),
        entry.get("asset", ""),
        entry.get("url", ""),
    )


def _url_for_local(repo_owner: str, repo_name: str, branch: str, rel_posix: str) -> str:
    """Build the LFS-aware CDN URL a locally-hosted blob is served from.

    Uses ``media.githubusercontent.com/media/`` rather than
    ``raw.githubusercontent.com``: the ``/media/`` endpoint follows
    Git-LFS pointer files to the actual binary blob (and falls back
    transparently to raw content for non-LFS files). Same URL form
    works for both pre- and post-LFS state of the assets tree.
    """
    return f"https://media.githubusercontent.com/media/{repo_owner}/{repo_name}/{branch}/{rel_posix}"


def _load_per_tool_releases(tool_manifest_path: Path) -> dict[str, dict[str, Any]]:
    """Read a per-tool ``manifest.json`` and return ``{tag: release_dict}``.

    Returns an empty dict on missing / malformed files. The per-tool
    file is the v5 flat-array shape (a list of self-describing release
    dicts).
    """
    try:
        payload = json.loads(tool_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(payload, dict) and payload.get("kind") == "Catalog":
        is_v1_catalog = True
        releases = payload.get("releases") or []
    elif isinstance(payload, list):
        is_v1_catalog = False
        releases = payload
    else:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for release in releases:
        if not isinstance(release, dict):
            continue
        identity = release.get("tag") or release.get("version")
        if isinstance(identity, str):
            normalized = dict(release)
            source = release.get("source") or {}
            repo_url = str(source.get("repo_url", ""))
            source_ref = str(source.get("ref", ""))
            if is_v1_catalog and re.fullmatch(r"[0-9a-f]{40}", source_ref) is None:
                continue
            match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)", repo_url)
            if match and re.fullmatch(r"[0-9a-f]{40}", source_ref):
                normalized.setdefault("owner", match.group(1))
                normalized.setdefault("repo", match.group(2))
            out[identity] = normalized
    return out


def collect_local_blob_entries(
    manifest_root: Path,
    repo_owner: str,
    repo_name: str,
    branch: str,
) -> list[dict[str, Any]]:
    """Walk the ``<tool>/<version>/<platform>/`` layout and emit one
    entry per locally-hosted blob.

    For each blob, sha256 is computed from on-disk bytes. The tool's
    per-tool ``manifest.json`` (``<tool>/manifest.json``) is consulted
    to attribute owner+repo for the matching version. Versions present
    on disk but absent from the per-tool manifest are self-attributed
    to ``(<repo_owner>, <repo_name>, <branch>)`` so the entry is still
    sha-verifiable downstream.

    Variants (gnu/musl/msvc/gnullvm) live as flat siblings inside the
    platform folder — the loop walks the platform folder recursively
    so any depth is supported, but typically the variants are a single
    file each.
    """
    entries: list[dict[str, Any]] = []
    for tool_dir in sorted(p for p in manifest_root.iterdir() if p.is_dir()):
        tool_name = tool_dir.name
        if tool_name.startswith("."):
            continue
        per_tool_path = tool_dir / "manifest.json"
        releases_by_tag = _load_per_tool_releases(per_tool_path)
        version_dirs = sorted(p for p in tool_dir.iterdir() if p.is_dir())
        if not version_dirs:
            continue
        for version_dir in version_dirs:
            version = version_dir.name
            release = releases_by_tag.get(version)
            if release is not None:
                owner = release.get("owner") or repo_owner
                repo = release.get("repo") or tool_name
                tag = version
            else:
                # No matching release entry — self-attribute so the
                # blob is still sha-bound in the index.
                owner = repo_owner
                repo = repo_name
                tag = branch
            for blob_path in sorted(version_dir.rglob("*")):
                if not blob_path.is_file():
                    continue
                rel = blob_path.relative_to(manifest_root).as_posix()
                sha = sha256_of_file(blob_path)
                entries.append(
                    {
                        "owner": owner,
                        "repo": repo,
                        "tag": tag,
                        "asset": blob_path.name,
                        "url": _url_for_local(repo_owner, repo_name, branch, rel),
                        "sha256": sha,
                    }
                )
    return entries


def collect_release_entries_for_tool(
    tool_manifest_path: Path,
    *,
    offline: bool = False,
    http_get_fn=http_get_text,
) -> list[dict[str, Any]]:
    """Read one per-tool ``manifest.json`` (flat array of releases) and
    emit one entry per asset for which a sha256 can be attributed.

    Today the only attributable releases are those whose ``assets``
    map contains a ``SHA256SUMS`` file. Releases without a SHA256SUMS
    contribute zero entries.

    ``offline=True`` skips the SHA256SUMS HTTP fetch entirely; used by
    unit tests so the build doesn't depend on github.com reachability.
    ``http_get_fn`` is overridable for tests that want to inject a
    fake SHA256SUMS body without network access.
    """
    entries: list[dict[str, Any]] = []
    try:
        payload = json.loads(tool_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return entries
    if not isinstance(payload, list):
        return entries
    for release in payload:
        if not isinstance(release, dict):
            continue
        owner = release.get("owner")
        repo = release.get("repo")
        tag = release.get("tag")
        assets = release.get("assets")
        if not (
            isinstance(owner, str)
            and isinstance(repo, str)
            and isinstance(tag, str)
            and isinstance(assets, dict)
        ):
            continue
        sums_entry = assets.get(SHA256SUMS_ASSET_NAME)
        if not isinstance(sums_entry, dict):
            continue
        sums_url = sums_entry.get("url")
        if not isinstance(sums_url, str) or offline:
            continue
        body = http_get_fn(sums_url)
        if body is None:
            print(
                f"  no SHA256SUMS available for {owner}/{repo}@{tag} "
                f"(url={sums_url})",
                file=sys.stderr,
            )
            continue
        sums = parse_sha256sums(body)
        for asset_name, sha in sums.items():
            asset_entry = assets.get(asset_name)
            if not isinstance(asset_entry, dict):
                continue
            url = asset_entry.get("url")
            if not isinstance(url, str):
                continue
            entries.append(
                {
                    "owner": owner,
                    "repo": repo,
                    "tag": tag,
                    "asset": asset_name,
                    "url": url,
                    "sha256": sha,
                }
            )
    return entries


def build_asset_index(
    manifest_root: Path,
    *,
    repo_owner: str = "zackees",
    repo_name: str = "soldr-toolchain",
    branch: str = "assets",
    offline: bool = False,
    http_get_fn=http_get_text,
) -> dict[str, Any]:
    """Walk ``manifest_root`` (an assets-branch checkout) and produce
    the full asset index payload."""
    entries: list[dict[str, Any]] = []
    entries.extend(
        collect_local_blob_entries(manifest_root, repo_owner, repo_name, branch)
    )

    for tool_manifest in sorted(manifest_root.glob("*/manifest.json")):
        entries.extend(
            collect_release_entries_for_tool(
                tool_manifest,
                offline=offline,
                http_get_fn=http_get_fn,
            )
        )

    entries.sort(key=_entry_sort_key)
    # De-duplicate exact logical URLs; if two sources collide, the first
    # (local-blob) entry wins because it carries the on-disk sha. The URL is
    # part of the key because local catalogue bundles reuse filenames such as
    # bundle.tar.zst across platform directories.
    seen: set[tuple[str, str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        key = _entry_sort_key(entry)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)

    return {
        "schema_version": ASSET_INDEX_SCHEMA_VERSION,
        "entries": deduped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest-checkout",
        type=Path,
        required=True,
        help=(
            "Path to a local checkout of the soldr-toolchain ``assets`` "
            "branch (the orphan branch that hosts per-tool "
            "manifest.json files and the locally-hosted blobs under "
            "<tool>/<version>/<platform>/)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write the generated asset-index.json to.",
    )
    parser.add_argument(
        "--repo-owner",
        default="zackees",
        help="GitHub owner that hosts the toolchain repo (default: zackees).",
    )
    parser.add_argument(
        "--repo-name",
        default="soldr-toolchain",
        help="GitHub repo name (default: soldr-toolchain).",
    )
    parser.add_argument(
        "--branch",
        default="assets",
        help="Branch served by the CDN URLs (default: assets).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Skip the SHA256SUMS HTTP fetch step. Only the "
            "locally-hosted blob entries are emitted."
        ),
    )
    args = parser.parse_args(argv)

    manifest_root = args.manifest_checkout.resolve()
    if not manifest_root.is_dir():
        print(
            f"error: --manifest-checkout {manifest_root} is not a directory",
            file=sys.stderr,
        )
        return 2

    index = build_asset_index(
        manifest_root,
        repo_owner=args.repo_owner,
        repo_name=args.repo_name,
        branch=args.branch,
        offline=args.offline,
    )

    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index, indent=2, sort_keys=False) + "\n"
    output_path.write_text(payload, encoding="utf-8")

    print(
        f"asset-index: wrote {output_path} "
        f"({len(index['entries'])} entries, "
        f"schema_version={index['schema_version']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
