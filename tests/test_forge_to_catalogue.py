"""Tests for `scripts/forge_to_catalogue.py`.

The script's two non-trivial operations are (1) finding the right
forge artifact in a `gh run download` output, and (2) mutating the
catalogue idempotently. Both are exercised here against synthetic
forge artifacts on a tmp dir — no live forge dispatch needed.
"""

from __future__ import annotations

import io
import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from scripts import forge_to_catalogue as fc


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
        for rel, content in (
            payload_files or {"package/sdk/usr/lib/libobjc.tbd": b"--- !tapi-tbd\n"}
        ).items():
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


def test_packages_forge_rust_artifact_for_catalogue(tmp_path: Path) -> None:
    artifact = tmp_path / "forge-rust-cargo-nextest-0.9.140-linux-x64-musl"
    artifact.mkdir()
    binary = b"native-nextest"
    (artifact / "cargo-nextest").write_bytes(binary)
    manifest = {
        "schema_version": 1,
        "tool": "cargo-nextest",
        "version": "0.9.140",
        "binary": "cargo-nextest",
        "target": "x86_64-unknown-linux-musl",
        "platform": "linux-x64-musl",
        "payload_sha256": hashlib.sha256(binary).hexdigest(),
        "source_repo": "nextest-rs/nextest",
        "source_ref": "a9fef2964e34f64ed4fceeee7c0c3559ce560920",
        "resolution_mode": "source-build",
        "smoke": {"command": "cargo-nextest --version", "result": "passed"},
    }
    (artifact / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert (
        fc._find_forge_rust_artifact(
            tmp_path, "cargo-nextest", "0.9.140", "linux-x64-musl"
        )
        == artifact
    )
    output = tmp_path / "bundle.tar.gz"
    provenance = fc._package_forge_rust_artifact(
        artifact,
        output,
        tool="cargo-nextest",
        version="0.9.140",
        shape="linux-x64-musl",
    )

    with tarfile.open(output, "r:gz") as archive:
        assert archive.getnames() == ["manifest.json", "package/cargo-nextest"]
        assert archive.extractfile("package/cargo-nextest").read() == binary
    assert provenance["producer"] == "forge-rust"
    assert provenance["smoke"]["result"] == "passed"


def test_forge_rust_platform_names_cover_every_catalog_shape(tmp_path: Path) -> None:
    expected = {
        "windows-x64": "windows-x64-msvc",
        "windows-arm64": "windows-arm64-msvc",
        "darwin-x64": "macos-x64",
        "darwin-arm64": "macos-arm64",
        "linux-x64-gnu": "linux-x64-gnu",
        "linux-arm64-gnu": "linux-arm64-gnu",
        "linux-x64-musl": "linux-x64-musl",
        "linux-arm64-musl": "linux-arm64-musl",
    }
    for shape, platform in expected.items():
        artifact = tmp_path / f"forge-rust-cargo-nextest-0.9.140-{platform}"
        artifact.mkdir()
        (artifact / "manifest.json").write_text("{}", encoding="utf-8")
        assert (
            fc._find_forge_rust_artifact(tmp_path, "cargo-nextest", "0.9.140", shape)
            == artifact
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("binary", "different-tool", "Rust binary"),
        ("target", "aarch64-unknown-linux-musl", "manifest target"),
    ],
)
def test_forge_rust_validation_binds_binary_and_target(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    binary = b"native-nextest"
    (artifact / "cargo-nextest").write_bytes(binary)
    manifest = {
        "tool": "cargo-nextest",
        "version": "0.9.140",
        "binary": "cargo-nextest",
        "target": "x86_64-unknown-linux-musl",
        "platform": "linux-x64-musl",
        "payload_sha256": hashlib.sha256(binary).hexdigest(),
        "source_repo": "nextest-rs/nextest",
        "source_ref": "a9fef2964e34f64ed4fceeee7c0c3559ce560920",
        "resolution_mode": "source-build",
        "smoke": {"result": "passed"},
    }
    manifest[field] = value
    (artifact / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SystemExit, match=message):
        fc._package_forge_rust_artifact(
            artifact,
            tmp_path / "out.tar.gz",
            tool="cargo-nextest",
            version="0.9.140",
            shape="linux-x64-musl",
        )


def test_rust_asset_names_are_target_qualified() -> None:
    for shape, target in fc.RUST_TARGET_BY_SHAPE.items():
        name = fc._forge_rust_asset_name("cargo-nextest", "0.9.140", shape)
        assert target in name
        assert name.endswith(".tar.gz")


def test_syslib_recipe_mapping_and_default_asset_names() -> None:
    assert fc._resolve_recipe_name("zstd", "linux-x64-musl") == "zstd-linux-x64-musl"
    assert fc._resolve_recipe_name("sqlite", "windows-arm64") == "sqlite-windows-arm64"
    assert fc._resolve_recipe_name("zstd", "windows-x64-gnu") == "zstd-windows-x64-gnu"
    assert (
        fc._resolve_recipe_name("bzip2", "windows-x64-gnu") == "bzip2-windows-x64-gnu"
    )
    assert fc.SHAPE_TO_PLATFORM["linux-arm64-gnu"] == "linux-arm64-gnu"
    assert fc.SHAPE_TO_PLATFORM["windows-x64-gnu"] == "windows-x64-gnu"
    assert fc.DEFAULT_ASSET_NAME["zstd"] == "bundle.tar.zst"
    assert fc.DEFAULT_ASSET_NAME["apple-sdk"] == "sdk.tar.zst"
    assert (
        fc._resolve_recipe_name("cargo-chef", "windows-arm64")
        == "cargo-chef-windows-arm64"
    )
    assert (
        fc._resolve_recipe_name("crgx", "linux-arm64-musl") == "crgx-linux-arm64-musl"
    )
    assert fc.DEFAULT_ASSET_NAME["cargo-chef"] == "bundle.tar.zst"
    assert fc.DEFAULT_ASSET_NAME["crgx"] == "bundle.tar.zst"
    assert (
        fc._resolve_recipe_name("mingw-w64-gcc", "windows-x64-gnu")
        == "mingw-w64-gcc-windows-x64-gnu"
    )
    assert fc.SHAPE_TO_PLATFORM["windows-x64-gnu"] == "windows-x64-gnu"
    assert fc.DEFAULT_ASSET_NAME["mingw-w64-gcc"] == "bundle.tar.zst"


def test_jemalloc_windows_shapes_are_not_mapped() -> None:
    try:
        fc._resolve_recipe_name("jemalloc", "windows-x64")
    except SystemExit as exc:
        assert "no recipe mapping" in str(exc)
    else:
        raise AssertionError("jemalloc Windows shapes should stay unsupported")


def test_extract_forge_payload_returns_package_and_provenance(tmp_path: Path) -> None:
    artifact = _make_fake_forge_artifact(
        tmp_path,
        "apple-sdk-universal2",
        "14.5",
        "macos-arm64",
        recipe_meta={"shape": "universal2", "captured_sdk_version": "14.5.0.21F77"},
    )
    package_root, provenance = fc._extract_forge_payload(artifact)
    assert package_root.is_dir()
    assert provenance["recipe_ref"].startswith("soldr-toolchain@abc1234")
    assert provenance["recipe_meta"]["captured_sdk_version"] == "14.5.0.21F77"


def test_stream_repack_to_zstd_round_trip(tmp_path: Path) -> None:
    # Build a fake forge artifact carrying a colon-bearing member, then
    # stream-repack and confirm:
    #   * zstd magic bytes lead the file
    #   * provenance dict harvested from the manifest + meta
    #   * the colon-bearing member rides end-to-end (would crash
    #     tarfile.extractall on Windows but the streaming form never
    #     touches the FS namespace)
    artifact = _make_fake_forge_artifact(
        tmp_path,
        "apple-sdk-universal2",
        "14.5",
        "macos-arm64",
        payload_files={
            "package/sdk/usr/lib/libobjc.tbd": b"--- !tapi-tbd\narchs: [ x86_64 ]\n",
            "package/sdk/usr/share/man/mann/ttk::progressbar.ntcl": b".manpage\n",
        },
    )
    out = tmp_path / "asset.tar.zst"
    provenance = fc._stream_repack_to_zstd(artifact, out)
    assert out.is_file()
    head = out.read_bytes()[:4]
    assert head == b"\x28\xb5\x2f\xfd", f"expected zstd magic, got {head.hex()}"
    assert provenance["recipe_ref"].startswith("soldr-toolchain@abc1234")
    assert "captured_sdk_version" in provenance["recipe_meta"]

    # Round-trip: re-open the tar.zst, confirm the colon-bearing member
    # is still there.
    import io as _io
    import tarfile as _tarfile
    import zstandard as _zstd

    raw = out.read_bytes()
    dctx = _zstd.ZstdDecompressor()
    plain = dctx.decompress(raw, max_output_size=10 * 1024 * 1024)
    names = []
    with _tarfile.open(fileobj=_io.BytesIO(plain), mode="r") as tf:
        for m in tf:
            names.append(m.name)
    assert any(
        "ttk::progressbar" in n for n in names
    ), f"colon-bearing member missing from output: {names}"


def test_update_catalogue_idempotent_and_provenance_logged(tmp_path: Path) -> None:
    # Stand up a minimal valid catalogue + the real schema.
    schema_src = (
        Path(__file__).resolve().parent.parent / "schemas" / "catalogue.v1.schema.json"
    )
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


def test_update_manifest_catalog_merges_rust_cli_platform(tmp_path: Path) -> None:
    assets_root = tmp_path
    (assets_root / "manifest.json").write_text(
        json.dumps(
            {
                "$schema": fc.V1_SCHEMA_URL,
                "kind": "Index",
                "schema_version": 1,
                "tools": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    fc._update_manifest_catalog(
        assets_root,
        tool="cargo-chef",
        package_version="0.1.73",
        shape="windows-arm64",
        asset_rel=Path("cargo-chef/v0.1.73/windows-aarch64-msvc/bundle.tar.zst"),
        asset_name="bundle.tar.zst",
        asset_size=12345,
        sha256="a" * 64,
    )

    catalog = json.loads((assets_root / "cargo-chef" / "manifest.json").read_text())
    assert catalog["tool"] == "cargo-chef"
    assert catalog["channels"]["pinned"] == "v0.1.73"
    release = next(r for r in catalog["releases"] if r["version"] == "v0.1.73")
    assert release["platforms"] == [
        {
            "platform": {"os": "windows", "arch": "aarch64", "abi": "msvc"},
            "asset": {
                "filename": "bundle.tar.zst",
                "size_bytes": 12345,
                "sha256": "a" * 64,
                "urls": [
                    "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/cargo-chef/v0.1.73/windows-aarch64-msvc/bundle.tar.zst",
                    "https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/cargo-chef/v0.1.73/windows-aarch64-msvc/bundle.tar.zst",
                ],
            },
        }
    ]
    index = json.loads((assets_root / "manifest.json").read_text())
    assert (
        index["tools"]["cargo-chef"]["descriptor"]["url"] == "cargo-chef/manifest.json"
    )


def test_nextest_ingest_repoints_channels_to_logical_version(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"kind": "Index", "schema_version": 1, "tools": {}}),
        encoding="utf-8",
    )
    catalog_dir = tmp_path / "cargo-nextest"
    catalog_dir.mkdir()
    (catalog_dir / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "Catalog",
                "schema_version": 1,
                "tool": "cargo-nextest",
                "channels": {"pinned": "cargo-nextest-0.9.140"},
                "releases": [],
            }
        ),
        encoding="utf-8",
    )

    asset_name = "cargo-nextest-0.9.140-aarch64-pc-windows-msvc.tar.gz"
    fc._update_manifest_catalog(
        tmp_path,
        tool="cargo-nextest",
        package_version="0.9.140",
        shape="windows-arm64",
        asset_rel=Path(f"cargo-nextest/0.9.140/windows-aarch64-msvc/{asset_name}"),
        asset_name=asset_name,
        asset_size=123,
        sha256="a" * 64,
    )

    catalog = json.loads((catalog_dir / "manifest.json").read_text())
    assert catalog["channels"] == {
        "pinned": "0.9.140",
        "latest-stable": "0.9.140",
        "stable": "0.9.140",
    }


def test_nextest_ingest_does_not_roll_back_newer_stable_channel(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"kind": "Index", "schema_version": 1, "tools": {}}),
        encoding="utf-8",
    )
    catalog_dir = tmp_path / "cargo-nextest"
    catalog_dir.mkdir()
    (catalog_dir / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "Catalog",
                "schema_version": 1,
                "tool": "cargo-nextest",
                "channels": {
                    "pinned": "cargo-nextest-0.9.140",
                    "latest-stable": "0.9.141",
                    "stable": "0.9.141",
                },
                "releases": [],
            }
        ),
        encoding="utf-8",
    )

    fc._update_manifest_catalog(
        tmp_path,
        tool="cargo-nextest",
        package_version="0.9.140",
        shape="linux-x64-musl",
        asset_rel=Path("cargo-nextest/0.9.140/linux-x86_64-musl/nextest.tar.gz"),
        asset_name="nextest.tar.gz",
        asset_size=123,
        sha256="b" * 64,
    )

    channels = json.loads((catalog_dir / "manifest.json").read_text())["channels"]
    assert channels == {
        "pinned": "0.9.140",
        "latest-stable": "0.9.141",
        "stable": "0.9.141",
    }
