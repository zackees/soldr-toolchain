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

External / curated entries
---------------------------

Some tools are discoverable in the public catalogue but their BYTES are
not — e.g. a proprietary redistribution (MSVC) hosted as a private
GitHub release asset. Those entries can't come from
``build_asset_index.py`` (it only enumerates *public* release
inventories) and they have no on-disk blob under the assets tree for a
producer to walk. Instead they're curated by hand in
``external-entries.v1.json`` on ``main`` (same shape as this script's
output: ``{"schema_version": 1, "entries": [...]}]``) and merged in
here via ``--external-entries``. Because the merge happens inside this
generator, curated entries survive the nightly regeneration by
construction instead of being clobbered as a hand-edit to the
generated ``assets/catalogue.v1.json``. See ``docs/ASSET_CATALOG.md``
for the full rationale and the consumer auth contract for private
assets.

Determinism: entries are emitted in the same order they appear in the
input asset-index. The producer (``build_asset_index.py``) sorts them
by ``(owner, repo, tag, asset, url)`` so the catalogue diff is reviewable.

The companion CI gate (``.github/workflows/catalogue-schema.yml``)
validates the output against ``schemas/catalogue.v1.schema.json``.

Usage::

    python scripts/build_catalogue_v1.py \\
        --asset-index ../soldr-toolchain-assets/asset-index.json \\
        --output ../soldr-toolchain-assets/catalogue.v1.json \\
        --external-entries external-entries.v1.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

try:
    # Normal case: invoked as `python -m scripts.build_catalogue_v1` (or
    # with the repo root already on PYTHONPATH), so `scripts` resolves as
    # a package.
    from scripts.validate_catalogue import DEFAULT_SCHEMA_PATH, iter_schema_errors
except ImportError:
    # refresh-manifest.yml invokes this file directly
    # (`python3 main/scripts/build_catalogue_v1.py`) with no PYTHONPATH
    # set, so `scripts` isn't importable as a package from there. Rather
    # than mutate `sys.path` (forbidden by
    # scripts/lint_python_import_paths.py — repo tooling is a package
    # rooted at the checkout), load the sibling module directly by path,
    # exactly the "explicit importlib loading" escape hatch that
    # linter's docstring calls out for Conan-style entrypoints.
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "_validate_catalogue_standalone",
        Path(__file__).resolve().parent / "validate_catalogue.py",
    )
    assert _spec is not None and _spec.loader is not None
    _validate_catalogue = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_validate_catalogue)
    DEFAULT_SCHEMA_PATH = _validate_catalogue.DEFAULT_SCHEMA_PATH
    iter_schema_errors = _validate_catalogue.iter_schema_errors

CATALOGUE_SCHEMA_VERSION = 1
DEFAULT_ORIGIN = "https://zackees.github.io/soldr-toolchain/catalogue.v1.json"

# Fields copied straight from a v5 asset-index entry into a v1 catalogue
# entry. Anything else on an asset-index entry is silently dropped.
COPIED_ENTRY_FIELDS = ("owner", "repo", "tag", "asset", "url", "sha256")
def is_direct_compatible_url(url: object) -> bool:
    """Return whether a URL can safely be exposed to a capability-1 client.

    v1 has no transport discriminator, immutable publication binding, or parts
    array. Repository/LFS paths therefore belong only in verified v2.
    """
    if not isinstance(url, str):
        return False
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if parsed.hostname == "media.githubusercontent.com":
        return False
    segments = [segment for segment in parsed.path.split("/") if segment]
    return not (
        parsed.hostname == "raw.githubusercontent.com"
        and segments[:3] == ["zackees", "soldr-toolchain", "assets"]
    )


