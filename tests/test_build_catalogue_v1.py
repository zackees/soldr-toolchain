"""Tests for `scripts/build_catalogue_v1.py`.

The script is intentionally narrow — it re-shapes a v5 asset-index
into a v1 catalogue payload. Cover the contract: schema_version is
pinned, COPIED_ENTRY_FIELDS round-trip exactly, extra fields are
dropped, and degenerate inputs raise ValueError instead of producing
a junk document.

Companion to the soldr#988 Phase 1 schema CI gate
(`.github/workflows/catalogue-schema.yml`): unit-test the producer
contract here, schema-validate the live output in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import build_catalogue_v1 as bc


def _asset_index_entry(**overrides: str) -> dict[str, str]:
    base = {
        "owner": "zackees",
        "repo": "zccache",
        "tag": "1.12.11",
        "asset": "zccache-v1.12.11-x86_64-pc-windows-msvc.zip",
        "url": "https://github.com/zackees/zccache/releases/download/1.12.11/zccache-v1.12.11-x86_64-pc-windows-msvc.zip",
        "sha256": "0" * 64,
    }
    base.update(overrides)
    return base


def test_schema_accepts_provider_neutral_https_origin() -> None:
    import jsonschema

    schema = json.loads((Path(__file__).parents[1] / "schemas/catalogue.v1.schema.json").read_text())
    doc = {"schema_version": 1, "entries": [{
        "owner": "forge", "repo": "producer", "tag": "assets", "asset": "bundle.tar.zst",
        "url": "https://cdn.example.invalid/sha256/aa/" + "a" * 64 + "/bundle.tar.zst",
        "sha256": "a" * 64,
    }]}
    jsonschema.validate(doc, schema)


def test_transform_pins_schema_version_to_1() -> None:
    payload = bc.transform({"schema_version": 5, "entries": []}, origin="x")
    assert payload["schema_version"] == 1


def test_transform_carries_origin_unchanged() -> None:
    payload = bc.transform({"entries": []}, origin="https://example.invalid/foo")
    assert payload["origin"] == "https://example.invalid/foo"


def test_transform_round_trips_known_fields() -> None:
    entry = _asset_index_entry()
    payload = bc.transform({"entries": [entry]}, origin="x")
    assert payload["entries"] == [entry]


def test_transform_drops_unknown_entry_fields() -> None:
    entry = _asset_index_entry(extra="should-be-dropped")  # type: ignore[arg-type]
    payload = bc.transform({"entries": [entry]}, origin="x")
    assert "extra" not in payload["entries"][0]
    # All known fields still present.
    for field in bc.COPIED_ENTRY_FIELDS:
        assert field in payload["entries"][0]


def test_transform_preserves_entry_order() -> None:
    a = _asset_index_entry(tag="1.12.10")
    b = _asset_index_entry(tag="1.12.11")
    payload = bc.transform({"entries": [a, b]}, origin="x")
    assert [e["tag"] for e in payload["entries"]] == ["1.12.10", "1.12.11"]


def test_transform_empty_entries_is_valid_payload() -> None:
    payload = bc.transform({"entries": []}, origin="x")
    assert payload["entries"] == []
    assert payload["schema_version"] == 1
    assert "generated_at" in payload


def test_transform_rejects_non_list_entries() -> None:
    try:
        bc.transform({"entries": "nope"}, origin="x")
    except ValueError as exc:
        assert "entries" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-list entries")


def test_transform_rejects_non_dict_entry_element() -> None:
    try:
        bc.transform({"entries": [["not", "a", "dict"]]}, origin="x")
    except ValueError as exc:
        assert "entries[0]" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-dict entry")


def test_now_iso_is_z_suffixed_utc() -> None:
    out = bc._now_iso()
    assert out.endswith("Z")
    # Round-trip parse to confirm a real ISO-8601 string.
    import datetime as dt

    parsed = dt.datetime.fromisoformat(out.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
