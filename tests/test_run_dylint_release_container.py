"""Tests for the Linux Dylint release container boundary."""

from pathlib import Path

import pytest

from scripts import run_dylint_release_container as container


@pytest.mark.parametrize(
    ("shape", "image_fragment", "python_fragment"),
    [
        ("linux-x64-gnu", "manylinux2014_x86_64", "/opt/python/cp311-cp311/bin/python"),
        ("linux-arm64-gnu", "manylinux2014_aarch64", "/opt/python/cp311-cp311/bin/python"),
        ("linux-x64-musl", "alpine", "python3"),
        ("linux-arm64-musl", "alpine", "python3"),
    ],
)
def test_container_command_is_native_and_runs_checked_in_producer(
    tmp_path: Path, shape: str, image_fragment: str, python_fragment: str
) -> None:
    command = container.docker_command(tmp_path, shape)

    assert command[:3] == ["docker", "run", "--rm"]
    assert any(image_fragment in part for part in command)
    assert python_fragment in command[-1]
    assert "-m scripts.produce_dylint_release build" in command[-1]
    assert f"--shape {shape}" in command[-1]
    assert "--dylint-checkout /workspace/dylint" in command[-1]


def test_container_command_rejects_non_linux_shape(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="containerized Linux"):
        container.docker_command(tmp_path, "windows-x64")


def test_external_dylint_checkout_is_mounted_read_only(tmp_path: Path) -> None:
    checkout = tmp_path / "upstream-dylint"
    command = container.docker_command(
        tmp_path / "producer", "linux-x64-gnu", dylint_checkout=checkout
    )

    assert f"{checkout.resolve()}:/dylint:ro" in command
    assert "--dylint-checkout /dylint" in command[-1]
    assert "git config --global --add safe.directory /dylint" in command[-1]


@pytest.mark.parametrize(
    ("shape", "checkout"),
    [
        ("linux-x64-gnu", "/workspace/dylint"),
        ("linux-arm64-gnu", "/workspace/dylint"),
        ("linux-x64-musl", "/workspace/dylint"),
        ("linux-arm64-musl", "/workspace/dylint"),
    ],
)
def test_container_marks_actions_checkout_safe_for_root(
    tmp_path: Path, shape: str, checkout: str
) -> None:
    command = container.docker_command(tmp_path, shape)

    assert f"git config --global --add safe.directory {checkout}" in command[-1]


@pytest.mark.parametrize("shape", ["linux-x64-musl", "linux-arm64-musl"])
def test_alpine_bootstraps_with_builtin_shell_before_bash(
    tmp_path: Path, shape: str
) -> None:
    command = container.docker_command(tmp_path, shape)
    image_index = next(index for index, part in enumerate(command) if "alpine@" in part)

    assert command[image_index + 1] == "/bin/sh"
    assert command[image_index + 2] == "-lc"
    assert command[-1].startswith("set -eu; apk add --no-cache bash ")
    assert "bash -lc" in command[-1]
