#!/usr/bin/env python3
"""Run one Linux Dylint release lane in its native ABI container."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path
from typing import Sequence

from scripts.produce_dylint_release import lane_for_shape

MANYLINUX_IMAGES = {
    "linux-x64-gnu": (
        "quay.io/pypa/manylinux2014_x86_64"
        "@sha256:0a42cb7e5f4ba6bbfb8d0a86d1aab0c8876ba9c3be16bd99360ae42bf010ec77"
    ),
    "linux-arm64-gnu": (
        "quay.io/pypa/manylinux2014_aarch64"
        "@sha256:63bfa74be47f0277e998cb7c1b571b27664ac848bb356b0f4588438f930285dd"
    ),
}
MUSL_IMAGE = (
    "alpine@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce"
)


def docker_command(
    repo_root: Path, shape: str, *, dylint_checkout: Path | None = None
) -> list[str]:
    """Create the auditable Docker invocation for one native Linux host."""
    lane = lane_for_shape(shape)
    if not shape.startswith("linux-"):
        raise RuntimeError(f"{shape!r} is not a containerized Linux release lane")

    if lane.environment == "manylinux2014":
        image = MANYLINUX_IMAGES[shape]
        python = "/opt/python/cp311-cp311/bin/python"
        shell = "bash"
        prepare = (
            "yum install -y openssl-devel perl-IPC-Cmd perl-Time-Piece"
            " && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs "
            "| sh -s -- -y --profile minimal"
        )
    elif lane.environment == "alpine":
        image = MUSL_IMAGE
        python = "python3"
        shell = "/bin/sh"
        prepare = (
            "apk add --no-cache bash build-base cmake curl git openssl-dev perl "
            "pkgconf python3"
        )
    else:
        raise RuntimeError(f"unsupported Linux release environment {lane.environment!r}")

    checkout_arg = "/dylint" if dylint_checkout is not None else "/workspace/dylint"
    producer = (
        f"{python} -m scripts.produce_dylint_release build "
        f"--repo-root /workspace --dylint-checkout {checkout_arg} "
        "--output-dir /workspace/out --work-dir /workspace/work "
        f"--shape {shape}"
    )
    safe_checkout = (
        f"git config --global --add safe.directory {checkout_arg}"
    )
    if lane.environment == "alpine":
        bash_inner = (
            "set -euo pipefail; "
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs "
            "| sh -s -- -y --profile minimal; "
            f". /root/.cargo/env; {safe_checkout}; {producer}"
        )
        inner = f"set -eu; {prepare}; bash -lc {shlex.quote(bash_inner)}"
    else:
        inner = (
            f"set -euo pipefail; {prepare}; . /root/.cargo/env; "
            f"{safe_checkout}; {producer}"
        )
    command = [
        "docker",
        "run",
        "--rm",
        "--volume",
        f"{repo_root.resolve()}:/workspace",
    ]
    if dylint_checkout is not None:
        command.extend(["--volume", f"{dylint_checkout.resolve()}:/dylint:ro"])
    command.extend([
        "--workdir",
        "/workspace",
        image,
        shell,
        "-lc",
        inner,
    ])
    return command


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--dylint-checkout", type=Path)
    parser.add_argument("--shape", required=True)
    args = parser.parse_args(argv)
    subprocess.run(
        docker_command(
            args.repo_root, args.shape, dylint_checkout=args.dylint_checkout
        ),
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
