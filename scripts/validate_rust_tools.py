#!/usr/bin/env python3
"""Validate the pinned Rust-tool vertical-slice contract."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def validate(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema_version") != 1:
        raise ValueError("unsupported managed Rust tools schema")
    platforms = doc.get("platforms")
    if (
        not isinstance(platforms, list)
        or len(platforms) != 8
        or len(set(platforms)) != 8
    ):
        raise ValueError("managed Rust tools must list eight unique platforms")
    tools = doc.get("tools") or {}
    for name in ("cargo-binstall", "cargo-nextest"):
        item = tools.get(name) or {}
        version = str(item.get("version", ""))
        if not version or version.lower() in {"latest", "*"}:
            raise ValueError(f"{name} must have an exact version")
        if (
            not item.get("source")
            or not item.get("binary")
            or not item.get("source_ref")
        ):
            raise ValueError(f"{name} is missing source/binary/source_ref")
        source_ref = str(item["source_ref"])
        if len(source_ref) != 40 or any(
            c not in "0123456789abcdef" for c in source_ref
        ):
            raise ValueError(f"{name} source_ref must be a full lowercase commit SHA")
    return doc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        type=Path,
        default=Path(__file__).parents[1] / "managed-rust-tools.json",
        nargs="?",
    )
    args = parser.parse_args()
    print(json.dumps(validate(args.path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
