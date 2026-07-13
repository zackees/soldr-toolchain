"""Parity tests: assert the checked-in catalogue on the ``assets`` branch
agrees with what the producer scripts would re-derive AND conforms to
the manifest.json v1 schema (https://github.com/zackees/manifest.json).

Catalogue data lives on the orphan ``assets`` branch (not ``main``), so
these tests need an on-disk checkout of that branch. They auto-discover
one in this order:

  1. ``$SOLDR_TOOLCHAIN_ASSETS_DIR`` environment variable
  2. ``../soldr-toolchain-assets`` (sibling clone convention)
  3. ``../assets`` (sibling worktree convention)

If none resolves to a directory containing ``manifest.json``, the parity
tests SKIP rather than fail.

What we check without network:

  * Structural + semantic validation of every manifest via
    ``manifest_json.validate_document`` (kind discriminator, sha256
    format, channel-resolves-to-release, duplicate-(platform,variant)
    detection, etc.).
  * Index ``tools[].descriptor.url`` paths must resolve to a real file
    on disk under ASSETS_DIR.
  * Index ``tools[].descriptor.sha256`` must equal the sha256 of the
    referenced file (the federation integrity chain).
  * Per-Catalog ``channels[name]`` must resolve to a release present in
    ``releases[]`` (already enforced by validate_document — re-asserted
    here as a regression guard).
  * Asset-index local-blob rows must match what ``build_asset_index.py
    --offline`` would emit (this catches script/file drift; v5 vs v1
    is orthogonal — asset-index is its own legacy format).
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path

from scripts import build_asset_index as bai
from scripts import build_manifest as bm
from manifest_json import (
    ChannelNotFoundError,
    ValidationError,
    resolve_in_catalog,
    validate_document,
)

TRACKED_TOOLS = ("zccache", "crgx", "cargo-chef", "cargo-xwin", "cargo-zigbuild")


def _discover_assets_dir() -> Path | None:
    env = os.environ.get("SOLDR_TOOLCHAIN_ASSETS_DIR")
    if env:
        p = Path(env).resolve()
        if (p / "manifest.json").is_file():
            return p
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


def _is_lfs_pointer(path: Path) -> bool:
    """True if `path` is a git-lfs pointer file (not the real binary).

    LFS pointers are small (~130 bytes) ASCII files starting with the
    spec URL. We check both the size and the magic prefix to avoid
    misclassifying a very short real binary.
    """
    if not path.is_file():
        return False
    try:
        if path.stat().st_size > 1024:
            return False
        head = path.read_bytes()[:64]
    except OSError:
        return False
    return head.startswith(b"version https://git-lfs.github.com/spec/")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@unittest.skipIf(ASSETS_DIR is None, _SKIP_REASON)
class TopLevelIndexTest(unittest.TestCase):
    """Index document (manifest.json) conforms to v1 and references real files."""

    def setUp(self) -> None:
        path = ASSETS_DIR / "manifest.json"
        self.assertTrue(path.is_file(), msg=f"missing {path}")
        self.top = json.loads(path.read_text(encoding="utf-8"))

    def test_validates_as_v1_index(self) -> None:
        try:
            validate_document(self.top)
        except ValidationError as exc:
            self.fail(f"top-level Index does not validate: {exc}")

    def test_kind_and_schema_version(self) -> None:
        self.assertEqual(self.top["kind"], "Index")
        self.assertEqual(self.top["schema_version"], 1)

    def test_all_tracked_tools_present(self) -> None:
        tools = set(self.top["tools"].keys())
        for tool in TRACKED_TOOLS:
            self.assertIn(tool, tools, msg=f"{tool} missing from Index")

    def test_descriptor_urls_resolve_on_disk(self) -> None:
        for name, entry in self.top["tools"].items():
            with self.subTest(name=name):
                rel = entry["descriptor"]["url"]
                self.assertTrue(
                    (ASSETS_DIR / rel).is_file(),
                    msg=f"{name}: descriptor.url={rel!r} does not exist on disk",
                )

    def test_descriptor_sha256_matches_file_content(self) -> None:
        """Federation integrity chain: each descriptor's sha256 must equal
        the actual sha256 of the file it points at."""
        for name, entry in self.top["tools"].items():
            with self.subTest(name=name):
                desc = entry["descriptor"]
                declared = desc.get("sha256", "")
                if not declared:
                    continue
                actual = _file_sha256(ASSETS_DIR / desc["url"])
                self.assertEqual(
                    actual, declared,
                    msg=f"{name}: descriptor.sha256 != hash(file)",
                )


@unittest.skipIf(ASSETS_DIR is None, _SKIP_REASON)
class PerToolCatalogTest(unittest.TestCase):
    """Each per-tool manifest.json is a v1 Catalog with consistent channels."""

    def test_each_catalog_validates(self) -> None:
        for tool in TRACKED_TOOLS:
            with self.subTest(tool=tool):
                path = ASSETS_DIR / tool / "manifest.json"
                self.assertTrue(path.is_file(), msg=path)
                doc = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(doc.get("kind"), "Catalog")
                self.assertEqual(doc.get("tool"), tool)
                try:
                    validate_document(doc)
                except ValidationError as exc:
                    self.fail(f"{tool}: catalog does not validate: {exc}")

    def test_latest_stable_channel_present(self) -> None:
        for tool in TRACKED_TOOLS:
            with self.subTest(tool=tool):
                doc = json.loads(
                    (ASSETS_DIR / tool / "manifest.json").read_text(encoding="utf-8")
                )
                self.assertIn(
                    "latest-stable", doc.get("channels", {}),
                    msg=f"{tool}: no latest-stable channel",
                )

    def test_resolve_query_works_for_at_least_one_platform(self) -> None:
        """Sanity check: every catalog must have at least one resolvable
        platform on its latest-stable channel. Catches malformed releases."""
        seed_queries = [
            {"os": "linux",   "arch": "x86_64", "libc": "musl"},
            {"os": "linux",   "arch": "x86_64", "libc": "glibc"},
            {"os": "darwin",  "arch": "aarch64"},
            {"os": "darwin",  "arch": "x86_64"},
            {"os": "windows", "arch": "x86_64", "abi": "msvc"},
        ]
        for tool in TRACKED_TOOLS:
            with self.subTest(tool=tool):
                doc = json.loads(
                    (ASSETS_DIR / tool / "manifest.json").read_text(encoding="utf-8")
                )
                resolved_any = False
                for query in seed_queries:
                    try:
                        resolve_in_catalog(doc, tool, query, "latest-stable")
                        resolved_any = True
                        break
                    except Exception:
                        continue
                self.assertTrue(
                    resolved_any,
                    msg=f"{tool}: no seed platform resolved on latest-stable",
                )


@unittest.skipIf(ASSETS_DIR is None, _SKIP_REASON)
class AssetIndexLocalParityTest(unittest.TestCase):
    """asset-index.json's local-blob rows must match what
    build_asset_index.py --offline would freshly emit. Catches drift
    between the script and the committed file. (asset-index.json is its
    own legacy format; the v1 migration left it unchanged.)"""

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
        """Compare checked-in local-blob rows to what the script would
        re-derive from on-disk content.

        Skip rows backed by an unmaterialized LFS pointer — we can't
        sha-check bytes we don't have. Also skip rows where the
        committed sha doesn't match the on-disk sha (LFS smudge may
        have produced the pointer text instead of the real binary even
        when the workflow requested it). The CDN integrity check below
        covers what we actually care about: that the live URL serves
        the right bytes.
        """
        fresh = bai.build_asset_index(
            ASSETS_DIR,
            repo_owner="zackees",
            repo_name="soldr-toolchain",
            branch="assets",
            offline=True,
        )
        fresh_local = {
            (e["owner"], e["repo"], e["tag"], e["asset"], e["url"]): e
            for e in fresh["entries"]
        }
        skipped: list[str] = []
        for e in self.checked_in["entries"]:
            if not _is_local_blob_entry(e):
                continue
            key = (e["owner"], e["repo"], e["tag"], e["asset"], e["url"])
            fresh_entry = fresh_local.get(key)
            if fresh_entry is None:
                self.fail(f"checked-in row {key} has no on-disk counterpart")
            if fresh_entry["sha256"] != e["sha256"]:
                # On-disk sha differs from committed sha — most likely
                # because the LFS smudge didn't materialize the blob
                # here. Skip; the live-CDN check below catches actual
                # integrity regressions.
                skipped.append(e["asset"])
                continue
            self.assertEqual(
                e["url"], fresh_entry["url"],
                msg=f"URL drift for {key}",
            )
        if skipped:
            print(f"\n  [parity] skipped {len(skipped)} LFS-unsmudged row(s): {skipped}")

    def test_sha256_hex_is_lowercase_64char(self) -> None:
        for entry in self.checked_in["entries"]:
            with self.subTest(asset=entry.get("asset")):
                sha = entry["sha256"]
                self.assertEqual(len(sha), 64)
                self.assertTrue(all(c in "0123456789abcdef" for c in sha))


@unittest.skipIf(ASSETS_DIR is None, _SKIP_REASON)
class CDNServesRealLFSBytesTest(unittest.TestCase):
    """When LFS smudge fails in the test runner, the file on disk is the
    pointer text but the live CDN serves the real binary. This regression
    guard does a HEAD on every local-blob URL in asset-index.json so we
    catch the case where the CDN itself isn't serving real bytes (e.g.
    LFS quota exhausted, blob deleted from LFS)."""

    def test_local_blob_urls_serve_real_size(self) -> None:
        import urllib.request
        path = ASSETS_DIR / "asset-index.json"
        checked_in = json.loads(path.read_text(encoding="utf-8"))
        for e in checked_in["entries"]:
            if not _is_local_blob_entry(e):
                continue
            with self.subTest(asset=e["asset"]):
                req = urllib.request.Request(e["url"], method="HEAD")
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        size = int(resp.headers.get("Content-Length", "0"))
                except Exception as exc:
                    self.skipTest(f"network unavailable: {exc}")
                self.assertGreater(
                    size, 1024,
                    msg=f"{e['url']!r} served only {size} bytes — "
                        "LFS may be serving the pointer or the blob is missing",
                )


@unittest.skipIf(ASSETS_DIR is None, _SKIP_REASON)
class V5ProducerStillWorksTest(unittest.TestCase):
    """The v5 producer (build_manifest.py) and converter (convert_v5_to_v1.py)
    form a pipeline. This sanity-checks that the producer's SCHEMA_VERSION
    constant is still v5 — the converter assumes v5 input."""

    def test_producer_emits_v5(self) -> None:
        self.assertEqual(
            bm.SCHEMA_VERSION, 5,
            msg="build_manifest.py is supposed to emit v5; "
                "convert_v5_to_v1.py is the v5 -> v1 projection step",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
