from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_rust_nightly_versions as brnv


MANIFEST = b"""manifest-version = "2"
date = "2026-05-26"
"""
VERBOSE = """rustc 1.98.0-nightly (31a9463c6 2026-05-25)
binary: rustc
commit-hash: 31a9463c6e2794a59ce57a8f37abc6966afc2a58
commit-date: 2026-05-25
host: x86_64-unknown-linux-gnu
release: 1.98.0-nightly
LLVM version: 21.1.0
"""


def _identity(date: str) -> dict[str, str]:
    return {
        "manifest_date": date,
        **brnv.parse_rustc_verbose(
            VERBOSE.replace("2026-05-25", date),
            channel=f"nightly-{date}",
        ),
        "manifest_url": brnv.dated_manifest_url(date),
        "manifest_sha256": "a" * 64,
    }


def test_verify_manifest_and_parse_date() -> None:
    digest = hashlib.sha256(MANIFEST).hexdigest()
    assert (
        brnv.verify_manifest(
            MANIFEST,
            f"{digest}  channel-rust-nightly.toml\n".encode(),
            source_url="https://example.invalid/manifest",
        )
        == digest
    )
    assert (
        brnv.parse_manifest_date(
            MANIFEST, source_url="https://example.invalid/manifest"
        )
        == "2026-05-26"
    )


def test_checksum_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        brnv.verify_manifest(
            MANIFEST,
            ("0" * 64).encode(),
            source_url="https://example.invalid/manifest",
        )


def test_parse_rustc_verbose_records_version_line_and_full_commit() -> None:
    identity = brnv.parse_rustc_verbose(
        VERBOSE, channel="nightly-2026-05-26"
    )
    assert identity["rust_version"] == "1.98"
    assert identity["rustc_release"] == "1.98.0-nightly"
    assert (
        identity["rustc_commit_hash"]
        == "31a9463c6e2794a59ce57a8f37abc6966afc2a58"
    )


def test_existing_nightly_is_never_downloaded_or_reprobed() -> None:
    payload = {
        "nightlies": {"nightly-2026-05-26": _identity("2026-05-26")}
    }
    called = False

    def probe(_channel: str) -> dict[str, str]:
        nonlocal called
        called = True
        raise AssertionError("existing nightly must not be probed")

    assert not brnv.ensure_nightly(payload, "2026-05-26", probe=probe)
    assert not called


def test_new_nightly_is_downloaded_and_probed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"nightlies": {}}
    digest = hashlib.sha256(MANIFEST).hexdigest()
    monkeypatch.setattr(
        brnv,
        "fetch_verified_manifest",
        lambda _url: (MANIFEST, digest),
    )
    calls: list[str] = []

    def probe(channel: str) -> dict[str, str]:
        calls.append(channel)
        return brnv.parse_rustc_verbose(VERBOSE, channel=channel)

    assert brnv.ensure_nightly(payload, "2026-05-26", probe=probe)
    assert calls == ["nightly-2026-05-26"]
    assert not brnv.ensure_nightly(payload, "2026-05-26", probe=probe)
    assert calls == ["nightly-2026-05-26"]


def test_probe_uses_disposable_private_rustup_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_homes: list[str] = []

    def run(_args: list[str], **kwargs: object) -> SimpleNamespace:
        rustup_home = str(kwargs["env"]["RUSTUP_HOME"])  # type: ignore[index]
        observed_homes.append(rustup_home)
        assert Path(rustup_home).is_dir()
        return SimpleNamespace(stdout=VERBOSE)

    monkeypatch.setattr(brnv.subprocess, "run", run)
    identity = brnv.probe_nightly("nightly-2026-05-26")
    assert identity["rust_version"] == "1.98"
    assert len(observed_homes) == 2
    assert observed_homes[0] == observed_homes[1]
    assert not Path(observed_homes[0]).exists()


