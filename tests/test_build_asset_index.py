"""Unit tests for ``build_asset_index.py``.

Pure local + ``--offline`` tests — no github.com network dependency.
The SHA256SUMS HTTP path is exercised by a few injected-fake tests
below; a test that requires a real github.com round-trip would be
flaky.

Run::

    uv run --group dev pytest tests/test_build_asset_index.py -v
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import build_asset_index as bai


class Sha256OfFileTest(unittest.TestCase):

    def test_matches_hashlib_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "hello.txt"
            payload = b"hello world\n"
            f.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            self.assertEqual(bai.sha256_of_file(f), expected)
            self.assertEqual(bai.sha256_of_file(f), bai.sha256_of_file(f).lower())

    def test_empty_file_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "empty"
            f.write_bytes(b"")
            self.assertEqual(
                bai.sha256_of_file(f),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )

    def test_git_lfs_pointer_sha_uses_object_oid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "bundle.tar.zst"
            oid = "a" * 64
            f.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{oid}\n"
                "size 123456\n",
                encoding="utf-8",
            )
            self.assertEqual(bai.sha256_of_file(f), oid)

    def test_large_file_streamed_correctly(self) -> None:
        """Multi-chunk read must produce the same hash as a one-shot read."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "big"
            payload = b"x" * (3 * 1024 * 1024 + 17)  # 3+ chunks
            f.write_bytes(payload)
            self.assertEqual(
                bai.sha256_of_file(f),
                hashlib.sha256(payload).hexdigest(),
            )


class ParseSha256SumsTest(unittest.TestCase):

    def test_strips_dot_slash_prefix(self) -> None:
        text = (
            "deadbeef"
            + "0" * 56
            + "  ./foo.tar.gz\n"
            + "cafebabe"
            + "0" * 56
            + "  bar.zip\n"
        )
        parsed = bai.parse_sha256sums(text)
        self.assertEqual(set(parsed.keys()), {"foo.tar.gz", "bar.zip"})

    def test_skips_self_and_installers_and_debug(self) -> None:
        text = (
            "0" * 64
            + "  SHA256SUMS\n"
            + "1" * 64
            + "  install.sh\n"
            + "2" * 64
            + "  install.ps1\n"
            + "3" * 64
            + "  zccache-v1.12.9-x86_64-pc-windows-msvc-debug.zip\n"
            + "4" * 64
            + "  zccache-v1.12.9-x86_64-pc-windows-msvc.zip\n"
        )
        parsed = bai.parse_sha256sums(text)
        self.assertEqual(
            set(parsed.keys()),
            {"zccache-v1.12.9-x86_64-pc-windows-msvc.zip"},
        )

    def test_rejects_malformed_lines(self) -> None:
        text = (
            "not-a-hash  some.zip\n"
            + "# comment line\n"
            + "\n"
            + ("a" * 64)
            + "  good.zip\n"
        )
        parsed = bai.parse_sha256sums(text)
        self.assertEqual(parsed, {"good.zip": "a" * 64})

    def test_lowercases_hex(self) -> None:
        text = ("ABCDEF" + "0" * 58) + "  upper.zip\n"
        parsed = bai.parse_sha256sums(text)
        self.assertEqual(parsed, {"upper.zip": "abcdef" + "0" * 58})


