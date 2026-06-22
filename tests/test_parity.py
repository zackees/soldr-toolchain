"""Parity tests: assert the checked-in catalogue files agree with what
the producer scripts would re-derive from local state.

Catalogue data lives on the orphan ``assets`` branch (not ``main``),
so these tests need an on-disk checkout of that branch. They auto-
discover one in this order:

  1. ``$SOLDR_TOOLCHAIN_ASSETS_DIR`` environment variable
  2. ``../soldr-toolchain-assets`` (sibling clone — convention for
     local dev when ``main`` is at ``../soldr-toolchain``)
  3. ``../assets`` (sibling worktree convention)

If none of the above resolves to a directory containing
``manifest.json``, the parity tests SKIP rather than fail — the
pure-function tests in the other test files still run, and CI is
expected to provide one of the discovery paths.

What we CAN check without network:

  * ``asset-index.json``'s local-blob entries (sha256 + URL form)
    must match what ``build_asset_index.py --offline`` would produce.
  * ``manifest.json``'s top-level shape (schema_version, tool keys,
    apple-sdk pointer) must agree with the per-tool files on disk.
  * Per-tool ``manifest.json`` files must be flat arrays of release
    dicts sorted newest-first by published_at.
  * Schema versions must match the producer scripts' constants.

What we DO NOT check here (would require github.com):

  * GitHub-Releases-derived entries in asset-index.json — those depend
    on a live SHA256SUMS fetch.
  * Freshness of the catalogue against upstream — that's the job of
    the nightly refresh, not the test suite.

Run::

    SOLDR_TOOLCHAIN_ASSETS_DIR=../soldr-toolchain-assets \
        uv run --group dev pytest tests/test_parity.py -v
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import build_asset_index as bai
import build_manifest as bm

TRACKED_TOOLS = ("zccache", "crgx", "cargo-chef", "cargo-xwin", "cargo-zigbuild")


def _discover_assets_dir() -> Path | None:
    """Return a Path to a usable assets-branch checkout, or None."""
    env = os.environ.get("SOLDR_TOOLCHAIN_ASSETS_DIR")
    if env:
        p = Path(env).resolve()
        if (p / "manifest.json").is_file():
            return p
    # Sibling-clone convention: main is at <parent>/soldr-toolchain/,
    # assets is at <parent>/soldr-toolchain-assets/.
    here = Path(__file__).resolve().parents[1]
    for candidate in (
        here.parent / "soldr-toolchain-assets",
        here.parent / "assets",
    ):
        if (candidate / "manifest.json").is_file():
            return candidate.resolve()
    return None


ASSETS_DIR = _discover_assets_dir()
_SKIP_REASON = (
    "no assets-branch checkout found; set SOLDR_TOOLCHAIN_ASSETS_DIR or "
    "clone the assets branch as ../soldr-toolchain-assets"
)


def _is_local_blob_entry(entry: dict) -> bool:
    """A local-blob entry's URL points at the assets branch via
    media.githubusercontent.com. Release-derived entries point at
    github.com/.../releases/download/..."""
    url = entry.get("url", "")
    return "media.githubusercontent.com/media/" in url


@unittest.skipIf(ASSETS_DIR is None, _SKIP_REASON)
class TopLevelManifestParityTest(unittest.TestCase):

    def setUp(self) -> None:
        path = ASSETS_DIR / "manifest.json"
        self.assertTrue(path.is_file(), msg=f"missing {path}")
        self.top = json.loads(path.read_text(encoding="utf-8"))

    def test_schema_version_matches_producer_constant(self) -> None:
        self.assertEqual(self.top["schema_version"], bm.SCHEMA_VERSION)

    def test_all_tracked_tools_present(self) -> None:
        tools = set(self.top["tools"].keys())
        for tool in TRACKED_TOOLS:
            self.assertIn(tool, tools, msg=f"{tool} missing from manifest.json")

    def test_apple_sdk_pointer_exists_and_resolves(self) -> None:
        """The vendored apple-sdk entry must survive the nightly refresh
        (``preserve_vendored_top_level_entries`` guarantees this) and
        its ``path`` must resolve to a real file on disk."""
        tools = self.top["tools"]
        self.assertIn("apple-sdk", tools)
        path_ref = tools["apple-sdk"]["path"]
        self.assertTrue((ASSETS_DIR / path_ref).is_file(), msg=path_ref)

    def test_per_tool_paths_resolve(self) -> None:
        for name, entry in self.top["tools"].items():
            with self.subTest(name=name):
                self.assertTrue(
                    (ASSETS_DIR / entry["path"]).is_file(),
                    msg=f"missing per-tool file: {entry['path']}",
                )


@unittest.skipIf(ASSETS_DIR is None, _SKIP_REASON)
class PerToolManifestShapeTest(unittest.TestCase):
    """Each per-tool manifest.json must be a flat array of release
    dicts (v5 schema), sorted newest-first by published_at."""

    def test_each_tracked_tool_is_flat_array(self) -> None:
        for tool in TRACKED_TOOLS:
            with self.subTest(tool=tool):
                path = ASSETS_DIR / tool / "manifest.json"
                self.assertTrue(path.is_file(), msg=path)
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(payload, list,
                                      msg=f"{tool}: expected list, got {type(payload)}")
                self.assertGreater(len(payload), 0)
                for entry in payload[:3]:
                    self.assertIsInstance(entry, dict)
                    self.assertIn("tag", entry)
                    self.assertIn("platforms", entry)
                    self.assertIn("assets", entry)

    def test_entries_sorted_newest_first(self) -> None:
        for tool in TRACKED_TOOLS:
            with self.subTest(tool=tool):
                path = ASSETS_DIR / tool / "manifest.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                published = [e.get("published_at") or "" for e in payload]
                self.assertEqual(
                    published,
                    sorted(published, reverse=True),
                    msg=f"{tool}: per-tool entries must be sorted newest-first",
                )


@unittest.skipIf(ASSETS_DIR is None, _SKIP_REASON)
class AssetIndexLocalParityTest(unittest.TestCase):
    """The local-blob rows in the checked-in asset-index.json must
    exactly match what ``build_asset_index.py --offline`` would emit
    today.

    This catches drift between the script and the committed file: if
    someone edits the script's URL shape, sha algorithm, or attribution
    logic without re-running it, this test fires.
    """

    def setUp(self) -> None:
        path = ASSETS_DIR / "asset-index.json"
        self.assertTrue(path.is_file(), msg=f"missing {path}")
        self.checked_in = json.loads(path.read_text(encoding="utf-8"))

    def test_schema_version_matches(self) -> None:
        self.assertEqual(
            self.checked_in["schema_version"],
            bai.ASSET_INDEX_SCHEMA_VERSION,
        )

    def test_local_entries_match_freshly_derived(self) -> None:
        fresh = bai.build_asset_index(
            ASSETS_DIR,
            repo_owner="zackees",
            repo_name="soldr-toolchain",
            branch="assets",
            offline=True,
        )
        fresh_local = {
            (e["owner"], e["repo"], e["tag"], e["asset"]): e
            for e in fresh["entries"]
        }
        checked_local = {
            (e["owner"], e["repo"], e["tag"], e["asset"]): e
            for e in self.checked_in["entries"]
            if _is_local_blob_entry(e)
        }

        # Every local-blob entry in the checked-in file must match
        # what we'd freshly derive (same key set + same per-entry sha
        # + same URL). The checked-in file may carry additional rows
        # from GitHub releases that ``--offline`` can't reproduce —
        # those are filtered out by ``_is_local_blob_entry``.
        self.assertEqual(
            set(checked_local.keys()),
            set(fresh_local.keys()) & set(checked_local.keys()),
            msg="checked-in asset-index.json has local-blob entries the "
                "script wouldn't re-derive — likely a script-vs-file drift",
        )
        for key, checked in checked_local.items():
            fresh_entry = fresh_local[key]
            self.assertEqual(
                checked["sha256"], fresh_entry["sha256"],
                msg=f"sha drift for {key}: file={checked['sha256']!r} "
                    f"vs fresh={fresh_entry['sha256']!r}",
            )
            self.assertEqual(
                checked["url"], fresh_entry["url"],
                msg=f"URL drift for {key}: file={checked['url']!r} "
                    f"vs fresh={fresh_entry['url']!r}",
            )

    def test_sha256_hex_is_lowercase_64char(self) -> None:
        for entry in self.checked_in["entries"]:
            with self.subTest(asset=entry.get("asset")):
                sha = entry["sha256"]
                self.assertEqual(len(sha), 64)
                self.assertTrue(all(c in "0123456789abcdef" for c in sha))


@unittest.skipIf(ASSETS_DIR is None, _SKIP_REASON)
class TopLevelTracksMatchPerToolFilesTest(unittest.TestCase):
    """`tools[<name>].tracked_tags` in manifest.json must be the same
    set as the tags present in `<name>/manifest.json` — the producer
    derives the first from the second."""

    def test_tracked_tags_match_per_tool_array(self) -> None:
        top = json.loads((ASSETS_DIR / "manifest.json").read_text(encoding="utf-8"))
        for tool in TRACKED_TOOLS:
            with self.subTest(tool=tool):
                entry = top["tools"][tool]
                tracked = entry.get("tracked_tags") or []
                per_tool = json.loads(
                    (ASSETS_DIR / entry["path"]).read_text(encoding="utf-8")
                )
                per_tool_tags = [e.get("tag") for e in per_tool if e.get("tag")]
                self.assertEqual(
                    list(tracked), per_tool_tags,
                    msg=f"{tool}: top-level tracked_tags must equal "
                        f"per-tool file's tag order",
                )

    def test_latest_matches_first_per_tool_entry(self) -> None:
        top = json.loads((ASSETS_DIR / "manifest.json").read_text(encoding="utf-8"))
        for tool in TRACKED_TOOLS:
            with self.subTest(tool=tool):
                entry = top["tools"][tool]
                per_tool = json.loads(
                    (ASSETS_DIR / entry["path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(entry["latest"], per_tool[0]["tag"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
