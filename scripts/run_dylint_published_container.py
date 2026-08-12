#!/usr/bin/env python3
"""Run one published Dylint Linux validation lane in its native ABI image."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path
from typing import Sequence

from scripts.produce_dylint_release import lane_for_shape
from scripts.run_dylint_release_container import MANYLINUX_IMAGES, MUSL_IMAGE


def docker_command(repo_root: Path, work_dir: Path, shape: str) -> list[str]:
    lane = lane_for_shape(shape)
    if not shape.startswith("linux-"):
        raise RuntimeError(f"{shape!r} is not a containerized Linux validation lane")

    if lane.environment == "manylinux2014":
        image = MANYLINUX_IMAGES[shape]
        python = "/opt/python/cp311-cp311/bin/python"
        shell = "bash"
        prepare = (
            "yum install -y openssl-devel perl-IPC-Cmd perl-Time-Piece"
            " && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs "
            "| sh -s -- -y --profile minimal"
        )
        inner = (
            f"set -euo pipefail; {prepare}; . /root/.cargo/env; "
            f"{python} -m scripts.validate_dylint_published validate "
            f"--repo-root /workspace --work-dir /work --shape {shape}"
        )
    elif lane.environment == "alpine":
        image = MUSL_IMAGE
        shell = "/bin/sh"
        prepare = (
            "apk add --no-cache bash binutils build-base curl git openssl-dev perl "
            "pkgconf python3"
        )
        bash_inner = (
            "set -euo pipefail; "
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs "
            "| sh -s -- -y --profile minimal; "
            ". /root/.cargo/env; "
            f"python3 -m scripts.validate_dylint_published validate "
            f"--repo-root /workspace --work-dir /work --shape {shape}"
        )
        inner = f"set -eu; {prepare}; bash -lc {shlex.quote(bash_inner)}"
    else:
        raise RuntimeError(f"unsupported Linux validation environment {lane.environment!r}")

    return [
        "docker",
        "run",
        "--rm",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--volume",
        f"{repo_root.resolve()}:/workspace:ro",
        "--volume",
        f"{work_dir.resolve()}:/work",
        "--workdir",
        "/workspace",
        image,
        shell,
        "-lc",
        inner,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--shape", required=True)
    args = parser.parse_args(argv)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        docker_command(args.repo_root, args.work_dir, args.shape), check=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