class BuildAssetIndexTest(unittest.TestCase):
    """Tests build an assets-branch tree under a tempdir and assert
    that ``build_asset_index`` produces the expected entries.

    Layout under the test tree:
        apple-sdk/
            manifest.json                      # per-tool meta
            MacOSX11.3/
                darwin/
                    sdk.tar.zstd               # the blob
        fake-tool/
            manifest.json                      # release listing
    """

    def _make_tree(self, root: Path, *, with_per_tool_meta: bool = True) -> bytes:
        """Lay out a fake assets-branch tree under ``root``.

        Returns the bytes of the vendored blob so the test can assert
        the sha matches the on-disk file.
        """
        sdk_dir = root / "apple-sdk" / "MacOSX11.3" / "darwin"
        sdk_dir.mkdir(parents=True)
        payload = b"\x28\xb5\x2f\xfd" + b"hello-vendored-payload\n" * 4
        (sdk_dir / "sdk.tar.zstd").write_bytes(payload)

        if with_per_tool_meta:
            per_tool_meta = [
                {
                    "tool": "apple-sdk",
                    "owner": "vendored",
                    "repo": "messense/cargo-zigbuild",
                    "tag": "MacOSX11.3",
                    "assets": {
                        "sdk.tar.zstd": {
                            "url": "https://example.invalid/sdk.tar.zstd",
                            "size": len(payload),
                        }
                    },
                }
            ]
            (root / "apple-sdk" / "manifest.json").write_text(
                json.dumps(per_tool_meta, indent=2),
                encoding="utf-8",
            )

        # A per-tool dir with metadata but no blobs (mirrors real
        # zccache today — manifest only, GitHub-hosted release blobs).
        # Its release has no SHA256SUMS so offline contributes nothing.
        tool_dir = root / "fake-tool"
        tool_dir.mkdir()
        (tool_dir / "manifest.json").write_text(
            json.dumps(
                [
                    {
                        "tool": "fake-tool",
                        "owner": "someone",
                        "repo": "fake-tool",
                        "tag": "v0.0.1",
                        "assets": {
                            "fake-tool-linux.tar.gz": {
                                "url": "https://example.invalid/fake-tool-linux.tar.gz",
                                "size": 1,
                            }
                        },
                    }
                ],
                indent=2,
            ),
            encoding="utf-8",
        )

        return payload

    def test_schema_version_and_entry_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._make_tree(root)
            expected_sha = hashlib.sha256(payload).hexdigest()

            index = bai.build_asset_index(
                root,
                repo_owner="zackees",
                repo_name="soldr-toolchain",
                branch="assets",
                offline=True,
            )

            self.assertEqual(
                index["schema_version"],
                bai.ASSET_INDEX_SCHEMA_VERSION,
            )

            # The locally-hosted SDK file: attributed via apple-sdk's
            # per-tool manifest to (vendored, messense/cargo-zigbuild,
            # MacOSX11.3); sha and URL come from on-disk state.
            sdk_entries = [e for e in index["entries"] if e["asset"] == "sdk.tar.zstd"]
            self.assertEqual(len(sdk_entries), 1, msg=index)
            entry = sdk_entries[0]
            self.assertEqual(entry["owner"], "vendored")
            self.assertEqual(entry["repo"], "messense/cargo-zigbuild")
            self.assertEqual(entry["tag"], "MacOSX11.3")
            self.assertEqual(entry["sha256"], expected_sha)
            self.assertEqual(
                entry["url"],
                "https://media.githubusercontent.com/media/zackees/soldr-toolchain/"
                "assets/apple-sdk/MacOSX11.3/darwin/sdk.tar.zstd",
            )

    def test_self_attributes_unowned_blobs(self) -> None:
        """A blob under <tool>/<version>/ whose <tool>/manifest.json
        has no matching tag must still appear in the index,
        self-attributed to ``(<repo_owner>, <repo_name>, <branch>)``."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_tree(root, with_per_tool_meta=False)

            index = bai.build_asset_index(
                root,
                repo_owner="zackees",
                repo_name="soldr-toolchain",
                branch="assets",
                offline=True,
            )

            self_attributed = [
                e
                for e in index["entries"]
                if e["owner"] == "zackees"
                and e["repo"] == "soldr-toolchain"
                and e["tag"] == "assets"
            ]
            assets = {e["asset"] for e in self_attributed}
            self.assertIn("sdk.tar.zstd", assets)

    def test_v1_catalog_attributes_forge_blob_to_source_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "cargo-nextest" / "0.9.140" / "linux-x86_64-musl"
            blob_dir.mkdir(parents=True)
            payload = b"forge-nextest"
            (blob_dir / "bundle.tar.gz").write_bytes(payload)
            (root / "cargo-nextest" / "manifest.json").write_text(
                json.dumps(
                    {
                        "kind": "Catalog",
                        "releases": [
                            {
                                "version": "0.9.140",
                                "source": {
                                    "repo_url": "https://github.com/nextest-rs/nextest",
                                    "ref": "a9fef2964e34f64ed4fceeee7c0c3559ce560920",
                                },
                                "platforms": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            index = bai.build_asset_index(
                root,
                repo_owner="zackees",
                repo_name="soldr-toolchain",
                branch="assets",
                offline=True,
            )

            entry = next(e for e in index["entries"] if e["asset"] == "bundle.tar.gz")
            self.assertEqual(entry["owner"], "nextest-rs")
            self.assertEqual(entry["repo"], "nextest")
            self.assertEqual(entry["tag"], "0.9.140")
            self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())

    def test_variants_flat_inside_platform_folder(self) -> None:
        """Variants for the same OS+arch (e.g. gnu/musl on linux-x64)
        live as flat siblings inside the platform folder. Both must
        appear in the asset-index as distinct entries with distinct
        shas, attributed to the same (owner, repo, tag)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_dir = root / "zccache"
            blob_dir = tool_dir / "1.12.9" / "linux-x64"
            blob_dir.mkdir(parents=True)
            gnu_payload = b"gnu-build-content\n" * 100
            musl_payload = b"musl-build-content\n" * 100
            (blob_dir / "zccache-v1.12.9-x86_64-unknown-linux-gnu.tar.gz").write_bytes(
                gnu_payload
            )
            (blob_dir / "zccache-v1.12.9-x86_64-unknown-linux-musl.tar.gz").write_bytes(
                musl_payload
            )
            (tool_dir / "manifest.json").write_text(
                json.dumps(
                    [
                        {
                            "tool": "zccache",
                            "owner": "zackees",
                            "repo": "zccache",
                            "tag": "1.12.9",
                            "assets": {},
                        }
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )

            index = bai.build_asset_index(
                root,
                repo_owner="zackees",
                repo_name="soldr-toolchain",
                branch="assets",
                offline=True,
            )
            zccache_entries = sorted(
                (
                    e
                    for e in index["entries"]
                    if e["owner"] == "zackees" and e["repo"] == "zccache"
                ),
                key=lambda e: e["asset"],
            )
            self.assertEqual(len(zccache_entries), 2)
            self.assertEqual(
                zccache_entries[0]["asset"],
                "zccache-v1.12.9-x86_64-unknown-linux-gnu.tar.gz",
            )
            self.assertEqual(
                zccache_entries[0]["sha256"],
                hashlib.sha256(gnu_payload).hexdigest(),
            )
            self.assertEqual(
                zccache_entries[1]["asset"],
                "zccache-v1.12.9-x86_64-unknown-linux-musl.tar.gz",
            )
            self.assertEqual(
                zccache_entries[1]["sha256"],
                hashlib.sha256(musl_payload).hexdigest(),
            )
            # Both URLs share the platform folder.
            for entry in zccache_entries:
                self.assertIn("/zccache/1.12.9/linux-x64/", entry["url"])

    def test_same_local_filename_survives_across_platform_dirs(self) -> None:
        """Forge-ingested catalogue bundles use stable filenames like
        bundle.tar.zst or sdk.tar.zst. Distinct platform directories must
        survive refresh even when owner/repo/tag/asset are identical."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for platform, payload in {
                "darwin-aarch64": b"arm64-sdk",
                "darwin-x86_64": b"x64-sdk",
                "darwin-universal2": b"fat-sdk",
            }.items():
                blob_dir = root / "apple-sdk" / "14.5" / platform
                blob_dir.mkdir(parents=True, exist_ok=True)
                (blob_dir / "sdk.tar.zst").write_bytes(payload)

            index = bai.build_asset_index(
                root,
                repo_owner="zackees",
                repo_name="soldr-toolchain",
                branch="assets",
                offline=True,
            )

            sdk_entries = [e for e in index["entries"] if e["asset"] == "sdk.tar.zst"]
            self.assertEqual(len(sdk_entries), 3, msg=index)
            self.assertEqual(
                {
                    e["url"].rsplit("/14.5/", 1)[1].rsplit("/", 1)[0]
                    for e in sdk_entries
                },
                {"darwin-aarch64", "darwin-x86_64", "darwin-universal2"},
            )
            self.assertEqual(
                len({e["sha256"] for e in sdk_entries}),
                3,
                msg="each platform payload must keep its own sha",
            )

    def test_offline_skips_release_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_tree(root)
            (root / "evil-tool").mkdir()
            (root / "evil-tool" / "manifest.json").write_text(
                json.dumps(
                    [
                        {
                            "tool": "evil",
                            "owner": "evil",
                            "repo": "evil",
                            "tag": "v1.0.0",
                            "assets": {
                                "SHA256SUMS": {
                                    "url": "https://example.invalid/SHA256SUMS",
                                    "size": 1,
                                },
                                "evil-payload.zip": {
                                    "url": "https://example.invalid/evil-payload.zip",
                                    "size": 1,
                                },
                            },
                        }
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )

            index = bai.build_asset_index(
                root,
                repo_owner="zackees",
                repo_name="soldr-toolchain",
                branch="assets",
                offline=True,
            )
            evil_entries = [
                e
                for e in index["entries"]
                if e["owner"] == "evil" or e["asset"].startswith("evil-")
            ]
            self.assertEqual(evil_entries, [], msg=index)

    def test_entries_sorted_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_tree(root)
            # Add more blobs (out of name order) to provoke a sort.
            d = root / "zzz-tool" / "v1" / "linux-x64"
            d.mkdir(parents=True)
            (d / "z-asset.bin").write_bytes(b"zzz")
            d2 = root / "aaa-tool" / "v1" / "linux-x64"
            d2.mkdir(parents=True)
            (d2 / "a-asset.bin").write_bytes(b"aaa")

            index = bai.build_asset_index(
                root,
                repo_owner="zackees",
                repo_name="soldr-toolchain",
                branch="assets",
                offline=True,
            )
            keys = [
                (e["owner"], e["repo"], e["tag"], e["asset"], e["url"])
                for e in index["entries"]
            ]
            self.assertEqual(keys, sorted(keys))

    def test_release_entries_via_injected_sha256sums(self) -> None:
        """The non-offline path must convert SHA256SUMS into one entry
        per non-skipped asset. Inject a fake http_get_fn so the test
        doesn't depend on github.com reachability."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_dir = root / "real-tool"
            tool_dir.mkdir()
            (tool_dir / "manifest.json").write_text(
                json.dumps(
                    [
                        {
                            "tool": "real-tool",
                            "owner": "vendor",
                            "repo": "real-tool",
                            "tag": "v2.3.4",
                            "assets": {
                                "SHA256SUMS": {
                                    "url": "https://example.invalid/SHA256SUMS",
                                    "size": 1,
                                },
                                "real-tool-linux.tar.gz": {
                                    "url": "https://example.invalid/real-tool-linux.tar.gz",
                                    "size": 1,
                                },
                                "real-tool-windows.zip": {
                                    "url": "https://example.invalid/real-tool-windows.zip",
                                    "size": 1,
                                },
                            },
                        }
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )

            fake_sums = (
                ("a" * 64)
                + "  real-tool-linux.tar.gz\n"
                + ("b" * 64)
                + "  real-tool-windows.zip\n"
                + ("c" * 64)
                + "  install.sh\n"  # excluded
            )

            def fake_http_get(url: str) -> str:
                self.assertEqual(url, "https://example.invalid/SHA256SUMS")
                return fake_sums

            index = bai.build_asset_index(
                root,
                repo_owner="zackees",
                repo_name="soldr-toolchain",
                branch="assets",
                offline=False,
                http_get_fn=fake_http_get,
            )

            tool_entries = sorted(
                (e for e in index["entries"] if e["owner"] == "vendor"),
                key=lambda e: e["asset"],
            )
            self.assertEqual(len(tool_entries), 2, msg=index)
            self.assertEqual(tool_entries[0]["asset"], "real-tool-linux.tar.gz")
            self.assertEqual(tool_entries[0]["sha256"], "a" * 64)
            self.assertEqual(tool_entries[1]["asset"], "real-tool-windows.zip")
            self.assertEqual(tool_entries[1]["sha256"], "b" * 64)

    def test_cli_writes_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_tree(root)
            out = Path(tmp) / "out" / "asset-index.json"
            rc = bai.main(
                [
                    "--manifest-checkout",
                    str(root),
                    "--output",
                    str(out),
                    "--offline",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("entries", payload)
            self.assertEqual(
                payload["schema_version"],
                bai.ASSET_INDEX_SCHEMA_VERSION,
            )


class CliHelpTest(unittest.TestCase):

    def test_help_text_mentions_asset_index(self) -> None:
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                bai.main(["--help"])
            except SystemExit as e:
                self.assertEqual(e.code, 0)
        self.assertIn("asset-index.json", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
