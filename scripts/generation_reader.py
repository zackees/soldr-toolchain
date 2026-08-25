"""Read the verified public catalogue-v2 generation control plane.

This module is deliberately transport-neutral: it validates the immutable
catalogue/state binding and yields direct URLs or verified multipart metadata.
It never turns a repository, LFS, or staging URL into a runtime download URL.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from scripts.catalogue_v2 import CURRENT_CLIENT_CAPABILITY, bind_catalogue_to_publication_state

_FORBIDDEN_HOSTS = frozenset(("media.githubusercontent.com",))


def _public_url(url: object) -> str:
    if not isinstance(url, str):
        raise ValueError("published URL must be a string")
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"published URL is not HTTPS: {url!r}")
    if hostname in _FORBIDDEN_HOSTS or "staging" in hostname.split("."):
        raise ValueError(f"published URL uses forbidden repository/staging transport: {url}")
    if "/assets/" in parsed.path and hostname.endswith("githubusercontent.com"):
        raise ValueError(f"published URL exposes an assets branch path: {url}")
    if hostname == "raw.githubusercontent.com":
        segments = [segment for segment in parsed.path.split("/") if segment]
        is_toolchain_repo = len(segments) >= 2 and segments[:2] == ["zackees", "soldr-toolchain"]
        immutable_generation = (
            len(segments) >= 4
            and segments[2] == "generations"
            and segments[3].endswith("-data")
            and segments[3][:-5]
            and all(character.isalnum() or character in "._-" for character in segments[3][:-5])
        )
        if is_toolchain_repo and not immutable_generation:
            raise ValueError(f"published URL is not an immutable generation ref: {url}")
    return url


@dataclass(frozen=True)
class PublishedEntry:
    """One v2 logical asset, already bound to its publication state."""

    owner: str
    repo: str
    tag: str
    asset: str
    sha256: str
    size_bytes: int
    min_client_version: int | None
    urls: tuple[str, ...] = ()
    parts: tuple[Mapping[str, Any], ...] = ()

    @property
    def is_multipart(self) -> bool:
        return bool(self.parts)

    def download_urls(self) -> Iterable[str]:
        if self.parts:
            for part in self.parts:
                yield from part["urls"]
        else:
            yield from self.urls


@dataclass(frozen=True)
class VerifiedGeneration:
    generation: str
    entries: tuple[PublishedEntry, ...]

    def find(self, owner: str, repo: str, tag: str, asset: str) -> PublishedEntry:
        matches = [entry for entry in self.entries if
                   (entry.owner, entry.repo, entry.tag, entry.asset) == (owner, repo, tag, asset)]
        if len(matches) != 1:
            raise ValueError(f"generation has {len(matches)} records for {owner}/{repo}@{tag}:{asset}")
        return matches[0]


def read_verified_generation(
    catalogue: Mapping[str, Any], publication_state: Mapping[str, Any],
) -> VerifiedGeneration:
    """Validate state binding plus public transports, then expose v2 entries.

    ``bind_catalogue_to_publication_state`` also enforces the schema-v2
    direct-or-parts union and requires capability 2 for every multipart row.
    This reader additionally forbids legacy repository and staging transports
    from public output URLs.
    """
    binding = bind_catalogue_to_publication_state(catalogue, publication_state).binding
    entries: list[PublishedEntry] = []
    for raw in catalogue["entries"]:
        min_client = raw.get("min_client_version")
        if min_client is not None and min_client != CURRENT_CLIENT_CAPABILITY:
            raise ValueError("migrated release min_client_version must be 2")
        urls = tuple(_public_url(url) for url in raw.get("urls", ()))
        parts = tuple(raw.get("parts", ()))
        for part in parts:
            for url in part["urls"]:
                _public_url(url)
        entries.append(PublishedEntry(
            raw["owner"], raw["repo"], raw["tag"], raw["asset"], raw["sha256"],
            raw["size_bytes"], min_client, urls, parts,
        ))
    return VerifiedGeneration(binding.generation, tuple(entries))


def load_verified_generation(catalogue_path: Path, publication_state_path: Path) -> VerifiedGeneration:
    """Load a local immutable catalogue/state pair without any network I/O."""
    catalogue = json.loads(catalogue_path.read_text(encoding="utf-8"))
    state = json.loads(publication_state_path.read_text(encoding="utf-8"))
    if not isinstance(catalogue, dict) or not isinstance(state, dict):
        raise ValueError("catalogue and publication state must be JSON objects")
    return read_verified_generation(catalogue, state)
