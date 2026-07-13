#!/usr/bin/env python3
"""Ingest every native platform artifact from one Forge Rust workflow run."""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts import forge_to_catalogue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forge-dir", type=Path, required=True)
    parser.add_argument(
        "--tool", choices=("cargo-binstall", "cargo-nextest"), required=True
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--forge-run-id", required=True)
    parser.add_argument("--assets-root", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    args = parser.parse_args()

    for shape in forge_to_catalogue.RUST_CLI_SHAPES:
        result = forge_to_catalogue.main(
            [
                "--forge-dir",
                str(args.forge_dir),
                "--tool",
                args.tool,
                "--version",
                args.version,
                "--shape",
                shape,
                "--forge-run-id",
                args.forge_run_id,
                "--assets-root",
                str(args.assets_root),
                "--schema",
                str(args.schema),
            ]
        )
        if result != 0:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
