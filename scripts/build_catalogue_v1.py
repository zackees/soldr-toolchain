#!/usr/bin/env python3
"""Convert ``asset-index.json`` (schema_version=5) to ``catalogue.v1.json``.

Reads the flat asset index produced by ``build_asset_index.py`` and
re-emits it under the v1 catalogue namespace defined in
``schemas/catalogue.v1.schema.json``. The shapes overlap heavily —
this is intentionally a re-host so the migration tracked in
`zackees/soldr#988 <https://github.com/zackees/soldr/issues/988>`_
Phase 2 is a wire-level swap, not a data rewrite.

Differences from the legacy v5 shape:

- ``schema_version: 1`` (v1 catalogue namespace, not v5 asset-index)
- adds top-level ``generated_at`` (ISO-8601 UTC) for diagnostic value
- adds top-level ``origin`` self-URL so cached copies prove what
  catalogue they came from
- entries field set is identical: ``owner, repo, tag, asset, url, sha256``
  (locally hosted platform bundles may repeat the four attribution fields;
  their URL is the unique identity)

Determinism: entries are emitted in the same order they appear in the
input asset-index. The producer (``build_asset_index.py``) sorts them
by ``(owner, repo, tag, asset, url)`` so the catalogue diff is reviewable.

The companion CI gate (``.github/workflows/catalogue-schema.yml``)
validates the output against ``schemas/catalogue.v1.schema.json``.

Usage::

    python scripts/build_catalogue_v1.py \\
        --asset-index ../soldr-toolchain-assets/asset-index.json \\
        --output ../soldr-toolchain-assets/catalogue.v1.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

CATALOGUE_SCHEMA_VERSION = 1
DEFAULT_ORIGIN = "https://zackees.github.io/soldr-toolchain/catalogue.v1.json"

# Fields copied straight from a v5 asset-index entry into a v1 catalogue
# entry. Anything else on an asset-index entry is silently dropped.
COPIED_ENTRY_FIELDS = ("owner", "repo", "tag", "asset", "url", "sha256")


def transform(asset_index: dict[str, Any], *, origin: str) -> dict[str, Any]:
    """Return a v1 catalogue payload built from a v5 asset-index payload.

    Caller-supplied ``origin`` lets the workflow override the default
    Pages URL (e.g. for a staging deploy).
    """
    raw_entries = asset_index.get("entries", [])
    if not isinstance(raw_entries, list):
        raise ValueError("asset-index.json `entries` must be a list")

    out_entries: list[dict[str, Any]] = []
    for i, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"asset-index.json `entries[{i}]` must be an object")
        out_entries.append({k: entry[k] for k in COPIED_ENTRY_FIELDS if k in entry})

    return {
        "schema_version": CATALOGUE_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "origin": origin,
        "entries": out_entries,
    }


def _now_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset-index",
        type=Path,
        required=True,
        help="Path to the v5 asset-index.json produced by build_asset_index.py.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write the v1 catalogue document to.",
    )
    parser.add_argument(
        "--origin",
        default=DEFAULT_ORIGIN,
        help=(
            "Self-URL stored in the catalogue's `origin` field. "
            f"Default: {DEFAULT_ORIGIN}"
        ),
    )
    args = parser.parse_args(argv)

    if not args.asset_index.is_file():
        sys.stderr.write(f"build_catalogue_v1.py: not a file: {args.asset_index}\n")
        return 2

    asset_index = json.loads(args.asset_index.read_text(encoding="utf-8"))
    try:
        catalogue = transform(asset_index, origin=args.origin)
    except ValueError as exc:
        sys.stderr.write(f"build_catalogue_v1.py: {exc}\n")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(catalogue, indent=2, sort_keys=False) + "\n"
    args.output.write_text(payload, encoding="utf-8")
    sys.stderr.write(
        f"catalogue: wrote {args.output} "
        f"({len(catalogue['entries'])} entries, "
        f"schema_version={catalogue['schema_version']})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
