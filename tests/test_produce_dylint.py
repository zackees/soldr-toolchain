"""Tests for the one-host Dylint producer plan.

The producer must be testable without a Dylint checkout, Soldr installation, network,
or cross compiler.  These tests intentionally exercise only planning and input
validation; native build and runtime checks belong to the producer host.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import forge_to_catalogue
from scripts import produce_dylint as producer


EXPECTED_TARGETS = (
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
)


def test_dry_run_emits_complete_immutable_build_plan(capsys: pytest.CaptureFixture) -> None:
    assert producer.main(["--dry-run"]) == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["upstream"] == {
        "repository": "trailofbits/dylint",
        "tag": "v6.0.3",
        "commit": "9adfa398661273ca7dc99df9bf2c26ae6f61b1c5",
    }
    assert [entry["target"] for entry in plan["builds"]] == list(EXPECTED_TARGETS)
    assert len(plan["builds"]) == 8
    for entry in plan["builds"]:
        assert entry["binaries"] == ["cargo-dylint", "dylint-link"]
        assert entry["command"] == [
            "soldr",
            "build",
            "--locked",
            "--release",
            "--target",
            entry["target"],
            "-p",
            "cargo-dylint",
            "-p",
            "dylint-link",
            "--features=dylint/__driver_from_crates_io",
        ]


def test_write_artifact_fails_for_missing_binary(tmp_path: Path) -> None:
    target_dir = tmp_path / "target" / EXPECTED_TARGETS[0] / "release"
    target_dir.mkdir(parents=True)
    (target_dir / "cargo-dylint.exe").write_bytes(b"dylint")

    with pytest.raises(RuntimeError, match="dylint-link.exe"):
        producer.write_artifact(
            target_dir=target_dir,
            output_dir=tmp_path / "artifacts",
            shape="windows-x64",
            binary="dylint-link",
            command=producer.build_plan()[0].command,
        )


def test_staged_dylint_pair_is_accepted_by_catalogue_packager(tmp_path: Path) -> None:
    target_dir = tmp_path / "target" / EXPECTED_TARGETS[0] / "release"
    target_dir.mkdir(parents=True)
    for binary in producer.DYLINT_TOOLS:
        (target_dir / f"{binary}.exe").write_bytes(binary.encode())
        artifact = producer.write_artifact(
            target_dir=target_dir,
            output_dir=tmp_path / "artifacts",
            shape="windows-x64",
            binary=binary,
            command=producer.build_plan()[0].command,
            runtime_validation={
                "result": "passed",
                "fixture": "small-dylint-fixture",
                "binaries": list(producer.DYLINT_TOOLS),
            },
        )
        bundle = tmp_path / f"{binary}.tar.gz"
        provenance = forge_to_catalogue._package_forge_rust_artifact(
            artifact,
            bundle,
            tool=binary,
            version=producer.DYLINT_VERSION,
            shape="windows-x64",
        )
        assert bundle.is_file()
        assert provenance["source_repo"] == producer.DYLINT_REPOSITORY
        assert provenance["source_ref"] == producer.DYLINT_COMMIT


def test_verify_checkout_fails_closed_for_dirty_or_wrong_commit(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def run(command: list[str], **_: object) -> str:
        calls.append(tuple(command))
        if command[-2:] == ["status", "--porcelain"]:
            return " M Cargo.lock\n"
        return producer.DYLINT_COMMIT + "\n"

    with pytest.raises(RuntimeError, match="dirty"):
        producer.verify_checkout(tmp_path, run=run)
    assert calls == [("git", "-C", str(tmp_path), "status", "--porcelain")]

    def wrong_commit(command: list[str], **_: object) -> str:
        if command[-2:] == ["status", "--porcelain"]:
            return ""
        return "0" * 40 + "\n"

    with pytest.raises(RuntimeError, match="expected immutable commit"):
        producer.verify_checkout(tmp_path, run=wrong_commit)


def test_publish_requires_clean_assets_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter(["assets\n", " M manifest.json\n"])
    monkeypatch.setattr(producer, "_command_output", lambda _: next(outputs))

    with pytest.raises(RuntimeError, match="dirty"):
        producer._require_clean_assets_checkout(tmp_path)
