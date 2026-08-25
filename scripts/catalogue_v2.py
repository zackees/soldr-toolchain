"""Shared stdlib semantic builder and validator for flat catalogue v2."""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from scripts.publication_model import (
    MAX_ASSET_BYTES, MAX_PART_BYTES, MAX_PART_COUNT, GenerationBinding,
    VerifiedPublicLedger, _digest, canonical_json_sha256, verified_public_ledger,
)

CATALOGUE_V2 = 2
CURRENT_CLIENT_CAPABILITY = 2
_TOP_KEYS = frozenset(("schema_version", "generation", "publication_state", "generated_at", "origin", "entries"))
_ENTRY_KEYS = frozenset(("owner", "repo", "tag", "asset", "sha256", "size_bytes", "min_client_version", "urls", "parts"))
_PART_KEYS = frozenset(("number", "sha256", "size_bytes", "urls"))
_GENERATION = re.compile(r"^[A-Za-z0-9._:-]+$", re.ASCII)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        if len(value.encode("utf-8")) > 8192 or any(char.isspace() for char in value):
            return False
        parsed = urlsplit(value)
    except (UnicodeError, ValueError):
        return False
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.username is None and parsed.password is None


def _generation(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 256 and bool(_GENERATION.fullmatch(value))


def _publication_state_url(value: Any, generation: str) -> bool:
    if not _https_url(value):
        return False
    parsed = urlsplit(value)
    segments = parsed.path.split("/")
    return (len(segments) >= 4 and segments[-3:] == ["generations", generation, "publish-state.v1.json"] and
            not parsed.query and not parsed.fragment)


def _urls(value: Any, prefix: str, errors: list[str], all_urls: set[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{prefix} must be a nonempty URL array")
        return
    local: set[str] = set()
    for url in value:
        if not _https_url(url):
            errors.append(f"{prefix} contains an invalid HTTPS URL")
        elif url in local or url in all_urls:
            errors.append(f"{prefix} contains a duplicate URL")
        local.add(url)
        all_urls.add(url)


def validate_document(document: Any) -> list[str]:
    """Return all capability-2 semantic violations, without third-party deps."""
    if not isinstance(document, dict):
        return ["document must be an object"]
    errors: list[str] = []
    if document.get("schema_version") != CATALOGUE_V2 or not _is_int(document.get("schema_version")):
        errors.append("schema_version must be 2")
    unknown = set(document) - _TOP_KEYS
    if unknown:
        errors.append("document contains unknown properties")
    if "generated_at" in document and not isinstance(document["generated_at"], str):
        errors.append("generated_at must be a string")
    if "origin" in document and not _https_url(document["origin"]):
        errors.append("origin must be an HTTPS URL")
    generation = document.get("generation")
    if not _generation(generation):
        errors.append("generation must be a nonempty path-safe string")
    publication_state = document.get("publication_state")
    if not isinstance(publication_state, dict):
        errors.append("publication_state must be an object")
    else:
        if set(publication_state) != {"generation", "url"}:
            errors.append("publication_state must contain exactly generation and url")
        if publication_state.get("generation") != generation:
            errors.append("publication_state.generation must equal generation")
        if not isinstance(generation, str) or not _publication_state_url(publication_state.get("url"), generation):
            errors.append("publication_state.url must be credential-free immutable generation-qualified HTTPS")
    entries = document.get("entries")
    if not isinstance(entries, list):
        return errors + ["entries must be a list"]
    records: set[tuple[str, str, str, str]] = set()
    all_urls: set[str] = set()
    if isinstance(publication_state, dict) and isinstance(publication_state.get("url"), str):
        all_urls.add(publication_state["url"])
    for i, entry in enumerate(entries):
        prefix = f"entries[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(entry) - _ENTRY_KEYS:
            errors.append(f"{prefix} contains unknown properties")
        strings: dict[str, Any] = {}
        for field in ("owner", "repo", "tag", "asset"):
            value = entry.get(field)
            if not isinstance(value, str) or not value:
                errors.append(f"{prefix}.{field} must be a nonempty string")
            else:
                strings[field] = value
        try:
            _digest(entry.get("sha256"))
        except ValueError:
            errors.append(f"{prefix}.sha256 is invalid")
        total_size = entry.get("size_bytes")
        if not _is_int(total_size) or not 0 < total_size <= MAX_ASSET_BYTES:
            errors.append(f"{prefix}.size_bytes must be within 1..8TiB")
        if len(strings) == 4:
            record = tuple(strings[field] for field in ("owner", "repo", "tag", "asset"))
            if record in records:
                errors.append(f"{prefix} duplicates a catalogue record")
            records.add(record)
        has_urls, has_parts = "urls" in entry, "parts" in entry
        if "min_client_version" in entry:
            if not _is_int(entry["min_client_version"]) or entry["min_client_version"] != CURRENT_CLIENT_CAPABILITY:
                errors.append(f"{prefix}.min_client_version must be capability {CURRENT_CLIENT_CAPABILITY}")
        if has_urls == has_parts:
            errors.append(f"{prefix} must contain exactly one of urls or parts")
            continue
        if has_urls:
            _urls(entry["urls"], f"{prefix}.urls", errors, all_urls)
            continue
        if entry.get("min_client_version") != CURRENT_CLIENT_CAPABILITY:
            errors.append(f"{prefix}.parts requires min_client_version {CURRENT_CLIENT_CAPABILITY}")
        parts = entry["parts"]
        if not isinstance(parts, list) or not 0 < len(parts) <= MAX_PART_COUNT:
            errors.append(f"{prefix}.parts must contain 1..{MAX_PART_COUNT} parts")
            continue
        checked_sum = 0
        for number, part in enumerate(parts, 1):
            part_prefix = f"{prefix}.parts[{number - 1}]"
            if not isinstance(part, dict):
                errors.append(f"{part_prefix} must be an object")
                continue
            if set(part) - _PART_KEYS:
                errors.append(f"{part_prefix} contains unknown properties")
            if part.get("number") != number or not _is_int(part.get("number")):
                errors.append(f"{prefix}.parts must be contiguous 1-based numbering")
            try:
                _digest(part.get("sha256"))
            except ValueError:
                errors.append(f"{part_prefix}.sha256 is invalid")
            part_size = part.get("size_bytes")
            if not _is_int(part_size) or not 0 < part_size <= MAX_PART_BYTES:
                errors.append(f"{part_prefix}.size_bytes is invalid")
            else:
                checked_sum += part_size
            _urls(part.get("urls"), f"{part_prefix}.urls", errors, all_urls)
        if _is_int(total_size) and checked_sum != total_size:
            errors.append(f"{prefix}.parts sizes do not sum to size_bytes")
    return errors


def build_document(
    entries: list[dict[str, Any]], *, generation: str, publication_state_url: str,
    generated_at: str | None = None, origin: str | None = None,
) -> dict[str, Any]:
    """Build the complete document bound to one immutable publish-state URL."""
    normalized_entries: list[dict[str, Any]] = []
    for entry in entries:
        normalized = dict(entry)
        if "parts" in normalized:
            normalized.setdefault("min_client_version", CURRENT_CLIENT_CAPABILITY)
        normalized_entries.append(normalized)
    document: dict[str, Any] = {
        "schema_version": CATALOGUE_V2,
        "generation": generation,
        "publication_state": {"generation": generation, "url": publication_state_url},
        "entries": normalized_entries,
    }
    if generated_at is not None:
        document["generated_at"] = generated_at
    if origin is not None:
        document["origin"] = origin
    errors = validate_document(document)
    if errors:
        raise ValueError("invalid catalogue v2: " + "; ".join(errors))
    return document


def generation_binding_from_publication_state(state: Mapping[str, Any]) -> GenerationBinding:
    """Extract the Phase-0 immutable identity binding from public state."""
    if state.get("schema_version") != 1 or not _is_int(state.get("schema_version")):
        raise ValueError("publication state schema_version must be 1")
    try:
        source, www = state["source"], state["www"]
        active, previous = state["active"], state["previous"]
        return GenerationBinding(
            state["generation"], source["commit"], source["tree"],
            www["commit"], www["tree"], active["slot"], active["commit"], active["tree"],
            previous["slot"], previous["commit"], previous["tree"], state["catalogue_sha256"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("publication state lacks generation binding fields") from exc


def bind_catalogue_to_publication_state(
    document: Mapping[str, Any], state: Mapping[str, Any],
) -> VerifiedPublicLedger:
    """Validate the full catalogue, then bind its exact canonical bytes to state."""
    errors = validate_document(document)
    if errors:
        raise ValueError("invalid catalogue v2: " + "; ".join(errors))
    binding = generation_binding_from_publication_state(state)
    if document["generation"] != binding.generation:
        raise ValueError("catalogue generation does not match publication state")
    if canonical_json_sha256(document) != binding.catalogue_sha256:
        raise ValueError("publication-state catalogue_sha256 does not match full canonical catalogue")
    return verified_public_ledger(state, binding, document)
