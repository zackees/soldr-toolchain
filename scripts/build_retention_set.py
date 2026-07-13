#!/usr/bin/env python3
"""Emit immutable sha256 object keys referenced by catalogue documents."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

def collect(root: Path) -> list[str]:
    digests: set[str] = set()
    for path in root.rglob("*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        def walk(value):
            if isinstance(value, dict):
                if isinstance(value.get("sha256"), str) and len(value["sha256"]) == 64:
                    digests.add(value["sha256"])
                for child in value.values(): walk(child)
            elif isinstance(value, list):
                for child in value: walk(child)
        walk(doc)
    return sorted(digests)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps({"schema_version": 1, "sha256": collect(args.root)}, indent=2) + "\n", encoding="utf-8")
    return 0

if __name__ == "__main__": raise SystemExit(main())
