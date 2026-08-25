from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import publish_multipart as pm
from scripts.publish_multipart import GitLfsPathMaterializer, _rewrite_assets, build_publication, materialize_selected, scan_unsmudged_catalogue
from scripts.publication_model import (
    GenerationBinding,
    PartitionedAsset,
    PublishedPart,
    VerifiedPublicLedger,
    canonical_json_sha256,
    verified_public_ledger,
)


@pytest.fixture(autouse=True)
def _empty_external_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pm, "_EXTERNAL_LLVM_POLICY", {})


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_source_controls(assets: Path) -> None:
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "source-inventory.v1.json").write_text(
        '{"schema_version":5,"entries":[]}'
    )
    (assets / "multipart-external-entries.v1.json").write_text(
        '{"schema_version":1,"expected_source_entries":0,'
        '"expected_external_entries":0,"entries":[]}'
    )


def _allow_external(
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str,
    path: str,
    asset: str,
    sha256: str,
    size_bytes: int,
) -> None:
    monkeypatch.setattr(
        pm,
        "_EXTERNAL_LLVM_POLICY",
        {
            url: {
                "path": path,
                "asset": asset,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
        },
    )


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
    _write_source_controls(assets)
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
    _write_source_controls(assets)
    rows = []
    payloads = {}
    for platform, payload in (("linux-x64", b"one"), ("linux-arm64", b"two")):
        relative = f"tool/1/{platform}/bundle.tar.zst"
        payloads[relative] = payload
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

    for relative, payload in payloads.items():
        (assets / relative).write_bytes((
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{_sha(payload)}\n"
            f"size {len(payload)}\n"
        ).encode())
    inventory = scan_unsmudged_catalogue(assets)
    assert len({row.logical_key for row in inventory}) == 2
    assert {row.logical_key for row in inventory} == set(state["logical_assets"])


def test_direct_non_lfs_rows_remain_direct(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    _write_source_controls(assets)
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


def test_external_llvm_is_inventory_bound_and_partitioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tmp_path / "assets"
    _write_source_controls(assets)
    payload = b"external llvm bytes"
    digest = _sha(payload)
    url = (
        "https://media.githubusercontent.com/media/zackees/clang-tool-chain-bins/"
        "main/assets/clang/linux/x86_64/llvm-21.1.5-linux-x86_64.tar.zst"
    )
    _allow_external(
        monkeypatch,
        url=url,
        path="clang/linux/x86_64/llvm-21.1.5-linux-x86_64.tar.zst",
        asset="llvm-21.1.5-linux-x86_64.tar.zst",
        sha256=digest,
        size_bytes=len(payload),
    )
    (assets / "catalogue.v1.json").write_text('{"entries": []}')
    (assets / "multipart-external-entries.v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "expected_source_entries": 0,
                "expected_external_entries": 1,
                "entries": [
                    {
                        "owner": "zackees",
                        "repo": "clang-tool-chain-bins",
                        "tag": "main",
                        "asset": "llvm-21.1.5-linux-x86_64.tar.zst",
                        "url": url,
                        "sha256": digest,
                        "size_bytes": len(payload),
                    }
                ]
            }
        )
    )
    (assets / "manifest.json").write_text(
        '{"kind":"Index","schema_version":1,"tools":{}}'
    )

    inventory = scan_unsmudged_catalogue(assets)
    assert len(inventory) == 1
    assert inventory[0].source_path == (
        "clang/linux/x86_64/llvm-21.1.5-linux-x86_64.tar.zst"
    )

    class NeverMaterialize:
        def __init__(self, *_: object) -> None:
            raise AssertionError("an exact public mapping must not refetch external LFS")

    assert materialize_selected(
        inventory,
        {inventory[0].logical_key: "exact"},
        assets,
        materializer_factory=NeverMaterialize,
    ) == {}

    class FakeMaterializer:
        def __init__(self, checkout: Path, source_path: str) -> None:
            self.path = checkout / source_path

        def materialize(self, oid_sha256: str, size_bytes: int) -> bytes:
            assert (oid_sha256, size_bytes) == (digest, len(payload))
            self.path.write_bytes(payload)
            return payload

    materialize_selected(
        inventory,
        {inventory[0].logical_key: "new_or_invalid"},
        assets,
        materializer_factory=FakeMaterializer,
    )
    result = build_publication(
        assets,
        tmp_path / "public",
        tmp_path / "www",
        source_commit="a" * 40,
        source_tree="b" * 40,
        active_slot="public-a",
        generation="llvm",
        target_bytes=8,
    )
    entry = json.loads((tmp_path / "www" / "catalogue.v2.json").read_text())["entries"][0]
    assert entry["repo"] == "clang-tool-chain-bins"
    assert entry["source_path"].startswith("clang/")
    assert "urls" not in entry
    assert result.part_count == 3
    assert not (tmp_path / "www" / "source-inventory.v1.json").exists()
    assert not (tmp_path / "www" / "multipart-external-entries.v1.json").exists()


