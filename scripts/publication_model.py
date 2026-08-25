"""Pure, stdlib-only foundations for the atomic multipart publisher.

This module deliberately performs no Git or network mutation.  The future
publisher may only classify/reuse a :class:`VerifiedPublicLedger`, and only
publishes parts after the LFS declaration has been checked against the exact
stream which was sliced.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

TARGET_PART_BYTES = 32 * 1024 * 1024
MAX_PART_BYTES = 95 * 1024 * 1024
MAX_PART_COUNT = 4096
MAX_ASSET_BYTES = 8 * 1024**4
PARTITIONER_VERSION = 1
SUPPORTED_PARTITIONER_VERSIONS = frozenset((PARTITIONER_VERSION,))
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}$")
_POINTER = re.compile(rb"\Aversion https://git-lfs\.github\.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([1-9][0-9]*)\n\Z")
_TRANSPORTS = frozenset(("lfs", "direct", "private", "external"))


@dataclass(frozen=True)
class PublishedPart:
    number: int
    sha256: str
    size_bytes: int
    path: str


@dataclass(frozen=True)
class PartitionedAsset:
    full_sha256: str
    size_bytes: int
    partitioner_version: int
    target_bytes: int
    parts: tuple[PublishedPart, ...]


@dataclass(frozen=True)
class SourceInventoryRow:
    logical_key: str
    source_path: str
    asset: str
    source_oid_sha256: str | None = None
    source_size_bytes: int | None = None
    provenance: Mapping[str, Any] | None = None
    transport: str = "lfs"
    metadata_fingerprint: str = ""


@dataclass(frozen=True)
class GenerationBinding:
    """The identities that bind an immutable public generation together."""
    generation: str
    source_commit: str
    source_tree: str
    www_commit: str
    www_tree: str
    active_slot: str
    active_commit: str
    active_tree: str
    previous_slot: str
    previous_commit: str
    previous_tree: str
    catalogue_sha256: str


@dataclass(frozen=True)
class VerifiedPublicLedger:
    """A ledger checked against a publicly verified generation binding."""
    ledger: Mapping[str, Any]
    binding: GenerationBinding


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _digest(value: Any, name: str = "sha256") -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase 64-hex sha256")
    return value


def _git_object(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _GIT_OBJECT.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase 40-hex Git object ID")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON bytes used for generation catalogue/state digests."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def parse_lfs_pointer(pointer: bytes) -> tuple[str, int]:
    """Parse only the canonical three-line LFS v1 pointer, fail closed."""
    match = _POINTER.fullmatch(pointer)
    if match is None:
        raise ValueError("not a strict Git LFS v1 pointer")
    return match.group(1).decode("ascii"), int(match.group(2))


def part_path(full_sha256: str, number: int, part_sha256: str) -> str:
    _digest(full_sha256, "full asset sha256")
    _digest(part_sha256, "part sha256")
    if not _is_int(number) or not 1 <= number <= MAX_PART_COUNT:
        raise ValueError(f"part number must be within 1..{MAX_PART_COUNT}")
    return f"sha256/{full_sha256}/{number:04d}-{part_sha256}.part"


def is_canonical_part_path(path: str, full_sha256: str, number: int, part_sha256: str) -> bool:
    try:
        return path == part_path(full_sha256, number, part_sha256)
    except ValueError:
        return False


def _validate_declaration(oid_sha256: str, size_bytes: int) -> None:
    _digest(oid_sha256, "declared LFS OID")
    if not _is_int(size_bytes) or not 0 < size_bytes <= MAX_ASSET_BYTES:
        raise ValueError("declared LFS size must be positive and within the asset ceiling")


def verify_declared_source(source: Path, oid_sha256: str, size_bytes: int) -> None:
    """Standalone diagnostic helper; publisher paths use ``partition_file``."""
    _validate_declaration(oid_sha256, size_bytes)
    digest = hashlib.sha256()
    count = 0
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            count += len(block)
    if count != size_bytes or digest.hexdigest() != oid_sha256:
        raise ValueError("materialized bytes do not match declared LFS OID/size")


def _part_bytes_match(path: Path, expected_sha256: str, expected_size: int) -> bool:
    if not path.is_file() or path.stat().st_size != expected_size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == expected_sha256


def partition_file(
    source: Path,
    output_dir: Path,
    *,
    declared_oid_sha256: str,
    declared_size_bytes: int,
    target_bytes: int = TARGET_PART_BYTES,
    promotion_hook: Callable[[str], None] | None = None,
) -> PartitionedAsset:
    """Slice declared LFS bytes and atomically promote a complete part set.

    The bytes are counted and hashed in the same read pass that writes staging
    parts; a pre-hash or ``stat`` is intentionally not used for validation.
    Promotion is one directory rename, so a failed run leaves no partial asset
    directory.  An existing final directory must exactly match or blocks the
    run; it is never overwritten.
    """
    _validate_declaration(declared_oid_sha256, declared_size_bytes)
    if not _is_int(target_bytes) or not 0 < target_bytes <= MAX_PART_BYTES:
        raise ValueError("target size must be positive and not exceed hard maximum")
    if (declared_size_bytes + target_bytes - 1) // target_bytes > MAX_PART_COUNT:
        raise ValueError("declared asset requires more than the maximum part count")

    root = output_dir / "sha256"
    final_dir = root / declared_oid_sha256
    staging_root = output_dir / ".publication-staging"
    staged_dir = staging_root / f"{declared_oid_sha256}-{uuid.uuid4().hex}"
    staged_dir.mkdir(parents=True)
    full_hash = hashlib.sha256()
    total = 0
    raw: list[tuple[int, str, int]] = []
    try:
        with source.open("rb") as input_file:
            number = 1
            while chunk := input_file.read(target_bytes):
                total += len(chunk)
                if total > MAX_ASSET_BYTES or number > MAX_PART_COUNT:
                    raise ValueError("materialized source exceeds publisher ceiling")
                full_hash.update(chunk)
                part_digest = hashlib.sha256(chunk).hexdigest()
                (staged_dir / f"{number:04d}-{part_digest}.part").write_bytes(chunk)
                raw.append((number, part_digest, len(chunk)))
                number += 1
        actual_oid = full_hash.hexdigest()
        if total != declared_size_bytes or actual_oid != declared_oid_sha256:
            raise ValueError("exact sliced bytes do not match declared LFS OID/size")
        parts = tuple(PublishedPart(n, digest, size, part_path(actual_oid, n, digest)) for n, digest, size in raw)
        if promotion_hook is not None:
            promotion_hook("before_promote")
        root.mkdir(parents=True, exist_ok=True)
        if final_dir.exists():
            expected = {f"{p.number:04d}-{p.sha256}.part" for p in parts}
            observed = {p.name for p in final_dir.iterdir()} if final_dir.is_dir() else set()
            if observed != expected or not all(_part_bytes_match(final_dir / f"{p.number:04d}-{p.sha256}.part", p.sha256, p.size_bytes) for p in parts):
                raise ValueError("conflicting existing published asset directory")
            shutil.rmtree(staged_dir)
            return PartitionedAsset(actual_oid, total, PARTITIONER_VERSION, target_bytes, parts)
        os.replace(staged_dir, final_dir)
        if promotion_hook is not None:
            promotion_hook("after_promote")
        return PartitionedAsset(actual_oid, total, PARTITIONER_VERSION, target_bytes, parts)
    except Exception:
        shutil.rmtree(staged_dir, ignore_errors=True)
        raise
    finally:
        if staging_root.exists() and not any(staging_root.iterdir()):
            staging_root.rmdir()


def validate_asset_mapping(full_sha256: str, mapping: Mapping[str, Any]) -> bool:
    try:
        _digest(full_sha256)
        if not _is_int(mapping.get("size_bytes")) or not 0 < mapping["size_bytes"] <= MAX_ASSET_BYTES:
            return False
        partitioner = mapping.get("partitioner")
        if not isinstance(partitioner, Mapping):
            return False
        version = partitioner.get("version")
        target_bytes = partitioner.get("target_bytes")
        if not _is_int(version) or version not in SUPPORTED_PARTITIONER_VERSIONS:
            return False
        if not _is_int(target_bytes) or not 0 < target_bytes <= MAX_PART_BYTES:
            return False
        parts = mapping.get("parts")
        if not isinstance(parts, list) or not 0 < len(parts) <= MAX_PART_COUNT:
            return False
        total = 0
        for expected, part in enumerate(parts, 1):
            if not isinstance(part, Mapping) or part.get("number") != expected:
                return False
            size = part.get("size_bytes")
            if not _is_int(size) or not 0 < size <= MAX_PART_BYTES:
                return False
            if expected < len(parts) and size != target_bytes:
                return False
            if expected == len(parts) and size > target_bytes:
                return False
            if not is_canonical_part_path(part.get("path", ""), full_sha256, expected, part.get("sha256", "")):
                return False
            _git_object(part.get("git_blob"), "Git blob")
            total += size
        return total == mapping["size_bytes"]
    except (AttributeError, ValueError, TypeError):
        return False


def asset_mapping_from_partition(asset: PartitionedAsset, git_blob_ids: Iterable[str]) -> dict[str, Any]:
    """Create a validated, provenance-preserving ledger vector from slicing."""
    blobs = tuple(git_blob_ids)
    if len(blobs) != len(asset.parts):
        raise ValueError("each partitioned part requires exactly one Git blob ID")
    mapping = {
        "size_bytes": asset.size_bytes,
        "partitioner": {"version": asset.partitioner_version, "target_bytes": asset.target_bytes},
        "parts": [{"number": part.number, "sha256": part.sha256, "size_bytes": part.size_bytes,
                   "path": part.path, "git_blob": blob} for part, blob in zip(asset.parts, blobs)],
    }
    if not validate_asset_mapping(asset.full_sha256, mapping):
        raise ValueError("partitioned asset cannot form a valid ledger mapping")
    return mapping


def explicit_repartition_mapping(asset: PartitionedAsset, git_blob_ids: Iterable[str]) -> dict[str, Any]:
    """Record an operator-requested replacement vector with its own partitioner."""
    return asset_mapping_from_partition(asset, git_blob_ids)


def validate_generation_binding(binding: GenerationBinding) -> None:
    if not isinstance(binding.generation, str) or not binding.generation:
        raise ValueError("generation must be nonempty")
    if binding.active_slot not in {"public-a", "public-b"} or binding.previous_slot not in {"public-a", "public-b"} or binding.active_slot == binding.previous_slot:
        raise ValueError("active and previous slots must be distinct public slots")
    for name in ("source_commit", "source_tree", "www_commit", "www_tree", "active_commit", "active_tree", "previous_commit", "previous_tree"):
        _git_object(getattr(binding, name), name)
    _digest(binding.catalogue_sha256, "catalogue digest")


def _validate_ledger_binding(ledger: Mapping[str, Any], binding: GenerationBinding) -> None:
    required = {
        "generation": binding.generation,
        "source": {"commit": binding.source_commit, "tree": binding.source_tree},
        "www": {"commit": binding.www_commit, "tree": binding.www_tree},
        "active": {"slot": binding.active_slot, "commit": binding.active_commit, "tree": binding.active_tree},
        "previous": {"slot": binding.previous_slot, "commit": binding.previous_commit, "tree": binding.previous_tree},
        "catalogue_sha256": binding.catalogue_sha256,
    }
    for key, expected in required.items():
        if ledger.get(key) != expected:
            raise ValueError(f"ledger {key} does not match public generation binding")
    assets = ledger.get("assets_by_sha256")
    logical_assets = ledger.get("logical_assets")
    if not isinstance(assets, Mapping) or not isinstance(logical_assets, Mapping):
        raise ValueError("ledger hash tables must be objects")
    for full, mapping in assets.items():
        if not validate_asset_mapping(full, mapping):
            raise ValueError("ledger contains invalid asset mapping")
    for logical_key, row in logical_assets.items():
        if not isinstance(logical_key, str) or not logical_key or not isinstance(row, Mapping):
            raise ValueError("ledger contains malformed logical provenance")
        _validate_logical_provenance(row)
        mapped = assets.get(row["source_oid_sha256"])
        if not isinstance(mapped, Mapping) or mapped.get("size_bytes") != row["source_size_bytes"]:
            raise ValueError("logical provenance does not bind to an asset mapping")


def validate_verified_public_ledger(value: VerifiedPublicLedger) -> None:
    """Recheck mutable mappings before they can drive reuse or retention."""
    if not isinstance(value, VerifiedPublicLedger):
        raise TypeError("expected a VerifiedPublicLedger")
    validate_generation_binding(value.binding)
    _validate_ledger_binding(value.ledger, value.binding)


def verified_public_ledger(ledger: Mapping[str, Any], binding: GenerationBinding, catalogue: Mapping[str, Any]) -> VerifiedPublicLedger:
    """Fail closed unless state identity and canonical catalogue digest bind."""
    validate_generation_binding(binding)
    if canonical_json_sha256(catalogue) != binding.catalogue_sha256:
        raise ValueError("catalogue digest does not match public generation binding")
    _validate_ledger_binding(ledger, binding)
    return VerifiedPublicLedger(ledger, binding)


def _validate_provenance(value: Any) -> None:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("provenance must be a nonempty object")
    if any(not isinstance(key, str) or not key for key in value):
        raise ValueError("provenance keys must be nonempty strings")
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError):
        raise ValueError("provenance must be canonical-JSON serializable") from None


def _validate_logical_provenance(row: Mapping[str, Any]) -> None:
    for field in ("source_path", "asset", "metadata_fingerprint"):
        if not isinstance(row.get(field), str) or not row[field]:
            raise ValueError(f"logical provenance {field} must be a nonempty string")
    _validate_declaration(row.get("source_oid_sha256"), row.get("source_size_bytes"))
    _validate_provenance(row.get("provenance"))


def _validate_inventory_row(row: SourceInventoryRow) -> None:
    if (not isinstance(row.logical_key, str) or not row.logical_key or
            not isinstance(row.source_path, str) or not row.source_path or
            not isinstance(row.asset, str) or not row.asset or
            not isinstance(row.metadata_fingerprint, str) or not row.metadata_fingerprint):
        raise ValueError("inventory logical provenance fields must be nonempty strings")
    _validate_provenance(row.provenance)
    if row.transport not in _TRANSPORTS:
        raise ValueError("unknown inventory transport")
    if row.transport == "lfs":
        if row.source_oid_sha256 is None or row.source_size_bytes is None:
            raise ValueError("LFS inventory row requires OID and size")
        _validate_declaration(row.source_oid_sha256, row.source_size_bytes)
    elif row.source_oid_sha256 is not None or row.source_size_bytes is not None:
        raise ValueError("direct/private/external inventory rows cannot carry LFS identity")


def classify_inventory(rows: Iterable[SourceInventoryRow], ledger: VerifiedPublicLedger, *, repartition: set[str] = frozenset()) -> dict[str, str]:
    """Classify rows using only a verified public ledger; never fetch LFS."""
    if not isinstance(ledger, VerifiedPublicLedger):
        raise TypeError("classification requires a VerifiedPublicLedger")
    validate_verified_public_ledger(ledger)
    assets, previous = ledger.ledger.get("assets_by_sha256", {}), ledger.ledger.get("logical_assets", {})
    result: dict[str, str] = {}
    for row in rows:
        _validate_inventory_row(row)
        if row.logical_key in result:
            raise ValueError("duplicate logical inventory key")
        if row.transport != "lfs":
            result[row.logical_key] = "direct"
            continue
        mapping = assets.get(row.source_oid_sha256)
        valid = isinstance(mapping, Mapping) and mapping.get("size_bytes") == row.source_size_bytes and validate_asset_mapping(row.source_oid_sha256, mapping)
        old = previous.get(row.logical_key)
        if row.source_oid_sha256 in repartition:
            result[row.logical_key] = "explicit_repartition"
        elif not valid:
            result[row.logical_key] = "new_or_invalid"
        elif not isinstance(old, Mapping) or old.get("source_oid_sha256") != row.source_oid_sha256:
            result[row.logical_key] = "alias"
        elif (old.get("source_size_bytes") != row.source_size_bytes or
              old.get("source_path") != row.source_path or
              old.get("asset") != row.asset or
              old.get("metadata_fingerprint") != row.metadata_fingerprint or
              old.get("provenance") != row.provenance):
            result[row.logical_key] = "metadata_only"
        else:
            result[row.logical_key] = "exact_hit"
    result.update({key: "removed" for key in set(previous) - set(result)})
    return result


def build_part_index(ledger: VerifiedPublicLedger) -> dict[tuple[str, int], dict[str, Any]]:
    if not isinstance(ledger, VerifiedPublicLedger):
        raise TypeError("part index requires a VerifiedPublicLedger")
    validate_verified_public_ledger(ledger)
    index: dict[tuple[str, int], dict[str, Any]] = {}
    for full, mapping in ledger.ledger.get("assets_by_sha256", {}).items():
        if not validate_asset_mapping(full, mapping):
            raise ValueError("verified ledger changed to contain invalid mapping")
        for part in mapping["parts"]:
            key = (part["sha256"], part["size_bytes"])
            found = index.setdefault(key, {"git_blob": part["git_blob"], "paths": []})
            if found["git_blob"] != part["git_blob"]:
                raise ValueError("conflicting blobs for identical part")
            found["paths"].append(part["path"])
    return index


def part_reuse_stats(parts: Iterable[PublishedPart], index: Mapping[tuple[str, int], Mapping[str, Any]]) -> dict[str, int]:
    result = {"novel_parts": 0, "reused_parts": 0, "novel_bytes": 0, "reused_bytes": 0, "avoided_bytes": 0}
    for part in parts:
        reused = (part.sha256, part.size_bytes) in index
        prefix = "reused" if reused else "novel"
        result[f"{prefix}_parts"] += 1
        result[f"{prefix}_bytes"] += part.size_bytes
        if reused:
            result["avoided_bytes"] += part.size_bytes
    return result


def retained_part_paths(generations: Iterable[VerifiedPublicLedger], *, now_timestamp: float, support_seconds: int = 14 * 86400, prior_successes: int = 2) -> set[str]:
    """Mark only mappings that remain cryptographically/state bound."""
    ledgers = list(generations)
    if not all(isinstance(item, VerifiedPublicLedger) for item in ledgers):
        raise TypeError("retention requires verified public ledgers")
    if not _is_int(prior_successes) or prior_successes < 0 or support_seconds < 0:
        raise ValueError("invalid retention policy")
    for item in ledgers:
        validate_verified_public_ledger(item)
    sorted_ledgers = sorted(ledgers, key=lambda item: item.ledger.get("published_at", 0), reverse=True)
    keep = sorted_ledgers[:prior_successes + 1] + [item for item in sorted_ledgers if now_timestamp - item.ledger.get("published_at", 0) < support_seconds]
    paths: set[str] = set()
    for item in keep:
        for full, mapping in item.ledger.get("assets_by_sha256", {}).items():
            if not validate_asset_mapping(full, mapping):
                raise ValueError("retention ledger mapping became invalid")
            paths.update(part["path"] for part in mapping["parts"])
    return paths
