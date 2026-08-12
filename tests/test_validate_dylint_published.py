from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.forge_to_catalogue import FORGE_RUST_PLATFORM_BY_SHAPE
from scripts.produce_dylint_release import (
    DRIVER_ASSET_VERSION,
    DRIVER_IDENTITY,
    DYLINT_COMMIT,
    DYLINT_REPOSITORY,
    DYLINT_TAG,
    DYLINT_VERSION,
    lane_for_shape,
)
from scripts.validate_dylint_published import (
    canonical_asset_url,
    expected_assets,
    install_verified_asset,
    verify_archive,
)


def _archive(
    tool: str,
    shape: str,
    *,
    target: str | None = None,
    include_pair_identity: bool = True,
    pair_target: str | None = None,
) -> bytes:
    lane = lane_for_shape(shape)
    suffix = ".exe" if shape.startswith("windows-") else ""
    binary = f"{tool}{suffix}"
    payload = f"published {tool} for {lane.target}".encode()
    version = DRIVER_ASSET_VERSION if tool == "dylint-driver" else DYLINT_VERSION
    manifest: dict[str, object] = {
        "schema_version": 1,
        "tool": tool,
        "version": version,
        "binary": binary,
        "target": target or lane.target,
        "platform": FORGE_RUST_PLATFORM_BY_SHAPE[shape],
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "source_repo": DYLINT_REPOSITORY,
        "source_ref": DYLINT_COMMIT,
        "source_tag": DYLINT_TAG,
        "resolution_mode": (
            "native-exact-nightly"
            if tool == "dylint-driver"
            else "native-locked-pair"
        ),
        "smoke": {
            "result": "passed",
            "fixture": "fixtures/dylint-release",
            "known_violation": "release_fixture_forbidden_io",
            "binaries": ["cargo-dylint", "dylint-link", "dylint-driver"],
            "execution_mode": "native",
            "warm_driver_builds": 0,
            "warm_network": "offline",
            "target": lane.target,
        },
    }
    if tool == "dylint-driver":
        manifest["driver_identity"] = {**DRIVER_IDENTITY, "host": lane.target}
    elif include_pair_identity:
        manifest["pair_identity"] = {
            "dylint_version": DYLINT_VERSION,
            "source_ref": DYLINT_COMMIT,
            "target": pair_target or lane.target,
        }

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in (
            ("manifest.json", json.dumps(manifest).encode()),
            (f"package/{binary}", payload),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_expected_assets_cover_three_components_on_all_eight_hosts() -> None:
    assets = expected_assets()

    assert len(assets) == 24
    assert len({asset.filename for asset in assets}) == 24
    assert {asset.tool for asset in assets} == {
        "cargo-dylint",
        "dylint-link",
        "dylint-driver",
    }
    assert {
        asset.shape for asset in assets if asset.tool == "dylint-driver"
    } == {
        "windows-x64",
        "windows-arm64",
        "darwin-x64",
        "darwin-arm64",
        "linux-x64-gnu",
        "linux-arm64-gnu",
        "linux-x64-musl",
        "linux-arm64-musl",
    }


def test_windows_driver_archive_installs_to_extensionless_cache_entry(
    tmp_path: Path,
) -> None:
    lane = lane_for_shape("windows-x64")
    archive = _archive("dylint-driver", lane.shape)
    verified = verify_archive(
        archive,
        expected_sha256=hashlib.sha256(archive).hexdigest(),
        lane=lane,
        tool="dylint-driver",
    )

    installed = install_verified_asset(verified, tmp_path, lane)

    assert installed.name == "dylint-driver"
    assert installed.parent.name == f"{DRIVER_IDENTITY['toolchain']}-{lane.target}"
    assert installed.read_bytes().startswith(b"published dylint-driver")


def test_archive_verification_rejects_lfs_pointer_and_wrong_identity() -> None:
    lane = lane_for_shape("linux-arm64-musl")
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\n"
    with pytest.raises(RuntimeError, match="Git LFS pointer"):
        verify_archive(
            pointer,
            expected_sha256=hashlib.sha256(pointer).hexdigest(),
            lane=lane,
            tool="cargo-dylint",
        )


@pytest.mark.parametrize(
    ("tool", "archive"),
    [
        (
            "cargo-dylint",
            _archive(
                "cargo-dylint", "linux-x64-gnu", include_pair_identity=False
            ),
        ),
        (
            "dylint-link",
            _archive(
                "dylint-link", "linux-x64-gnu", pair_target="wrong-target"
            ),
        ),
    ],
    ids=["missing", "wrong-target"],
)
def test_pair_archive_requires_exact_nested_identity(tool: str, archive: bytes) -> None:
    lane = lane_for_shape("linux-x64-gnu")

    with pytest.raises(RuntimeError, match="pair_identity"):
        verify_archive(
            archive,
            expected_sha256=hashlib.sha256(archive).hexdigest(),
            lane=lane,
            tool=tool,
        )

    archive = _archive("dylint-driver", lane.shape, target="wrong-target")
    with pytest.raises(RuntimeError, match="manifest target"):
        verify_archive(
            archive,
            expected_sha256=hashlib.sha256(archive).hexdigest(),
            lane=lane,
            tool="dylint-driver",
        )


def test_canonical_urls_include_version_platform_and_filename() -> None:
    lane = lane_for_shape("darwin-arm64")
    assets = [asset for asset in expected_assets() if asset.shape == lane.shape]

    assert {
        canonical_asset_url(asset) for asset in assets
    } == {
        "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/"
        "cargo-dylint/v6.0.3/darwin-aarch64/"
        "cargo-dylint-6.0.3-aarch64-apple-darwin.tar.gz",
        "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/"
        "dylint-link/v6.0.3/darwin-aarch64/"
        "dylint-link-6.0.3-aarch64-apple-darwin.tar.gz",
        "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/"
        "dylint-driver/v6.0.3-nightly-2026-05-28/darwin-aarch64/"
        "dylint-driver-6.0.3-nightly-2026-05-28-aarch64-apple-darwin.tar.gz",
    }