def test_external_http_materializer_is_bounded_and_used_by_production_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tmp_path / "assets"
    _write_source_controls(assets)
    payload = b"bounded external llvm"
    digest = _sha(payload)
    url = (
        "https://media.githubusercontent.com/media/zackees/clang-tool-chain-bins/"
        "main/assets/clang/linux/arm64/llvm-21.1.5-linux-arm64.tar.zst"
    )
    _allow_external(
        monkeypatch,
        url=url,
        path="clang/linux/arm64/llvm-21.1.5-linux-arm64.tar.zst",
        asset="llvm-21.1.5-linux-arm64.tar.zst",
        sha256=digest,
        size_bytes=len(payload),
    )
    (assets / "catalogue.v1.json").write_text('{"entries":[]}')
    (assets / "multipart-external-entries.v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "expected_source_entries": 0,
                "expected_external_entries": 1,
                "entries": [
                    {
                        "owner": "zackees",
                        "repo": "clang-tool-chain-bins",
                        "tag": "main",
                        "asset": "llvm-21.1.5-linux-arm64.tar.zst",
                        "url": url,
                        "sha256": digest,
                        "size_bytes": len(payload),
                    }
                ],
            }
        )
    )
    inventory = scan_unsmudged_catalogue(assets)

    class Response(io.BytesIO):
        def __init__(self, body: bytes, content_length: int | None) -> None:
            super().__init__(body)
            self.headers = (
                {} if content_length is None else {"Content-Length": str(content_length)}
            )

    calls: list[str] = []

    def open_valid(request: object, *, timeout: int) -> Response:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        assert timeout == 120
        return Response(payload, len(payload))

    monkeypatch.setattr(pm.urllib.request, "urlopen", open_valid)
    result = materialize_selected(
        inventory, {inventory[0].logical_key: "new_or_invalid"}, assets
    )
    assert Path(result[digest]).read_bytes() == payload
    assert calls == [url]

    overflow_path = "clang/linux/arm64/llvm-21.1.5-overflow.tar.zst"
    overflow = pm.HttpPathMaterializer(assets, overflow_path, url)
    monkeypatch.setattr(
        pm.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(payload + b"x", None),
    )
    before = set((assets / "clang" / "linux" / "arm64").iterdir())
    with pytest.raises(ValueError, match="exceeds declared size"):
        overflow.materialize_path(digest, len(payload))
    assert set((assets / "clang" / "linux" / "arm64").iterdir()) == before


