#!/usr/bin/env python3
"""soldr#1064 phase B — batch wrapper around forge_to_catalogue.py.

Downloads every successful forge-conan run artifact for the syslib
matrix, parses the (tool, version, shape) tuple from the artifact name,
and runs forge_to_catalogue.py once per (run, shape) so the assets land
in the local assets-branch checkout.

The script is idempotent: forge_to_catalogue.py replaces existing
catalogue entries with the same `(url, sha256)`, and a re-run on the
same forge_run_id is a no-op past the first ingestion.

Usage:

    python scripts/batch_ingest_syslibs.py \\
        --assets-root /path/to/soldr-toolchain-checkout-on-assets-branch \\
        [--run-id <id>]  # restrict to one run, default: all successes
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_FORGE = "zackees/forge"
WORKFLOW = "forge-conan.yml"
HERE = Path(__file__).resolve().parent

# forge artifact name shape:
#   forge-<lib>-<shape>-<version>-<conan-os-arch>
# We can pull <lib> and <shape> from the artifact name; the
# version is also embedded.
#
# Long-form shapes (linux-x64-gnu / linux-arm64-musl / etc.) come
# first in the alternation so the regex prefers them over the
# bare `linux-x64` / `linux-arm64` forms that llvm-tools uses
# (recipe `recipes/llvm-tools-linux-x64` ships without a `-gnu`
# suffix). Regex alternation is left-to-right first-match, so this
# order is load-bearing for correctness on the musl/gnu shapes.
_ARTIFACT_RE = re.compile(
    r"^forge-(?P<tool>[a-z0-9-]+?)-(?P<shape>"
    r"windows-x64|windows-arm64|darwin-x64|darwin-arm64|"
    r"linux-x64-gnu|linux-arm64-gnu|linux-x64-musl|linux-arm64-musl|"
    r"linux-x64|linux-arm64"
    r")-(?P<version>[0-9.]+)-"
)


# soldr#1010 phase 3: tools whose `--shape <value>` differs from the
# shape captured by the artifact-name regex. llvm-tools ships under
# the bare `linux-x64` shape in the recipe directory, but the
# `forge_to_catalogue.py` TOOL_RECIPE_NAME table keys it as
# `linux-x64-gnu` for consistency with the other Linux x86_64
# entries. Re-key here before dispatching to forge_to_catalogue.py.
_TOOL_SHAPE_REMAP = {
    "llvm-tools": {
        "linux-x64": "linux-x64-gnu",
    },
}


def parse_artifact_name(name: str) -> dict | None:
    m = _ARTIFACT_RE.match(name)
    if not m:
        return None
    return m.groupdict()


def list_successful_runs(limit: int = 50) -> list[dict]:
    out = subprocess.check_output(
        [
            "gh",
            "run",
            "list",
            "--repo",
            REPO_FORGE,
            "--workflow",
            WORKFLOW,
            "--limit",
            str(limit),
            "--json",
            "status,conclusion,databaseId,createdAt",
        ]
    )
    runs = json.loads(out)
    runs.sort(key=lambda r: r["createdAt"], reverse=True)
    return [r for r in runs if r.get("conclusion") == "success"]


def list_artifacts(run_id: int) -> list[dict]:
    out = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/{REPO_FORGE}/actions/runs/{run_id}/artifacts",
        ]
    )
    data = json.loads(out)
    return data.get("artifacts", [])


def download_run(run_id: int, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            "gh",
            "run",
            "download",
            str(run_id),
            "--repo",
            REPO_FORGE,
            "--dir",
            str(dest),
        ]
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets-root", required=True)
    ap.add_argument("--run-id", type=int, default=None)
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    assets_root = Path(args.assets_root).resolve()
    if not assets_root.is_dir():
        print(f"ERROR: --assets-root {assets_root} not a directory", file=sys.stderr)
        return 1

    forge_to_cat = HERE / "forge_to_catalogue.py"
    if not forge_to_cat.is_file():
        print(f"ERROR: missing {forge_to_cat}", file=sys.stderr)
        return 1

    if args.run_id is not None:
        runs = [{"databaseId": args.run_id}]
    else:
        runs = list_successful_runs(args.limit)

    ingested = 0
    skipped = 0
    failed = 0
    for run in runs:
        run_id = run["databaseId"]
        artifacts = list_artifacts(run_id)
        if not artifacts:
            print(f"run {run_id}: no artifacts (build likely failed) — skip")
            skipped += 1
            continue
        parsed = parse_artifact_name(artifacts[0]["name"])
        if not parsed or parsed["tool"] not in {
            # syslibs (soldr#1064)
            "zstd",
            "sqlite",
            "jemalloc",
            "mimalloc",
            "zlib-ng",
            "lzma",
            "bzip2",
            # blessed-build tool families (soldr#1010 phase 2-3)
            "python",
            "nodelib",
            "openssl",
            "llvm-tools",
        }:
            print(
                f"run {run_id}: artifact {artifacts[0]['name']!r} not in the known tool set; skip"
            )
            skipped += 1
            continue

        # Re-key the shape if the tool has a recipe-name vs --shape
        # mismatch (llvm-tools is the only known case today).
        remap = _TOOL_SHAPE_REMAP.get(parsed["tool"], {})
        if parsed["shape"] in remap:
            print(
                f"run {run_id}: remapping shape {parsed['shape']!r} → "
                f"{remap[parsed['shape']]!r} for tool {parsed['tool']!r}"
            )
            parsed["shape"] = remap[parsed["shape"]]

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            try:
                download_run(run_id, tmp)
            except subprocess.CalledProcessError as e:
                print(f"run {run_id}: download failed: {e}")
                failed += 1
                continue

            cmd = [
                sys.executable,
                str(forge_to_cat),
                "--forge-dir",
                str(tmp),
                "--tool",
                parsed["tool"],
                "--version",
                parsed["version"],
                "--shape",
                parsed["shape"],
                "--forge-run-id",
                str(run_id),
                "--assets-root",
                str(assets_root),
            ]
            print(f"run {run_id}: ingesting {parsed['tool']} {parsed['version']} {parsed['shape']}")
            try:
                subprocess.check_call(cmd)
                ingested += 1
            except subprocess.CalledProcessError as e:
                print(f"run {run_id}: forge_to_catalogue.py failed: {e}")
                failed += 1

    print(f"---\nDone: ingested={ingested} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
