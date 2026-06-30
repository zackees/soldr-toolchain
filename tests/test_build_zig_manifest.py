"""Unit tests for ``build_zig_manifest.py``.

Pure-function tests — no network. The ``main`` test uses a frozen
``index.json`` fixture passed via ``--fixture`` so the upstream
ziglang.org JSON shape never changes the test outcomes.

Run::

    uv run --group dev pytest tests/test_build_zig_manifest.py
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import build_zig_manifest as bzm
from manifest_json import validate_document


# Minimal but realistic index — two stable versions, one pre-release,
# one `master`, with a partial-platform release to test the skip path.
# Modeled after the real ziglang.org/download/index.json (2026-06).
FIXTURE_INDEX: dict = {
    "master": {
        "version": "0.17.0-dev.123+abcdef",
        "date": "2026-06-25",
        "x86_64-linux": {
            "tarball": "https://ziglang.org/download/0.17.0-dev/zig-x86_64-linux-master.tar.xz",
            "shasum":  "deadbeef" * 8,
            "size":    "60000000",
        },
    },
    "0.16.0": {
        "version": "0.16.0",
        "date":    "2026-04-13",
        "x86_64-linux": {
            "tarball": "https://ziglang.org/download/0.16.0/zig-x86_64-linux-0.16.0.tar.xz",
            "shasum":  "a" * 64,
            "size":    "55478392",
        },
        "aarch64-linux": {
            "tarball": "https://ziglang.org/download/0.16.0/zig-aarch64-linux-0.16.0.tar.xz",
            "shasum":  "b" * 64,
            "size":    "50000000",
        },
        "x86_64-macos": {
            "tarball": "https://ziglang.org/download/0.16.0/zig-x86_64-macos-0.16.0.tar.xz",
            "shasum":  "c" * 64,
            "size":    "55000000",
        },
        "aarch64-macos": {
            "tarball": "https://ziglang.org/download/0.16.0/zig-aarch64-macos-0.16.0.tar.xz",
            "shasum":  "d" * 64,
            "size":    "50000000",
        },
        "x86_64-windows": {
            "tarball": "https://ziglang.org/download/0.16.0/zig-x86_64-windows-0.16.0.zip",
            "shasum":  "e" * 64,
            "size":    "90000000",
        },
        "aarch64-windows": {
            "tarball": "https://ziglang.org/download/0.16.0/zig-aarch64-windows-0.16.0.zip",
            "shasum":  "f" * 64,
            "size":    "88000000",
        },
        # The producer must skip these — not in the surfaced platform set.
        "x86-linux": {
            "tarball": "https://ziglang.org/download/0.16.0/zig-x86-linux-0.16.0.tar.xz",
            "shasum":  "1" * 64,
            "size":    "50000000",
        },
        "riscv64-linux": {
            "tarball": "https://ziglang.org/download/0.16.0/zig-riscv64-linux-0.16.0.tar.xz",
            "shasum":  "2" * 64,
            "size":    "50000000",
        },
    },
    "0.15.2": {
        "version": "0.15.2",
        "date":    "2025-10-11",
        "x86_64-linux": {
            "tarball": "https://ziglang.org/download/0.15.2/zig-x86_64-linux-0.15.2.tar.xz",
            "shasum":  "02aa270f183da276e5b5920b1dac44a63f1a49e55050ebde3aecc9eb82f93239",
            "size":    "53733924",
        },
        # 0.15.2 deliberately missing all other platforms to test
        # the partial-platform handling.
    },
    "0.15.1": {
        "version": "0.15.1",
        "date":    "2025-08-19",
        "x86_64-linux": {
            "tarball": "https://ziglang.org/download/0.15.1/zig-x86_64-linux-0.15.1.tar.xz",
            "shasum":  "3" * 64,
            "size":    "53000000",
        },
        "aarch64-linux": {
            "tarball": "https://ziglang.org/download/0.15.1/zig-aarch64-linux-0.15.1.tar.xz",
            "shasum":  "4" * 64,
            "size":    "50000000",
        },
        "x86_64-windows": {
            "tarball": "https://ziglang.org/download/0.15.1/zig-x86_64-windows-0.15.1.zip",
            "shasum":  "5" * 64,
            "size":    "90000000",
        },
        "aarch64-windows": {
            "tarball": "https://ziglang.org/download/0.15.1/zig-aarch64-windows-0.15.1.zip",
            "shasum":  "6" * 64,
            "size":    "88000000",
        },
        "x86_64-macos": {
            "tarball": "https://ziglang.org/download/0.15.1/zig-x86_64-macos-0.15.1.tar.xz",
            "shasum":  "7" * 64,
            "size":    "55000000",
        },
        "aarch64-macos": {
            "tarball": "https://ziglang.org/download/0.15.1/zig-aarch64-macos-0.15.1.tar.xz",
            "shasum":  "8" * 64,
            "size":    "50000000",
        },
    },
    "0.13.0": {
        "version": "0.13.0",
        "date":    "2025-06-07",
        "x86_64-linux": {
            "tarball": "https://ziglang.org/download/0.13.0/zig-linux-x86_64-0.13.0.tar.xz",
            "shasum":  "d45312e61ebcc48032b77bc4cf7fd6915c11fa16e4aad116b66c9468211230ea",
            "size":    "47082308",
        },
    },
    # Source tarball — has no platform-keyed entries; producer must
    # tolerate this kind of pseudo-version-key.
    "src": {
        "tarball": "https://ziglang.org/download/0.16.0/zig-0.16.0.tar.xz",
        "shasum":  "ff" * 32,
        "size":    "21366268",
    },
}


class IsStableVersionTest(unittest.TestCase):
    def test_master_is_not_stable(self) -> None:
        self.assertFalse(bzm.is_stable_version("master"))

    def test_prerelease_is_not_stable(self) -> None:
        self.assertFalse(bzm.is_stable_version("0.16.0-dev.123"))
        self.assertFalse(bzm.is_stable_version("0.16.0-rc1"))

    def test_three_digit_semver_is_stable(self) -> None:
        self.assertTrue(bzm.is_stable_version("0.15.2"))
        self.assertTrue(bzm.is_stable_version("0.16.0"))
        self.assertTrue(bzm.is_stable_version("1.0.0"))

    def test_non_semver_is_not_stable(self) -> None:
        self.assertFalse(bzm.is_stable_version("src"))
        self.assertFalse(bzm.is_stable_version("0.16"))
        self.assertFalse(bzm.is_stable_version("0.16.0.1"))
        self.assertFalse(bzm.is_stable_version("v0.16.0"))


class SelectStableVersionsTest(unittest.TestCase):
    def test_newest_first(self) -> None:
        got = bzm.select_stable_versions(FIXTURE_INDEX, keep_n=10)
        self.assertEqual(got, ["0.16.0", "0.15.2", "0.15.1", "0.13.0"])

    def test_keep_n_caps_size(self) -> None:
        got = bzm.select_stable_versions(FIXTURE_INDEX, keep_n=2)
        self.assertEqual(got, ["0.16.0", "0.15.2"])

    def test_skips_master_and_pseudo_keys(self) -> None:
        got = bzm.select_stable_versions(FIXTURE_INDEX, keep_n=10)
        self.assertNotIn("master", got)
        self.assertNotIn("src", got)


class BuildReleaseEntryTest(unittest.TestCase):
    def test_full_platform_release(self) -> None:
        entry = bzm.build_release_entry("0.16.0", FIXTURE_INDEX["0.16.0"])
        self.assertEqual(entry["version"], "0.16.0")
        self.assertEqual(entry["schema_version"], 1)
        self.assertEqual(entry["min_client_version"], 1)
        self.assertEqual(entry["published_at"], "2026-04-13T00:00:00Z")
        # 6 surfaced platforms, the unsurfaced x86-linux + riscv64-linux skipped.
        plat_keys = {
            (p["platform"]["os"], p["platform"]["arch"])
            for p in entry["platforms"]
        }
        self.assertEqual(
            plat_keys,
            {
                ("linux", "x86_64"),
                ("linux", "aarch64"),
                ("darwin", "x86_64"),
                ("darwin", "aarch64"),
                ("windows", "x86_64"),
                ("windows", "aarch64"),
            },
        )

    def test_partial_release_keeps_what_it_has(self) -> None:
        entry = bzm.build_release_entry("0.15.2", FIXTURE_INDEX["0.15.2"])
        # Only x86_64-linux ships for 0.15.2 in the fixture.
        self.assertEqual(len(entry["platforms"]), 1)
        p = entry["platforms"][0]
        self.assertEqual(p["platform"]["os"], "linux")
        self.assertEqual(p["platform"]["arch"], "x86_64")
        self.assertEqual(p["asset"]["size_bytes"], 53733924)
        self.assertEqual(
            p["asset"]["sha256"],
            "02aa270f183da276e5b5920b1dac44a63f1a49e55050ebde3aecc9eb82f93239",
        )
        self.assertEqual(
            p["asset"]["urls"][0],
            "https://ziglang.org/download/0.15.2/zig-x86_64-linux-0.15.2.tar.xz",
        )
        self.assertEqual(
            p["asset"]["filename"], "zig-x86_64-linux-0.15.2.tar.xz",
        )

    def test_filename_derived_from_url(self) -> None:
        # The 0.13.0 entry uses the OLD `zig-linux-x86_64-` URL pattern
        # vs 0.15.2's NEW `zig-x86_64-linux-` — the producer must consume
        # whatever URL upstream publishes, not reconstruct it from
        # version + arch + os.
        entry = bzm.build_release_entry("0.13.0", FIXTURE_INDEX["0.13.0"])
        self.assertEqual(len(entry["platforms"]), 1)
        self.assertEqual(
            entry["platforms"][0]["asset"]["filename"],
            "zig-linux-x86_64-0.13.0.tar.xz",
        )


class BuildCatalogTest(unittest.TestCase):
    def test_full_catalog_shape(self) -> None:
        cat = bzm.build_catalog(FIXTURE_INDEX, keep_n=5)
        # Schema headers
        self.assertEqual(cat["kind"], "Catalog")
        self.assertEqual(cat["schema_version"], 1)
        self.assertEqual(cat["tool"], "zig")
        self.assertEqual(
            cat["$schema"],
            "https://zackees.github.io/manifest.json/v1/manifest.schema.json",
        )
        # Channels point at the newest selected stable.
        self.assertEqual(cat["channels"]["latest-stable"], "0.16.0")
        self.assertEqual(cat["channels"]["stable"], "0.16.0")
        # Newest-first; 0.13.0 is included but no master / no src.
        versions = [r["version"] for r in cat["releases"]]
        self.assertEqual(versions, ["0.16.0", "0.15.2", "0.15.1", "0.13.0"])
        validate_document(cat)

    def test_keep_caps(self) -> None:
        cat = bzm.build_catalog(FIXTURE_INDEX, keep_n=2)
        versions = [r["version"] for r in cat["releases"]]
        self.assertEqual(versions, ["0.16.0", "0.15.2"])

    def test_refuses_to_emit_empty_manifest(self) -> None:
        # Master-only and pseudo-key-only index; no stable releases.
        with self.assertRaises(RuntimeError):
            bzm.build_catalog(
                {
                    "master": FIXTURE_INDEX["master"],
                    "src":    FIXTURE_INDEX["src"],
                },
            )


class WriteIfChangedTest(unittest.TestCase):
    def test_write_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "manifest.json"
            self.assertTrue(
                bzm.write_if_changed(path, {"foo": "bar"}),
            )
            self.assertTrue(path.is_file())
            # Trailing newline matches the existing producer convention.
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))

    def test_unchanged_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            bzm.write_if_changed(path, {"foo": "bar"})
            self.assertFalse(
                bzm.write_if_changed(path, {"foo": "bar"}),
            )

    def test_changed_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            bzm.write_if_changed(path, {"foo": "bar"})
            self.assertTrue(
                bzm.write_if_changed(path, {"foo": "baz"}),
            )


class MainEndToEndTest(unittest.TestCase):
    def test_via_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fixture = tmp_path / "index.json"
            fixture.write_text(json.dumps(FIXTURE_INDEX), encoding="utf-8")
            out_dir = tmp_path / "assets"
            rc = bzm.main(
                argv=[
                    "--output-dir", str(out_dir),
                    "--keep", "3",
                    "--fixture", str(fixture),
                ],
            )
            self.assertEqual(rc, 0)
            manifest_path = out_dir / "zig" / "manifest.json"
            self.assertTrue(manifest_path.is_file())
            doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(doc["tool"], "zig")
            self.assertEqual(len(doc["releases"]), 3)
            self.assertEqual(doc["channels"]["latest-stable"], "0.16.0")


if __name__ == "__main__":
    unittest.main()
