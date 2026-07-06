from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import convert_v5_to_v1 as cv


def _write_json(path: Path, doc: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _platform_key(entry: dict) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(entry["platform"].items()))


def test_convert_preserves_local_support_assets_for_v5_tool(
    tmp_path: Path,
    monkeypatch,
) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"

    _write_json(
        src / "manifest.json",
        {
            "schema_version": 5,
            "tools": {
                "cargo-chef": {
                    "owner": "LukeMathWalker",
                    "repo": "cargo-chef",
                    "latest": "v0.1.73",
                    "pinned": "v0.1.73",
                }
            },
        },
    )
    _write_json(src / "asset-index.json", {"schema_version": 1, "entries": []})
    _write_json(
        src / "cargo-chef" / "manifest.json",
        [
            {
                "owner": "LukeMathWalker",
                "repo": "cargo-chef",
                "tag": "v0.1.73",
                "version": "0.1.73",
                "published_at": "2026-01-01T00:00:00Z",
                "platforms": {
                    "windows-x64-msvc": {
                        "filename": "cargo-chef-windows-x64.zip",
                        "url": "https://example.invalid/upstream-x64.zip",
                        "size": 111,
                        "sha256": "1" * 64,
                    },
                    "windows-arm64-msvc": {
                        "filename": "cargo-chef-windows-arm64.zip",
                        "url": "https://example.invalid/upstream-arm64.zip",
                        "size": 222,
                        "sha256": "2" * 64,
                    },
                },
            }
        ],
    )

    _write_json(
        dest / "cargo-chef" / "manifest.json",
        {
            "$schema": cv.SCHEMA_URL,
            "kind": "Catalog",
            "schema_version": 1,
            "tool": "cargo-chef",
            "online_url": "https://zackees.github.io/soldr-toolchain/cargo-chef/manifest.json",
            "channels": {"pinned": "v0.1.73"},
            "releases": [
                {
                    "schema_version": 1,
                    "version": "v0.1.73",
                    "published_at": "",
                    "min_client_version": 1,
                    "platforms": [
                        {
                            "platform": {
                                "os": "windows",
                                "arch": "aarch64",
                                "abi": "msvc",
                            },
                            "asset": {
                                "filename": "bundle.tar.zst",
                                "size_bytes": 333,
                                "sha256": "a" * 64,
                                "urls": [
                                    "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/cargo-chef/v0.1.73/windows-aarch64-msvc/bundle.tar.zst",
                                    "https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/cargo-chef/v0.1.73/windows-aarch64-msvc/bundle.tar.zst",
                                ],
                            },
                        },
                        {
                            "platform": {
                                "os": "linux",
                                "arch": "x86_64",
                                "libc": "musl",
                            },
                            "asset": {
                                "filename": "stale.tar.gz",
                                "size_bytes": 444,
                                "sha256": "b" * 64,
                                "urls": ["https://example.invalid/stale.tar.gz"],
                            },
                        },
                    ],
                }
            ],
        },
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["convert_v5_to_v1.py", "--src", str(src), "--dest", str(dest)],
    )

    assert cv.main() == 0

    catalog_path = dest / "cargo-chef" / "manifest.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    release = next(r for r in catalog["releases"] if r["version"] == "v0.1.73")
    platforms = {_platform_key(p): p for p in release["platforms"]}

    windows_x64 = (("abi", "msvc"), ("arch", "x86_64"), ("os", "windows"))
    windows_arm64 = (("abi", "msvc"), ("arch", "aarch64"), ("os", "windows"))
    linux_musl = (("arch", "x86_64"), ("libc", "musl"), ("os", "linux"))

    assert platforms[windows_x64]["asset"]["urls"] == ["https://example.invalid/upstream-x64.zip"]
    assert platforms[windows_arm64]["asset"]["filename"] == "bundle.tar.zst"
    assert "soldr-toolchain/assets" in platforms[windows_arm64]["asset"]["urls"][0]
    assert linux_musl not in platforms

    index = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    desc = index["tools"]["cargo-chef"]["descriptor"]
    assert desc["url"] == "cargo-chef/manifest.json"
    assert desc["sha256"] == hashlib.sha256(catalog_path.read_bytes()).hexdigest()
