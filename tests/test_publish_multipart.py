from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.publish_multipart import GitLfsPathMaterializer, _rewrite_assets, build_publication, materialize_selected, scan_unsmudged_catalogue
from scripts.publication_model import GenerationBinding, PartitionedAsset, PublishedPart, VerifiedPublicLedger


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_production_module_entrypoint_imports_from_repository_root() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.publish_multipart", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_build_publication_rewrites_lfs_rows_and_hierarchical_assets(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    payload = b"abcdefghij"
    blob = assets / "tool" / "1" / "linux-x64" / "bundle.tar.zst"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)
    lfs_url = "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/tool/1/linux-x64/bundle.tar.zst"
    (assets / "catalogue.v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [{"owner": "o", "repo": "r", "tag": "1", "asset": "bundle.tar.zst", "url": lfs_url, "sha256": _sha(payload)}],
            }
        )
    )
    (assets / "manifest.json").write_text(json.dumps({"kind": "Index", "schema_version": 1, "tools": {"tool": {"descriptor": {"url": "tool/manifest.json"}}}}))
    (assets / "tool" / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "Catalog",
                "schema_version": 1,
                "tool": "tool",
                "releases": [
                    {
                        "version": "1",
                        "min_client_version": 1,
                        "platforms": [
                            {
                                "asset": {
                                    "filename": "bundle.tar.zst",
                                    "size_bytes": len(payload),
                                    "sha256": _sha(payload),
                                    "urls": [lfs_url],
                                }
                            }
                        ],
                    }
                ],
            }
        )
    )

    public = tmp_path / "public"
    www = tmp_path / "www"
    result = build_publication(
        assets,
        public,
        www,
        source_commit="a" * 40,
        source_tree="b" * 40,
        active_slot="public-a",
        generation="g",
        target_bytes=4,
    )

    catalogue = json.loads((www / "catalogue.v2.json").read_text())
    entry = catalogue["entries"][0]
    assert "urls" not in entry
    assert [part["size_bytes"] for part in entry["parts"]] == [4, 4, 2]
    assert all("raw.githubusercontent.com/zackees/soldr-toolchain/public-a/" in part["urls"][0] for part in entry["parts"])
    assert b"".join((public / part.path).read_bytes() for part in result.assets[_sha(payload)].parts) == payload
    rendered = json.loads((www / "tool" / "manifest.json").read_text())
    asset = rendered["releases"][0]["platforms"][0]["asset"]
    assert "urls" not in asset and len(asset["parts"]) == 3
    assert rendered["releases"][0]["min_client_version"] == 2
    published_text = "\n".join(path.read_text() for path in www.rglob("*.json"))
    assert "media.githubusercontent.com" not in published_text
    assert result.max_part_bytes == 4
    assert result.part_count == 3


def test_duplicate_v1_filenames_use_unique_source_path_identities(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    rows = []
    for platform, payload in (("linux-x64", b"one"), ("linux-arm64", b"two")):
        relative = f"tool/1/{platform}/bundle.tar.zst"
        source = assets / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)
        rows.append({
            "owner": "zackees",
            "repo": "soldr-toolchain",
            "tag": "assets",
            "asset": "bundle.tar.zst",
            "url": f"https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/{relative}",
            "sha256": _sha(payload),
        })
    (assets / "catalogue.v1.json").write_text(json.dumps({"entries": rows}))
    (assets / "manifest.json").write_text('{"kind":"Index","schema_version":1,"tools":{}}')

    build_publication(
        assets,
        tmp_path / "public",
        tmp_path / "www",
        source_commit="a" * 40,
        source_tree="b" * 40,
        active_slot="public-a",
        generation="g",
    )

    catalogue = json.loads((tmp_path / "www" / "catalogue.v2.json").read_text())
    assert {entry["asset"] for entry in catalogue["entries"]} == {
        "tool/1/linux-arm64/bundle.tar.zst",
        "tool/1/linux-x64/bundle.tar.zst",
    }
    state = json.loads((tmp_path / "www" / "publish-state.v1.json").read_text())
    assert len(state["logical_assets"]) == 2


def test_direct_non_lfs_rows_remain_direct(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "catalogue.v1.json").write_text(
        json.dumps(
            {
                "entries": [{"owner": "o", "repo": "r", "tag": "1", "asset": "a", "url": "https://example.test/a", "sha256": "a" * 64, "size_bytes": 1}],
            }
        )
    )
    (assets / "manifest.json").write_text('{"kind":"Index","schema_version":1,"tools":{}}')
    result = build_publication(
        assets,
        tmp_path / "public",
        tmp_path / "www",
        source_commit="a" * 40,
        source_tree="b" * 40,
        active_slot="public-b",
        generation="g",
    )
    entry = json.loads((tmp_path / "www" / "catalogue.v2.json").read_text())["entries"][0]
    assert entry["urls"] == ["https://example.test/a"]
    assert result.part_count == 0


