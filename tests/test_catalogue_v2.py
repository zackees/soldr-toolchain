from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.catalogue_v2 import CURRENT_CLIENT_CAPABILITY, bind_catalogue_to_publication_state, build_document, validate_document
from scripts.publication_model import MAX_PART_COUNT, canonical_json_sha256

GENERATION = "2026-08-24T00:00:00Z-source0001"
STATE_URL = f"https://zackees.github.io/soldr-toolchain/generations/{GENERATION}/publish-state.v1.json"


def _direct() -> dict:
    return {"owner": "o", "repo": "r", "tag": "t", "asset": "a", "sha256": "a" * 64,
            "size_bytes": 1, "urls": ["https://example.test/a"]}


def _multipart() -> dict:
    return {"owner": "o", "repo": "r", "tag": "t", "asset": "b", "sha256": "b" * 64,
            "size_bytes": 3, "min_client_version": 2, "parts": [{"number": 1, "sha256": "c" * 64, "size_bytes": 3,
            "urls": ["https://example.test/b"]}]}


def _document(entries: list[dict]) -> dict:
    return {"schema_version": 2, "generation": GENERATION,
            "publication_state": {"generation": GENERATION, "url": STATE_URL}, "entries": entries}


def _schema_errors(doc: dict) -> list:
    schema = json.loads((Path("schemas") / "catalogue.v2.schema.json").read_text())
    return list(Draft202012Validator(schema).iter_errors(doc))


def test_capability_two_union_accepts_direct_and_multipart() -> None:
    assert validate_document(build_document([_direct(), _multipart()], generation=GENERATION, publication_state_url=STATE_URL)) == []
    multipart_without_field = _multipart(); multipart_without_field.pop("min_client_version")
    built = build_document([multipart_without_field], generation=GENERATION, publication_state_url=STATE_URL)
    assert built["entries"][0]["min_client_version"] == CURRENT_CLIENT_CAPABILITY


def test_schema_and_semantic_agree_on_structural_hostile_inputs() -> None:
    cases = []
    bad = _document([_direct()]); bad["extra"] = 1; cases.append(bad)
    bad = _document([_direct()]); bad["entries"][0]["extra"] = 1; cases.append(bad)
    bad = _document([_direct()]); bad["entries"][0]["size_bytes"] = True; cases.append(bad)
    bad = _document([_direct()]); bad["entries"][0]["parts"] = []; cases.append(bad)
    bad = _document([_multipart()]); bad["entries"][0]["parts"][0]["urls"] = ["http://example.test"]; cases.append(bad)
    bad = _document([_multipart()]); bad["entries"][0]["parts"] *= MAX_PART_COUNT + 1; cases.append(bad)
    bad = _document([_direct()]); bad["origin"] = None; cases.append(bad)
    bad = _document([_direct()]); bad["generated_at"] = None; cases.append(bad)
    for document in cases:
        assert _schema_errors(document), document
        assert validate_document(document), document
    bad = _document([_direct()]); bad["publication_state"]["generation"] = "other"
    assert validate_document(bad)
    bad = _document([_direct()]); bad["publication_state"]["url"] = STATE_URL + "?mutable=1"
    assert any("credential-free immutable" in error for error in validate_document(bad))


def test_generation_ascii_and_capability_hostile_inputs() -> None:
    for generation in ("slash/name", "percent%2Fname", "query?name", "hash#name", "space name", "\x00control", "caf\u00e9"):
        document = _document([_direct()])
        document["generation"] = generation
        document["publication_state"]["generation"] = generation
        assert _schema_errors(document), generation
        assert validate_document(document), generation
    document = _document([_direct()])
    document["publication_state"]["url"] = STATE_URL.replace("00:00", "00%3A00")
    assert validate_document(document)
    missing = _document([_multipart()]); missing["entries"][0].pop("min_client_version")
    assert _schema_errors(missing) and validate_document(missing)
    for value in (True, 0, 1, 3, "2"):
        document = _document([_multipart()]); document["entries"][0]["min_client_version"] = value
        assert _schema_errors(document), value
        assert validate_document(document), value


def test_semantic_limits_duplicates_contiguity_and_utf8_url_bytes() -> None:
    direct = _direct(); direct["urls"] = ["https://example.test/a", "https://example.test/a"]
    duplicate_record = _direct()
    assert validate_document(_document([direct, duplicate_record]))
    multipart = _multipart(); multipart["parts"][0]["number"] = 2
    assert any("contiguous" in error for error in validate_document(_document([multipart])))
    huge_url = "https://example.test/" + "é" * 5000
    external = _direct(); external["asset"] = "long"; external["urls"] = [huge_url]
    assert any("invalid HTTPS" in error for error in validate_document(_document([external])))
    too_large = _direct(); too_large["size_bytes"] = 8 * 1024**4 + 1
    assert validate_document(_document([too_large]))


def test_schema_semantic_parity_for_example() -> None:
    document = json.loads(Path("examples/catalogue.v2.json").read_text())
    assert not _schema_errors(document)
    assert validate_document(document) == []


def test_external_publication_state_binds_full_catalogue_and_generation() -> None:
    catalogue = json.loads(Path("examples/catalogue.v2.json").read_text())
    state = json.loads(Path("examples/publish-state.v1.json").read_text())
    assert state["catalogue_sha256"] == canonical_json_sha256(catalogue)
    binding = bind_catalogue_to_publication_state(catalogue, state).binding
    assert binding.generation == catalogue["generation"]
    bad_digest = copy.deepcopy(state); bad_digest["catalogue_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="catalogue_sha256"):
        bind_catalogue_to_publication_state(catalogue, bad_digest)
    changed_catalogue = copy.deepcopy(catalogue); changed_catalogue["origin"] = "https://example.test/changed"
    with pytest.raises(ValueError, match="catalogue_sha256"):
        bind_catalogue_to_publication_state(changed_catalogue, state)
    bad_generation = copy.deepcopy(state); bad_generation["generation"] = "other"
    with pytest.raises(ValueError, match="generation"):
        bind_catalogue_to_publication_state(catalogue, bad_generation)


def test_cross_repo_contract_fixture_is_copyable_and_bound() -> None:
    fixture = json.loads(Path("fixtures/catalogue-v2-contract.json").read_text())
    assert bind_catalogue_to_publication_state(
        fixture["catalogue"], fixture["publication_state"],
    ).binding.generation == GENERATION
