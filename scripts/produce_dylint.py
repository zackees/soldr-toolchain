#!/usr/bin/env python3
"""Build and optionally publish the complete Dylint 6.0.3 catalogue matrix.

Run this command on a Windows x64 producer with Soldr's documented cross-build
support.  The command builds the unmodified, immutable Dylint workspace once
per canonical target, then stages the two version-locked tools as standard
Rust CLI artefacts.  ``--assets-root`` ingests the staged output only after a
native fixture-validation record is supplied.  ``--publish`` is deliberately
opt-in: it creates one assets-branch PR containing the complete 16-bundle
matrix.

A dry run is hermetic: it merely prints the eight exact Soldr invocations as
JSON and does not inspect a checkout, invoke Git, access the network, or run a
compiler.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from scripts import forge_to_catalogue

DYLINT_REPOSITORY = "trailofbits/dylint"
DYLINT_TAG = "v6.0.3"
DYLINT_COMMIT = "9adfa398661273ca7dc99df9bf2c26ae6f61b1c5"
DYLINT_VERSION = "6.0.3"
DYLINT_TOOLS = ("cargo-dylint", "dylint-link")
FEATURE = "dylint/__driver_from_crates_io"


@dataclass(frozen=True)
class DylintBuild:
    shape: str
    target: str
    command: tuple[str, ...]


def build_plan(soldr: str = "soldr") -> tuple[DylintBuild, ...]:
    """Return the complete, ordered eight-target Dylint build plan."""
    return tuple(
        DylintBuild(
            shape=shape,
            target=target,
            command=(
                soldr,
                "build",
                "--locked",
                "--release",
                "--target",
                target,
                "-p",
                "cargo-dylint",
                "-p",
                "dylint-link",
                f"--features={FEATURE}",
            ),
        )
        for shape, target in forge_to_catalogue.RUST_TARGET_BY_SHAPE.items()
    )


def _command_output(command: Sequence[str]) -> str:
    result = subprocess.run(list(command), check=True, capture_output=True, text=True)
    return result.stdout


def verify_checkout(
    checkout: Path,
    *,
    run: Callable[..., str] = _command_output,
) -> None:
    """Fail closed unless ``checkout`` is the exact clean upstream commit."""
    if not checkout.is_dir():
        raise RuntimeError(f"Dylint checkout does not exist: {checkout}")
    prefix = ["git", "-C", str(checkout)]
    dirty = run(prefix + ["status", "--porcelain"])
    if dirty.strip():
        raise RuntimeError(f"Dylint checkout is dirty: {checkout}")
    commit = run(prefix + ["rev-parse", "HEAD"]).strip()
    if commit != DYLINT_COMMIT:
        raise RuntimeError(
            "Dylint checkout has unexpected commit "
            f"{commit!r}; expected immutable commit {DYLINT_COMMIT!r} for {DYLINT_TAG}"
        )
    tag_commit = run(prefix + ["rev-list", "-n", "1", DYLINT_TAG]).strip()
    if tag_commit != DYLINT_COMMIT:
        raise RuntimeError(
            f"Dylint tag {DYLINT_TAG!r} resolves to {tag_commit!r}; "
            f"expected immutable commit {DYLINT_COMMIT!r}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_artifact(
    *,
    target_dir: Path,
    output_dir: Path,
    shape: str,
    binary: str,
    command: Sequence[str],
    runtime_validation: dict | None = None,
) -> Path:
    """Stage one checked binary in the existing Forge Rust-artifact layout."""
    if shape not in forge_to_catalogue.RUST_TARGET_BY_SHAPE:
        raise RuntimeError(f"unknown Dylint shape: {shape}")
    target = forge_to_catalogue.RUST_TARGET_BY_SHAPE[shape]
    suffix = ".exe" if shape.startswith("windows-") else ""
    filename = f"{binary}{suffix}"
    source = target_dir / filename
    if not source.is_file():
        raise RuntimeError(f"Dylint build did not produce expected binary: {source}")
    platform = forge_to_catalogue.FORGE_RUST_PLATFORM_BY_SHAPE[shape]
    artifact = output_dir / f"forge-rust-{binary}-{DYLINT_VERSION}-{platform}"
    if artifact.exists():
        shutil.rmtree(artifact)
    artifact.mkdir(parents=True)
    destination = artifact / filename
    shutil.copy2(source, destination)
    destination.chmod(0o755)
    manifest = {
        "schema_version": 1,
        "tool": binary,
        "version": DYLINT_VERSION,
        "binary": filename,
        "target": target,
        "platform": platform,
        "payload_sha256": _sha256(destination),
        "source_repo": DYLINT_REPOSITORY,
        "source_ref": DYLINT_COMMIT,
        "source_tag": DYLINT_TAG,
        "resolution_mode": "soldr-cross-build",
        "build_command": list(command),
        "smoke": runtime_validation or {"result": "not-validated"},
    }
    (artifact / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return artifact


def _load_validation(path: Path) -> dict[str, dict]:
    """Load fixture evidence keyed by target triple and reject partial records."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document["targets"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"invalid Dylint runtime-validation record: {path}") from exc
    expected = {step.target for step in build_plan()}
    if set(rows) != expected:
        missing = sorted(expected - set(rows))
        extra = sorted(set(rows) - expected)
        raise RuntimeError(
            f"runtime validation must cover the complete target matrix; "
            f"missing={missing}, unexpected={extra}"
        )
    for target, record in rows.items():
        if not isinstance(record, dict) or record.get("result") != "passed":
            raise RuntimeError(f"runtime validation failed or is absent for {target}")
        if not record.get("fixture"):
            raise RuntimeError(f"runtime validation fixture is absent for {target}")
        validated = set(record.get("binaries", []))
        if validated != set(DYLINT_TOOLS):
            raise RuntimeError(
                f"runtime validation for {target} must exercise both Dylint tools"
            )
    return rows