def test_publication_fails_closed_without_complete_control_inventories(
    tmp_path: Path,
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "catalogue.v1.json").write_text('{"entries":[]}')
    with pytest.raises(ValueError, match="requires .*inventory"):
        scan_unsmudged_catalogue(assets)

    _write_source_controls(assets)
    policy = json.loads((assets / "multipart-external-entries.v1.json").read_text())
    policy["expected_source_entries"] = 1
    (assets / "multipart-external-entries.v1.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="source inventory entry count"):
        scan_unsmudged_catalogue(assets)


def test_pages_reject_any_unrewritten_external_lfs_url(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    _write_source_controls(assets)
    (assets / "catalogue.v1.json").write_text('{"entries":[]}')
    (assets / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "Index",
                "schema_version": 1,
                "leak": "https://media.githubusercontent.com/media/zackees/clang-tool-chain-bins/main/assets/clang/leak.tar.zst",
            }
        )
    )
    with pytest.raises(ValueError, match="retains an LFS delivery URL"):
        build_publication(
            assets,
            tmp_path / "public",
            tmp_path / "www",
            source_commit="a" * 40,
            source_tree="b" * 40,
            active_slot="public-a",
            generation="leak",
        )


def test_local_pages_json_is_pinned_to_immutable_generation(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    _write_source_controls(assets)
    nightly = {"schema_version": 1, "nightlies": {"nightly-2026-08-25": {}}}
    source_bytes = json.dumps(nightly, indent=2).encode()
    (assets / "rust-nightly-versions.v1.json").write_bytes(source_bytes)
    (assets / "catalogue.v1.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "owner": "zackees",
                        "repo": "soldr-toolchain",
                        "tag": "assets",
                        "asset": "rust-nightly-versions.v1.json",
                        "url": "https://zackees.github.io/soldr-toolchain/rust-nightly-versions.v1.json",
                        "sha256": _sha(source_bytes),
                        "size_bytes": len(source_bytes),
                    }
                ]
            }
        )
    )
    (assets / "manifest.json").write_text(
        '{"kind":"Index","schema_version":1,"tools":{}}'
    )

    build_publication(
        assets,
        tmp_path / "public",
        tmp_path / "www",
        source_commit="a" * 40,
        source_tree="b" * 40,
        active_slot="public-a",
        generation="nightly-pin",
    )

    deployed = (tmp_path / "www" / "generations" / "nightly-pin" / "rust-nightly-versions.v1.json").read_bytes()
    entry = json.loads((tmp_path / "www" / "catalogue.v2.json").read_text())["entries"][0]
    assert deployed == source_bytes
    assert (tmp_path / "www" / "rust-nightly-versions.v1.json").read_bytes() == source_bytes
    assert entry["urls"] == [
        "https://zackees.github.io/soldr-toolchain/generations/nightly-pin/rust-nightly-versions.v1.json"
    ]
    assert entry["size_bytes"] == len(source_bytes)
    assert entry["sha256"] == _sha(source_bytes)


@pytest.mark.parametrize("corrupt_field", ["sha256", "size_bytes"])
def test_local_pages_json_must_match_v1_integrity_metadata(tmp_path: Path, corrupt_field: str) -> None:
    assets = tmp_path / "assets"
    _write_source_controls(assets)
    source_bytes = b'{\n  "schema_version": 1\n}\n'
    row = {
        "owner": "zackees",
        "repo": "soldr-toolchain",
        "tag": "assets",
        "asset": "rust-nightly-versions.v1.json",
        "url": "https://zackees.github.io/soldr-toolchain/rust-nightly-versions.v1.json",
        "sha256": _sha(source_bytes),
        "size_bytes": len(source_bytes),
    }
    row[corrupt_field] = "0" * 64 if corrupt_field == "sha256" else len(source_bytes) + 1
    (assets / "rust-nightly-versions.v1.json").write_bytes(source_bytes)
    (assets / "catalogue.v1.json").write_text(json.dumps({"entries": [row]}))
    (assets / "manifest.json").write_text('{"kind":"Index","schema_version":1,"tools":{}}')

    with pytest.raises(ValueError, match="verification"):
        build_publication(
            assets,
            tmp_path / "public",
            tmp_path / "www",
            source_commit="a" * 40,
            source_tree="b" * 40,
            active_slot="public-a",
            generation="bad-direct",
        )


