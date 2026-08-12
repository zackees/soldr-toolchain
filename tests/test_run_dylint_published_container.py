from pathlib import Path

import pytest

from scripts.run_dylint_published_container import docker_command


@pytest.mark.parametrize(
    ("shape", "image_fragment"),
    [
        ("linux-x64-gnu", "manylinux2014_x86_64@sha256:"),
        ("linux-arm64-gnu", "manylinux2014_aarch64@sha256:"),
        ("linux-x64-musl", "alpine@sha256:"),
        ("linux-arm64-musl", "alpine@sha256:"),
    ],
)
def test_container_runs_checked_in_published_validator_natively(
    tmp_path: Path, shape: str, image_fragment: str
) -> None:
    repo = tmp_path / "repo"
    work = tmp_path / "work"
    repo.mkdir()
    work.mkdir()

    command = docker_command(repo, work, shape)
    rendered = " ".join(command)

    assert image_fragment in rendered
    assert f"{repo.resolve()}:/workspace:ro" in command
    assert f"{work.resolve()}:/work" in command
    assert "scripts.validate_dylint_published validate" in rendered
    assert f"--shape {shape}" in rendered
    assert "--work-dir /work" in rendered
