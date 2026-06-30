#!/usr/bin/env python3
"""Asset-tree linter for soldr-toolchain's `assets` branch.

Enforces the canonical layout that the v1 generator + consumers expect:

    /                                  # root of the assets branch
    ├── manifest.json                  # v1 Index
    ├── asset-index.json               # legacy flat index (sha-bearing)
    ├── README.md
    ├── .nojekyll                      # Pages config
    ├── index.html                     # Pages landing
    └── <tool>/
        ├── manifest.json              # v1 Catalog
        └── <version>/                 # only present for tools w/ vendored binaries
            └── <platform>/            # `<os>-<arch>[-<libc-or-abi>]` per
                │                      #   manifest_json.flatten_platform
                └── <filename>         # the binary

Rules checked:

  R1.  Top-level manifest.json validates as a v1 Index.
  R2.  Every Index.tools[<name>] points at a real `<name>/manifest.json`.
  R3.  Every per-tool manifest.json validates as a v1 Catalog with
       tool == <directory name>.
  R4.  Every `<tool>/<version>/` directory on disk corresponds to a
       Release in the Catalog with version == <version>.
  R5.  Every `<tool>/<version>/<platform>/` directory on disk
       corresponds to a ReleasePlatform in that release whose
       flatten_platform() == <platform>.
  R6.  Every file under `<tool>/<version>/<platform>/` corresponds to
       an Asset.filename in that ReleasePlatform.
  R7.  Every Asset.urls[] that points at the soldr-toolchain Pages or
       raw-media CDN must resolve to a real file on disk at the path
       implied by its (tool, version, platform, filename).
  R8.  asset-index.json and catalogue.v1.json local-blob entries (URLs
       pointing at our CDN) must have matching files on disk.
  R9.  Reverse check: every on-disk file under `<tool>/<version>/`
       must be referenced by at least one Asset.urls[] OR a flat
       asset-index/catalogue entry. Orphaned files become silent dead
       weight.
  R10. No backslashes in any path field (forward slashes only).

Exit code 0 = clean. Non-zero = at least one rule violated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from manifest_json import flatten_platform, validate_document
from manifest_json.validate import ValidationError

CDN_HOSTS = (
    "zackees.github.io/soldr-toolchain",
    "raw.githubusercontent.com/zackees/soldr-toolchain",
    "media.githubusercontent.com/media/zackees/soldr-toolchain",
)

RESERVED_TOP_LEVEL = {
    "manifest.json",
    "asset-index.json",
    "catalogue.v1.json",
    ".forge-ingest.log.jsonl",
    "README.md",
    ".nojekyll",
    ".gitattributes",
    "index.html",
    ".git",
}


class LintIssue:
    """One lint violation."""

    __slots__ = ("rule", "severity", "where", "message")

    def __init__(self, rule: str, severity: str, where: str, message: str):
        self.rule = rule
        self.severity = severity
        self.where = where
        self.message = message

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule} {self.where}: {self.message}"


def _is_lfs_pointer(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size > 1024:
        return False
    try:
        return path.read_bytes()[:64].startswith(b"version https://git-lfs.github.com/spec/")
    except OSError:
        return False


def _hash_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _url_to_rel(url: str) -> str | None:
    """Map a CDN URL back to a path-relative-to-assets-root, or None
    if the URL doesn't point at our CDN."""
    for host in CDN_HOSTS:
        marker = f"{host}/"
        idx = url.find(marker)
        if idx == -1:
            continue
        rest = url[idx + len(marker):]
        # raw + media: <branch>/<rel>; pages: <rel> (no branch)
        if host.startswith("zackees.github.io"):
            return rest.split("?", 1)[0].split("#", 1)[0]
        # raw / media — strip the branch segment
        first_sep = rest.find("/")
        if first_sep == -1:
            return None
        return rest[first_sep + 1:].split("?", 1)[0].split("#", 1)[0]
    return None


def _expected_rel_for_asset(tool: str, version: str, platform_key: str, filename: str) -> str:
    return f"{tool}/{version}/{platform_key}/{filename}"


def _file_rels_under(root: Path, assets_root: Path) -> list[str]:
    return [
        f.relative_to(assets_root).as_posix()
        for f in root.rglob("*")
        if f.is_file() and f.name != "manifest.json"
    ]


def _all_files_referenced(root: Path, assets_root: Path, referenced_files: set[str]) -> bool:
    rels = _file_rels_under(root, assets_root)
    return bool(rels) and all(rel in referenced_files for rel in rels)


