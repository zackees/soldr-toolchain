#!/usr/bin/env python3
"""Incrementally publish Rust nightly identities and a catalogue asset row.

The producer downloads only newly observed minimal nightly toolchains, runs
``rustc -vV`` once, and preserves the result forever. It never builds Rust or
any consumer project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date as CalendarDate
from datetime import timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CURRENT_MANIFEST_URL = "https://static.rust-lang.org/dist/channel-rust-nightly.toml"
PAGES_URL = (
    "https://zackees.github.io/soldr-toolchain/rust-nightly-versions.v1.json"
)
ASSET_NAME = "rust-nightly-versions.v1.json"
USER_AGENT = "soldr-toolchain-nightly-version-map"
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def verify_manifest(
    manifest: bytes, checksum_payload: bytes, *, source_url: str
) -> str:
    checksum_text = checksum_payload.decode("utf-8", errors="strict")
    match = _SHA256_RE.search(checksum_text)
    if match is None:
        raise ValueError(f"{source_url}.sha256 contains no SHA-256 digest")
    expected = match.group(1).lower()
    actual = hashlib.sha256(manifest).hexdigest()
    if actual != expected:
        raise ValueError(
            f"{source_url} SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return actual


def fetch_verified_manifest(url: str) -> tuple[bytes, str]:
    manifest = fetch_bytes(url)
    checksum = fetch_bytes(f"{url}.sha256")
    return manifest, verify_manifest(manifest, checksum, source_url=url)


def parse_manifest_date(manifest: bytes, *, source_url: str) -> str:
    for raw_line in manifest.decode("utf-8", errors="strict").splitlines():
        line = raw_line.strip()
        if line.startswith("date") and "=" in line:
            value = line.split("=", 1)[1].strip().strip('"')
            if _DATE_RE.fullmatch(value):
                return value
    raise ValueError(f"{source_url} has no valid top-level date")


def dated_manifest_url(date: str) -> str:
    if not _DATE_RE.fullmatch(date):
        raise ValueError(f"invalid nightly date: {date!r}")
    return f"https://static.rust-lang.org/dist/{date}/channel-rust-nightly.toml"


def parse_rustc_verbose(output: str, *, channel: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    first_line = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not first_line:
            first_line = line
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()

    release = fields.get("release", "")
    commit_hash = fields.get("commit-hash", "")
    commit_date = fields.get("commit-date", "")
    host = fields.get("host", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+-nightly", release):
        raise ValueError(f"{channel} reported malformed rustc release: {release!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit_hash):
        raise ValueError(f"{channel} reported malformed rustc commit: {commit_hash!r}")
    if not _DATE_RE.fullmatch(commit_date):
        raise ValueError(
            f"{channel} reported malformed rustc commit date: {commit_date!r}"
        )
    if not host:
        raise ValueError(f"{channel} reported no rustc host")

    major, minor, _patch_and_suffix = release.split(".", 2)
    return {
        "rust_version": f"{major}.{minor}",
        "rustc_release": release,
        "rustc_version": first_line,
        "rustc_commit_hash": commit_hash,
        "rustc_commit_date": commit_date,
        "rustc_host": host,
    }


def probe_nightly(channel: str) -> dict[str, str]:
    # A fresh private rustup home per probe guarantees a bounded disk
    # footprint: the minimal nightly disappears as soon as rustc -vV returns.
    with tempfile.TemporaryDirectory(prefix="soldr-nightly-probe-") as rustup_home:
        probe_env = os.environ.copy()
        probe_env["RUSTUP_HOME"] = rustup_home
        subprocess.run(
            [
                "rustup",
                "toolchain",
                "install",
                channel,
                "--profile",
                "minimal",
                "--no-self-update",
            ],
            check=True,
            env=probe_env,
        )
        completed = subprocess.run(
            ["rustup", "run", channel, "rustc", "-vV"],
            check=True,
            capture_output=True,
            text=True,
            env=probe_env,
        )
        return parse_rustc_verbose(completed.stdout, channel=channel)


def load_map(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "source": CURRENT_MANIFEST_URL,
            "nightlies": {},
            "unavailable_dates": [],
            "versions": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{path} has unsupported schema_version "
            f"{payload.get('schema_version')!r}"
        )
    if not isinstance(payload.get("nightlies"), dict):
        raise ValueError(f"{path} nightlies must be an object")
    if not isinstance(payload.get("unavailable_dates", []), list):
        raise ValueError(f"{path} unavailable_dates must be an array")
    payload.setdefault("unavailable_dates", [])
    return payload


def ensure_nightly(
    payload: dict[str, Any],
    date: str,
    *,
    probe: Callable[[str], dict[str, str]] = probe_nightly,
) -> bool:
    """Probe a nightly only when its immutable row is not already present."""

    channel = f"nightly-{date}"
    if channel in payload["nightlies"]:
        return False

    url = dated_manifest_url(date)
    manifest, manifest_sha256 = fetch_verified_manifest(url)
    declared_date = parse_manifest_date(manifest, source_url=url)
    if declared_date != date:
        raise ValueError(
            f"{url} declares date {declared_date}, expected {date}"
        )
    observed = probe(channel)
    payload["nightlies"][channel] = {
        "manifest_date": date,
        **observed,
        "manifest_url": url,
        "manifest_sha256": manifest_sha256,
    }
    return True


def rebuild_versions(payload: dict[str, Any]) -> None:
    grouped: dict[str, list[str]] = {}
    for channel, identity in payload["nightlies"].items():
        version = identity.get("rust_version")
        if not isinstance(version, str) or not version:
            raise ValueError(f"{channel} has no rust_version")
        grouped.setdefault(version, []).append(channel)

    payload["versions"] = {}
    for version in sorted(grouped):
        nightlies = sorted(grouped[version], reverse=True)
        payload["versions"][version] = {
            "nightlies": nightlies,
            "selected": nightlies[0],
        }


def date_range(start: str, end: str) -> list[str]:
    first = CalendarDate.fromisoformat(start)
    last = CalendarDate.fromisoformat(end)
    if first > last:
        raise ValueError(f"backfill start {start} is after current nightly {end}")
    days = (last - first).days
    return [(first + timedelta(days=offset)).isoformat() for offset in range(days + 1)]


def backfill_nightlies(
    payload: dict[str, Any],
    start: str,
    end: str,
    *,
    max_checks: int,
    ensure: Callable[[dict[str, Any], str], bool] = ensure_nightly,
) -> int:
    """Check the oldest unprocessed dates, bounded for one daily refresh."""

    unavailable = set(payload.get("unavailable_dates", []))
    checks = 0
    for candidate in date_range(start, end):
        channel = f"nightly-{candidate}"
        if channel in payload["nightlies"] or candidate in unavailable:
            continue
        if checks >= max_checks:
            break
        checks += 1
        try:
            ensure(payload, candidate)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                unavailable.add(candidate)
            else:
                sys.stderr.write(
                    f"nightly-version-map: will retry {candidate}: {exc}\n"
                )
        except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as exc:
            # Historical catch-up is best-effort. Preserve any rows already
            # observed in this run and retry this date on the next refresh.
            sys.stderr.write(
                f"nightly-version-map: will retry {candidate}: {exc}\n"
            )
    payload["unavailable_dates"] = sorted(unavailable)
    return checks


def encode_map(payload: dict[str, Any]) -> bytes:
    rebuild_versions(payload)
    ordered = {
        "schema_version": SCHEMA_VERSION,
        "source": CURRENT_MANIFEST_URL,
        "nightlies": {
            key: payload["nightlies"][key]
            for key in sorted(payload["nightlies"], reverse=True)
        },
        "unavailable_dates": sorted(payload.get("unavailable_dates", [])),
        "versions": payload["versions"],
    }
    return (json.dumps(ordered, indent=2) + "\n").encode("utf-8")


def catalogue_entry(map_bytes: bytes) -> dict[str, str]:
    return {
        "owner": "zackees",
        "repo": "soldr-toolchain",
        "tag": "assets",
        "asset": ASSET_NAME,
        "url": PAGES_URL,
        "sha256": hashlib.sha256(map_bytes).hexdigest(),
    }


def update_catalogue(catalogue: dict[str, Any], entry: dict[str, str]) -> None:
    entries = catalogue.get("entries")
    if not isinstance(entries, list):
        raise ValueError("catalogue entries must be a list")
    entries[:] = [
        item
        for item in entries
        if not (
            isinstance(item, dict)
            and item.get("owner") == entry["owner"]
            and item.get("repo") == entry["repo"]
            and item.get("tag") == entry["tag"]
            and item.get("asset") == entry["asset"]
        )
    ]
    entries.append(entry)
    entries.sort(
        key=lambda item: (
            item.get("owner", ""),
            item.get("repo", ""),
            item.get("tag", ""),
            item.get("asset", ""),
            item.get("url", ""),
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--catalogue", required=True, type=Path)
    parser.add_argument(
        "--seed",
        action="append",
        default=[],
        metavar="YYYY-MM-DD",
        help="Historical dated nightly to probe if it is not already mapped.",
    )
    parser.add_argument(
        "--backfill-start",
        metavar="YYYY-MM-DD",
        help="Oldest nightly date to cover incrementally.",
    )
    parser.add_argument(
        "--max-backfill-checks",
        type=int,
        default=8,
        help="Maximum previously-unseen dates checked per refresh.",
    )
    args = parser.parse_args(argv)

    try:
        payload = load_map(args.output)
        for seed_date in args.seed:
            ensure_nightly(payload, seed_date)

        current_manifest, _current_sha = fetch_verified_manifest(
            CURRENT_MANIFEST_URL
        )
        current_date = parse_manifest_date(
            current_manifest, source_url=CURRENT_MANIFEST_URL
        )
        ensure_nightly(payload, current_date)
        if args.backfill_start:
            if args.max_backfill_checks < 1:
                raise ValueError("--max-backfill-checks must be positive")
            backfill_nightlies(
                payload,
                args.backfill_start,
                current_date,
                max_checks=args.max_backfill_checks,
            )

        map_bytes = encode_map(payload)
        catalogue = json.loads(args.catalogue.read_text(encoding="utf-8"))
        update_catalogue(catalogue, catalogue_entry(map_bytes))
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        sys.stderr.write(f"build_rust_nightly_versions.py: {exc}\n")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(map_bytes)
    args.catalogue.write_text(
        json.dumps(catalogue, indent=2) + "\n", encoding="utf-8"
    )
    sys.stderr.write(
        f"nightly-version-map: wrote {args.output} "
        f"({len(payload['nightlies'])} nightlies)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
