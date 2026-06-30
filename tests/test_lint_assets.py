from __future__ import annotations

import json
from pathlib import Path

import lint_assets


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _index(tools: dict) -> dict:
    return {
        "kind": "Index",
        "schema_version": 1,
        "tools": tools,
    }


def _catalog(tool: str) -> dict:
    return {
        "kind": "Catalog",
        "schema_version": 1,
        "tool": tool,
        "channels": {"stable": "MacOSX11.3"},
        "releases": [
            {
                "schema_version": 1,
                "version": "MacOSX11.3",
                "platforms": [],
            }
        ],
    }


def _catalogue_entry(rel: str) -> dict:
    return {
        "owner": "zackees",
        "repo": "soldr-toolchain",
        "tag": "assets",
        "asset": Path(rel).name,
        "url": f"https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/{rel}",
        "sha256": "0" * 64,
    }


def test_flat_catalogue_reference_allows_version_absent_from_tool_catalog(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "manifest.json",
        _index({
            "apple-sdk": {
                "descriptor": {"url": "apple-sdk/manifest.json"},
                "summary": "Apple SDK",
                "kind_hint": "sysroot",
            }
        }),
    )
    _write_json(tmp_path / "apple-sdk" / "manifest.json", _catalog("apple-sdk"))

    rel = "apple-sdk/14.5/darwin-aarch64/sdk.tar.zst"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_bytes(b"sdk")
    _write_json(
        tmp_path / "catalogue.v1.json",
        {"schema_version": 1, "entries": [_catalogue_entry(rel)]},
    )

    issues = lint_assets.lint(tmp_path)
    assert not [i for i in issues if i.severity == "ERROR"], [str(i) for i in issues]


def test_flat_catalogue_reference_allows_unindexed_tool_directory(tmp_path: Path) -> None:
    _write_json(tmp_path / "manifest.json", _index({}))

    rel = "zstd/1.5.7/linux-x64-musl/bundle.tar.zst"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_bytes(b"bundle")
    _write_json(
        tmp_path / "catalogue.v1.json",
        {"schema_version": 1, "entries": [_catalogue_entry(rel)]},
    )

    issues = lint_assets.lint(tmp_path)
    assert not [i for i in issues if i.severity == "ERROR"], [str(i) for i in issues]