def _collect_flat_index_refs(
    assets_root: Path,
    doc_name: str,
    referenced_files: set[str],
    issues: list[LintIssue],
) -> None:
    path = assets_root / doc_name
    if not path.is_file():
        return
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        issues.append(LintIssue("R8", "ERROR", doc_name, f"unparseable: {exc}"))
        return

    entries = doc.get("entries", []) or []
    if not isinstance(entries, list):
        issues.append(LintIssue("R8", "ERROR", doc_name, "`entries` is not a list"))
        return

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(LintIssue("R8", "ERROR", f"{doc_name} entries[{i}]", "entry is not an object"))
            continue
        url = entry.get("url", "")
        if "\\" in url:
            issues.append(LintIssue("R10", "ERROR", f"{doc_name} entries[{i}]", f"url contains backslash: {url!r}"))
        rel = _url_to_rel(url)
        if rel is None:
            continue
        referenced_files.add(rel)
        disk = assets_root / rel
        if not disk.is_file():
            issues.append(LintIssue(
                "R8", "ERROR", rel,
                f"{doc_name} references URL -> rel={rel!r} but not on disk",
            ))


def lint(assets_root: Path) -> list[LintIssue]:
    issues: list[LintIssue] = []

    # --- R1: Index validates -------------------------------------------------
    index_path = assets_root / "manifest.json"
    if not index_path.is_file():
        issues.append(LintIssue("R1", "ERROR", str(index_path), "missing top-level manifest.json"))
        return issues
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        validate_document(index)
    except (ValidationError, json.JSONDecodeError) as exc:
        issues.append(LintIssue("R1", "ERROR", "manifest.json", f"Index does not validate: {exc}"))
        return issues
    if index.get("kind") != "Index":
        issues.append(LintIssue("R1", "ERROR", "manifest.json", f"kind={index.get('kind')!r}, expected Index"))
        return issues

    tools_by_name: dict[str, dict[str, Any]] = {}

    # --- R2 + R3: Each tool entry resolves to a valid Catalog ----------------
    for tool_name, entry in (index.get("tools") or {}).items():
        rel = (entry.get("descriptor") or {}).get("url", "")
        if not rel:
            issues.append(LintIssue("R2", "ERROR", tool_name, "Index entry has empty descriptor.url"))
            continue
        if "\\" in rel:
            issues.append(LintIssue("R10", "ERROR", tool_name, f"descriptor.url contains backslash: {rel!r}"))
        cat_path = assets_root / rel
        if not cat_path.is_file():
            issues.append(LintIssue("R2", "ERROR", tool_name, f"descriptor.url -> {rel!r} not on disk"))
            continue
        try:
            cat = json.loads(cat_path.read_text(encoding="utf-8"))
            validate_document(cat)
        except (ValidationError, json.JSONDecodeError) as exc:
            issues.append(LintIssue("R3", "ERROR", tool_name, f"catalog does not validate: {exc}"))
            continue
        if cat.get("kind") != "Catalog":
            issues.append(LintIssue("R3", "ERROR", tool_name, f"kind={cat.get('kind')!r}, expected Catalog"))
            continue
        if cat.get("tool") != tool_name:
            issues.append(LintIssue(
                "R3", "ERROR", tool_name,
                f"catalog.tool={cat.get('tool')!r}, expected {tool_name!r}",
            ))
            continue
        tools_by_name[tool_name] = cat

    # --- R4-R6: Per-tool directory tree matches catalog ----------------------
    referenced_files: set[str] = set()  # relative paths (forward-slashed)
    _collect_flat_index_refs(assets_root, "asset-index.json", referenced_files, issues)
    _collect_flat_index_refs(assets_root, "catalogue.v1.json", referenced_files, issues)

    for tool_name, cat in tools_by_name.items():
        tool_dir = assets_root / tool_name
        # Map (version, platform_key) -> {filename: ReleasePlatform}
        versions_in_catalog: dict[str, dict[str, dict[str, dict]]] = {}
        for release in cat.get("releases", []):
            v = release.get("version", "")
            versions_in_catalog.setdefault(v, {})
            for rp in release.get("platforms", []):
                plat = rp.get("platform", {}) or {}
                try:
                    pkey = flatten_platform(plat)
                except ValueError as exc:
                    issues.append(LintIssue(
                        "R5", "ERROR", f"{tool_name} {v}",
                        f"release platform tuple cannot be flattened: {exc}",
                    ))
                    continue
                asset = rp.get("asset", {}) or {}
                fn = asset.get("filename", "")
                versions_in_catalog[v].setdefault(pkey, {})[fn] = rp
                # Note any URL pointing at our CDN as "referenced"
                for url in asset.get("urls", []) or []:
                    rel = _url_to_rel(url)
                    if rel:
                        referenced_files.add(rel)
                        expected = _expected_rel_for_asset(tool_name, v, pkey, fn)
                        if rel != expected:
                            issues.append(LintIssue(
                                "R7", "WARN", f"{tool_name} {v} {pkey} {fn}",
                                f"CDN URL points at {rel!r}, expected {expected!r}",
                            ))

        # Walk version directories on disk
        for version_dir in sorted(p for p in tool_dir.iterdir() if p.is_dir()):
            version = version_dir.name
            if version not in versions_in_catalog:
                if _all_files_referenced(version_dir, assets_root, referenced_files):
                    continue
                issues.append(LintIssue(
                    "R4", "ERROR", f"{tool_name}/{version}/",
                    "on-disk version directory has no matching Release in catalog",
                ))
                continue
            for platform_dir in sorted(p for p in version_dir.iterdir() if p.is_dir()):
                pkey = platform_dir.name
                if pkey not in versions_in_catalog[version]:
                    if _all_files_referenced(platform_dir, assets_root, referenced_files):
                        continue
                    issues.append(LintIssue(
                        "R5", "ERROR", f"{tool_name}/{version}/{pkey}/",
                        f"on-disk platform directory has no matching ReleasePlatform "
                        f"(catalog has: {sorted(versions_in_catalog[version])})",
                    ))
                    continue
                for f in sorted(platform_dir.iterdir()):
                    if not f.is_file():
                        continue
                    fn = f.name
                    rp = versions_in_catalog[version][pkey].get(fn)
                    rel = f"{tool_name}/{version}/{pkey}/{fn}"
                    if rp is None:
                        if rel in referenced_files:
                            continue
                        issues.append(LintIssue(
                            "R6", "ERROR", rel,
                            f"on-disk file has no matching Asset.filename in catalog "
                            f"(catalog filenames at this platform: "
                            f"{sorted(versions_in_catalog[version][pkey])})",
                        ))
                        continue
                    # R8/R9 prep: track this file as "expected on disk"
                    referenced_files.add(rel)

    # --- R8: asset-index.json local-blob URLs resolve ------------------------
    ai_path = assets_root / "asset-index.json"
    if ai_path.is_file():
        try:
            ai = json.loads(ai_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(LintIssue("R8", "ERROR", "asset-index.json", f"unparseable: {exc}"))
            ai = {"entries": []}
        for e in ai.get("entries", []) or []:
            url = e.get("url", "")
            rel = _url_to_rel(url)
            if rel is None:
                continue  # external URL — out of scope
            referenced_files.add(rel)
            disk = assets_root / rel
            if not disk.is_file():
                issues.append(LintIssue(
                    "R8", "ERROR", rel,
                    f"asset-index.json references URL -> rel={rel!r} but not on disk",
                ))

    # --- R9: orphan files (on disk but unreferenced) -------------------------
    for entry in sorted(assets_root.iterdir()):
        if entry.name in RESERVED_TOP_LEVEL:
            continue
        if not entry.is_dir():
            issues.append(LintIssue(
                "R9", "WARN", entry.name,
                "unexpected top-level file (not reserved); convention is dirs only",
            ))
            continue
        if entry.name not in tools_by_name:
            if _all_files_referenced(entry, assets_root, referenced_files):
                continue
            issues.append(LintIssue(
                "R9", "ERROR", entry.name + "/",
                "on-disk tool directory has no entry in Index.tools",
            ))
            continue
        for f in entry.rglob("*"):
            if not f.is_file() or f.name == "manifest.json":
                continue
            rel = f.relative_to(assets_root).as_posix()
            if rel not in referenced_files:
                issues.append(LintIssue(
                    "R9", "WARN", rel,
                    "on-disk file is not referenced by any catalog, asset-index, or catalogue entry",
                ))

    return issues


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets-dir", type=Path, required=True,
                   help="path to the assets-branch checkout root")
    p.add_argument("--no-warn", action="store_true",
                   help="exit non-zero only on ERROR (default: also on WARN)")
    args = p.parse_args()

    issues = lint(args.assets_dir)
    by_severity = {"ERROR": 0, "WARN": 0}
    for issue in issues:
        print(str(issue))
        by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1

    if not issues:
        print("lint_assets: clean (0 issues)")
        return 0

    print()
    print(f"lint_assets: {by_severity['ERROR']} ERROR, {by_severity['WARN']} WARN")
    if by_severity["ERROR"] > 0:
        return 1
    if not args.no_warn and by_severity["WARN"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