def test_legacy_root_direct_url_migrates_to_raw_p2_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_payload = b'{"legacy":true}'
    legacy_catalogue = {
        "schema_version": 2,
        "entries": [
            {
                "owner": "zackees",
                "repo": "soldr-toolchain",
                "tag": "assets",
                "asset": "rust-nightly-versions.v1.json",
                "sha256": _sha(legacy_payload),
                "size_bytes": len(legacy_payload),
                "urls": ["https://zackees.github.io/soldr-toolchain/rust-nightly-versions.v1.json"],
            }
        ],
    }
    legacy_digest = canonical_json_sha256(legacy_catalogue)
    binding = GenerationBinding(
        "source-legacy",
        "1" * 40,
        "2" * 40,
        "public-a",
        "3" * 40,
        "4" * 40,
        "public-b",
        "5" * 40,
        "6" * 40,
        legacy_digest,
    )
    legacy_state = {
        "generation": binding.generation,
        "source": {"branch": "assets", "commit": binding.source_commit, "tree": binding.source_tree},
        "active": {"slot": binding.active_slot, "commit": binding.active_commit, "tree": binding.active_tree},
        "previous": {
            "slot": binding.previous_slot,
            "commit": binding.previous_commit,
            "tree": binding.previous_tree,
        },
        "catalogue_sha256": legacy_digest,
        "assets_by_sha256": {},
        "logical_assets": {},
    }
    legacy = verified_public_ledger(legacy_state, binding, legacy_catalogue)

    def unexpected_network(*_args, **_kwargs):
        pytest.fail("legacy mutable direct URL must not be fetched for retention")

    monkeypatch.setattr("scripts.publisher_transaction.urlopen", unexpected_network)
    retained = pm.fetch_retained_direct_payloads(
        [legacy], pages_base="https://zackees.github.io/soldr-toolchain"
    )
    assert retained == {}

    assets = tmp_path / "assets"
    _write_source_controls(assets)
    current_payload = b'{\n  "current": true\n}\n'
    (assets / "rust-nightly-versions.v1.json").write_bytes(current_payload)
    (assets / "catalogue.v1.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "owner": "zackees",
                        "repo": "soldr-toolchain",
                        "tag": "assets",
                        "asset": "rust-nightly-versions.v1.json",
                        "url": "https://zackees.github.io/soldr-toolchain/rust-nightly-versions.v1.json",
                        "sha256": _sha(current_payload),
                        "size_bytes": len(current_payload),
                    }
                ]
            }
        )
    )
    (assets / "manifest.json").write_text('{"kind":"Index","schema_version":1,"tools":{}}')
    www = tmp_path / "www"
    build_publication(
        assets,
        tmp_path / "public",
        www,
        source_commit="a" * 40,
        source_tree="b" * 40,
        active_slot="public-b",
        generation="source-current-p2",
        retained_ledgers=[legacy],
        retained_direct_payloads=retained,
    )

    current_path = www / "generations" / "source-current-p2" / "rust-nightly-versions.v1.json"
    assert current_path.read_bytes() == current_payload
    assert not (www / "generations" / "source-legacy" / "rust-nightly-versions.v1.json").exists()
    entry = json.loads((www / "catalogue.v2.json").read_text())["entries"][0]
    assert entry["sha256"] == _sha(current_payload)
    assert entry["urls"] == [
        "https://zackees.github.io/soldr-toolchain/"
        "generations/source-current-p2/rust-nightly-versions.v1.json"
    ]


def test_refresh_never_materializes_the_assets_lfs_checkout() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/refresh-manifest.yml").read_text()
    assert "lfs: true" not in workflow
    assert "lfs: false" in workflow
    assert "--include-legacy-local" in workflow
    assert "source-inventory.v1.json" in workflow


def test_publisher_generation_includes_wire_format_version() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/publish-multipart.yml").read_text()
    assert 'generation="source-${source_commit:0:12}-p3"' in workflow


