"""Unit tests for ``build_manifest.py``.

Pure-function tests — no GitHub API calls. The
``build_merged_tool_releases`` test injects a fake ``list_releases_fn``
so the merge logic is exercised without network access.

Run::

    python3 -m unittest tests.test_build_manifest -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import build_manifest as bm


class DerivePlatformKeyTest(unittest.TestCase):
    """The filename-to-platform mapping must agree with the documented
    schema (npm short arch names, ABI extras only when meaningful).

    The whole consumer side of the manifest depends on this — a key
    drift here silently breaks every `platforms[<host>]` lookup.
    """

    def assert_key(self, filename: str, expected: str | None) -> None:
        with self.subTest(filename=filename):
            self.assertEqual(bm.derive_platform_key(filename), expected)

    def test_linux_variants(self) -> None:
        self.assert_key(
            "zccache-v1.12.9-x86_64-unknown-linux-gnu.tar.gz",
            "linux-x64-gnu",
        )
        self.assert_key(
            "zccache-v1.12.9-x86_64-unknown-linux-musl.tar.gz",
            "linux-x64-musl",
        )
        self.assert_key(
            "zccache-v1.12.9-aarch64-unknown-linux-gnu.tar.gz",
            "linux-arm64-gnu",
        )
        self.assert_key(
            "zccache-v1.12.9-aarch64-unknown-linux-musl.tar.gz",
            "linux-arm64-musl",
        )

    def test_darwin_variants(self) -> None:
        self.assert_key(
            "zccache-v1.12.9-x86_64-apple-darwin.tar.gz",
            "darwin-x64",
        )
        self.assert_key(
            "zccache-v1.12.9-aarch64-apple-darwin.tar.gz",
            "darwin-arm64",
        )
        self.assert_key(
            "tool-universal2-apple-darwin.tar.gz",
            "darwin-universal2",
        )
        self.assert_key(
            "tool-universal-apple-darwin.tar.gz",
            "darwin-universal2",
        )

    def test_windows_variants(self) -> None:
        self.assert_key(
            "zccache-v1.12.9-x86_64-pc-windows-msvc.zip",
            "windows-x64-msvc",
        )
        self.assert_key(
            "zccache-v1.12.9-aarch64-pc-windows-msvc.zip",
            "windows-arm64-msvc",
        )
        # cargo-xwin's unsuffixed Windows zips → MSVC (per soldr's rule).
        self.assert_key("cargo-xwin-windows-x64.zip", "windows-x64-msvc")

    def test_drops_32bit_and_armv7(self) -> None:
        self.assert_key("tool-i686-unknown-linux-gnu.tar.gz", None)
        self.assert_key("tool-armv7-unknown-linux-musleabihf.tar.gz", None)

    def test_drops_non_archives(self) -> None:
        self.assert_key("zccache-v1.12.9.deb", None)
        self.assert_key("install.sh", None)
        self.assert_key("SHA256SUMS", None)

    def test_drops_debug_and_symbols(self) -> None:
        self.assert_key(
            "zccache-v1.12.9-x86_64-pc-windows-msvc-debug.zip",
            None,
        )
        self.assert_key("tool.debug.tar.gz", None)
        self.assert_key("tool-syms.tar.gz", None)

    def test_drops_source_and_installers_and_dist_manifest(self) -> None:
        self.assert_key("foo-source.tar.gz", None)
        self.assert_key("foo-dist-manifest.tar.gz", None)
        self.assert_key("foo-installer.zip", None)


class BuildReleaseEntryTest(unittest.TestCase):

    def test_strips_v_prefix_for_version(self) -> None:
        release = {
            "tag_name": "v1.2.3",
            "name": "Release 1.2.3",
            "draft": False,
            "prerelease": False,
            "created_at": "2026-01-01T00:00:00Z",
            "published_at": "2026-01-02T00:00:00Z",
            "html_url": "https://example.invalid/1.2.3",
            "assets": [],
        }
        entry = bm.build_release_entry(release, tool="t", owner="o", repo="r")
        self.assertEqual(entry["tag"], "v1.2.3")
        self.assertEqual(entry["version"], "1.2.3")
        self.assertEqual(entry["tool"], "t")
        self.assertEqual(entry["owner"], "o")
        self.assertEqual(entry["repo"], "r")

    def test_tag_without_v_prefix(self) -> None:
        release = {
            "tag_name": "1.2.3",
            "name": "Release 1.2.3",
            "draft": False,
            "prerelease": False,
            "created_at": None,
            "published_at": None,
            "html_url": None,
            "assets": [],
        }
        entry = bm.build_release_entry(release)
        self.assertEqual(entry["tag"], "1.2.3")
        self.assertEqual(entry["version"], "1.2.3")

    def test_platforms_and_assets_populated(self) -> None:
        release = {
            "tag_name": "v1.0.0",
            "name": None,
            "draft": False,
            "prerelease": False,
            "created_at": None,
            "published_at": None,
            "html_url": None,
            "assets": [
                {
                    "name": "tool-x86_64-unknown-linux-gnu.tar.gz",
                    "browser_download_url": "https://example.invalid/lin.tar.gz",
                    "size": 100,
                    "content_type": "application/gzip",
                    "created_at": None,
                    "updated_at": None,
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": "https://example.invalid/SHA256SUMS",
                    "size": 200,
                    "content_type": "text/plain",
                    "created_at": None,
                    "updated_at": None,
                },
            ],
        }
        entry = bm.build_release_entry(release, tool="t", owner="o", repo="r")
        self.assertIn("linux-x64-gnu", entry["platforms"])
        self.assertEqual(
            entry["platforms"]["linux-x64-gnu"]["url"],
            "https://example.invalid/lin.tar.gz",
        )
        # SHA256SUMS is NOT a runnable platform — it lives in `assets`
        # but must not appear in `platforms`.
        self.assertIn("SHA256SUMS", entry["assets"])
        self.assertNotIn(
            "SHA256SUMS", {v.get("filename") for v in entry["platforms"].values()}
        )

    def test_platforms_deterministically_sorted(self) -> None:
        """`platforms` is keyed by platform string; the dict iteration
        order has to be sorted so two refresh runs produce byte-identical
        per-tool manifests when the inputs match (the whole point of
        ``write_if_changed``)."""
        release = {
            "tag_name": "v1.0.0",
            "name": None,
            "draft": False,
            "prerelease": False,
            "created_at": None,
            "published_at": None,
            "html_url": None,
            "assets": [
                {
                    "name": "tool-x86_64-pc-windows-msvc.zip",
                    "browser_download_url": "u1",
                    "size": 1,
                    "content_type": None,
                    "created_at": None,
                    "updated_at": None,
                },
                {
                    "name": "tool-x86_64-unknown-linux-gnu.tar.gz",
                    "browser_download_url": "u2",
                    "size": 1,
                    "content_type": None,
                    "created_at": None,
                    "updated_at": None,
                },
                {
                    "name": "tool-aarch64-apple-darwin.tar.gz",
                    "browser_download_url": "u3",
                    "size": 1,
                    "content_type": None,
                    "created_at": None,
                    "updated_at": None,
                },
            ],
        }
        entry = bm.build_release_entry(release)
        keys = list(entry["platforms"].keys())
        self.assertEqual(keys, sorted(keys))


class LoadExistingPerToolTest(unittest.TestCase):
    """The schema migration logic must read v2/v3, v4, AND v5 outputs
    so a one-time refresh after a schema bump doesn't drop history."""

    def test_v5_flat_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(
                json.dumps(
                    [
                        {"tag": "v1", "tool": "t"},
                        {"tag": "v2", "tool": "t"},
                    ]
                )
            )
            entries = bm.load_existing_per_tool(p)
            self.assertEqual([e["tag"] for e in entries], ["v1", "v2"])

    def test_v4_dict_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "name": "t",
                        "owner": "o",
                        "repo": "r",
                        "pinned": None,
                        "tracked_tags": ["v1.0", "v2.0"],
                        "latest": "v2.0",
                        "v1.0": {"tag": "v1.0", "platforms": {}},
                        "v2.0": {"tag": "v2.0", "platforms": {}},
                    }
                )
            )
            entries = bm.load_existing_per_tool(p)
            tags = sorted(e["tag"] for e in entries)
            self.assertEqual(tags, ["v1.0", "v2.0"])

    def test_v3_releases_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "releases": {
                            "v1.0": {"tag": "v1.0", "platforms": {}},
                            "v2.0": {"tag": "v2.0", "platforms": {}},
                        },
                    }
                )
            )
            entries = bm.load_existing_per_tool(p)
            tags = sorted(e["tag"] for e in entries)
            self.assertEqual(tags, ["v1.0", "v2.0"])

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(bm.load_existing_per_tool(Path("/nope/missing.json")), [])

    def test_malformed_json_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{not valid json")
            self.assertEqual(bm.load_existing_per_tool(p), [])


class BuildMergedToolReleasesTest(unittest.TestCase):
    """The merge invariant: existing entries that aren't in the fetched
    window are preserved; entries the API just returned overwrite their
    counterparts on file; the result is sorted newest-first."""

    def test_merges_preserving_history(self) -> None:
        existing = [
            {
                "tag": "v1.0",
                "published_at": "2020-01-01T00:00:00Z",
                "platforms": {},
                "assets": {},
            },
            {
                "tag": "v2.0",
                "published_at": "2021-01-01T00:00:00Z",
                "platforms": {},
                "assets": {},
                "stale_field": "preserved",
            },
        ]
        fetched = [
            {
                "tag_name": "v3.0",
                "name": None,
                "draft": False,
                "prerelease": False,
                "created_at": None,
                "published_at": "2022-01-01T00:00:00Z",
                "html_url": None,
                "assets": [],
            },
            {
                # Overwrites v2.0 — fresh from API.
                "tag_name": "v2.0",
                "name": None,
                "draft": False,
                "prerelease": False,
                "created_at": None,
                "published_at": "2021-01-01T00:00:00Z",
                "html_url": None,
                "assets": [],
            },
        ]

        def fake_list(owner: str, repo: str, token: str | None) -> list:
            self.assertEqual((owner, repo), ("o", "r"))
            return fetched

        entries, latest = bm.build_merged_tool_releases(
            "t",
            "o",
            "r",
            pinned_tag="v2.0",
            token=None,
            existing=existing,
            list_releases_fn=fake_list,
        )

        # v1.0 is preserved from existing; v2.0 is overwritten (no
        # `stale_field` since the fresh dict replaces it); v3.0 is new.
        tags = [e["tag"] for e in entries]
        self.assertEqual(tags, ["v3.0", "v2.0", "v1.0"])  # newest-first
        self.assertEqual(latest, "v3.0")
        # v2.0 was overwritten — stale field should be gone.
        v2 = next(e for e in entries if e["tag"] == "v2.0")
        self.assertNotIn("stale_field", v2)
        self.assertTrue(v2["is_pinned"])
        # v3.0 is not pinned.
        v3 = next(e for e in entries if e["tag"] == "v3.0")
        self.assertFalse(v3["is_pinned"])

    def test_empty_history(self) -> None:
        def fake_list(*args, **kwargs):
            return []

        entries, latest = bm.build_merged_tool_releases(
            "t",
            "o",
            "r",
            pinned_tag=None,
            token=None,
            existing=[],
            list_releases_fn=fake_list,
        )
        self.assertEqual(entries, [])
        self.assertIsNone(latest)


class WriteIfChangedTest(unittest.TestCase):

    def test_no_write_when_content_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.txt"
            p.write_text("hello\n")
            mtime_before = p.stat().st_mtime_ns
            self.assertFalse(bm.write_if_changed(p, "hello\n"))
            self.assertEqual(p.stat().st_mtime_ns, mtime_before)

    def test_writes_when_content_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.txt"
            p.write_text("hello\n")
            self.assertTrue(bm.write_if_changed(p, "world\n"))
            self.assertEqual(p.read_text(), "world\n")

    def test_creates_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "deep" / "nested" / "f.txt"
            self.assertTrue(bm.write_if_changed(p, "x\n"))
            self.assertEqual(p.read_text(), "x\n")


class ReadConstantTest(unittest.TestCase):
    """Reading pinned versions out of Rust source must tolerate the
    formatting variations soldr uses (``pub const`` vs ``pub(crate) const``,
    inline attributes, etc.)."""

    def test_simple_const(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "src.rs"
            p.write_text('pub const FOO_VERSION: &str = "1.2.3";\n')
            self.assertEqual(bm.read_constant(p, "FOO_VERSION"), "1.2.3")

    def test_pub_crate_const(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "src.rs"
            p.write_text('pub(crate) const FOO_VERSION: &str = "9.0.1";\n')
            self.assertEqual(bm.read_constant(p, "FOO_VERSION"), "9.0.1")

    def test_const_with_surrounding_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "src.rs"
            p.write_text(
                "// noise\n"
                'pub const OTHER: &str = "x";\n'
                'pub const MANAGED_ZCCACHE_VERSION: &str = "1.12.9";\n'
                'pub const ANOTHER: &str = "y";\n'
            )
            self.assertEqual(
                bm.read_constant(p, "MANAGED_ZCCACHE_VERSION"),
                "1.12.9",
            )

    def test_raises_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "src.rs"
            p.write_text("// nothing here\n")
            with self.assertRaises(RuntimeError):
                bm.read_constant(p, "FOO_VERSION")


class PreserveVendoredTopLevelEntriesTest(unittest.TestCase):

    def test_preserves_entry_when_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deps" / "mac").mkdir(parents=True)
            (root / "deps" / "mac" / "manifest.json").write_text("[]")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 5,
                        "tools": {
                            "apple-sdk": {
                                "path": "deps/mac/manifest.json",
                                "kind": "vendored-sdk",
                            },
                            "stale-tool": {
                                "path": "stale/missing.json",
                            },
                        },
                    }
                )
            )
            per_tool_index: dict = {}
            bm.preserve_vendored_top_level_entries(root, per_tool_index)
            self.assertIn("apple-sdk", per_tool_index)
            self.assertNotIn("stale-tool", per_tool_index)

    def test_does_not_overwrite_existing_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deps" / "mac").mkdir(parents=True)
            (root / "deps" / "mac" / "manifest.json").write_text("[]")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 5,
                        "tools": {
                            "apple-sdk": {
                                "path": "deps/mac/manifest.json",
                                "marker": "OLD",
                            },
                        },
                    }
                )
            )
            per_tool_index = {"apple-sdk": {"marker": "NEW"}}
            bm.preserve_vendored_top_level_entries(root, per_tool_index)
            # NEW must stay — the freshly-built index wins for known names.
            self.assertEqual(per_tool_index["apple-sdk"]["marker"], "NEW")