def transform(
    asset_index: dict[str, Any],
    *,
    origin: str,
    external_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a v1 catalogue payload built from a v5 asset-index payload.

    Caller-supplied ``origin`` lets the workflow override the default
    Pages URL (e.g. for a staging deploy). ``external_entries`` (already
    loaded/validated via :func:`load_external_entries`) are appended
    after the generated entries; a duplicate ``url`` between the two
    sets raises ``ValueError`` rather than silently emitting a
    duplicate row.
    """
    raw_entries = asset_index.get("entries", [])
    if not isinstance(raw_entries, list):
        raise ValueError("asset-index.json `entries` must be a list")

    out_entries: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for i, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"asset-index.json `entries[{i}]` must be an object")
        copied = {k: entry[k] for k in COPIED_ENTRY_FIELDS if k in entry}
        url = copied.get("url")
        if not is_direct_compatible_url(url):
            # Never offer a v1 bypass around verified v2 multipart control.
            continue
        seen_urls.add(url)
        out_entries.append(copied)

    for i, entry in enumerate(external_entries or []):
        url = entry.get("url")
        if not isinstance(url, str):
            raise ValueError(f"external-entries.json entries[{i}] missing `url`")
        if not is_direct_compatible_url(url):
            raise ValueError(
                f"external-entries.json entries[{i}] is not a direct-compatible URL: {url}"
            )
        if url in seen_urls:
            raise ValueError(
                f"external-entries.json entries[{i}] duplicates an existing "
                f"catalogue entry url (refusing to emit a duplicate row): {url}"
            )
        seen_urls.add(url)
        out_entries.append(entry)

    return {
        "schema_version": CATALOGUE_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "origin": origin,
        "entries": out_entries,
    }


def load_external_entries(path: Path) -> list[dict[str, Any]]:
    """Load + validate curated entries from an external-entries.v1.json.

    The document uses the same top-level shape as this script's output
    (``{"schema_version": 1, "entries": [...]}]``). This function
    enforces, ahead of the schema check, that every entry carries all
    of ``COPIED_ENTRY_FIELDS`` and drops any unexpected extra fields
    (mirroring the asset-index round-trip above); it then runs the
    *filtered* document through the exact same jsonschema validation
    routine (:func:`scripts.validate_catalogue.iter_schema_errors`
    against ``schemas/catalogue.v1.schema.json``) that
    ``scripts/validate_catalogue.py``'s CLI and the
    ``catalogue-schema.yml`` CI gate use. That means a wrong-length
    ``sha256`` or a malformed ``url`` fails loudly right here, at
    generation time, not only in the separate CI gate.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    raw_entries = doc.get("entries", [])
    if not isinstance(raw_entries, list):
        raise ValueError(f"{path}: `entries` must be a list")

    out: list[dict[str, Any]] = []
    for i, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: entries[{i}] must be an object")
        missing = [f for f in COPIED_ENTRY_FIELDS if f not in entry]
        if missing:
            raise ValueError(
                f"{path}: entries[{i}] missing required field(s): {missing}"
            )
        out.append({k: entry[k] for k in COPIED_ENTRY_FIELDS})

    schema = json.loads(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    filtered_doc = {"schema_version": CATALOGUE_SCHEMA_VERSION, "entries": out}
    errors = iter_schema_errors(filtered_doc, schema)
    if errors:
        detail = "; ".join(
            f"at {'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
            for err in errors
        )
        raise ValueError(
            f"{path}: {len(errors)} schema violation(s) against "
            f"catalogue.v1.schema.json: {detail}"
        )

    return out


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
    parser.add_argument(
        "--external-entries",
        type=Path,
        default=None,
        help=(
            "Optional path to external-entries.v1.json — curated entries "
            "(e.g. private-release tools like msvc) merged into the "
            "output verbatim. See docs/ASSET_CATALOG.md."
        ),
    )
    args = parser.parse_args(argv)

    if not args.asset_index.is_file():
        sys.stderr.write(f"build_catalogue_v1.py: not a file: {args.asset_index}\n")
        return 2

    external_entries: list[dict[str, Any]] | None = None
    if args.external_entries is not None:
        if not args.external_entries.is_file():
            sys.stderr.write(
                f"build_catalogue_v1.py: not a file: {args.external_entries}\n"
            )
            return 2
        try:
            external_entries = load_external_entries(args.external_entries)
        except ValueError as exc:
            sys.stderr.write(f"build_catalogue_v1.py: {exc}\n")
            return 1

    asset_index = json.loads(args.asset_index.read_text(encoding="utf-8"))
    try:
        catalogue = transform(
            asset_index, origin=args.origin, external_entries=external_entries
        )
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