def test_new_logical_aliases_share_one_materialized_oid_regardless_of_sort_order(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    payload = b"shared immutable bytes"
    digest = _sha(payload)
    materialized = assets / "tool" / "z-materialized.bin"
    pointer = assets / "tool" / "a-pointer.bin"
    materialized.parent.mkdir(parents=True)
    materialized.write_bytes(payload)
    pointer.write_bytes(f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(payload)}\n".encode())
    base = "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/tool/"
    # Inventory order selects z-materialized, while deterministic build order
    # encounters a-pointer first. The OID, not either path, owns partitioning.
    (assets / "catalogue.v1.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"owner": "o", "repo": "r", "tag": "1", "asset": "z", "url": base + materialized.name, "sha256": digest},
                    {"owner": "o", "repo": "r", "tag": "1", "asset": "a", "url": base + pointer.name, "sha256": digest},
                ]
            }
        )
    )
    result = build_publication(
        assets,
        tmp_path / "public",
        tmp_path / "www",
        source_commit="a" * 40,
        source_tree="b" * 40,
        active_slot="public-a",
        generation="g",
        target_bytes=4,
    )
    catalogue = json.loads((tmp_path / "www" / "catalogue.v2.json").read_text())
    assert len(result.assets) == 1
    assert catalogue["entries"][0]["parts"] == catalogue["entries"][1]["parts"]


def test_pages_snapshot_preserves_retained_generation_metadata(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "catalogue.v1.json").write_text('{"entries":[]}')
    (assets / "manifest.json").write_text('{"kind":"Index","schema_version":1,"tools":{}}')
    old_state = {"generation": "old", "sentinel": "retained"}
    old_catalogue = {"schema_version": 2, "entries": []}
    old = VerifiedPublicLedger(
        old_state,
        GenerationBinding(
            "old",
            "1" * 40,
            "2" * 40,
            "public-a",
            "3" * 40,
            "4" * 40,
            "public-b",
            "5" * 40,
            "6" * 40,
            "7" * 64,
        ),
        old_catalogue,
    )
    www = tmp_path / "www"
    build_publication(
        assets,
        tmp_path / "public",
        www,
        source_commit="a" * 40,
        source_tree="b" * 40,
        active_slot="public-b",
        generation="new",
        retained_ledgers=[old],
    )
    assert json.loads((www / "generations" / "old" / "publish-state.v1.json").read_text()) == old_state
    assert json.loads((www / "generations" / "old" / "catalogue.v2.json").read_text()) == old_catalogue


def test_lfs_source_must_match_declared_oid_and_stay_inside_assets(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "manifest.json").write_text('{"kind":"Index","schema_version":1,"tools":{}}')
    (assets / "catalogue.v1.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "owner": "o",
                        "repo": "r",
                        "tag": "1",
                        "asset": "x",
                        "sha256": "0" * 64,
                        "url": "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/../outside",
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="unsafe path"):
        build_publication(assets, tmp_path / "public", tmp_path / "www", source_commit="a" * 40, source_tree="b" * 40, active_slot="public-a", generation="g")


def test_unsmudged_scan_and_selective_deduplicated_materialization(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    payload = b"same"
    oid = _sha(payload)
    for name in ("one", "two"):
        (assets / name).write_bytes(f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize 4\n".encode())
    (assets / "catalogue.v1.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"owner": "o", "repo": "r", "tag": "1", "asset": "one", "url": "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/one"},
                    {"owner": "o", "repo": "r", "tag": "1", "asset": "two", "url": "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/two"},
                    {"owner": "o", "repo": "r", "tag": "1", "asset": "direct", "url": "https://example.test/a"},
                ]
            }
        )
    )
    rows = scan_unsmudged_catalogue(assets)
    assert [row.transport for row in rows] == ["lfs", "lfs", "direct"]
    calls = []

    class M:
        def __init__(self, _root, path):
            calls.append(path)

        def materialize(self, *_):
            return payload

    classifications = {rows[0].logical_key: "new_or_invalid", rows[1].logical_key: "explicit_repartition", rows[2].logical_key: "direct"}
    assert materialize_selected(rows, classifications, assets, materializer_factory=M) == {oid: payload}
    assert calls == ["one"]


def test_exact_lfs_materializer_pulls_only_the_current_ref_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"same"
    oid = _sha(payload)
    source = tmp_path / "tool" / "bundle.tar.zst"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    materialized = GitLfsPathMaterializer(tmp_path, "tool/bundle.tar.zst").materialize_path(
        oid,
        len(payload),
    )
    assert materialized == source
    assert calls == [
        [
            "git",
            "lfs",
            "pull",
            "origin",
            "--include=tool/bundle.tar.zst",
        ]
    ]


def test_asset_rewrite_handles_deep_manifests_without_recursion() -> None:
    sha = "a" * 64
    part = PublishedPart(1, "b" * 64, 3, "sha256/a/0001.part")
    published = {
        sha: PartitionedAsset(sha, 3, 1, 3, (part,)),
    }
    leaf: dict[str, object] = {
        "sha256": sha,
        "urls": ["https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/deep"],
    }
    document: dict[str, object] = leaf
    for _ in range(2_000):
        document = {"child": document}

    _rewrite_assets(document, published, "generations/g-data")

    assert "urls" not in leaf
    assert leaf["size_bytes"] == 3
    assert leaf["parts"] == [{
        "number": 1,
        "sha256": "b" * 64,
        "size_bytes": 3,
        "urls": ["https://raw.githubusercontent.com/zackees/soldr-toolchain/generations/g-data/sha256/a/0001.part"],
    }]
