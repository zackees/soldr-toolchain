"""Unit tests for ``tool_query.py``.

Pure-function tests over the alias maps and candidate-key builder.
The network-fetching path (``fetch_json``) is not exercised here.

Run::

    python3 -m unittest tests.test_tool_query -v
"""

from __future__ import annotations

import unittest

from scripts import tool_query as tq


class AliasMapsTest(unittest.TestCase):

    def test_os_aliases(self) -> None:
        self.assertEqual(tq.OS_ALIASES["mac"], "darwin")
        self.assertEqual(tq.OS_ALIASES["macos"], "darwin")
        self.assertEqual(tq.OS_ALIASES["darwin"], "darwin")
        self.assertEqual(tq.OS_ALIASES["windows"], "windows")
        self.assertEqual(tq.OS_ALIASES["win"], "windows")
        self.assertEqual(tq.OS_ALIASES["linux"], "linux")

    def test_arch_aliases(self) -> None:
        # "x86" is the user-facing short name for 64-bit Intel
        # (npm/Node.js convention), NOT 32-bit i686.
        for k in ("x86", "x64", "amd64", "x86_64"):
            self.assertEqual(tq.ARCH_ALIASES[k], "x64", msg=k)
        for k in ("arm", "arm64", "aarch64"):
            self.assertEqual(tq.ARCH_ALIASES[k], "arm64", msg=k)
        self.assertEqual(tq.ARCH_ALIASES["universal2"], "universal2")


class BuildCandidateKeysTest(unittest.TestCase):

    def test_explicit_extra_returns_single_key(self) -> None:
        self.assertEqual(
            tq.build_candidate_keys("linux", "x64", "musl"),
            ["linux-x64-musl"],
        )
        self.assertEqual(
            tq.build_candidate_keys("windows", "arm64", "gnullvm"),
            ["windows-arm64-gnullvm"],
        )

    def test_linux_default_chain(self) -> None:
        # gnu first, then musl, then unsuffixed.
        self.assertEqual(
            tq.build_candidate_keys("linux", "x64", None),
            ["linux-x64-gnu", "linux-x64-musl", "linux-x64"],
        )

    def test_windows_default_chain(self) -> None:
        # msvc is the mainstream ABI per soldr's "MSVC on Windows always" rule.
        self.assertEqual(
            tq.build_candidate_keys("windows", "x64", None),
            [
                "windows-x64-msvc",
                "windows-x64-gnu",
                "windows-x64-gnullvm",
                "windows-x64",
            ],
        )

    def test_darwin_default_chain(self) -> None:
        # Darwin typically ships an unsuffixed key — prefer that first.
        self.assertEqual(
            tq.build_candidate_keys("darwin", "arm64", None),
            ["darwin-arm64", "darwin-arm64-gnu"],
        )


class FindReleaseTest(unittest.TestCase):

    def test_latest_returns_first_entry(self) -> None:
        per_tool = [
            {"tag": "v3", "platforms": {}},
            {"tag": "v2", "platforms": {}},
            {"tag": "v1", "platforms": {}},
        ]
        self.assertEqual(tq.find_release(per_tool, "latest")["tag"], "v3")
        self.assertEqual(tq.find_release(per_tool, "")["tag"], "v3")

    def test_specific_tag_found(self) -> None:
        per_tool = [
            {"tag": "v3", "platforms": {}},
            {"tag": "v2", "platforms": {}},
        ]
        self.assertEqual(tq.find_release(per_tool, "v2")["tag"], "v2")

    def test_unknown_tag_raises_system_exit(self) -> None:
        per_tool = [{"tag": "v1", "platforms": {}}]
        with self.assertRaises(SystemExit) as ctx:
            tq.find_release(per_tool, "v99")
        self.assertIn("v99", str(ctx.exception))

    def test_empty_per_tool_raises(self) -> None:
        with self.assertRaises(SystemExit):
            tq.find_release([], "latest")


class V1CatalogTest(unittest.TestCase):
    def test_v1_resolver_returns_digest_and_urls(self) -> None:
        catalog = {
            "releases": [{"version": "0.9.140", "platforms": [{
                "platform": {"os": "linux", "arch": "aarch64", "libc": "musl"},
                "asset": {"filename": "nextest.tar.gz", "urls": ["https://cdn.example/nextest.tar.gz"], "sha256": "a" * 64, "size_bytes": 12},
            }]}]
        }
        old = tq.fetch_json
        try:
            tq.fetch_json = lambda _: catalog
            result = tq.resolve_v1({"tools": {"cargo-nextest": {"descriptor": {"url": "cargo-nextest/manifest.json"}}}}, "https://example/manifest.json", "cargo-nextest", "linux", "arm64", "musl", "0.9.140")
        finally:
            tq.fetch_json = old
        self.assertEqual(result["sha256"], "a" * 64)
        self.assertEqual(result["platform"], "linux-arm64-musl")


if __name__ == "__main__":
    unittest.main(verbosity=2)
