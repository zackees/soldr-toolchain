"""RED contract tests for the content-addressed multipart publisher core."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.publication_model import (
    MAX_PART_BYTES,
    TARGET_PART_BYTES,
    parse_lfs_pointer,
    part_path,
    partition_file,
)


def test_parse_lfs_pointer_returns_pristine_identity() -> None:
    digest = "a" * 64
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{digest}\n"
        "size 123\n"
    ).encode()
    assert parse_lfs_pointer(pointer) == (digest, 123)


@pytest.mark.parametrize(
    "pointer",
    [
        b"not an lfs pointer",
        b"version https://git-lfs.github.com/spec/v1\noid sha256:BAD\nsize 1\n",
        b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 0\n",
    ],
)
def test_parse_lfs_pointer_fails_closed(pointer: bytes) -> None:
    with pytest.raises(ValueError):
        parse_lfs_pointer(pointer)


def test_part_path_is_canonical() -> None:
    full = "a" * 64
    part = "b" * 64
    assert part_path(full, 7, part) == f"sha256/{full}/0007-{part}.part"


def test_partition_file_is_deterministic_raw_slicing(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(251)) * 5)
    full_sha = hashlib.sha256(source.read_bytes()).hexdigest()

    first = partition_file(source, tmp_path / "first", target_bytes=128)
    second = partition_file(source, tmp_path / "second", target_bytes=128)

    assert first.full_sha256 == second.full_sha256 == full_sha
    assert first.size_bytes == second.size_bytes == source.stat().st_size
    assert [(p.number, p.sha256, p.size_bytes, p.path) for p in first.parts] == [
        (p.number, p.sha256, p.size_bytes, p.path) for p in second.parts
    ]
    reconstructed = b"".join((tmp_path / "first" / p.path).read_bytes() for p in first.parts)
    assert reconstructed == source.read_bytes()
    assert all(p.size_bytes == 128 for p in first.parts[:-1])
    assert first.parts[-1].size_bytes <= 128


def test_publisher_constants_match_v1_contract() -> None:
    assert TARGET_PART_BYTES == 32 * 1024 * 1024
    assert MAX_PART_BYTES == 95 * 1024 * 1024


def test_partition_rejects_target_over_hard_ceiling(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"x")
    with pytest.raises(ValueError, match="maximum"):
        partition_file(source, tmp_path / "parts", target_bytes=MAX_PART_BYTES + 1)
