#!/usr/bin/env python3
"""Validate a catalogue.v1.json document against schemas/catalogue.v1.schema.json.

CI gate for soldr#988 Phase 1. The runtime resolver in soldr
(`crates/soldr-cli/src/fetch/manifest_lookup.rs`) will read this
document from the toolchain origin once the migration lands; the
schema is the contract that keeps producers + consumers in sync.

Usage:
  python scripts/validate_catalogue.py PATH_TO_CATALOGUE.json
                                       [--schema PATH_TO_SCHEMA.json]

Exit codes:
  0  document validates
  1  document violates schema (errors printed to stderr)
  2  script could not run (file not found, dep missing, etc.)

Dependency: `jsonschema` (pulled in via `uv run --with jsonschema`
in the CI workflow so the repo's pyproject doesn't grow a runtime
dep just for validation).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "catalogue.v1.schema.json"
)


def iter_schema_errors(document: dict[str, Any], schema: dict[str, Any]) -> list[Any]:
    """Return the sorted list of jsonschema ``ValidationError``s for ``document``
    against ``schema`` (Draft 2020-12). Empty list means the document is valid.

    This is the single validation routine for the ``catalogue.v1.json`` shape —
    both this script's CLI and ``scripts/build_catalogue_v1.py``'s
    ``load_external_entries()`` call it, so a wrong-length ``sha256`` or a
    malformed ``url`` is caught the same way in both places rather than by
    two independently-maintained checks.
    """
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    return sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "document",
        type=Path,
        help="Path to a catalogue.v1.json document to validate.",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help="Path to the schema file (default: schemas/catalogue.v1.schema.json next to scripts/).",
    )
    args = parser.parse_args()

    if not args.document.is_file():
        sys.stderr.write(f"validate_catalogue.py: not a file: {args.document}\n")
        return 2
    if not args.schema.is_file():
        sys.stderr.write(f"validate_catalogue.py: schema missing: {args.schema}\n")
        return 2

    try:
        import jsonschema  # noqa: F401 — proves import works
    except ImportError:
        sys.stderr.write(
            "validate_catalogue.py: missing `jsonschema` — install via "
            "`uv pip install jsonschema` or `pip install jsonschema`.\n"
        )
        return 2

    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    document = json.loads(args.document.read_text(encoding="utf-8"))

    errors = iter_schema_errors(document, schema)
    if errors:
        sys.stderr.write(
            f"validate_catalogue.py: {len(errors)} schema violation(s) in "
            f"{args.document}:\n"
        )
        for err in errors:
            path = "/".join(str(p) for p in err.absolute_path) or "<root>"
            sys.stderr.write(f"  at {path}: {err.message}\n")
        return 1

    n_entries = len(document.get("entries", []))
    sys.stdout.write(
        f"validate_catalogue.py: {args.document} OK "
        f"({n_entries} entries, schema v{document.get('schema_version')})\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
