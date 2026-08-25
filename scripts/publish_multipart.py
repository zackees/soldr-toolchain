"""Compile an LFS-backed assets checkout into non-LFS public snapshots.

The input checkout must have its LFS objects materialized.  Payload bytes are
deterministically sliced into ordinary-Git-safe parts; the output directories
contain no attributes file and no LFS pointers.  Git ref creation is kept in
the workflow so this module remains deterministic and fully testable.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from scripts.catalogue_v2 import build_document
from scripts.publication_model import (
    MAX_PART_BYTES,
    TARGET_PART_BYTES,
    PartitionedAsset,
    VerifiedPublicLedger,
    canonical_json_sha256,
    partition_file,
    verify_declared_source,
)
from scripts.publisher_transaction import (
    GitDataApi,
    ExactOidMaterializer,
    PublishError,
    PublicStateAbsent,
    active_tree_covers,
    fetch_staging_ledger,
    fetch_retained_public_ledgers,
    fetch_verified_public_ledger,
    git_force_with_lease,
    github_transport,
    materialize_exact,
    publish_snapshot_pair,
    publish_www_snapshot,
    raw_files_verifier,
    raw_asset_verifier,
    record_public_proof,
    recover_retained_public_ledger,
    retention_plan,
    verified_retained_entries,
    verified_retained_part_index,
    verified_reused_entries,
)
from scripts.publication_model import PublishedPart, SourceInventoryRow, canonical_json_bytes, classify_inventory, parse_lfs_pointer

_LOCAL_URL = re.compile(
    r"^https://(?:media\.githubusercontent\.com/media|raw\.githubusercontent\.com)/"
    r"zackees/soldr-toolchain/assets/(?P<path>.+)$"
)
_EXTERNAL_LLVM_POLICY: dict[str, dict[str, str | int]] = {
    "https://media.githubusercontent.com/media/zackees/clang-tool-chain-bins/main/assets/clang/linux/x86_64/llvm-21.1.5-linux-x86_64.tar.zst": {
        "path": "clang/linux/x86_64/llvm-21.1.5-linux-x86_64.tar.zst",
        "asset": "llvm-21.1.5-linux-x86_64.tar.zst",
        "sha256": "4021cc49d70472122761709e7376835dfc857b5ec77183fa969b5f61d0f13a2f",
        "size_bytes": 98921700,
    },
    "https://media.githubusercontent.com/media/zackees/clang-tool-chain-bins/main/assets/clang/linux/arm64/llvm-21.1.5-linux-arm64.tar.zst": {
        "path": "clang/linux/arm64/llvm-21.1.5-linux-arm64.tar.zst",
        "asset": "llvm-21.1.5-linux-arm64.tar.zst",
        "sha256": "df774b7fc1e392458325552addb67bf8c11bd452ad7bc660cf77103c617f89c5",
        "size_bytes": 95393222,
    },
    "https://media.githubusercontent.com/media/zackees/clang-tool-chain-bins/main/assets/clang/win/x86_64/llvm-21.1.5-win-x86_64.tar.zst": {
        "path": "clang/win/x86_64/llvm-21.1.5-win-x86_64.tar.zst",
        "asset": "llvm-21.1.5-win-x86_64.tar.zst",
        "sha256": "8d6dd1cbc2261f8e6fa657b48f10a6e44223441d4b5487f056838cb8c2403a77",
        "size_bytes": 61647904,
    },
}
_SOURCE_INVENTORY = "source-inventory.v1.json"
_EXTERNAL_INVENTORY = "multipart-external-entries.v1.json"
_LOCAL_PAGES_JSON = re.compile(
    r"^https://zackees\.github\.io/soldr-toolchain/(?P<path>[^?#]+\.json)$"
)
_LFS_SIGNATURE = b"version https://git-lfs.github.com/spec/v1\n"
_PUBLIC_DATA_REF = re.compile(r"^public-[ab]$")


@dataclass(frozen=True)
class PublicationResult:
    assets: dict[str, PartitionedAsset]
    part_count: int
    total_part_bytes: int
    max_part_bytes: int


def _reused_asset(sha: str, mapping: dict[str, Any]) -> PartitionedAsset:
    part = mapping["partitioner"]
    return PartitionedAsset(
        sha, mapping["size_bytes"], part["version"], part["target_bytes"], tuple(PublishedPart(row["number"], row["sha256"], row["size_bytes"], row["path"]) for row in mapping["parts"])
    )


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # These files are fetched back by the publisher as digest-bound evidence.
    # A single canonical representation prevents a whitespace-only state from
    # changing the bytes which recovery trusts.
    path.write_bytes(canonical_json_bytes(value))


def _git_blob_id(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1()  # Git's object identity, not the integrity pin.
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry_path(url: str) -> str | None:
    match = _LOCAL_URL.fullmatch(url)
    if match is not None:
        return match.group("path")
    policy = _EXTERNAL_LLVM_POLICY.get(url)
    return str(policy["path"]) if policy is not None else None


def _source_entries(assets_dir: Path) -> list[dict[str, Any]]:
    """Merge multipart-local, direct compatibility, and external rows."""

    inventory = assets_dir / _SOURCE_INVENTORY
    external = assets_dir / _EXTERNAL_INVENTORY
    if not inventory.is_file() or not external.is_file():
        raise ValueError("multipart publication requires source and external inventories")
    source_document = _load(inventory)
    external_document = _load(external)
    if source_document.get("schema_version") != 5:
        raise ValueError("source inventory must use schema_version 5")
    if external_document.get("schema_version") != 1:
        raise ValueError("external inventory must use schema_version 1")
    source_rows = source_document.get("entries")
    external_rows = external_document.get("entries")
    expected_source = external_document.get("expected_source_entries")
    expected_external = external_document.get("expected_external_entries")
    if not isinstance(source_rows, list) or len(source_rows) != expected_source:
        raise ValueError("source inventory entry count does not match publication policy")
    if not isinstance(external_rows, list) or len(external_rows) != expected_external:
        raise ValueError("external inventory entry count does not match publication policy")
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("url"), str)
        or _LOCAL_URL.fullmatch(row["url"]) is None
        for row in source_rows
    ):
        raise ValueError("source inventory contains a non-local multipart row")
    documents = [source_document, _load(assets_dir / "catalogue.v1.json"), external_document]

    entries: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for document in documents:
        rows = document.get("entries") if isinstance(document, dict) else None
        if not isinstance(rows, list):
            raise ValueError("source inventory entries must be a list")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                raise ValueError("source inventory entry lacks url")
            if row["url"] not in seen_urls:
                entries.append(row)
                seen_urls.add(row["url"])
    return entries


def _external_entries(assets_dir: Path) -> dict[str, dict[str, Any]]:
    path = assets_dir / _EXTERNAL_INVENTORY
    if not path.is_file():
        raise ValueError("multipart publication requires an external inventory")
    document = _load(path)
    if document.get("schema_version") != 1:
        raise ValueError("external inventory must use schema_version 1")
    entries = document.get("entries")
    if not isinstance(entries, list) or len(entries) != document.get("expected_external_entries"):
        raise ValueError("external inventory entry count does not match publication policy")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            raise ValueError("external multipart entry lacks url")
        relative = _entry_path(entry["url"])
        sha, size = entry.get("sha256"), entry.get("size_bytes")
        policy = _EXTERNAL_LLVM_POLICY.get(entry["url"])
        if relative is None or policy is None:
            raise ValueError("external multipart URL is not allowlisted")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("external multipart entry has invalid sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError("external multipart entry has invalid size_bytes")
        for key in ("asset", "sha256", "size_bytes"):
            if entry.get(key) != policy[key]:
                raise ValueError(f"external multipart entry violates pinned {key} policy")
        if relative in result:
            raise ValueError("duplicate external multipart source path")
        result[relative] = entry
    if set(entry["url"] for entry in entries) != set(_EXTERNAL_LLVM_POLICY):
        raise ValueError("external multipart inventory does not match the pinned URL policy")
    return result


def _prepare_external_pointers(assets_dir: Path) -> dict[str, dict[str, Any]]:
    entries = _external_entries(assets_dir)
    for relative, entry in entries.items():
        source = _source_path(assets_dir, relative)
        if source.is_file():
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(
            (
                "version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{entry['sha256']}\n"
                f"size {entry['size_bytes']}\n"
            ).encode("ascii")
        )
    return entries


def _source_path(root: Path, relative: str) -> Path:
    """Resolve a catalogue path without allowing a hostile URL to escape assets."""
    candidate = (root / Path(relative)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"catalogued LFS source has an unsafe path: {relative}") from None
    return candidate


class GitLfsPathMaterializer(ExactOidMaterializer):
    """Production materializer: fetch one declared path, never a broad pull."""

    def __init__(self, checkout: Path, source_path: str) -> None:
        self.checkout, self.source_path = Path(checkout), source_path

    def materialize_path(self, oid_sha256: str, size_bytes: int) -> Path:
        path = _source_path(self.checkout, self.source_path)
        # ``--include`` is deliberately an exact repo-relative path.  The
        # subsequent OID/size check in materialize_exact makes server/filter
        # mistakes fail closed.
        completed = subprocess.run(
            ["git", "lfs", "pull", "origin", "--include=" + self.source_path],
            cwd=self.checkout,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise ValueError("git lfs failed for exact path " + self.source_path)
        verify_declared_source(path, oid_sha256, size_bytes)
        return path

    def materialize(self, oid_sha256: str, size_bytes: int) -> bytes:
        """Compatibility adapter; production selection uses ``materialize_path``."""
        return self.materialize_path(oid_sha256, size_bytes).read_bytes()


class HttpPathMaterializer(ExactOidMaterializer):
    """Fetch one allowlisted external LFS object exactly once for slicing."""

    def __init__(self, checkout: Path, source_path: str, url: str) -> None:
        self.checkout, self.source_path, self.url = Path(checkout), source_path, url

    def materialize_path(self, oid_sha256: str, size_bytes: int) -> Path:
        path = _source_path(self.checkout, self.source_path)
        request = urllib.request.Request(self.url, headers={"User-Agent": "soldr-toolchain-multipart-publisher/1"})
        temp_path: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise ValueError("external response has an invalid Content-Length") from exc
                    if declared_length != size_bytes:
                        raise ValueError("external response Content-Length does not match inventory")
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                    temp_path = Path(temporary.name)
                    observed = 0
                    while chunk := response.read(1024 * 1024):
                        observed += len(chunk)
                        if observed > size_bytes:
                            raise ValueError("external response exceeds declared size")
                        temporary.write(chunk)
                if observed != size_bytes:
                    raise ValueError("external response is shorter than declared size")
            assert temp_path is not None
            verify_declared_source(temp_path, oid_sha256, size_bytes)
            temp_path.replace(path)
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise
        return path

    def materialize(self, oid_sha256: str, size_bytes: int) -> bytes:
        return self.materialize_path(oid_sha256, size_bytes).read_bytes()


def scan_unsmudged_catalogue(assets_dir: Path) -> list[SourceInventoryRow]:
    """Inventory every catalogue row before any LFS transfer.

    Strict pointers are LFS rows; all other URL forms are direct rows.  A
    materialized byte stream at this stage is rejected: the source checkout is
    expected to be unsmudged, so accidental broad LFS pulls cannot silently
    enlarge the publisher's transfer set.
    """
    _prepare_external_pointers(assets_dir)
    rows = _source_entries(assets_dir)
    identity_counts = Counter(
        tuple(entry.get(key) for key in ("owner", "repo", "tag", "asset"))
        for entry in rows
        if isinstance(entry, dict)
    )
    result: list[SourceInventoryRow] = []
    for entry in rows:
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            raise ValueError("catalogue v1 entry lacks url")
        provenance = {key: entry.get(key) for key in ("owner", "repo", "tag", "asset")}
        fingerprint = hashlib.sha256(canonical_json_bytes(entry)).hexdigest()
        relative = _entry_path(entry["url"])
        identity = tuple(entry.get(key) for key in ("owner", "repo", "tag", "asset"))
        logical_asset = relative if relative is not None and identity_counts[identity] > 1 else entry.get("asset", "")
        logical = "\0".join(str(entry.get(key, "")) for key in ("owner", "repo", "tag")) + "\0" + str(logical_asset)
        if relative is None:
            result.append(SourceInventoryRow(logical, "-", str(entry.get("asset", "-")), provenance=provenance, transport="direct", metadata_fingerprint=fingerprint))
            continue
        raw = _source_path(assets_dir, relative).read_bytes()
        try:
            oid, size = parse_lfs_pointer(raw)
        except ValueError as exc:
            raise ValueError("source is not an unsmudged strict LFS pointer: " + relative) from exc
        result.append(SourceInventoryRow(logical, relative, str(entry.get("asset", "-")), oid, size, provenance, "lfs", fingerprint))
    return result


def materialize_selected(rows: Iterable[SourceInventoryRow], classifications: dict[str, str], assets_dir: Path, *, materializer_factory=GitLfsPathMaterializer) -> dict[str, bytes | Path]:
    """Materialize only new/invalid/repartition OIDs, once per immutable OID."""
    result: dict[str, bytes | Path] = {}
    external = _external_entries(assets_dir)
    for row in rows:
        if row.transport != "lfs" or classifications.get(row.logical_key) not in {"new_or_invalid", "explicit_repartition"}:
            continue
        assert row.source_oid_sha256 is not None and row.source_size_bytes is not None
        if row.source_oid_sha256 not in result:
            if row.source_path in external and materializer_factory is GitLfsPathMaterializer:
                materializer = HttpPathMaterializer(assets_dir, row.source_path, external[row.source_path]["url"])
            else:
                materializer = materializer_factory(assets_dir, row.source_path)
            materialize_path = getattr(materializer, "materialize_path", None)
            if callable(materialize_path):
                path = materialize_path(row.source_oid_sha256, row.source_size_bytes)
                verify_declared_source(path, row.source_oid_sha256, row.source_size_bytes)
                result[row.source_oid_sha256] = path
            else:
                result[row.source_oid_sha256] = materialize_exact(materializer, row.source_oid_sha256, row.source_size_bytes)
    return result


def _verify_source_alias(source: Path, oid_sha256: str, size_bytes: int) -> None:
    """Accept exact bytes or an exact unsmudged pointer for one OID alias."""
    if source.stat().st_size <= 1024:
        raw = source.read_bytes()
        if raw.startswith(_LFS_SIGNATURE):
            pointer_oid, pointer_size = parse_lfs_pointer(raw)
            if (pointer_oid, pointer_size) != (oid_sha256, size_bytes):
                raise ValueError("LFS alias pointer does not match catalogue OID/size")
            return
    verify_declared_source(source, oid_sha256, size_bytes)


def _part_wire(asset: PartitionedAsset, data_ref: str) -> list[dict[str, Any]]:
    return [
        {
            "number": part.number,
            "sha256": part.sha256,
            "size_bytes": part.size_bytes,
            "urls": [f"https://raw.githubusercontent.com/zackees/soldr-toolchain/{data_ref}/{part.path}"],
        }
        for part in asset.parts
    ]


def _rewrite_assets(value: Any, published: dict[str, PartitionedAsset], data_ref: str) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            # Queue only the original children. Generated multipart descriptors
            # are already final wire data and must not be traversed or rewritten.
            pending.extend(current.values())
            sha = current.get("sha256")
            if isinstance(sha, str) and sha in published and "urls" in current:
                current.pop("urls", None)
                current["size_bytes"] = published[sha].size_bytes
                current["parts"] = _part_wire(published[sha], data_ref)
        elif isinstance(current, list):
            pending.extend(current)


def _raise_on_lfs_reference(root: Path) -> None:
    for path in root.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        if "media.githubusercontent.com/media/" in text or "raw.githubusercontent.com/zackees/soldr-toolchain/assets" in text:
            raise ValueError(f"www metadata retains an LFS delivery URL: {path}")
        raw_base = "https://raw.githubusercontent.com/zackees/soldr-toolchain/"
        allowed = re.compile(re.escape(raw_base) + r"public-[ab]/")
        for match in re.finditer(re.escape(raw_base), text):
            if allowed.match(text, match.start()) is None:
                raise ValueError(f"www metadata exposes a mutable data ref: {path}")


def retarget_public_data_ref(www_dir: Path, old_ref: str, new_ref: str, generation: str) -> None:
    """Retarget a metadata-only build and restore its catalogue binding."""
    old = f"/soldr-toolchain/{old_ref}/".encode()
    new = f"/soldr-toolchain/{new_ref}/".encode()
    for path in www_dir.rglob("*.json"):
        raw = path.read_bytes()
        if old in raw:
            path.write_bytes(raw.replace(old, new))
    catalogue = _load(www_dir / "generations" / generation / "catalogue.v2.json")
    digest = canonical_json_sha256(catalogue)
    for state_path in (www_dir / "publish-state.v1.json", www_dir / "generations" / generation / "publish-state.v1.json"):
        state = _load(state_path)
        state["catalogue_sha256"] = digest
        _write(state_path, state)


def build_publication(
    assets_dir: Path,
    public_dir: Path,
    www_dir: Path,
    *,
    source_commit: str,
    source_tree: str,
    active_slot: str,
    generation: str,
    data_ref: str | None = None,
    target_bytes: int = TARGET_PART_BYTES,
    direct_sizes: dict[str, dict[str, Any]] | None = None,
    reuse_mappings: dict[str, Any] | None = None,
    reuse_part_blobs: dict[tuple[str, int], str] | None = None,
    retained_generations: list[dict[str, Any]] | None = None,
    retained_ledgers: list[VerifiedPublicLedger] | None = None,
    published_at: int | None = None,
) -> PublicationResult:
    """Build one complete data tree plus one generation-qualified site."""
    if active_slot not in {"public-a", "public-b"}:
        raise ValueError("active slot must be public-a or public-b")
    if data_ref is None:
        data_ref = active_slot
    if not _PUBLIC_DATA_REF.fullmatch(data_ref):
        raise ValueError("data ref must name public-a or public-b")
    publish_timestamp = int(time.time()) if published_at is None else published_at
    if public_dir.exists() or www_dir.exists():
        raise ValueError("publication outputs must not already exist")
    _prepare_external_pointers(assets_dir)
    entries_v1 = _source_entries(assets_dir)
    identity_counts = Counter(
        tuple(row.get(key) for key in ("owner", "repo", "tag", "asset"))
        for row in entries_v1
        if isinstance(row, dict)
    )

    # Materialization is OID-deduplicated, so the checkout path holding bytes
    # need not be the first logical alias after deterministic catalogue sort.
    materialized_sources: dict[str, Path] = {}
    for candidate in entries_v1:
        if not isinstance(candidate, dict):
            continue
        candidate_url, candidate_sha = candidate.get("url"), candidate.get("sha256")
        if not isinstance(candidate_url, str) or not isinstance(candidate_sha, str):
            continue
        candidate_rel = _entry_path(candidate_url)
        if candidate_rel is None:
            continue
        candidate_source = _source_path(assets_dir, candidate_rel)
        if candidate_source.is_file():
            with candidate_source.open("rb") as handle:
                is_pointer = handle.read(len(_LFS_SIGNATURE)) == _LFS_SIGNATURE
            if not is_pointer:
                materialized_sources.setdefault(candidate_sha, candidate_source)

    public_dir.mkdir(parents=True)
    published: dict[str, PartitionedAsset] = {}
    entries_v2: list[dict[str, Any]] = []
    logical_assets: dict[str, Any] = {}
    for row in sorted(entries_v1, key=lambda value: tuple(str(value.get(key, "")) for key in ("owner", "repo", "tag", "asset")) if isinstance(value, dict) else ("",)):
        if not isinstance(row, dict):
            raise ValueError("catalogue v1 contains a non-object entry")
        url = row.get("url")
        sha = row.get("sha256")
        if not isinstance(url, str) or not isinstance(sha, str):
            raise ValueError("catalogue v1 entry lacks url/sha256")
        rel = _entry_path(url)
        base = {key: row[key] for key in ("owner", "repo", "tag", "asset", "sha256")}
        identity = tuple(row[key] for key in ("owner", "repo", "tag", "asset"))
        if rel is not None and identity_counts[identity] > 1:
            # v1 historically reused generic filenames such as
            # `bundle.tar.zst` across many publisher-owned paths. v2 identities
            # are unique, so only those collisions use the already-canonical
            # source path as their asset key. Direct upstream identities stay
            # byte-for-byte compatible.
            base["asset"] = rel
        logical_key = "\0".join(str(base[key]) for key in ("owner", "repo", "tag", "asset"))
        if rel is None:
            pages_match = _LOCAL_PAGES_JSON.fullmatch(url)
            if pages_match is not None:
                pages_source = _source_path(assets_dir, pages_match.group("path"))
                if not pages_source.is_file():
                    raise ValueError(f"local Pages catalogue source is missing: {url}")
                deployed = canonical_json_bytes(_load(pages_source))
                entries_v2.append(
                    {
                        **base,
                        "sha256": hashlib.sha256(deployed).hexdigest(),
                        "size_bytes": len(deployed),
                        "urls": [
                            "https://zackees.github.io/soldr-toolchain/"
                            f"generations/{generation}/{pages_match.group('path')}"
                        ],
                    }
                )
                continue
            size = row.get("size_bytes")
            local_pages = assets_dir / Path(url).name
            if not isinstance(size, int) and local_pages.is_file():
                size = local_pages.stat().st_size
            pinned = (direct_sizes or {}).get(url)
            if not isinstance(size, int) and isinstance(pinned, dict):
                if pinned.get("sha256") != sha:
                    raise ValueError(f"direct size metadata has the wrong SHA-256: {url}")
                size = pinned.get("size_bytes")
            if not isinstance(size, int) or size <= 0:
                raise ValueError(f"direct catalogue entry needs size_bytes: {url}")
            entries_v2.append({**base, "size_bytes": size, "urls": [url]})
            continue
        source = _source_path(assets_dir, rel)
        if not source.is_file():
            raise ValueError(f"catalogued LFS source is missing: {rel}")
        prior = (reuse_mappings or {}).get(sha)
        # A verified immutable mapping supplies the size and part vector for
        # aliases/exact hits; leave their source pointer unsmudged.
        if isinstance(prior, dict):
            found = published.get(sha) or _reused_asset(sha, prior)
            size = found.size_bytes
            _verify_source_alias(source, sha, size)
            published[sha] = found
            entries_v2.append({**base, "size_bytes": size, "source_path": rel, "parts": _part_wire(found, data_ref)})
            logical_assets[logical_key] = {
                "source_path": rel,
                "asset": row["asset"],
                "source_oid_sha256": sha,
                "source_size_bytes": size,
                "metadata_fingerprint": hashlib.sha256(canonical_json_bytes(row)).hexdigest(),
                "provenance": {key: row.get(key) for key in ("owner", "repo", "tag", "asset")},
            }
            continue
        found = published.get(sha)
        if found is None:
            payload_source = materialized_sources.get(sha)
            if payload_source is None:
                raise ValueError(f"LFS source was not materialized: {rel}")
            size = payload_source.stat().st_size
            found = partition_file(
                payload_source,
                public_dir,
                declared_oid_sha256=sha,
                declared_size_bytes=size,
                target_bytes=target_bytes,
            )
            published[sha] = found
        size = found.size_bytes
        _verify_source_alias(source, sha, size)
        entries_v2.append({**base, "size_bytes": size, "source_path": rel, "parts": _part_wire(found, data_ref)})
        logical_assets[logical_key] = {
            "source_path": rel,
            "asset": row["asset"],
            "source_oid_sha256": sha,
            "source_size_bytes": size,
            "metadata_fingerprint": hashlib.sha256(canonical_json_bytes(row)).hexdigest(),
            "provenance": {key: row.get(key) for key in ("owner", "repo", "tag", "asset")},
        }

    generation_root = www_dir / "generations" / generation
    generation_root.mkdir(parents=True)
    if (assets_dir / "index.html").is_file():
        shutil.copyfile(assets_dir / "index.html", generation_root / "index.html")
    for source in sorted(assets_dir.rglob("*.json"), key=lambda item: item.as_posix()):
        rel = source.relative_to(assets_dir)
        if rel.as_posix() in {
            "catalogue.v1.json",
            "asset-index.json",
            _SOURCE_INVENTORY,
            _EXTERNAL_INVENTORY,
        }:
            continue
        document = _load(source)
        _rewrite_assets(document, published, data_ref)
        # A multipart release requires capability 2.
        if isinstance(document, dict) and document.get("kind") == "Catalog":
            for release in document.get("releases", []):
                if isinstance(release, dict) and "parts" in json.dumps(release):
                    release["min_client_version"] = 2
        _write(generation_root / rel, document)

    root_manifest = generation_root / "manifest.json"
    if root_manifest.is_file():
        root = _load(root_manifest)
        for tool, item in root.get("tools", {}).items():
            child = generation_root / tool / "manifest.json"
            if child.is_file() and isinstance(item, dict) and isinstance(item.get("descriptor"), dict):
                content = child.read_bytes()
                item["descriptor"]["url"] = f"generations/{generation}/{tool}/manifest.json"
                item["descriptor"]["size_bytes"] = len(content)
                item["descriptor"]["sha256"] = hashlib.sha256(content).hexdigest()
        _write(root_manifest, root)

    state_url = f"https://zackees.github.io/soldr-toolchain/generations/{generation}/publish-state.v1.json"
    catalogue_v2 = build_document(
        entries_v2,
        generation=generation,
        publication_state_url=state_url,
        origin="https://zackees.github.io/soldr-toolchain/catalogue.v2.json",
    )
    _write(generation_root / "catalogue.v2.json", catalogue_v2)
    assets_by_sha = {}
    for sha, asset in published.items():
        assets_by_sha[sha] = {
            "size_bytes": asset.size_bytes,
            "partitioner": {"version": asset.partitioner_version, "target_bytes": asset.target_bytes},
            "parts": [
                {
                    "number": part.number,
                    "sha256": part.sha256,
                    "size_bytes": part.size_bytes,
                    "path": part.path,
                    # Reused parts retain their known immutable blob identity;
                    # novel parts are on disk and acquire their Git identity
                    # before the data tree is constructed.
                    "git_blob": (
                        (reuse_part_blobs or {}).get((part.sha256, part.size_bytes))
                        or (
                            (reuse_mappings or {}).get(sha, {}).get("parts", [{}] * len(asset.parts))[part.number - 1].get("git_blob")
                            if not (public_dir / part.path).is_file()
                            else _git_blob_id(public_dir / part.path)
                        )
                    ),
                }
                for part in asset.parts
            ],
        }
    state = {
        "schema_version": 1,
        "generation": generation,
        "source": {"branch": "assets", "commit": source_commit, "tree": source_tree},
        "active": {"slot": active_slot},
        "previous": {"slot": "public-b" if active_slot == "public-a" else "public-a"},
        "partitioner_default": {"version": 1, "target_bytes": target_bytes, "max_bytes": MAX_PART_BYTES},
        "catalogue_sha256": canonical_json_sha256(catalogue_v2),
        "logical_assets": logical_assets,
        "assets_by_sha256": assets_by_sha,
        "published_at": publish_timestamp,
        "retained_generations": retained_generations or [{"generation": generation, "published_at": publish_timestamp}],
    }
    parts_by_sha256: dict[str, dict[str, Any]] = {}
    for mapping in assets_by_sha.values():
        for part in mapping["parts"]:
            row = {"size_bytes": part["size_bytes"], "git_blob": part["git_blob"]}
            old = parts_by_sha256.setdefault(part["sha256"], row)
            if old != row:
                raise ValueError("part digest maps to conflicting immutable blob")
    state["parts_by_sha256"] = dict(sorted(parts_by_sha256.items()))
    _write(generation_root / "publish-state.v1.json", state)

    # Pages artifacts replace the prior deployment wholesale.  Preserve the
    # canonical state/catalogue pair for every retained generation so an older
    # catalogue's generation-qualified publication_state URL remains public.
    for retained in retained_ledgers or []:
        old_generation = retained.binding.generation
        if old_generation == generation:
            continue
        old_root = www_dir / "generations" / old_generation
        _write(old_root / "publish-state.v1.json", retained.ledger)
        _write(old_root / "catalogue.v2.json", retained.catalogue)

    schema_source = Path(__file__).resolve().parent.parent / "schemas" / "catalogue.v2.schema.json"
    if schema_source.is_file():
        schema_target = generation_root / "schemas" / "catalogue.v2.schema.json"
        schema_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(schema_source, schema_target)

    # Stable aliases are copied only after every immutable generation file is complete.
    for source in generation_root.rglob("*"):
        if not source.is_file():
            continue
        rel = source.relative_to(generation_root)
        target = www_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (www_dir / ".nojekyll").write_text("", encoding="utf-8")
    _raise_on_lfs_reference(www_dir)
    parts = [part for asset in published.values() for part in asset.parts]
    return PublicationResult(
        published,
        len(parts),
        sum(part.size_bytes for part in parts),
        max((part.size_bytes for part in parts), default=0),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--public-dir", type=Path, required=True)
    parser.add_argument("--www-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument(
        "--active-slot",
        choices=("public-a", "public-b"),
        help="target slot override; publishers derive it from verified state",
    )
    parser.add_argument("--generation", required=True)
    parser.add_argument(
        "--published-at",
        type=int,
        help="publication-attempt timestamp (persisted in immutable staging for deterministic retries)",
    )
    parser.add_argument("--direct-sizes", type=Path)
    parser.add_argument("--target-bytes", type=int, default=TARGET_PART_BYTES)
    parser.add_argument("--owner", default="zackees")
    parser.add_argument("--repo", default="soldr-toolchain")
    parser.add_argument("--publish", action="store_true", help="perform authenticated Git Data API writes (default is local-only)")
    parser.add_argument("--publish-existing", action="store_true", help="publish already-built snapshots; requires --publish")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--bootstrap", action="store_true", help="explicitly create the first two-slot publication")
    parser.add_argument("--verify-public", action="store_true", help="verify Pages bytes against immutable generation-www evidence")
    args = parser.parse_args()
    if args.publish_existing and not args.publish:
        parser.error("--publish-existing requires --publish")
    direct_sizes = None
    if args.direct_sizes is not None:
        size_document = _load(args.direct_sizes)
        direct_sizes = size_document.get("entries")
        if not isinstance(direct_sizes, dict):
            raise ValueError("direct-sizes entries must be an object")
    if args.verify_public:
        token = os.environ.get(args.token_env, "")
        verified_api = GitDataApi(args.owner, args.repo, github_transport(token))
        verified, www_commit, www_tree = fetch_verified_public_ledger(verified_api, pages_base=f"https://{args.owner}.github.io/{args.repo}")
        record_public_proof(verified_api, generation=verified.binding.generation, www_commit=www_commit)
        print(json.dumps({"public_verified": True, "generation": verified.binding.generation, "generation_www_commit": www_commit, "generation_www_tree": www_tree}, sort_keys=True))
        return 0
    api = None
    classifications: dict[str, str] = {}
    ledger = None
    reuse_mappings: dict[str, Any] = {}
    reused_entries: dict[str, str] = {}
    retained_entries: dict[str, str] = {}
    reuse_part_blobs: dict[tuple[str, int], str] = {}
    retained_index: list[dict[str, Any]] = []
    retained_ledgers: list[VerifiedPublicLedger] = []
    staged_metadata: Mapping[str, Any] | None = None
    staged_entries: dict[str, str] = {}
    staged_part_blobs: dict[tuple[str, int], str] = {}
    inventory_rows: list[SourceInventoryRow] = []
    publication_time = args.published_at if args.published_at is not None else int(time.time())
    recovered_public_state = False
    if args.publish:
        token = os.environ.get(args.token_env, "")
        api = GitDataApi(
            args.owner,
            args.repo,
            github_transport(token),
            lease_mover=git_force_with_lease(args.assets_dir, args.owner, args.repo, token),
        )
        try:
            ledger, _, _ = fetch_verified_public_ledger(api, pages_base=f"https://{args.owner}.github.io/{args.repo}", require_public_proof=True)
        except PublicStateAbsent:
            try:
                ledger = recover_retained_public_ledger(api)
                recovered_public_state = True
            except PublishError:
                if not args.bootstrap:
                    raise ValueError("no verified retained public generation; explicit --bootstrap is required") from None
                if any(api.optional_ref(ref) is not None for ref in ("refs/heads/public-a", "refs/heads/public-b")):
                    raise ValueError("public slots already exist; bootstrap cannot replace missing public state")
                if args.active_slot is None:
                    args.active_slot = "public-a"
                inventory_rows = scan_unsmudged_catalogue(args.assets_dir)
                classifications = {row.logical_key: ("new_or_invalid" if row.transport == "lfs" else "direct") for row in inventory_rows}
                staged = fetch_staging_ledger(api, generation=args.generation, source_commit=args.source_commit, source_tree=args.source_tree)
                if staged is not None:
                    staged_state, _ = staged
                    staged_metadata, publication_time = staged_state, staged_state["published_at"]
                    reuse_mappings = dict(staged_state["assets_by_sha256"])
                    for row in inventory_rows:
                        if row.transport == "lfs" and row.source_oid_sha256 in reuse_mappings:
                            classifications[row.logical_key] = "alias"
                    for mapping in reuse_mappings.values():
                        for part in mapping["parts"]:
                            staged_entries[part["path"]] = part["git_blob"]
                            staged_part_blobs[(part["sha256"], part["size_bytes"])] = part["git_blob"]
                    reuse_part_blobs, reused_entries = dict(staged_part_blobs), dict(staged_entries)
                materialize_selected(inventory_rows, classifications, args.assets_dir)
        except Exception:
            # Stable root can be corrupt or temporarily stale.  Recovery has a
            # much narrower trust root: only immutable refs created after a
            # successful post-deploy verifier may participate.
            ledger = recover_retained_public_ledger(api)
            recovered_public_state = True
        if ledger is not None:
            expected_target = "public-b" if ledger.binding.active_slot == "public-a" else "public-a"
            if args.active_slot is None:
                args.active_slot = expected_target
            elif args.active_slot != expected_target:
                raise ValueError("active-slot must be the verified inactive public slot")
            inventory_rows = scan_unsmudged_catalogue(args.assets_dir)
            classifications = classify_inventory(inventory_rows, ledger)
            # A source-identical exact inventory is a true no-op: no LFS,
            # blobs, trees, commits, or refs are created.
            if not recovered_public_state and args.source_commit == ledger.binding.source_commit and all(kind in {"exact_hit", "direct"} for kind in classifications.values()):
                # Pages proof binds metadata, while this separately proves the
                # complete path/blob/type/size set in the active data tree.
                verified_reused_entries(api, ledger)
                print(json.dumps({"published": False, "noop": True, "metrics": {"lfs_bytes": 0, "uploads": 0, "commits": 0}}, sort_keys=True))
                return 0
            staged = fetch_staging_ledger(api, generation=args.generation, source_commit=args.source_commit, source_tree=args.source_tree)
            if staged is not None:
                staged_state, _ = staged
                staged_metadata = staged_state
                publication_time = staged_state["published_at"]
                staged_assets = staged_state["assets_by_sha256"]
                for row in inventory_rows:
                    if row.transport == "lfs" and row.source_oid_sha256 in staged_assets:
                        # Exact source is already sliced, tree-bound, and raw
                        # revalidated when its immutable ref is reused.
                        classifications[row.logical_key] = "alias"
                reuse_mappings = dict(staged_assets)
                for mapping in staged_assets.values():
                    for part in mapping["parts"]:
                        staged_entries[part["path"]] = part["git_blob"]
                        staged_part_blobs[(part["sha256"], part["size_bytes"])] = part["git_blob"]
            if recovered_public_state and args.generation == ledger.binding.generation:
                publication_time = int(ledger.ledger["published_at"])
            materialize_selected(inventory_rows, classifications, args.assets_dir)
            retained = fetch_retained_public_ledgers(api, pages_base=f"https://{args.owner}.github.io/{args.repo}", stable=ledger)
            retained_ledgers = retained
            reuse_mappings = reuse_mappings | {sha: mapping for retained_ledger in retained for sha, mapping in retained_ledger.ledger["assets_by_sha256"].items()}
            retained_entries = verified_retained_entries(api, retained)
            reuse_part_blobs = staged_part_blobs | verified_retained_part_index(api, retained)
            reused_entries = staged_entries | dict(retained_entries)
            now = publication_time
            candidates = [{"generation": args.generation, "published_at": now}] + [
                {"generation": item.binding.generation, "published_at": int(item.ledger.get("published_at", 0))} for item in retained
            ]
            kept = retention_plan(candidates, now)["keep"]
            retained_index = []
            seen_generations: set[str] = set()
            for item in candidates:
                if item["generation"] in kept and item["generation"] not in seen_generations:
                    retained_index.append(item)
                    seen_generations.add(item["generation"])
    if args.active_slot is None:
        raise ValueError("--active-slot is required for a local-only publication build")
    # Build against the inactive slot first. Only the complete active-tree
    # coverage proof below may retarget this to the currently live slot. In
    # particular, exact/alias staging metadata does not prove those blobs are
    # present in the live alias after an interrupted transaction.
    metadata_only = False
    published_slot = args.active_slot
    data_ref = published_slot
    result = (
        None
        if args.publish_existing
        else build_publication(
            args.assets_dir,
            args.public_dir,
            args.www_dir,
            source_commit=args.source_commit,
            source_tree=args.source_tree,
            active_slot=published_slot,
            generation=args.generation,
            data_ref=data_ref,
            target_bytes=args.target_bytes,
            direct_sizes=direct_sizes,
            reuse_mappings=reuse_mappings,
            reuse_part_blobs=reuse_part_blobs,
            retained_generations=retained_index or None,
            retained_ledgers=retained_ledgers,
            published_at=publication_time,
        )
    )
    if result is not None:
        # Desired tree = current catalogue only.  Never retain blobs solely
        # because an older ledger mentioned them.
        desired = set(result.assets)
        reused_entries = retained_entries | {
            part["path"]: part["git_blob"] for sha, mapping in reuse_mappings.items() if sha in desired for part in mapping["parts"] if not (args.public_dir / part["path"]).is_file()
        }
        for asset in result.assets.values():
            for part in asset.parts:
                blob = reuse_part_blobs.get((part.sha256, part.size_bytes))
                if blob is not None:
                    reused_entries[part.path] = blob
        built_state = _load(args.www_dir / "generations" / args.generation / "publish-state.v1.json")
        staged_metadata = {
            "schema_version": 1,
            "kind": "staging-data",
            "generation": args.generation,
            "source": {"branch": "assets", "commit": args.source_commit, "tree": args.source_tree},
            "published_at": publication_time,
            "assets_by_sha256": built_state["assets_by_sha256"],
            "logical_assets": built_state["logical_assets"],
            "parts_by_sha256": built_state["parts_by_sha256"],
        }
    expected: dict[str, tuple[str, int]] = {}
    novel_expected: dict[str, tuple[str, int]] = {}
    if result is not None:
        expected = {part.path: (part.sha256, part.size_bytes) for asset in result.assets.values() for part in asset.parts}
        for file in sorted(args.public_dir.rglob("*.part"), key=lambda item: item.as_posix()):
            data = file.read_bytes()
            relative = file.relative_to(args.public_dir).as_posix()
            if relative not in reused_entries:
                novel_expected[relative] = (hashlib.sha256(data).hexdigest(), len(data))
    elif args.public_dir.is_dir():
        for file in sorted(args.public_dir.rglob("*.part"), key=lambda item: item.as_posix()):
            data = file.read_bytes()
            expected[file.relative_to(args.public_dir).as_posix()] = (hashlib.sha256(data).hexdigest(), len(data))
        novel_expected = dict(expected)
    else:
        parser.error("--publish-existing public directory does not exist")
    summary: dict[str, Any] = {
        "parts": len(expected),
        "bytes": sum(size for _, size in expected.values()),
        "max_part_bytes": max((size for _, size in expected.values()), default=0),
        "published": False,
    }
    if args.publish:
        assert api is not None
        # Source is leased too: a completed local compilation is invalid if
        # assets moved while LFS bytes were being fetched/sliced.
        if api.ref("refs/heads/assets") != args.source_commit or api.commit_tree(args.source_commit) != args.source_tree:
            raise ValueError("assets source lease changed during publication")
        raw_base = f"https://raw.githubusercontent.com/{args.owner}/{args.repo}"

        def verify_www(commit: str) -> bool:
            expected_www = {item.relative_to(args.www_dir).as_posix(): item.read_bytes() for item in args.www_dir.rglob("*") if item.is_file()}
            return raw_files_verifier(raw_base, expected_www)(commit)

        def verify_source_lease() -> bool:
            return api.ref("refs/heads/assets") == args.source_commit and api.commit_tree(args.source_commit) == args.source_tree

        def bind_data_state(active_commit: str, active_tree: str, previous_commit: str, previous_tree: str) -> None:
            for state_path in (args.www_dir / "publish-state.v1.json", args.www_dir / "generations" / args.generation / "publish-state.v1.json"):
                state = _load(state_path)
                state["active"].update({"commit": active_commit, "tree": active_tree})
                state["previous"].update({"commit": previous_commit, "tree": previous_tree})
                _write(state_path, state)

        if ledger is not None and result is not None:
            desired_entries = dict(reused_entries)
            generated_state = _load(args.www_dir / "generations" / args.generation / "publish-state.v1.json")
            for mapping in generated_state["assets_by_sha256"].values():
                for part in mapping["parts"]:
                    desired_entries[part["path"]] = part["git_blob"]
            if active_tree_covers(api, ledger.binding.active_tree, desired_entries):
                retarget_public_data_ref(
                    args.www_dir,
                    args.active_slot,
                    ledger.binding.active_slot,
                    args.generation,
                )
                metadata_only = True

        selected_sha256 = {
            row.source_oid_sha256
            for row in inventory_rows
            if row.transport == "lfs" and row.source_oid_sha256 is not None and classifications.get(row.logical_key) in {"new_or_invalid", "explicit_repartition"}
        }
        if metadata_only:
            www_commit = publish_www_snapshot(
                api,
                www_dir=args.www_dir,
                generation=args.generation,
                active_commit=ledger.binding.active_commit,
                active_tree=ledger.binding.active_tree,
                previous_commit=ledger.binding.previous_commit,
                previous_tree=ledger.binding.previous_tree,
                active_slot=ledger.binding.active_slot,
                previous_slot=ledger.binding.previous_slot,
                raw_verify_www=verify_www,
            )
            data_commit = ledger.binding.active_commit
        else:
            verification_state = staged_metadata if staged_metadata is not None else _load(args.www_dir / "generations" / args.generation / "publish-state.v1.json")
            data_commit, www_commit = publish_snapshot_pair(
                api,
                public_dir=args.public_dir,
                www_dir=args.www_dir,
                generation=args.generation,
                active_slot=args.active_slot,
                raw_verify=lambda commit, metadata: raw_asset_verifier(
                    raw_base,
                    verification_state["assets_by_sha256"],
                    None if staged_metadata is not None or result is None else selected_sha256,
                )(commit, metadata)
                and verify_source_lease(),
                prepare_www=bind_data_state,
                bootstrap=args.bootstrap,
                reused_public_entries=reused_entries,
                raw_verify_www=verify_www,
                staging_metadata=staged_metadata,
            )
        selected: dict[str, int] = {}
        for row in inventory_rows:
            if row.transport == "lfs" and row.source_oid_sha256 is not None and classifications.get(row.logical_key) in {"new_or_invalid", "explicit_repartition"}:
                selected.setdefault(row.source_oid_sha256, row.source_size_bytes or 0)
        novel_bytes = sum(size for _, size in novel_expected.values())
        reused_bytes = sum(size for path, (_, size) in expected.items() if path not in novel_expected)
        summary.update(
            {
                "published": True,
                "data_commit": data_commit,
                "www_commit": www_commit,
                "requests": len(api.requests),
                "classifications": classifications,
                "metrics": {
                    "lfs_bytes": sum(selected.values()),
                    "reused_parts": len(reused_entries),
                    "novel_parts": len(novel_expected),
                    "novel_bytes": novel_bytes,
                    "reused_bytes": reused_bytes,
                    "avoided_bytes": reused_bytes,
                },
            }
        )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
