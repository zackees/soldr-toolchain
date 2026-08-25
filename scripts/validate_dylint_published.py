#!/usr/bin/env python3
"""Validate one native lane of the published Dylint 6.0.3 triplet.

The validator consumes the public catalogue and CDN exactly as Soldr will. It
checks all archive and payload identities before execution, relocates the three
components, installs the driver under cargo-dylint's extensionless cache
convention, and runs the checked-in clean/known-violation fixture twice. The
second fixture run is offline and must not replace or rebuild the driver.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import io
import json
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from scripts import forge_to_catalogue
from scripts.catalogue_v2 import validate_document as validate_catalogue_v2
from scripts.generation_reader import PublishedEntry, VerifiedGeneration, read_verified_generation
from scripts.produce_dylint_release import (
    DRIVER_ASSET_VERSION,
    DRIVER_IDENTITY,
    DRIVER_TOOLCHAIN,
    DYLINT_COMMIT,
    DYLINT_REPOSITORY,
    DYLINT_TAG,
    DYLINT_TOOLS,
    DYLINT_VERSION,
    ReleaseLane,
    _inspect_linux_binary,
    _run,
    _run_fixture,
    catalogue_artifact_matrix,
    lane_for_shape,
    release_plan,
    validate_smoke,
)

CATALOGUE_URL = (
    "https://zackees.github.io/soldr-toolchain/catalogue.v2.json"
)
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"


@dataclass(frozen=True)
class ExpectedAsset:
    tool: str
    version: str
    shape: str
    target: str
    platform_dir: str
    filename: str


@dataclass(frozen=True)
class VerifiedAsset:
    expected: ExpectedAsset
    binary_name: str
    payload: bytes
    manifest: Mapping[str, object]


def expected_assets() -> tuple[ExpectedAsset, ...]:
    """Return the exact three-component by eight-host public asset set."""
    assets = []
    for tool, version, shape in catalogue_artifact_matrix():
        lane = lane_for_shape(shape)
        platform_dir = forge_to_catalogue._asset_platform_dir(tool, shape)
        filename = f"{tool}-{version}-{lane.target}.tar.gz"
        assets.append(
            ExpectedAsset(
                tool=tool,
                version=version,
                shape=shape,
                target=lane.target,
                platform_dir=platform_dir,
                filename=filename,
            )
        )
    return tuple(assets)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _require_equal(field: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise RuntimeError(f"Dylint manifest {field} is {actual!r}; expected {expected!r}")


def _validate_manifest(
    manifest: Mapping[str, object], lane: ReleaseLane, tool: str, binary_name: str
) -> None:
    version = DRIVER_ASSET_VERSION if tool == "dylint-driver" else DYLINT_VERSION
    expected_fields = {
        "schema_version": 1,
        "tool": tool,
        "version": version,
        "binary": binary_name,
        "target": lane.target,
        "platform": forge_to_catalogue.FORGE_RUST_PLATFORM_BY_SHAPE[lane.shape],
        "source_repo": DYLINT_REPOSITORY,
        "source_ref": DYLINT_COMMIT,
        "source_tag": DYLINT_TAG,
        "resolution_mode": (
            "native-exact-nightly"
            if tool == "dylint-driver"
            else "native-locked-pair"
        ),
    }
    for field, expected in expected_fields.items():
        _require_equal(field, manifest.get(field), expected)

    smoke = manifest.get("smoke")
    if not isinstance(smoke, Mapping):
        raise RuntimeError(f"Dylint manifest smoke evidence is absent for {tool} {lane.target}")
    validate_smoke(lane, smoke)
    for field, expected in {
        "fixture": "fixtures/dylint-release",
        "known_violation": "release_fixture_forbidden_io",
        "binaries": [*DYLINT_TOOLS, "dylint-driver"],
        "execution_mode": "native",
        "warm_driver_builds": 0,
        "warm_network": "offline",
        "target": lane.target,
    }.items():
        _require_equal(f"smoke.{field}", smoke.get(field), expected)

    if tool == "dylint-driver":
        _require_equal(
            "driver_identity",
            manifest.get("driver_identity"),
            {**DRIVER_IDENTITY, "host": lane.target},
        )
        if manifest.get("pair_identity") is not None:
            raise RuntimeError("Dylint driver unexpectedly has pair_identity")
    else:
        if manifest.get("driver_identity") is not None:
            raise RuntimeError(f"Dylint pair member {tool} unexpectedly has driver_identity")
        _require_equal(
            "pair_identity",
            manifest.get("pair_identity"),
            {
                "dylint_version": DYLINT_VERSION,
                "source_ref": DYLINT_COMMIT,
                "target": lane.target,
            },
        )


def verify_archive(
    archive_bytes: bytes,
    *,
    expected_sha256: str,
    lane: ReleaseLane,
    tool: str,
) -> VerifiedAsset:
    """Verify an immutable published archive without trusting its paths."""
    if archive_bytes.startswith(LFS_POINTER_PREFIX):
        raise RuntimeError(f"published {tool} payload is a Git LFS pointer, not an archive")
    actual_archive_sha256 = _sha256_bytes(archive_bytes)
    if actual_archive_sha256 != expected_sha256.lower():
        raise RuntimeError(
            f"published {tool} archive SHA-256 is {actual_archive_sha256}; "
            f"catalogue requires {expected_sha256}"
        )

    suffix = ".exe" if lane.shape.startswith("windows-") else ""
    binary_name = f"{tool}{suffix}"
    expected_members = {"manifest.json", f"package/{binary_name}"}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                raise RuntimeError(f"published {tool} archive contains duplicate members")
            if set(names) != expected_members:
                raise RuntimeError(
                    f"published {tool} archive members are {sorted(names)!r}; "
                    f"expected {sorted(expected_members)!r}"
                )
            if any(not member.isfile() for member in members):
                raise RuntimeError(f"published {tool} archive contains a non-file member")
            manifest_file = archive.extractfile("manifest.json")
            payload_file = archive.extractfile(f"package/{binary_name}")
            if manifest_file is None or payload_file is None:
                raise RuntimeError(f"published {tool} archive could not be read")
            manifest_bytes = manifest_file.read()
            payload = payload_file.read()
    except tarfile.TarError as error:
        raise RuntimeError(f"published {tool} archive is not valid tar.gz: {error}") from error

    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"published {tool} manifest is invalid JSON: {error}") from error
    if not isinstance(manifest, dict):
        raise RuntimeError(f"published {tool} manifest is not an object")
    _validate_manifest(manifest, lane, tool, binary_name)
    _require_equal("payload_sha256", manifest.get("payload_sha256"), _sha256_bytes(payload))

    expected = next(
        asset
        for asset in expected_assets()
        if asset.tool == tool and asset.shape == lane.shape
    )
    return VerifiedAsset(expected, binary_name, payload, manifest)


def install_verified_asset(
    asset: VerifiedAsset, root: Path, lane: ReleaseLane
) -> Path:
    """Relocate a verified component into the layout cargo-dylint consumes."""
    if asset.expected.tool == "dylint-driver":
        destination = (
            root
            / "drivers"
            / f"{DRIVER_TOOLCHAIN}-{lane.target}"
            / "dylint-driver"
        )
    else:
        destination = root / "pair" / asset.binary_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(asset.payload)
    destination.chmod(0o755)
    return destination


def _download(url: str, *, attempts: int = 3) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "soldr-dylint-validator/1"})
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            if attempt < attempts:
                time.sleep(attempt)
    raise RuntimeError(f"could not download {url} after {attempts} attempts: {last_error}")


def _catalogue_entry(
    generation: VerifiedGeneration, expected: ExpectedAsset
) -> PublishedEntry:
    try:
        row = generation.find("zackees", "soldr-toolchain", "assets", expected.filename)
    except ValueError as exc:
        raise RuntimeError(f"published v2 catalogue lacks {expected.filename}: {exc}") from exc
    if row.is_multipart and row.min_client_version != 2:
        raise RuntimeError(f"migrated {expected.filename} lacks min_client_version=2")
    return row


def _download_verified_entry(entry: PublishedEntry) -> bytes:
    """Fetch and verify a direct entry or every part from a v2 generation."""
    if not entry.is_multipart:
        failures: list[str] = []
        for url in entry.urls:
            try:
                data = _download(url)
            except RuntimeError as exc:
                failures.append(str(exc))
                continue
            if _sha256_bytes(data) == entry.sha256 and len(data) == entry.size_bytes:
                return data
            raise RuntimeError(f"direct asset {entry.asset} failed digest/size verification")
        raise RuntimeError(f"published direct asset {entry.asset} did not verify: {failures}")
    chunks: list[bytes] = []
    for part in entry.parts:
        failures = []
        for url in part["urls"]:
            try:
                data = _download(url)
            except RuntimeError as exc:
                failures.append(str(exc))
                continue
            if _sha256_bytes(data) == part["sha256"] and len(data) == part["size_bytes"]:
                chunks.append(data)
                break
            raise RuntimeError(
                f"published part {part['number']} failed digest/size verification"
            )
        else:
            raise RuntimeError(f"published part {part['number']} did not verify: {failures}")
    data = b"".join(chunks)
    if len(data) != entry.size_bytes or _sha256_bytes(data) != entry.sha256:
        raise RuntimeError(f"reassembled published asset {entry.asset} does not match catalogue identity")
    return data


def _load_generation(catalogue_url: str) -> VerifiedGeneration:
    catalogue_bytes = _download(catalogue_url)
    if catalogue_bytes.startswith(LFS_POINTER_PREFIX):
        raise RuntimeError("published catalogue is a Git LFS pointer")
    try:
        catalogue = json.loads(catalogue_bytes)
        semantic_errors = validate_catalogue_v2(catalogue)
        if semantic_errors:
            raise ValueError("; ".join(semantic_errors))
        state_url = catalogue["publication_state"]["url"]
        state = json.loads(_download(state_url))
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"published v2 catalogue/state is invalid: {error}") from error
    if not isinstance(catalogue, dict) or not isinstance(state, dict):
        raise RuntimeError("published catalogue/state is not an object")
    try:
        return read_verified_generation(catalogue, state)
    except ValueError as error:
        raise RuntimeError(f"published v2 generation did not verify: {error}") from error


def _install_driver_toolchain(repo_root: Path) -> None:
    _run(
        [
            "rustup",
            "toolchain",
            "install",
            DRIVER_TOOLCHAIN,
            "--profile",
            "minimal",
            "--component",
            "rustc-dev",
            "--component",
            "rust-src",
            "--component",
            "llvm-tools-preview",
            "--no-self-update",
        ],
        cwd=repo_root,
    )


def validate_lane(
    *, repo_root: Path, work_dir: Path, shape: str, catalogue_url: str = CATALOGUE_URL
) -> None:
    lane = lane_for_shape(shape)
    work_dir.mkdir(parents=True, exist_ok=True)
    if any(work_dir.iterdir()):
        raise RuntimeError(f"published-validation work directory is not empty: {work_dir}")

    generation = _load_generation(catalogue_url)

    published_root = work_dir / "published"
    installed: dict[str, Path] = {}
    verified: dict[str, VerifiedAsset] = {}
    for expected in (
        asset for asset in expected_assets() if asset.shape == lane.shape
    ):
        row = _catalogue_entry(generation, expected)
        archive = _download_verified_entry(row)
        component = verify_archive(
            archive,
            expected_sha256=row.sha256,
            lane=lane,
            tool=expected.tool,
        )
        verified[expected.tool] = component
        installed[expected.tool] = install_verified_asset(component, published_root, lane)
        print(f"verified and relocated {expected.filename}", flush=True)

    if lane.shape.startswith("linux-"):
        for tool, binary in installed.items():
            actual = _inspect_linux_binary(
                repo_root=repo_root, lane=lane, tool=tool, binary=binary
            )
            _require_equal(
                f"{tool} binary_evidence",
                actual,
                verified[tool].manifest.get("binary_evidence"),
            )

    _install_driver_toolchain(repo_root)
    version = _run(
        [str(installed["cargo-dylint"]), "dylint", "--version"], cwd=repo_root
    ).stdout
    if not any(line.strip() == f"cargo-dylint {DYLINT_VERSION}" for line in version.splitlines()):
        raise RuntimeError(f"published cargo-dylint has unexpected version output: {version!r}")

    evidence = _run_fixture(
        repo_root=repo_root,
        work_dir=work_dir / "runtime",
        lane=lane,
        pair={tool: installed[tool] for tool in DYLINT_TOOLS},
        driver=installed["dylint-driver"],
    )
    validate_smoke(lane, evidence)
    print(
        f"published Dylint triplet passed native clean, violation, and offline warm fixture for {lane.target}",
        flush=True,
    )


def _github_matrix() -> str:
    return json.dumps(
        {"include": [dataclasses.asdict(lane) for lane in release_plan()]},
        separators=(",", ":"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--github-matrix", action="store_true")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--repo-root", type=Path, required=True)
    validate.add_argument("--work-dir", type=Path, required=True)
    validate.add_argument("--shape", required=True)
    validate.add_argument("--catalogue-url", default=CATALOGUE_URL)
    args = parser.parse_args(argv)

    if args.command == "plan":
        if args.github_matrix:
            print(_github_matrix())
        else:
            for lane in release_plan():
                print(f"{lane.shape}: {lane.runner} {lane.target} ({lane.environment})")
        return 0
    validate_lane(
        repo_root=args.repo_root.resolve(),
        work_dir=args.work_dir.resolve(),
        shape=args.shape,
        catalogue_url=args.catalogue_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
