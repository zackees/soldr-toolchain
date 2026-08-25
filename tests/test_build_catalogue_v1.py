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

import pytest

from scripts import build_catalogue_v1 as bc


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


@pytest.mark.parametrize("url", [
    "https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/x",
    "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/x",
])
def test_transform_excludes_repository_hosted_transport_rows(url: str) -> None:
    payload = bc.transform({"entries": [_asset_index_entry(url=url)]}, origin="x")
    assert payload["entries"] == []


def test_transform_retains_private_or_external_direct_entries() -> None:
    external = _external_entry()
    payload = bc.transform({"entries": []}, origin="x", external_entries=[external])
    assert payload["entries"] == [external]


def test_transform_retains_external_raw_direct_entry() -> None:
    entry = _asset_index_entry(
        owner="example",
        repo="tool",
        url="https://raw.githubusercontent.com/example/tool/v1/install.sh",
    )
    assert bc.transform({"entries": [entry]}, origin="x")["entries"] == [entry]


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


def _external_entry(**overrides: str) -> dict[str, str]:
    base = {
        "owner": "zackees",
        "repo": "soldr-toolchain-private",
        "tag": "msvc-14.44.35207",
        "asset": "bundle.tar.zst",
        "url": "https://api.github.com/repos/zackees/soldr-toolchain-private/releases/assets/505002676",
        "sha256": "932d9e36cbad6243b6ca9e50332b258f7efeb086d2f21c80447738064418c714",
    }
    base.update(overrides)
    return base


def test_transform_merges_external_entries_after_generated() -> None:
    generated = _asset_index_entry()
    external = _external_entry()
    payload = bc.transform(
        {"entries": [generated]}, origin="x", external_entries=[external]
    )
    assert payload["entries"] == [generated, external]


def test_transform_external_entries_none_is_noop() -> None:
    generated = _asset_index_entry()
    payload = bc.transform({"entries": [generated]}, origin="x", external_entries=None)
    assert payload["entries"] == [generated]


def test_transform_rejects_external_entry_duplicating_generated_url() -> None:
    generated = _asset_index_entry()
    dup = _external_entry(url=generated["url"])
    try:
        bc.transform({"entries": [generated]}, origin="x", external_entries=[dup])
    except ValueError as exc:
        assert "duplicates" in str(exc)
    else:
        raise AssertionError("expected ValueError for duplicate url")


def test_transform_rejects_external_entry_missing_url() -> None:
    bad = _external_entry()
    del bad["url"]
    try:
        bc.transform({"entries": []}, origin="x", external_entries=[bad])
    except ValueError as exc:
        assert "url" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing url")


def test_load_external_entries_round_trips(tmp_path: Path) -> None:
    doc = {"schema_version": 1, "entries": [_external_entry()]}
    path = tmp_path / "external-entries.v1.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    entries = bc.load_external_entries(path)
    assert entries == [_external_entry()]


def test_load_external_entries_rejects_missing_field(tmp_path: Path) -> None:
    bad_entry = _external_entry()
    del bad_entry["sha256"]
    doc = {"schema_version": 1, "entries": [bad_entry]}
    path = tmp_path / "external-entries.v1.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    try:
        bc.load_external_entries(path)
    except ValueError as exc:
        assert "sha256" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing field")


def test_load_external_entries_rejects_schema_invalid_entry(tmp_path: Path) -> None:
    """Field-complete but schema-invalid (63-char sha256) must fail loudly
    at generation time, not only in the separate catalogue-schema.yml gate."""
    bad_entry = _external_entry(sha256="a" * 63)  # one short of the required 64
    doc = {"schema_version": 1, "entries": [bad_entry]}
    path = tmp_path / "external-entries.v1.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    try:
        bc.load_external_entries(path)
    except ValueError as exc:
        assert "schema violation" in str(exc)
    else:
        raise AssertionError("expected ValueError for schema-invalid sha256")


def test_load_external_entries_drops_unknown_fields(tmp_path: Path) -> None:
    entry = _external_entry(extra="should-be-dropped")  # type: ignore[arg-type]
    doc = {"schema_version": 1, "entries": [entry]}
    path = tmp_path / "external-entries.v1.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    entries = bc.load_external_entries(path)
    assert "extra" not in entries[0]


def test_repo_external_entries_v1_json_has_expected_msvc_entry() -> None:
    """Guard against silent drift of the checked-in curated entry."""
    path = Path(__file__).parents[1] / "external-entries.v1.json"
    entries = bc.load_external_entries(path)
    assert entries == [_external_entry()]


def test_now_iso_is_z_suffixed_utc() -> None:
    out = bc._now_iso()
    assert out.endswith("Z")
    # Round-trip parse to confirm a real ISO-8601 string.
    import datetime as dt

    parsed = dt.datetime.fromisoformat(out.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
