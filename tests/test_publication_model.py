"""Adversarial contracts for the local-only multipart publisher model."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.publication_model import (
    MAX_PART_BYTES, MAX_PART_COUNT, PublishedPart, SourceInventoryRow,
    GenerationBinding, asset_mapping_from_partition, build_part_index, canonical_json_bytes,
    canonical_json_sha256, classify_inventory, parse_lfs_pointer, part_path,
    explicit_repartition_mapping, partition_file, part_reuse_stats, retained_part_paths,
    validate_asset_mapping, verified_public_ledger,
)

PROVENANCE = {"tool": "x", "version": "1", "channel": "stable"}


def _mapping(full: str, part: str = "b" * 64) -> dict:
    return {"size_bytes": 3, "partitioner": {"version": 1, "target_bytes": 3}, "parts": [{"number": 1, "sha256": part, "size_bytes": 3,
            "path": f"sha256/{full}/0001-{part}.part", "git_blob": "c" * 40}]}


def _verified(full: str = "a" * 64, *, published_at: int = 100):
    catalogue = {"schema_version": 2, "entries": []}
    digest = canonical_json_sha256(catalogue)
    binding = GenerationBinding("g", "1" * 40, "2" * 40, "3" * 40, "4" * 40,
        "public-a", "5" * 40, "6" * 40, "public-b", "7" * 40, "8" * 40, digest)
    ledger = {"generation": "g", "source": {"commit": "1" * 40, "tree": "2" * 40},
        "www": {"commit": "3" * 40, "tree": "4" * 40},
        "active": {"slot": "public-a", "commit": "5" * 40, "tree": "6" * 40},
        "previous": {"slot": "public-b", "commit": "7" * 40, "tree": "8" * 40},
        "catalogue_sha256": digest, "published_at": published_at,
        "assets_by_sha256": {full: _mapping(full)},
        "logical_assets": {
            "old": {"source_path": "p", "asset": "x", "source_oid_sha256": full, "source_size_bytes": 3, "metadata_fingerprint": "old", "provenance": PROVENANCE},
            "gone": {"source_path": "gone", "asset": "x", "source_oid_sha256": full, "source_size_bytes": 3, "metadata_fingerprint": "gone", "provenance": PROVENANCE},
        }}
    return verified_public_ledger(ledger, binding, catalogue)


def _declared(source: Path) -> tuple[str, int]:
    data = source.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def test_strict_pointer_and_canonical_paths() -> None:
    digest = "a" * 64
    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize 123\n".encode()
    assert parse_lfs_pointer(pointer) == (digest, 123)
    assert part_path(digest, 7, "b" * 64) == f"sha256/{digest}/0007-{'b' * 64}.part"
    for pointer in (b"not a pointer", pointer + b"extra\n", pointer.replace(b"size 123", b"size 0")):
        with pytest.raises(ValueError): parse_lfs_pointer(pointer)
    with pytest.raises(ValueError): part_path(digest, MAX_PART_COUNT + 1, "b" * 64)


def test_partition_requires_exact_declaration_and_raw_slices(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.write_bytes(bytes(range(251)) * 5)
    oid, size = _declared(source)
    with pytest.raises(TypeError): partition_file(source, tmp_path / "missing")  # type: ignore[call-arg]
    asset = partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=size, target_bytes=128)
    assert asset.full_sha256 == oid and asset.size_bytes == size
    assert b"".join((tmp_path / "out" / part.path).read_bytes() for part in asset.parts) == source.read_bytes()
    assert all(part.size_bytes == 128 for part in asset.parts[:-1])
    with pytest.raises(ValueError): partition_file(source, tmp_path / "bad", declared_oid_sha256="0" * 64, declared_size_bytes=size, target_bytes=128)
    assert not (tmp_path / "bad" / "sha256").exists()


def test_atomic_promotion_failure_and_conflict_never_expose_partial_set(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.write_bytes(b"abcdef")
    oid, size = _declared(source)
    def fail(point: str) -> None:
        if point == "before_promote": raise RuntimeError("injected")
    with pytest.raises(RuntimeError): partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=size, target_bytes=2, promotion_hook=fail)
    assert not (tmp_path / "out" / "sha256" / oid).exists()
    asset = partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=size, target_bytes=2)
    final = tmp_path / "out" / "sha256" / oid
    (final / f"0001-{asset.parts[0].sha256}.part").write_bytes(b"bad")
    with pytest.raises(ValueError, match="conflicting"): partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=size, target_bytes=2)


def test_post_promotion_fault_leaves_only_a_complete_recoverable_set(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.write_bytes(b"abcdef")
    oid, size = _declared(source)
    def fail(point: str) -> None:
        if point == "after_promote": raise RuntimeError("injected after rename")
    with pytest.raises(RuntimeError):
        partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=size, target_bytes=2, promotion_hook=fail)
    final = tmp_path / "out" / "sha256" / oid
    assert len(list(final.glob("*.part"))) == 3
    assert partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=size, target_bytes=2).size_bytes == size


def test_mapping_uses_git_sha1_not_asset_sha256() -> None:
    full = "a" * 64; mapping = _mapping(full)
    assert validate_asset_mapping(full, mapping)
    mapping["parts"][0]["git_blob"] = "c" * 64
    assert not validate_asset_mapping(full, mapping)


def test_mapping_requires_asset_partitioner_and_exact_nonfinal_target(tmp_path: Path) -> None:
    full = "a" * 64; mapping = _mapping(full)
    mapping.pop("partitioner")
    assert not validate_asset_mapping(full, mapping)
    mapping = _mapping(full); mapping["partitioner"]["version"] = 2
    assert not validate_asset_mapping(full, mapping)
    mapping = {"size_bytes": 5, "partitioner": {"version": 1, "target_bytes": 2}, "parts": [
        {"number": 1, "sha256": "b" * 64, "size_bytes": 1, "path": f"sha256/{full}/0001-{'b' * 64}.part", "git_blob": "c" * 40},
        {"number": 2, "sha256": "d" * 64, "size_bytes": 4, "path": f"sha256/{full}/0002-{'d' * 64}.part", "git_blob": "e" * 40},
    ]}
    assert not validate_asset_mapping(full, mapping)
    source = tmp_path / "source"; source.write_bytes(b"abc")
    oid, size = _declared(source)
    sliced = partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=size, target_bytes=2)
    result = asset_mapping_from_partition(sliced, ["a" * 40, "b" * 40])
    assert result["partitioner"] == {"version": 1, "target_bytes": 2}
    assert validate_asset_mapping(oid, result)
    assert explicit_repartition_mapping(sliced, ["a" * 40, "b" * 40]) == result


def test_verified_ledger_binding_canonical_digest_classification_and_retention() -> None:
    full = "a" * 64; verified = _verified(full)
    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert classify_inventory([SourceInventoryRow("old", "p", "x", full, 3, provenance=PROVENANCE, metadata_fingerprint="old"),
        SourceInventoryRow("changed", "p", "x", full, 3, provenance=PROVENANCE, metadata_fingerprint="new"),
        SourceInventoryRow("alias", "p", "x", full, 3, provenance=PROVENANCE, metadata_fingerprint="old"), SourceInventoryRow("new", "p", "x", "d" * 64, 3, provenance=PROVENANCE, metadata_fingerprint="old"),
        SourceInventoryRow("external", "-", "x", provenance=PROVENANCE, transport="external", metadata_fingerprint="external")], verified) == {"old": "exact_hit", "changed": "alias", "alias": "alias", "new": "new_or_invalid", "external": "direct", "gone": "removed"}
    for row in (SourceInventoryRow("old", "renamed", "x", full, 3, provenance=PROVENANCE, metadata_fingerprint="old"),
                SourceInventoryRow("old", "p", "renamed", full, 3, provenance=PROVENANCE, metadata_fingerprint="old"),
                SourceInventoryRow("old", "p", "x", full, 3, provenance={"tool": "different"}, metadata_fingerprint="old"),
                SourceInventoryRow("old", "p", "x", full, 3, provenance=PROVENANCE, metadata_fingerprint="changed")):
        assert classify_inventory([row], verified)["old"] == "metadata_only"
    assert classify_inventory([SourceInventoryRow("old", "p", "x", full, 3, provenance=PROVENANCE, metadata_fingerprint="old")], verified, repartition={full})["old"] == "explicit_repartition"
    with pytest.raises(TypeError): classify_inventory([], verified.ledger)  # type: ignore[arg-type]
    with pytest.raises(ValueError): classify_inventory([SourceInventoryRow("old", "p", "x", full, 3, provenance=PROVENANCE, metadata_fingerprint="old"), SourceInventoryRow("old", "p", "x", full, 3, provenance=PROVENANCE, metadata_fingerprint="old")], verified)
    with pytest.raises(ValueError): classify_inventory([SourceInventoryRow("direct", "p", "x", full, 3, provenance=PROVENANCE, transport="direct", metadata_fingerprint="direct")], verified)
    with pytest.raises(ValueError): classify_inventory([SourceInventoryRow("unknown", "p", "x", provenance=PROVENANCE, transport="mystery", metadata_fingerprint="unknown")], verified)
    index = build_part_index(verified)
    stats = part_reuse_stats([PublishedPart(1, "b" * 64, 3, _mapping(full)["parts"][0]["path"])], index)
    assert stats == {"novel_parts": 0, "reused_parts": 1, "novel_bytes": 0, "reused_bytes": 3, "avoided_bytes": 3}
    assert retained_part_paths([verified], now_timestamp=100 + 15 * 86400) == {_mapping(full)["parts"][0]["path"]}
    with pytest.raises(TypeError): retained_part_paths([verified.ledger], now_timestamp=0)  # type: ignore[list-item]
    verified.ledger["assets_by_sha256"][full]["parts"][0]["path"] = "not/canonical"
    with pytest.raises(ValueError): retained_part_paths([verified], now_timestamp=0)
    verified = _verified(full)
    verified.ledger["active"]["tree"] = "f" * 40
    with pytest.raises(ValueError): classify_inventory([], verified)


def test_partition_limits_reject_oversized_target_and_count(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.write_bytes(b"abcd")
    oid, size = _declared(source)
    with pytest.raises(ValueError, match="hard maximum"):
        partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=size, target_bytes=MAX_PART_BYTES + 1)
    with pytest.raises(ValueError, match="maximum part count"):
        partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=MAX_PART_COUNT + 1, target_bytes=1)
    with pytest.raises(ValueError, match="asset ceiling"):
        partition_file(source, tmp_path / "out", declared_oid_sha256=oid, declared_size_bytes=8 * 1024**4 + 1)
