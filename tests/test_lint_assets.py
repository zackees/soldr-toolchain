from __future__ import annotations

import json
from pathlib import Path

from scripts import lint_assets


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


def test_generated_nightly_catalogue_is_a_reserved_top_level_file(tmp_path: Path) -> None:
    _write_json(tmp_path / "manifest.json", _index({}))
    _write_json(tmp_path / "rust-nightly-versions.v1.json", {"schema_version": 1})

    issues = lint_assets.lint(tmp_path)
    assert not [issue for issue in issues if issue.rule == "R9"], [str(issue) for issue in issues]


def test_multipart_source_inventory_reference_allows_unindexed_tool_directory(tmp_path: Path) -> None:
    # Post-v2-cutover, raw/media rows live only in source-inventory.v1.json
    # (soldr-toolchain#193).
    _write_json(tmp_path / "manifest.json", _index({}))
    rel = "bzip2/1.0.8/linux-x64-musl/bundle.tar.zst"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_bytes(b"bundle")
    _write_json(
        tmp_path / "source-inventory.v1.json",
        {"schema_version": 5, "entries": [_catalogue_entry(rel)]},
    )
    _write_json(tmp_path / "multipart-external-entries.v1.json", {"schema_version": 1, "entries": []})

    issues = lint_assets.lint(tmp_path)
    assert not issues, [str(i) for i in issues]


def test_multipart_source_inventory_url_missing_on_disk_is_r8_error(tmp_path: Path) -> None:
    _write_json(tmp_path / "manifest.json", _index({}))
    _write_json(
        tmp_path / "source-inventory.v1.json",
        {"schema_version": 5, "entries": [_catalogue_entry("zstd/1.5.7/linux-x64-musl/bundle.tar.zst")]},
    )

    issues = lint_assets.lint(tmp_path)
    assert any(i.rule == "R8" and i.severity == "ERROR" for i in issues), [str(i) for i in issues]


def test_unrelated_top_level_file_is_still_an_r9_warning(tmp_path: Path) -> None:
    _write_json(tmp_path / "manifest.json", _index({}))
    (tmp_path / "unexpected.json").write_text("{}\n", encoding="utf-8")

    issues = lint_assets.lint(tmp_path)
    assert any(issue.rule == "R9" and issue.where == "unexpected.json" for issue in issues)


def test_index_descriptor_sha256_must_match_catalog_bytes(tmp_path: Path) -> None:
    _write_json(tmp_path / "tool" / "manifest.json", _catalog("tool"))
    _write_json(
        tmp_path / "manifest.json",
        _index({
            "tool": {
                "descriptor": {
                    "url": "tool/manifest.json",
                    "size_bytes": 1,
                    "sha256": "0" * 64,
                },
                "summary": "Tool",
                "kind_hint": "tool",
            }
        }),
    )

    issues = lint_assets.lint(tmp_path)
    messages = [str(i) for i in issues]
    assert any("R11" in msg and "descriptor.sha256" in msg for msg in messages), messages
    assert any("R11" in msg and "descriptor.size_bytes" in msg for msg in messages), messages


def test_content_addressed_store_accepts_matching_digest(tmp_path: Path) -> None:
    import hashlib

    _write_json(tmp_path / "manifest.json", _index({}))
    body = b'{"schema_version": 1}\n'
    digest = hashlib.sha256(body).hexdigest()
    target = tmp_path / "sha256" / digest / "rust-nightly-versions.v1.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(body)

    issues = lint_assets.lint(tmp_path)
    assert not issues, [str(i) for i in issues]


def test_content_addressed_store_rejects_mismatched_digest(tmp_path: Path) -> None:
    _write_json(tmp_path / "manifest.json", _index({}))
    target = tmp_path / "sha256" / ("0" * 64) / "rust-nightly-versions.v1.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"tampered\n")
    (tmp_path / "sha256" / "loose.json").write_bytes(b"{}")

    issues = lint_assets.lint(tmp_path)
    assert sum(1 for i in issues if i.rule == "R12" and i.severity == "ERROR") == 2, [str(i) for i in issues]
