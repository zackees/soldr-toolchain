"""Tests for `scripts/forge_to_catalogue.py`.

The script's two non-trivial operations are (1) finding the right
forge artifact in a `gh run download` output, and (2) mutating the
catalogue idempotently. Both are exercised here against synthetic
forge artifacts on a tmp dir — no live forge dispatch needed.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import forge_to_catalogue as fc


def _make_fake_forge_artifact(
    out_dir: Path,
    recipe_name: str,
    version: str,
    platform: str,
    payload_files: dict[str, bytes] | None = None,
    recipe_ref: str = "soldr-toolchain@abc1234:recipes/x",
    recipe_meta: dict | None = None,
) -> Path:
    """Build a single forge artifact tarball matching the shape
    `.github/workflows/forge-conan.yml` produces.

    Layout inside the .tar.gz:
        ./manifest.json
        ./package/<payload>
        ./package/meta.json (recipe-written)
    """
    artifact_name = f"forge-{recipe_name}-{version}-{platform}"
    art_subdir = out_dir / artifact_name
    art_subdir.mkdir(parents=True, exist_ok=True)
    artifact_path = art_subdir / f"{artifact_name}.tar.gz"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # manifest.json at root
        manifest = {
            "recipe_ref": recipe_ref,
            "package_ref": f"{recipe_ref}#deadbeef:0123:cafefeed",
            "package_path": "/conan-cache/fake/path",
        }
        manifest_bytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo("./manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))

        # Recipe meta inside package/
        meta = recipe_meta or {
            "shape": "universal2",
            "captured_sdk_version": version,
            "xcode_version": "16.1",
        }
        meta_bytes = json.dumps(meta).encode()
        meta_info = tarfile.TarInfo("./package/meta.json")
        meta_info.size = len(meta_bytes)
        tf.addfile(meta_info, io.BytesIO(meta_bytes))

        # Payload files
        for rel, content in (payload_files or {"package/sdk/usr/lib/libobjc.tbd": b"--- !tapi-tbd\n"}).items():
            info = tarfile.TarInfo(f"./{rel}")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))

    artifact_path.write_bytes(buf.getvalue())
    return artifact_path


def test_find_forge_artifact_matches_name_and_version(tmp_path: Path) -> None:
    _make_fake_forge_artifact(tmp_path, "apple-sdk-universal2", "14.5", "macos-arm64")
    _make_fake_forge_artifact(tmp_path, "apple-sdk-universal2", "15.2", "macos-arm64")

    hit = fc._find_forge_artifact(tmp_path, "apple-sdk-universal2", "14.5")
    assert hit is not None
    assert "14.5" in hit.name
    assert "universal2" in hit.name

    miss = fc._find_forge_artifact(tmp_path, "apple-sdk-thin-x86_64", "14.5")
    assert miss is None  # no matching recipe name


def test_extract_forge_payload_returns_package_and_provenance(tmp_path: Path) -> None:
    artifact = _make_fake_forge_artifact(
        tmp_path, "apple-sdk-universal2", "14.5", "macos-arm64",
        recipe_meta={"shape": "universal2", "captured_sdk_version": "14.5.0.21F77"},
    )
    package_root, provenance = fc._extract_forge_payload(artifact)
    assert package_root.is_dir()
    assert provenance["recipe_ref"].startswith("soldr-toolchain@abc1234")
    assert provenance["recipe_meta"]["captured_sdk_version"] == "14.5.0.21F77"


def test_repack_to_zstd_is_zstd_magic(tmp_path: Path) -> None:
    # Make a tiny payload + repack, then confirm zstd magic bytes lead the file.
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "hello.txt").write_text("hi from the recipe")
    out = tmp_path / "asset.tar.zst"
    fc._repack_to_zstd(payload, out)
    assert out.is_file()
    head = out.read_bytes()[:4]
    # Zstandard frame magic number: 0xFD2FB528 little-endian
    assert head == b"\x28\xb5\x2f\xfd", f"expected zstd magic, got {head.hex()}"


def test_update_catalogue_idempotent_and_provenance_logged(tmp_path: Path) -> None:
    # Stand up a minimal valid catalogue + the real schema.
    schema_src = Path(__file__).resolve().parent.parent / "schemas" / "catalogue.v1.schema.json"
    schema_dst = tmp_path / "catalogue.v1.schema.json"
    schema_dst.write_text(schema_src.read_text(encoding="utf-8"))

    catalogue_path = tmp_path / "catalogue.v1.json"
    catalogue_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-06-27T00:00:00Z",
                "origin": "https://zackees.github.io/soldr-toolchain/catalogue.v1.json",
                "entries": [],
            },
            indent=2,
        )
        + "\n"
    )

    asset_rel = Path("apple-sdk/14.5/darwin-universal2/sdk.tar.zst")
    fc._update_catalogue(
        catalogue_path,
        schema_dst,
        asset_rel=asset_rel,
        asset_name="sdk.tar.zst",
        sha256="0" * 64,
        forge_run_id="28299235391",
        provenance={"recipe_ref": "foo", "package_ref": "bar", "recipe_meta": {}},
    )
    cat1 = json.loads(catalogue_path.read_text())
    assert len(cat1["entries"]) == 1
    assert cat1["entries"][0]["sha256"] == "0" * 64

    # Re-run with a different sha256; should REPLACE not duplicate.
    fc._update_catalogue(
        catalogue_path,
        schema_dst,
        asset_rel=asset_rel,
        asset_name="sdk.tar.zst",
        sha256="1" * 64,
        forge_run_id="28299235392",
        provenance={"recipe_ref": "foo", "package_ref": "bar2", "recipe_meta": {}},
    )
    cat2 = json.loads(catalogue_path.read_text())
    assert len(cat2["entries"]) == 1, "rerun should replace, not append"
    assert cat2["entries"][0]["sha256"] == "1" * 64

    # Provenance log captured both runs.
    log = (tmp_path / ".forge-ingest.log.jsonl").read_text().strip().split("\n")
    assert len(log) == 2
    assert json.loads(log[0])["forge_run_id"] == "28299235391"
    assert json.loads(log[1])["forge_run_id"] == "28299235392"