class LoadPinnedVersionsTest(unittest.TestCase):
    """End-to-end test: synthesize a fake soldr source tree and verify
    the three pinned versions are extracted correctly."""

    def test_reads_all_three_pins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fetch_dir = root / "crates" / "soldr-cli" / "src" / "fetch"
            fetch_dir.mkdir(parents=True)
            (fetch_dir / "mod.rs").write_text(
                'pub const MANAGED_ZCCACHE_VERSION: &str = "1.12.9";\n'
                'pub const MANAGED_CRGX_VERSION: &str = "0.3.4";\n'
            )
            (fetch_dir / "known_tools.rs").write_text(
                'pub const CARGO_CHEF_PINNED_VERSION: &str = "0.1.73";\n'
            )
            pins = bm.load_pinned_versions(root)
            self.assertEqual(pins["zccache"], "1.12.9")
            self.assertEqual(pins["crgx"], "v0.3.4")
            self.assertEqual(pins["cargo-chef"], "v0.1.73")

    def test_supports_split_soldr_fetch_and_embedded_zccache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fetch_dir = root / "crates" / "soldr-fetch" / "src" / "fetch"
            fetch_dir.mkdir(parents=True)
            (fetch_dir / "mod.rs").write_text(
                'pub const MANAGED_CRGX_VERSION: &str = "0.3.4";\n'
            )
            (fetch_dir / "known_tools.rs").write_text(
                'pub const CARGO_CHEF_PINNED_VERSION: &str = "0.1.73";\n'
            )
            pins = bm.load_pinned_versions(root)
            self.assertIsNone(pins["zccache"])
            self.assertEqual(pins["crgx"], "v0.3.4")
            self.assertEqual(pins["cargo-chef"], "v0.1.73")


class LoadManagedRustToolsTest(unittest.TestCase):
    def test_reads_release_tag_conventions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-rust-tools.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "tools": {
                            "cargo-binstall": {
                                "version": "1.20.1",
                                "source": "cargo-bins/cargo-binstall",
                                "release_tag_prefix": "v",
                            },
                            "cargo-nextest": {
                                "version": "0.9.140",
                                "source": "nextest-rs/nextest",
                                "release_tag_prefix": "cargo-nextest-",
                            },
                        },
                    }
                )
            )
            self.assertEqual(
                bm.load_managed_rust_tools(path),
                [
                    ("cargo-binstall", "cargo-bins", "cargo-binstall", "v1.20.1"),
                    ("cargo-nextest", "nextest-rs", "nextest", "cargo-nextest-0.9.140"),
                ],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
