from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.catalogue_v2 import build_document, validate_document
from scripts.publication_model import MAX_PART_COUNT


def _direct() -> dict:
    return {"owner": "o", "repo": "r", "tag": "t", "asset": "a", "sha256": "a" * 64,
            "size_bytes": 1, "urls": ["https://example.test/a"]}


def _multipart() -> dict:
    return {"owner": "o", "repo": "r", "tag": "t", "asset": "b", "sha256": "b" * 64,
            "size_bytes": 3, "parts": [{"number": 1, "sha256": "c" * 64, "size_bytes": 3,
            "urls": ["https://example.test/b"]}]}


def _schema_errors(doc: dict) -> list:
    schema = json.loads((Path("schemas") / "catalogue.v2.schema.json").read_text())
    return list(Draft202012Validator(schema).iter_errors(doc))


def test_capability_two_union_accepts_direct_and_multipart() -> None:
    assert validate_document(build_document([_direct(), _multipart()])) == []


def test_schema_and_semantic_agree_on_structural_hostile_inputs() -> None:
    cases = []
    bad = {"schema_version": 2, "entries": [_direct()]}; bad["extra"] = 1; cases.append(bad)
    bad = {"schema_version": 2, "entries": [_direct()]}; bad["entries"][0]["extra"] = 1; cases.append(bad)
    bad = {"schema_version": 2, "entries": [_direct()]}; bad["entries"][0]["size_bytes"] = True; cases.append(bad)
    bad = {"schema_version": 2, "entries": [_direct()]}; bad["entries"][0]["parts"] = []; cases.append(bad)
    bad = {"schema_version": 2, "entries": [_multipart()]}; bad["entries"][0]["parts"][0]["urls"] = ["http://example.test"]; cases.append(bad)
    bad = {"schema_version": 2, "entries": [_multipart()]}; bad["entries"][0]["parts"] *= MAX_PART_COUNT + 1; cases.append(bad)
    bad = {"schema_version": 2, "entries": [_direct()], "origin": None}; cases.append(bad)
    bad = {"schema_version": 2, "entries": [_direct()], "generated_at": None}; cases.append(bad)
    for document in cases:
        assert _schema_errors(document), document
        assert validate_document(document), document


def test_semantic_limits_duplicates_contiguity_and_utf8_url_bytes() -> None:
    direct = _direct(); direct["urls"] = ["https://example.test/a", "https://example.test/a"]
    duplicate_record = _direct()
    assert validate_document({"schema_version": 2, "entries": [direct, duplicate_record]})
    multipart = _multipart(); multipart["parts"][0]["number"] = 2
    assert any("contiguous" in error for error in validate_document({"schema_version": 2, "entries": [multipart]}))
    huge_url = "https://example.test/" + "é" * 5000
    external = _direct(); external["asset"] = "long"; external["urls"] = [huge_url]
    assert any("invalid HTTPS" in error for error in validate_document({"schema_version": 2, "entries": [external]}))
    too_large = _direct(); too_large["size_bytes"] = 8 * 1024**4 + 1
    assert validate_document({"schema_version": 2, "entries": [too_large]})


def test_schema_semantic_parity_for_example() -> None:
    document = json.loads(Path("examples/catalogue.v2.json").read_text())
    assert not _schema_errors(document)
    assert validate_document(document) == []
