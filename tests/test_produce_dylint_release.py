"""Contract tests for the native Dylint pair + driver release matrix."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import produce_dylint_release as release
from scripts import forge_to_catalogue


EXPECTED = {
    "windows-x64": ("windows-2022", "x86_64-pc-windows-msvc"),
    "windows-arm64": ("windows-11-arm", "aarch64-pc-windows-msvc"),
    "darwin-x64": ("macos-15-intel", "x86_64-apple-darwin"),
    "darwin-arm64": ("macos-15", "aarch64-apple-darwin"),
    "linux-x64-gnu": ("ubuntu-22.04", "x86_64-unknown-linux-gnu"),
    "linux-arm64-gnu": ("ubuntu-24.04-arm", "aarch64-unknown-linux-gnu"),
    "linux-x64-musl": ("ubuntu-22.04", "x86_64-unknown-linux-musl"),
    "linux-arm64-musl": ("ubuntu-24.04-arm", "aarch64-unknown-linux-musl"),
}


def test_plan_is_complete_native_and_exactly_identified() -> None:
    plan = release.release_plan()

    assert {lane.shape: (lane.runner, lane.target) for lane in plan} == EXPECTED
    assert all(lane.evidence_level == "fixture-validated-native" for lane in plan)
    assert release.DRIVER_IDENTITY == {
        "dylint_version": "6.0.3",
        "toolchain": "nightly-2026-05-28",
        "rustc_release": "1.98.0-nightly",
        "rustc_commit": "57d06900fd7d9ee06d3a7f323bb77f17ab3cfaf8",
    }
    assert release.DRIVER_ASSET_VERSION == "6.0.3-nightly-2026-05-28"
    matrix = release.catalogue_artifact_matrix()
    assert len(matrix) == 24
    assert {(tool, version) for tool, version, _ in matrix} == {
        ("cargo-dylint", "6.0.3"),
        ("dylint-link", "6.0.3"),
        ("dylint-driver", "6.0.3-nightly-2026-05-28"),
    }
    assert {shape for _, _, shape in matrix} == set(EXPECTED)


def test_driver_artifact_binds_payload_to_full_identity(tmp_path: Path) -> None:
    lane = release.release_plan()[0]
    binary = tmp_path / "target" / "release" / "soldr-dylint-driver.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"prebuilt-driver")

    artifact = release.stage_driver_artifact(
        built_binary=binary,
        output_dir=tmp_path / "out",
        lane=lane,
        rustc_verbose=(
            "rustc 1.98.0-nightly (57d06900f 2026-05-27)\n"
            "commit-hash: 57d06900fd7d9ee06d3a7f323bb77f17ab3cfaf8\n"
            "host: x86_64-pc-windows-msvc\n"
            "release: 1.98.0-nightly\n"
        ),
        smoke={
            "result": "passed",
            "known_violation": "try_io_result",
            "warm_driver_builds": 0,
        },
        binary_evidence={
            "format": "PE",
            "machine": "AMD64",
        },
    )

    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tool"] == "dylint-driver"
    assert manifest["version"] == release.DRIVER_ASSET_VERSION
    assert manifest["driver_identity"] == {
        **release.DRIVER_IDENTITY,
        "host": lane.target,
    }
    assert manifest["binary"] == "dylint-driver.exe"
    assert manifest["binary_evidence"] == {
        "format": "PE",
        "machine": "AMD64",
    }
    assert manifest["smoke"]["warm_driver_builds"] == 0


@pytest.mark.parametrize(
    ("rustc_verbose", "message"),
    [
        (
            "commit-hash: deadbeef\nhost: x86_64-pc-windows-msvc\n"
            "release: 1.98.0-nightly\n",
            "rustc commit",
        ),
        (
            "commit-hash: 57d06900fd7d9ee06d3a7f323bb77f17ab3cfaf8\n"
            "host: aarch64-pc-windows-msvc\nrelease: 1.98.0-nightly\n",
            "rustc host",
        ),
    ],
)
def test_driver_staging_rejects_wrong_rustc_identity(
    tmp_path: Path, rustc_verbose: str, message: str
) -> None:
    lane = release.release_plan()[0]
    binary = tmp_path / "soldr-dylint-driver.exe"
    binary.write_bytes(b"wrong-driver")

    with pytest.raises(RuntimeError, match=message):
        release.stage_driver_artifact(
            built_binary=binary,
            output_dir=tmp_path / "out",
            lane=lane,
            rustc_verbose=rustc_verbose,
            smoke={"result": "passed", "warm_driver_builds": 0},
        )


def test_smoke_evidence_must_prove_real_lint_and_warm_no_build() -> None:
    lane = release.release_plan()[0]

    with pytest.raises(RuntimeError, match="known violation"):
        release.validate_smoke(
            lane,
            {"result": "passed", "warm_driver_builds": 0},
        )
    with pytest.raises(RuntimeError, match="warm rerun"):
        release.validate_smoke(
            lane,
            {
                "result": "passed",
                "known_violation": "try_io_result",
                "warm_driver_builds": 1,
            },
        )


def test_fixture_path_preserves_rustup_proxy_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toolchain_root = tmp_path / "toolchain"
    monkeypatch.setattr(release, "_toolchain_root", lambda _repo_root: toolchain_root)
    monkeypatch.setenv("PATH", os.pathsep.join(["rustup-proxies", "system-bin"]))

    environment = release._fixture_environment(tmp_path, tmp_path / "relocated")

    assert environment["PATH"].split(os.pathsep) == [
        str(tmp_path / "relocated" / "pair"),
        "rustup-proxies",
        "system-bin",
    ]
    assert str(toolchain_root / "bin") not in environment["PATH"].split(os.pathsep)
    if os.name != "nt":
        assert str(toolchain_root / "lib") in environment["LD_LIBRARY_PATH"]


def test_fixture_environment_exports_rustup_home_for_prebuilt_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rustup_home = tmp_path / "rustup"
    toolchain_root = rustup_home / "toolchains" / "nightly-qualified-host"
    monkeypatch.setattr(release, "_toolchain_root", lambda _repo_root: toolchain_root)
    monkeypatch.delenv("RUSTUP_HOME", raising=False)

    environment = release._fixture_environment(tmp_path, tmp_path / "relocated")

    assert environment["RUSTUP_HOME"] == str(rustup_home)


def test_gnu_elf_evidence_enforces_architecture_and_glibc_ceiling() -> None:
    lane = release.lane_for_shape("linux-x64-gnu")
    evidence = release.validate_linux_elf_evidence(
        lane,
        "cargo-dylint",
        header="Machine: Advanced Micro Devices X86-64",
        program_headers="[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]",
        version_info="Name: GLIBC_2.2.5\nName: GLIBC_2.16",
    )

    assert evidence == {
        "format": "ELF",
        "machine": "Advanced Micro Devices X86-64",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
        "glibc_max": "2.16",
        "glibc_ceiling": "2.17",
    }

    with pytest.raises(RuntimeError, match="GLIBC 2.34"):
        release.validate_linux_elf_evidence(
            lane,
            "cargo-dylint",
            header="Machine: Advanced Micro Devices X86-64",
            program_headers="[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]",
            version_info="Name: GLIBC_2.34",
        )
    with pytest.raises(RuntimeError, match="ELF machine"):
        release.validate_linux_elf_evidence(
            lane,
            "cargo-dylint",
            header="Machine: AArch64",
            program_headers="",
            version_info="Name: GLIBC_2.16",
        )


def test_musl_elf_evidence_rejects_glibc_and_dynamic_pair() -> None:
    lane = release.lane_for_shape("linux-arm64-musl")

    with pytest.raises(RuntimeError, match="must be static"):
        release.validate_linux_elf_evidence(
            lane,
            "dylint-link",
            header="Machine: AArch64",
            program_headers="[Requesting program interpreter: /lib/ld-musl-aarch64.so.1]",
            version_info="No version information found",
        )
    with pytest.raises(RuntimeError, match="GLIBC"):
        release.validate_linux_elf_evidence(
            lane,
            "cargo-dylint",
            header="Machine: AArch64",
            program_headers="",
            version_info="Name: GLIBC_2.17",
        )

    driver = release.validate_linux_elf_evidence(
        lane,
        "dylint-driver",
        header="Machine: AArch64",
        program_headers="[Requesting program interpreter: /lib/ld-musl-aarch64.so.1]",
        version_info="No version information found",
    )
    assert driver["interpreter"] == "/lib/ld-musl-aarch64.so.1"
    assert driver["glibc_max"] is None


def test_packager_rejects_driver_with_wrong_nightly_identity(tmp_path: Path) -> None:
    lane = release.release_plan()[0]
    binary = tmp_path / "soldr-dylint-driver.exe"
    binary.write_bytes(b"driver")
    artifact = release.stage_driver_artifact(
        built_binary=binary,
        output_dir=tmp_path / "out",
        lane=lane,
        rustc_verbose=(
            "commit-hash: 57d06900fd7d9ee06d3a7f323bb77f17ab3cfaf8\n"
            "host: x86_64-pc-windows-msvc\nrelease: 1.98.0-nightly\n"
        ),
        smoke={
            "result": "passed",
            "known_violation": "release_fixture_forbidden_io",
            "warm_driver_builds": 0,
        },
    )
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["driver_identity"]["toolchain"] = "nightly"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SystemExit, match="driver_identity"):
        forge_to_catalogue._package_forge_rust_artifact(
            artifact,
            tmp_path / "driver.tar.gz",
            tool="dylint-driver",
            version=release.DRIVER_ASSET_VERSION,
            shape=lane.shape,
        )


def test_ingest_failure_leaves_real_assets_checkout_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = tmp_path / "artifacts"
    assets = tmp_path / "assets"
    artifacts.mkdir()
    assets.mkdir()
    original = assets / "manifest.json"
    original.write_text("original\n", encoding="utf-8")

    def fake_run(command, *, cwd, env=None, check=True):
        stdout = "assets\n" if command[1:3] == ["branch", "--show-current"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    calls = 0

    def failing_ingest(argv):
        nonlocal calls
        calls += 1
        staged = Path(argv[argv.index("--assets-root") + 1])
        (staged / "partial-output").write_text(str(calls), encoding="utf-8")
        return 1 if calls == 2 else 0

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release.forge_to_catalogue, "main", failing_ingest)

    with pytest.raises(RuntimeError, match="failed to ingest"):
        release.ingest_release(
            artifacts_dir=artifacts,
            assets_root=assets,
            schema=tmp_path / "schema.json",
            forge_run_id="123",
        )

    assert original.read_text(encoding="utf-8") == "original\n"
    assert not (assets / "partial-output").exists()


def test_index_failure_leaves_real_assets_checkout_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = tmp_path / "artifacts"
    assets = tmp_path / "assets"
    artifacts.mkdir()
    assets.mkdir()

    def fake_run(command, *, cwd, env=None, check=True):
        stdout = "assets\n" if command[1:3] == ["branch", "--show-current"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def successful_ingest(argv):
        staged = Path(argv[argv.index("--assets-root") + 1])
        (staged / "complete-matrix").write_text("ready", encoding="utf-8")
        return 0

    def failing_index(*_args, **_kwargs):
        raise RuntimeError("index failed")

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release.forge_to_catalogue, "main", successful_ingest)
    monkeypatch.setattr(release, "build_asset_index", failing_index)

    with pytest.raises(RuntimeError, match="index failed"):
        release.ingest_release(
            artifacts_dir=artifacts,
            assets_root=assets,
            schema=tmp_path / "schema.json",
            forge_run_id="123",
        )

    assert not (assets / "complete-matrix").exists()
