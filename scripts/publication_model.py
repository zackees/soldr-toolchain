"""Core data-model scaffold for issue #149's atomic multipart publisher.

The implementation deliberately begins behind pure functions so the Git/LFS
transaction layer can be tested without network or repository mutations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

TARGET_PART_BYTES = 32 * 1024 * 1024
MAX_PART_BYTES = 95 * 1024 * 1024
PARTITIONER_VERSION = 1


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


def parse_lfs_pointer(pointer: bytes) -> tuple[str, int]:
    """Return ``(pristine_sha256, declared_size)`` from a strict LFS pointer."""
    raise NotImplementedError("Phase 1 implementation")


def part_path(full_sha256: str, number: int, part_sha256: str) -> str:
    """Return the canonical content-addressed ordinary-Git part path."""
    raise NotImplementedError("Phase 1 implementation")


def partition_file(
    source: Path,
    output_dir: Path,
    *,
    target_bytes: int = TARGET_PART_BYTES,
) -> PartitionedAsset:
    """Verify and deterministically raw-slice one pristine source artifact."""
    raise NotImplementedError("Phase 1 implementation")
