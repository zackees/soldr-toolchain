from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.generation_reader import read_verified_generation
from scripts.publication_model import canonical_json_sha256
from scripts import build_asset_index, lint_assets
from scripts import forge_to_catalogue
from scripts.build_site import render_verified_generation


def _bound_document() -> tuple[dict, dict]:
    catalogue = json.loads(Path("examples/catalogue.v2.json").read_text())
    state = json.loads(Path("examples/publish-state.v1.json").read_text())
    state["catalogue_sha256"] = canonical_json_sha256(catalogue)
    return catalogue, state


def test_reader_accepts_bound_public_slot_parts_and_capability_two() -> None:
    catalogue, state = _bound_document()
    generation = read_verified_generation(catalogue, state)
    assert generation.entries[0].is_multipart
    assert generation.entries[0].min_client_version == 2


@pytest.mark.parametrize("url", [
    "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/a",
    "https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/a",
    "https://staging.example.test/a",
])
def test_reader_rejects_legacy_or_staging_public_transports(url: str) -> None:
    catalogue, state = _bound_document()
    catalogue["entries"][0]["parts"][0]["urls"] = [url]
    state["catalogue_sha256"] = canonical_json_sha256(catalogue)
    with pytest.raises(ValueError, match="forbidden|immutable public slot|assets branch"):
        read_verified_generation(catalogue, state)


def test_reader_preserves_direct_private_entry() -> None:
    catalogue, state = _bound_document()
    catalogue = copy.deepcopy(catalogue)
    catalogue["entries"].append({
        "owner": "zackees", "repo": "private", "tag": "v1", "asset": "licensed.zip",
        "sha256": "d" * 64, "size_bytes": 3,
        "urls": ["https://api.github.com/repos/zackees/private/releases/assets/1"],
    })
    state["catalogue_sha256"] = canonical_json_sha256(catalogue)
    generation = read_verified_generation(catalogue, state)
    assert generation.entries[-1].urls == ("https://api.github.com/repos/zackees/private/releases/assets/1",)
    legacy = build_asset_index.build_asset_index_from_generation(generation)
    assert legacy["entries"] == [{
        "owner": "zackees", "repo": "private", "tag": "v1", "asset": "licensed.zip",
        "url": "https://api.github.com/repos/zackees/private/releases/assets/1", "sha256": "d" * 64,
    }]


def test_reader_preserves_external_raw_direct_entry() -> None:
    catalogue, state = _bound_document()
    catalogue = copy.deepcopy(catalogue)
    catalogue["entries"].append({
        "owner": "example", "repo": "tool", "tag": "v1", "asset": "install.sh",
        "sha256": "e" * 64, "size_bytes": 3,
        "urls": ["https://raw.githubusercontent.com/example/tool/v1/install.sh"],
    })
    state["catalogue_sha256"] = canonical_json_sha256(catalogue)
    generation = read_verified_generation(catalogue, state)
    assert generation.entries[-1].urls == (
        "https://raw.githubusercontent.com/example/tool/v1/install.sh",
    )


def test_legacy_index_projection_drops_repository_transports_only() -> None:
    index = {"entries": [
        {"url": "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/x"},
        {"url": "https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/x"},
        {"url": "https://raw.githubusercontent.com/example/tool/v1/install.sh"},
        {"url": "https://github.com/example/tool/releases/download/v1/tool.zip"},
        {"url": "https://api.github.com/repos/zackees/private/releases/assets/1"},
    ]}
    projected = build_asset_index.direct_compatible_entries(index)
    assert [entry["url"] for entry in projected["entries"]] == [
        "https://raw.githubusercontent.com/example/tool/v1/install.sh",
        "https://github.com/example/tool/releases/download/v1/tool.zip",
        "https://api.github.com/repos/zackees/private/releases/assets/1",
    ]


def test_v2_lint_reports_forbidden_public_transport(tmp_path: Path) -> None:
    catalogue, state = _bound_document()
    catalogue["entries"][0]["parts"][0]["urls"] = [
        "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/x"
    ]
    state["catalogue_sha256"] = canonical_json_sha256(catalogue)
    cat_path, state_path = tmp_path / "catalogue.v2.json", tmp_path / "publish-state.v1.json"
    cat_path.write_text(json.dumps(catalogue), encoding="utf-8")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    issues = lint_assets.lint_verified_generation(cat_path, state_path)
    assert len(issues) == 1
    assert issues[0].rule == "V2" and issues[0].severity == "ERROR"


def test_v2_site_is_control_plane_only() -> None:
    catalogue, state = _bound_document()
    page = render_verified_generation(read_verified_generation(catalogue, state))
    assert "catalogue v2" in page
    assert "assets/LFS" in page
    assert "media.githubusercontent.com" not in page
    assert "raw.githubusercontent.com" not in page


def test_forge_preflight_uses_the_shared_verified_generation_reader(tmp_path: Path) -> None:
    catalogue, state = _bound_document()
    catalogue_path, state_path = tmp_path / "catalogue.v2.json", tmp_path / "publish-state.v1.json"
    catalogue_path.write_text(json.dumps(catalogue), encoding="utf-8")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert forge_to_catalogue._load_verified_generation(catalogue_path, state_path).generation == catalogue["generation"]