def test_retarget_refreshes_stable_and_generation_descriptor_hashes(
    tmp_path: Path,
) -> None:
    www = tmp_path / "www"
    generation = "source-test-p2"
    old_ref = "public-a"
    new_ref = "public-b"
    child = {
        "kind": "Catalog",
        "schema_version": 1,
        "releases": [
            {
                "version": "1",
                "platforms": [
                    {
                        "asset": {
                            "sha256": "a" * 64,
                            "parts": [
                                {
                                    "urls": [
                                        "https://raw.githubusercontent.com/"
                                        f"zackees/soldr-toolchain/{old_ref}/part"
                                    ]
                                }
                            ],
                        }
                    }
                ],
            }
        ],
    }
    child_bytes = pm.canonical_json_bytes(child)
    root = {
        "kind": "Index",
        "schema_version": 1,
        "tools": {
            "tool": {
                "descriptor": {
                    "url": f"generations/{generation}/tool/manifest.json",
                    "size_bytes": len(child_bytes),
                    "sha256": _sha(child_bytes),
                }
            }
        },
    }
    for base in (www, www / "generations" / generation):
        (base / "tool").mkdir(parents=True)
        (base / "tool" / "manifest.json").write_bytes(child_bytes)
        (base / "manifest.json").write_bytes(pm.canonical_json_bytes(root))
    catalogue = {"schema_version": 2, "entries": []}
    (www / "generations" / generation / "catalogue.v2.json").write_bytes(
        pm.canonical_json_bytes(catalogue)
    )
    for state in (
        www / "publish-state.v1.json",
        www / "generations" / generation / "publish-state.v1.json",
    ):
        state.write_text('{"catalogue_sha256":"old"}', encoding="utf-8")

    pm.retarget_public_data_ref(www, old_ref, new_ref, generation)

    for base in (www, www / "generations" / generation):
        rewritten = (base / "tool" / "manifest.json").read_bytes()
        descriptor = json.loads((base / "manifest.json").read_text())["tools"][
            "tool"
        ]["descriptor"]
        assert f"/{new_ref}/" in rewritten.decode()
        assert descriptor["size_bytes"] == len(rewritten)
        assert descriptor["sha256"] == _sha(rewritten)


def test_wire_generation_change_is_not_a_source_identical_noop() -> None:
    binding = GenerationBinding(
        "source-abc-p1",
        "1" * 40,
        "2" * 40,
        "public-a",
        "3" * 40,
        "4" * 40,
        "public-b",
        "5" * 40,
        "6" * 40,
        "7" * 64,
    )
    ledger = VerifiedPublicLedger({}, binding, {})
    classifications = {"direct": "direct", "asset": "exact_hit"}

    assert pm.source_identical_noop(
        source_commit=binding.source_commit,
        generation=binding.generation,
        ledger=ledger,
        classifications=classifications,
    )
    assert not pm.source_identical_noop(
        source_commit=binding.source_commit,
        generation="source-abc-p2",
        ledger=ledger,
        classifications=classifications,
    )


def test_new_logical_aliases_share_one_materialized_oid_regardless_of_sort_order(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    _write_source_controls(assets)
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
    _write_source_controls(assets)
    (assets / "catalogue.v1.json").write_text('{"entries":[]}')
    (assets / "manifest.json").write_text('{"kind":"Index","schema_version":1,"tools":{}}')
    old_state = {"generation": "old", "sentinel": "retained"}
    retained_payload = b'{\n  "retained": true\n}\n'
    retained_path = "generations/old/direct.json"
    old_catalogue = {
        "schema_version": 2,
        "entries": [
            {
                "sha256": _sha(retained_payload),
                "size_bytes": len(retained_payload),
                "urls": ["https://zackees.github.io/soldr-toolchain/" + retained_path],
            }
        ],
    }
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
        retained_direct_payloads={retained_path: retained_payload},
    )
    assert json.loads((www / "generations" / "old" / "publish-state.v1.json").read_text()) == old_state
    assert json.loads((www / "generations" / "old" / "catalogue.v2.json").read_text()) == old_catalogue
    assert (www / retained_path).read_bytes() == retained_payload


def test_lfs_source_must_match_declared_oid_and_stay_inside_assets(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    _write_source_controls(assets)
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
    _write_source_controls(assets)
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