def test_versions_are_descending_and_select_first() -> None:
    previous_train = _identity("2026-04-01")
    previous_train["rust_version"] = "1.97"
    previous_train["rustc_release"] = "1.97.0-nightly"
    payload = {
        "nightlies": {
            "nightly-2026-04-01": previous_train,
            "nightly-2026-05-01": _identity("2026-05-01"),
            "nightly-2026-05-26": _identity("2026-05-26"),
            "nightly-2026-04-15": _identity("2026-04-15"),
        }
    }
    brnv.rebuild_versions(payload)
    bucket = payload["versions"]["1.98"]
    assert bucket["nightlies"] == [
        "nightly-2026-05-26",
        "nightly-2026-05-01",
        "nightly-2026-04-15",
    ]
    assert bucket["selected"] == bucket["nightlies"][0]
    assert "nightly-2026-04-01" not in bucket["nightlies"]
    assert payload["versions"]["1.97"]["nightlies"] == ["nightly-2026-04-01"]


def test_backfill_catches_oldest_missed_days_without_rechecking() -> None:
    payload = {
        "nightlies": {
            "nightly-2026-05-01": _identity("2026-05-01"),
            "nightly-2026-05-05": _identity("2026-05-05"),
        },
        "unavailable_dates": ["2026-05-02"],
    }
    checked: list[str] = []

    def ensure(target: dict[str, object], candidate: str) -> bool:
        checked.append(candidate)
        target["nightlies"][f"nightly-{candidate}"] = _identity(candidate)  # type: ignore[index]
        return True

    assert (
        brnv.backfill_nightlies(
            payload,
            "2026-05-01",
            "2026-05-05",
            max_checks=2,
            ensure=ensure,
        )
        == 2
    )
    assert checked == ["2026-05-03", "2026-05-04"]
    checked.clear()
    assert (
        brnv.backfill_nightlies(
            payload,
            "2026-05-01",
            "2026-05-05",
            max_checks=2,
            ensure=ensure,
        )
        == 0
    )
    assert checked == []


def test_backfill_retries_transient_failure_and_keeps_other_progress() -> None:
    payload: dict[str, object] = {
        "nightlies": {"nightly-2026-05-01": _identity("2026-05-01")},
        "unavailable_dates": [],
    }
    checked: list[str] = []

    def ensure(target: dict[str, object], candidate: str) -> bool:
        checked.append(candidate)
        if candidate == "2026-05-02":
            raise subprocess.CalledProcessError(1, ["rustup", candidate])
        target["nightlies"][f"nightly-{candidate}"] = _identity(candidate)  # type: ignore[index]
        return True

    assert (
        brnv.backfill_nightlies(
            payload,
            "2026-05-01",
            "2026-05-03",
            max_checks=2,
            ensure=ensure,
        )
        == 2
    )
    assert checked == ["2026-05-02", "2026-05-03"]
    assert "nightly-2026-05-02" not in payload["nightlies"]  # type: ignore[operator]
    assert "nightly-2026-05-03" in payload["nightlies"]  # type: ignore[operator]
    assert payload["unavailable_dates"] == []


def test_catalogue_entry_is_replaced_and_sha_verified() -> None:
    map_bytes = brnv.encode_map(
        {"nightlies": {"nightly-2026-05-26": _identity("2026-05-26")}}
    )
    entry = brnv.catalogue_entry(map_bytes)
    catalogue = {
        "entries": [
            {**entry, "sha256": "0" * 64},
            {
                "owner": "zackees",
                "repo": "zccache",
                "tag": "1",
                "asset": "zccache.zip",
                "url": "https://example.invalid/zccache.zip",
                "sha256": "1" * 64,
            },
        ]
    }
    brnv.update_catalogue(catalogue, entry)
    rows = [
        row
        for row in catalogue["entries"]
        if row["asset"] == brnv.ASSET_NAME
    ]
    assert rows == [entry]
    assert entry["sha256"] == hashlib.sha256(map_bytes).hexdigest()