def _run_builds(checkout: Path, output_dir: Path, soldr: str) -> None:
    for step in build_plan(soldr):
        subprocess.run(list(step.command), cwd=checkout, check=True)
        target_dir = checkout / "target" / step.target / "release"
        for binary in DYLINT_TOOLS:
            write_artifact(
                target_dir=target_dir,
                output_dir=output_dir,
                shape=step.shape,
                binary=binary,
                command=step.command,
            )


def _replace_validation(output_dir: Path, validation: dict[str, dict]) -> None:
    """Bind externally-run fixture evidence to every staged artifact."""
    for step in build_plan():
        for binary in DYLINT_TOOLS:
            artifact = output_dir / (
                f"forge-rust-{binary}-{DYLINT_VERSION}-"
                f"{forge_to_catalogue.FORGE_RUST_PLATFORM_BY_SHAPE[step.shape]}"
            )
            manifest_path = artifact / "manifest.json"
            if not manifest_path.is_file():
                raise RuntimeError(f"partial Dylint artifact matrix: missing {manifest_path}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["smoke"] = validation[step.target]
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )


def _ingest(output_dir: Path, assets_root: Path, schema: Path) -> None:
    for binary in DYLINT_TOOLS:
        for step in build_plan():
            result = forge_to_catalogue.main(
                [
                    "--forge-dir", str(output_dir), "--tool", binary,
                    "--version", DYLINT_VERSION, "--shape", step.shape,
                    "--forge-run-id", "local-dylint-producer", "--assets-root", str(assets_root),
                    "--schema", str(schema),
                ]
            )
            if result:
                raise RuntimeError(f"failed to ingest {binary} for {step.shape}")


def _require_clean_assets_checkout(assets_root: Path) -> None:
    """Ensure publication starts from a clean assets-branch checkout."""
    if not assets_root.is_dir():
        raise RuntimeError(f"assets checkout does not exist: {assets_root}")
    branch = _command_output(["git", "-C", str(assets_root), "branch", "--show-current"]).strip()
    if branch != "assets":
        raise RuntimeError(f"assets checkout must be on the assets branch, not {branch!r}")
    if _command_output(["git", "-C", str(assets_root), "status", "--porcelain"]).strip():
        raise RuntimeError("assets checkout is dirty before Dylint publication")


def _publish_assets(assets_root: Path, branch: str) -> str:
    """Create one reviewable assets PR; callers opt in with ``--publish``."""
    subprocess.run(["git", "-C", str(assets_root), "switch", "-c", branch], check=True)
    subprocess.run(["git", "-C", str(assets_root), "add", "cargo-dylint", "dylint-link", "manifest.json", "catalogue.v1.json", ".forge-ingest.log.jsonl"], check=True)
    subprocess.run(["git", "-C", str(assets_root), "commit", "-m", "feat(catalogue): publish Dylint 6.0.3 matrix"], check=True)
    subprocess.run(["git", "-C", str(assets_root), "push", "-u", "origin", branch], check=True)
    result = subprocess.run(
        ["gh", "pr", "create", "--repo", "zackees/soldr-toolchain", "--base", "assets", "--head", branch,
         "--title", "feat(catalogue): publish Dylint 6.0.3 matrix",
         "--body", "Publishes cargo-dylint and dylint-link for all eight canonical targets."],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dylint-checkout", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--assets-root", type=Path)
    parser.add_argument("--schema", type=Path, default=Path(__file__).parents[1] / "schemas" / "catalogue.v1.schema.json")
    parser.add_argument("--runtime-validation", type=Path, help="JSON fixture evidence for every target and both tools")
    parser.add_argument("--soldr", default="soldr")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--branch", default="publish/dylint-6.0.3")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.dry_run:
        print(json.dumps({
            "upstream": {"repository": DYLINT_REPOSITORY, "tag": DYLINT_TAG, "commit": DYLINT_COMMIT},
            "builds": [
                {"shape": step.shape, "target": step.target, "binaries": list(DYLINT_TOOLS), "command": list(step.command)}
                for step in build_plan(args.soldr)
            ],
        }, indent=2))
        return 0
    if args.dylint_checkout is None or args.output_dir is None:
        parser.error("--dylint-checkout and --output-dir are required unless --dry-run is used")
    if args.publish and args.assets_root is None:
        parser.error("--publish requires --assets-root")
    if args.assets_root is not None and args.runtime_validation is None:
        parser.error("--assets-root requires --runtime-validation; unvalidated binaries cannot be catalogued")
    if args.publish:
        assert args.assets_root is not None
        _require_clean_assets_checkout(args.assets_root)

    verify_checkout(args.dylint_checkout)
    _run_builds(args.dylint_checkout, args.output_dir, args.soldr)
    if args.assets_root is not None:
        _replace_validation(args.output_dir, _load_validation(args.runtime_validation))
        _ingest(args.output_dir, args.assets_root, args.schema)
    if args.publish:
        assert args.assets_root is not None
        print(_publish_assets(args.assets_root, args.branch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
