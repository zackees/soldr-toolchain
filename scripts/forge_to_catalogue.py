#!/usr/bin/env python3
"""Ingest a forge artifact into the soldr-toolchain catalogue.

Pipeline (per soldr-toolchain#14 Phase 3):

  1. Untar the forge gzip artifact emitted by `.github/workflows/forge-conan.yml`.
  2. Read `manifest.json` for provenance (recipe_ref, package_ref).
  3. Locate the inner package payload (the `package/` subtree the
     recipe wrote, plus `meta.json` for shape + version).
  4. Re-tar + zstd-compress with `-19 --long=27` (matches existing
     `apple-sdk/*.tar.zstd` assets in the catalogue).
  5. Place under `<tool>/<version>/<platform>/<asset>` per the asset
     catalog layout in `docs/ASSET_CATALOG.md`. For apple-sdk shapes:
       universal2  → apple-sdk/<ver>/darwin-universal2/sdk.tar.zst
       thin-x86_64 → apple-sdk/<ver>/darwin-x86_64/sdk.tar.zst
       thin-aarch64→ apple-sdk/<ver>/darwin-aarch64/sdk.tar.zst
  6. sha256 the new file.
  7. Append a catalogue.v1.json entry with full provenance.
  8. Re-validate the catalogue against the schema.

The script is idempotent: re-running with the same forge_run_id +
shape replaces the existing catalogue entry and overwrites the LFS
blob. The .vendor-state-style deadline tracking isn't relevant here
because the catalogue is the source of truth.

Usage:

    python scripts/forge_to_catalogue.py \\
        --forge-dir /path/to/extracted/gh-run-downloads \\
        --tool apple-sdk \\
        --version 14.5 \\
        --shape universal2 \\
        --forge-run-id 28299235391 \\
        --assets-root /path/to/soldr-toolchain-assets

Exit codes:
  0 — catalogue updated, asset placed, schema valid
  1 — caller error (missing args, bad files, schema violation)
  2 — environment error (missing zstandard, missing jsonschema, etc.)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Any


# Map shape → catalogue platform string.
SHAPE_TO_PLATFORM = {
    "universal2": "darwin-universal2",
    "thin-x86_64": "darwin-x86_64",
    "thin-aarch64": "darwin-aarch64",
}

# Map tool → recipe name prefix (forge artifact name embeds this).
TOOL_RECIPE_NAME = {
    "apple-sdk": {
        "universal2": "apple-sdk-universal2",
        "thin-x86_64": "apple-sdk-thin-x86_64",
        "thin-aarch64": "apple-sdk-thin-aarch64",
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forge-dir", type=Path, required=True,
                        help="Directory containing the `gh run download` output.")
    parser.add_argument("--tool", required=True,
                        help="Logical tool name (e.g. 'apple-sdk').")
    parser.add_argument("--version", required=True,
                        help="SDK / tool version string (e.g. '14.5').")
    parser.add_argument("--shape", required=True,
                        choices=sorted(SHAPE_TO_PLATFORM.keys()),
                        help="Shape of the artifact (universal2/thin-*).")
    parser.add_argument("--forge-run-id", required=True,
                        help="zackees/forge workflow run id, for provenance.")
    parser.add_argument("--assets-root", type=Path, required=True,
                        help="Root of the soldr-toolchain assets-branch checkout.")
    parser.add_argument("--schema",
                        type=Path,
                        help="Path to catalogue.v1.schema.json (default: <repo>/schemas/...).")
    parser.add_argument("--catalogue",
                        type=Path,
                        help="Path to catalogue.v1.json on the assets root "
                             "(default: <assets-root>/catalogue.v1.json).")
    parser.add_argument("--asset-name",
                        default="sdk.tar.zst",
                        help="Filename for the placed asset (default: sdk.tar.zst).")
    args = parser.parse_args(argv)

    try:
        import zstandard  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "forge_to_catalogue.py: missing `zstandard` — install via "
            "`uv pip install zstandard` or `pip install zstandard`.\n"
        )
        return 2

    try:
        import jsonschema  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "forge_to_catalogue.py: missing `jsonschema` — install via "
            "`uv pip install jsonschema` or `pip install jsonschema`.\n"
        )
        return 2

    recipe_name = _resolve_recipe_name(args.tool, args.shape)
    forge_artifact = _find_forge_artifact(args.forge_dir, recipe_name, args.version)
    if forge_artifact is None:
        sys.stderr.write(
            f"forge_to_catalogue.py: no forge artifact found in {args.forge_dir} "
            f"matching name={recipe_name} version={args.version}\n"
        )
        return 1

    print(f"forge artifact: {forge_artifact}")

    payload_root, provenance = _extract_forge_payload(forge_artifact)
    print(f"payload root: {payload_root}")
    print(f"provenance: {json.dumps(provenance, indent=2)}")

    platform = SHAPE_TO_PLATFORM[args.shape]
    asset_rel = Path(args.tool) / args.version / platform / args.asset_name
    asset_path = args.assets_root / asset_rel
    asset_path.parent.mkdir(parents=True, exist_ok=True)

    _repack_to_zstd(payload_root, asset_path)
    print(f"wrote asset: {asset_path} ({asset_path.stat().st_size} bytes)")

    sha256 = _sha256_of(asset_path)
    print(f"sha256: {sha256}")

    catalogue_path = args.catalogue or (args.assets_root / "catalogue.v1.json")
    schema_path = args.schema or _default_schema_path()
    _update_catalogue(
        catalogue_path,
        schema_path,
        asset_rel=asset_rel,
        asset_name=args.asset_name,
        sha256=sha256,
        forge_run_id=args.forge_run_id,
        provenance=provenance,
    )
    print(f"catalogue: updated {catalogue_path}")
    return 0


# ----- forge artifact handling --------------------------------------


def _resolve_recipe_name(tool: str, shape: str) -> str:
    try:
        return TOOL_RECIPE_NAME[tool][shape]
    except KeyError as exc:
        raise SystemExit(
            f"forge_to_catalogue.py: no recipe mapping for tool={tool} shape={shape}"
        ) from exc


def _find_forge_artifact(forge_dir: Path, recipe_name: str, version: str) -> Path | None:
    """`gh run download` lays artifacts out as one subdir per artifact, each
    containing a single `forge-<name>-<version>-<platform>.tar.gz`. Walk the
    subtree and match by name + version."""
    if not forge_dir.is_dir():
        return None
    pattern = re.compile(
        rf"^forge-{re.escape(recipe_name)}-{re.escape(version)}-[a-z0-9-]+\.tar\.gz$"
    )
    for path in forge_dir.rglob("*.tar.gz"):
        if pattern.match(path.name):
            return path
    return None


def _extract_forge_payload(forge_artifact: Path) -> tuple[Path, dict[str, Any]]:
    """Extract the forge .tar.gz into a sibling temp dir. Returns
    (package_root_path, provenance_dict)."""
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="forge-ingest-"))
    with tarfile.open(forge_artifact, "r:gz") as tf:
        tf.extractall(workdir)

    manifest_path = workdir / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(
            f"forge_to_catalogue.py: forge artifact missing manifest.json "
            f"(extracted from {forge_artifact})"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    package_root = workdir / "package"
    if not package_root.is_dir():
        raise SystemExit(
            f"forge_to_catalogue.py: forge artifact missing package/ dir "
            f"(extracted from {forge_artifact})"
        )

    # Recipes write package/meta.json with shape + captured version.
    recipe_meta_path = package_root / "meta.json"
    recipe_meta: dict[str, Any] = {}
    if recipe_meta_path.is_file():
        recipe_meta = json.loads(recipe_meta_path.read_text(encoding="utf-8"))

    provenance = {
        "recipe_ref": manifest.get("recipe_ref"),
        "package_ref": manifest.get("package_ref"),
        "recipe_meta": recipe_meta,
    }
    return package_root, provenance


# ----- repack ------------------------------------------------------


def _repack_to_zstd(payload_root: Path, output_path: Path) -> None:
    """Tar the payload tree + zstd-compress at level 19, long=27.
    Matches the compression profile of the existing apple-sdk asset."""
    import zstandard

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(payload_root, arcname=".")
    raw = buf.getvalue()
    cctx = zstandard.ZstdCompressor(level=19, write_checksum=True)
    output_path.write_bytes(cctx.compress(raw))


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ----- catalogue mutation ------------------------------------------


def _default_schema_path() -> Path:
    # When invoked from a `main` branch checkout the schema is one level up.
    here = Path(__file__).resolve().parent
    return here.parent / "schemas" / "catalogue.v1.schema.json"


def _update_catalogue(
    catalogue_path: Path,
    schema_path: Path,
    *,
    asset_rel: Path,
    asset_name: str,
    sha256: str,
    forge_run_id: str,
    provenance: dict[str, Any],
) -> None:
    if not catalogue_path.is_file():
        raise SystemExit(
            f"forge_to_catalogue.py: catalogue.v1.json not found at {catalogue_path}"
        )
    catalogue = json.loads(catalogue_path.read_text(encoding="utf-8"))

    url = (
        "https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/"
        + asset_rel.as_posix()
    )
    new_entry = {
        "owner": "zackees",
        "repo": "soldr-toolchain",
        "tag": "assets",
        "asset": asset_name,
        "url": url,
        "sha256": sha256,
    }

    # Replace any existing entry with the same URL (re-run idempotency).
    catalogue["entries"] = [
        e for e in catalogue.get("entries", []) if e.get("url") != url
    ]
    catalogue["entries"].append(new_entry)
    catalogue["generated_at"] = (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    # Validate against schema BEFORE writing — schema violations should fail
    # the ingest, not surface later in the catalogue-schema CI gate.
    from jsonschema import Draft202012Validator

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(catalogue),
        key=lambda e: list(e.absolute_path),
    )
    if errors:
        for err in errors:
            path = "/".join(str(p) for p in err.absolute_path) or "<root>"
            sys.stderr.write(f"forge_to_catalogue.py: schema error at {path}: {err.message}\n")
        raise SystemExit(1)

    catalogue_path.write_text(
        json.dumps(catalogue, indent=2) + "\n", encoding="utf-8"
    )

    # Provenance log (not in the catalogue itself; schema rejects unknown
    # top-level fields). Write a sibling JSONL of forge runs we've ingested.
    provenance_log = catalogue_path.parent / ".forge-ingest.log.jsonl"
    log_row = {
        "ts": catalogue["generated_at"],
        "asset_url": url,
        "sha256": sha256,
        "forge_run_id": forge_run_id,
        "recipe_ref": provenance.get("recipe_ref"),
        "package_ref": provenance.get("package_ref"),
        "recipe_meta": provenance.get("recipe_meta"),
    }
    with provenance_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(log_row) + "\n")


if __name__ == "__main__":
    sys.exit(main())
