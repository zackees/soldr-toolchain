#!/usr/bin/env python3
"""Produce a native Dylint executable pair and exact-nightly driver matrix.

The module owns the immutable release identity and platform topology.  Workflow
YAML should only select a lane and invoke these helpers; identity validation,
artifact layout, and evidence validation stay here where pytest can exercise
them without a compiler or network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from scripts import forge_to_catalogue
from scripts.build_asset_index import build_asset_index

DYLINT_REPOSITORY = "trailofbits/dylint"
DYLINT_TAG = "v6.0.3"
DYLINT_COMMIT = "9adfa398661273ca7dc99df9bf2c26ae6f61b1c5"
DYLINT_VERSION = "6.0.3"
DRIVER_ASSET_VERSION = "6.0.3-nightly-2026-05-28"
DRIVER_IDENTITY = {
    "dylint_version": DYLINT_VERSION,
    "toolchain": "nightly-2026-05-28",
    "rustc_release": "1.98.0-nightly",
    "rustc_commit": "57d06900fd7d9ee06d3a7f323bb77f17ab3cfaf8",
}
PAIR_TOOLCHAIN = "1.97.1"
DRIVER_TOOLCHAIN = str(DRIVER_IDENTITY["toolchain"])
DYLINT_TOOLS = ("cargo-dylint", "dylint-link")
GLIBC_CEILING = "2.17"


@dataclass(frozen=True)
class ReleaseLane:
    """One natively executed host in the immutable release matrix."""

    shape: str
    runner: str
    target: str
    environment: str
    evidence_level: str = "fixture-validated-native"


def release_plan() -> tuple[ReleaseLane, ...]:
    """Return all supported Dylint hosts in stable publication order."""
    return (
        ReleaseLane("windows-x64", "windows-2022", "x86_64-pc-windows-msvc", "native"),
        ReleaseLane(
            "windows-arm64", "windows-11-arm", "aarch64-pc-windows-msvc", "native"
        ),
        ReleaseLane("darwin-x64", "macos-15-intel", "x86_64-apple-darwin", "native"),
        ReleaseLane("darwin-arm64", "macos-15", "aarch64-apple-darwin", "native"),
        ReleaseLane(
            "linux-x64-gnu", "ubuntu-22.04", "x86_64-unknown-linux-gnu", "manylinux2014"
        ),
        ReleaseLane(
            "linux-arm64-gnu",
            "ubuntu-24.04-arm",
            "aarch64-unknown-linux-gnu",
            "manylinux2014",
        ),
        ReleaseLane(
            "linux-x64-musl", "ubuntu-22.04", "x86_64-unknown-linux-musl", "alpine"
        ),
        ReleaseLane(
            "linux-arm64-musl",
            "ubuntu-24.04-arm",
            "aarch64-unknown-linux-musl",
            "alpine",
        ),
    )


def lane_for_shape(shape: str) -> ReleaseLane:
    """Resolve a canonical shape or fail with the complete supported set."""
    try:
        return next(lane for lane in release_plan() if lane.shape == shape)
    except StopIteration as exc:
        supported = ", ".join(lane.shape for lane in release_plan())
        raise RuntimeError(f"unknown Dylint release shape {shape!r}; expected one of {supported}") from exc


def catalogue_artifact_matrix() -> tuple[tuple[str, str, str], ...]:
    """Return the complete three-component by eight-host ingest matrix."""
    tools = (
        ("cargo-dylint", DYLINT_VERSION),
        ("dylint-link", DYLINT_VERSION),
        ("dylint-driver", DRIVER_ASSET_VERSION),
    )
    return tuple(
        (tool, version, lane.shape)
        for tool, version in tools
        for lane in release_plan()
    )


def validate_smoke(lane: ReleaseLane, smoke: Mapping[str, object]) -> None:
    """Require generated evidence for a real lint and a no-build warm rerun."""
    if smoke.get("result") != "passed":
        raise RuntimeError(f"Dylint fixture did not pass for {lane.target}")
    if not smoke.get("known_violation"):
        raise RuntimeError(f"Dylint evidence has no known violation for {lane.target}")
    if smoke.get("warm_driver_builds") != 0:
        raise RuntimeError(f"Dylint warm rerun built a driver for {lane.target}")
    if smoke.get("execution_mode") not in {None, "native"}:
        raise RuntimeError(f"Dylint evidence is not native for {lane.target}")


def _rustc_field(verbose: str, name: str) -> str | None:
    prefix = f"{name}:"
    return next(
        (line.partition(":")[2].strip() for line in verbose.splitlines() if line.startswith(prefix)),
        None,
    )


def validate_rustc_identity(lane: ReleaseLane, rustc_verbose: str) -> None:
    """Bind a driver to the exact compiler ABI used by its native host."""
    observed_commit = _rustc_field(rustc_verbose, "commit-hash")
    if observed_commit != DRIVER_IDENTITY["rustc_commit"]:
        raise RuntimeError(
            f"rustc commit mismatch for {lane.target}: {observed_commit!r}; "
            f"expected {DRIVER_IDENTITY['rustc_commit']!r}"
        )
    observed_host = _rustc_field(rustc_verbose, "host")
    if observed_host != lane.target:
        raise RuntimeError(
            f"rustc host mismatch for {lane.target}: {observed_host!r}; expected {lane.target!r}"
        )
    observed_release = _rustc_field(rustc_verbose, "release")
    if observed_release != DRIVER_IDENTITY["rustc_release"]:
        raise RuntimeError(
            f"rustc release mismatch for {lane.target}: {observed_release!r}; "
            f"expected {DRIVER_IDENTITY['rustc_release']!r}"
        )


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(component) for component in version.split("."))


def validate_linux_elf_evidence(
    lane: ReleaseLane,
    tool: str,
    *,
    header: str,
    program_headers: str,
    version_info: str,
) -> dict[str, object]:
    """Validate and normalize the ABI proof for one native Linux binary."""
    expected_machine = (
        "Advanced Micro Devices X86-64"
        if lane.target.startswith("x86_64-")
        else "AArch64"
    )
    machine_match = re.search(r"^\s*Machine:\s*(.+?)\s*$", header, re.MULTILINE)
    observed_machine = machine_match.group(1) if machine_match else None
    if observed_machine != expected_machine:
        raise RuntimeError(
            f"ELF machine mismatch for {tool} {lane.target}: "
            f"{observed_machine!r}; expected {expected_machine!r}"
        )

    interpreter_match = re.search(
        r"Requesting program interpreter:\s*([^\]]+)", program_headers
    )
    interpreter = interpreter_match.group(1).strip() if interpreter_match else None
    glibc_versions = set(re.findall(r"\bGLIBC_(\d+(?:\.\d+)+)\b", version_info))
    glibc_max = max(glibc_versions, key=_version_key) if glibc_versions else None

    if lane.environment == "manylinux2014":
        if glibc_max is None:
            raise RuntimeError(f"no GLIBC requirements found for GNU binary {tool}")
        if _version_key(glibc_max) > _version_key(GLIBC_CEILING):
            raise RuntimeError(
                f"{tool} requires GLIBC {glibc_max}; ceiling is {GLIBC_CEILING}"
            )
    elif lane.environment == "alpine":
        if glibc_max is not None:
            raise RuntimeError(
                f"musl binary {tool} unexpectedly requires GLIBC {glibc_max}"
            )
        if tool in DYLINT_TOOLS and interpreter is not None:
            raise RuntimeError(
                f"musl executable pair member {tool} must be static; "
                f"found interpreter {interpreter}"
            )
    else:
        raise RuntimeError(f"{lane.shape!r} is not a Linux release lane")

    return {
        "format": "ELF",
        "machine": observed_machine,
        "interpreter": interpreter,
        "glibc_max": glibc_max,
        "glibc_ceiling": GLIBC_CEILING if lane.environment == "manylinux2014" else None,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_tool_artifact(
    *,
    tool: str,
    version: str,
    built_binary: Path,
    output_dir: Path,
    lane: ReleaseLane,
    smoke: Mapping[str, object],
    resolution_mode: str,
    extra_manifest: Mapping[str, object] | None = None,
) -> Path:
    validate_smoke(lane, smoke)
    if not built_binary.is_file():
        raise RuntimeError(f"Dylint build did not produce {built_binary}")

    platform = forge_to_catalogue.FORGE_RUST_PLATFORM_BY_SHAPE[lane.shape]
    artifact = output_dir / f"forge-rust-{tool}-{version}-{platform}"
    if artifact.exists():
        shutil.rmtree(artifact)
    artifact.mkdir(parents=True)
    suffix = ".exe" if lane.shape.startswith("windows-") else ""
    binary_name = f"{tool}{suffix}"
    destination = artifact / binary_name
    shutil.copy2(built_binary, destination)
    destination.chmod(0o755)

    manifest = {
        "schema_version": 1,
        "tool": tool,
        "version": version,
        "binary": binary_name,
        "target": lane.target,
        "platform": platform,
        "payload_sha256": _sha256(destination),
        "source_repo": DYLINT_REPOSITORY,
        "source_ref": DYLINT_COMMIT,
        "source_tag": DYLINT_TAG,
        "resolution_mode": resolution_mode,
        "smoke": dict(smoke),
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    (artifact / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return artifact


def stage_driver_artifact(
    *,
    built_binary: Path,
    output_dir: Path,
    lane: ReleaseLane,
    rustc_verbose: str,
    smoke: Mapping[str, object],
    binary_evidence: Mapping[str, object] | None = None,
) -> Path:
    """Stage one verified driver in the managed Rust artifact layout."""
    validate_rustc_identity(lane, rustc_verbose)
    extra_manifest: dict[str, object] = {
        "driver_identity": {**DRIVER_IDENTITY, "host": lane.target}
    }
    if binary_evidence is not None:
        extra_manifest["binary_evidence"] = dict(binary_evidence)
    return _stage_tool_artifact(
        tool="dylint-driver",
        version=DRIVER_ASSET_VERSION,
        built_binary=built_binary,
        output_dir=output_dir,
        lane=lane,
        smoke=smoke,
        resolution_mode="native-exact-nightly",
        extra_manifest=extra_manifest,
    )


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {subprocess.list2cmdline(list(command))}", flush=True)
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=None if env is None else dict(env),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode != 0:
        print(result.stdout, end="", flush=True)
        result.check_returncode()
    return result


def _inspect_linux_binary(
    *, repo_root: Path, lane: ReleaseLane, tool: str, binary: Path
) -> dict[str, object]:
    header = _run(["readelf", "-hW", str(binary)], cwd=repo_root).stdout
    program_headers = _run(["readelf", "-lW", str(binary)], cwd=repo_root).stdout
    version_info = _run(
        ["readelf", "--version-info", str(binary)], cwd=repo_root
    ).stdout
    return validate_linux_elf_evidence(
        lane,
        tool,
        header=header,
        program_headers=program_headers,
        version_info=version_info,
    )


def _verify_dylint_checkout(checkout: Path) -> None:
    commit = _run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    if commit != DYLINT_COMMIT:
        raise RuntimeError(f"Dylint source is {commit!r}; expected {DYLINT_COMMIT!r}")
    dirty = _run(["git", "status", "--porcelain"], cwd=checkout).stdout.strip()
    if dirty:
        raise RuntimeError(f"Dylint source checkout is dirty: {dirty}")


def _install_toolchains(repo_root: Path) -> None:
    _run(
        [
            "rustup",
            "toolchain",
            "install",
            PAIR_TOOLCHAIN,
            "--profile",
            "minimal",
            "--no-self-update",
        ],
        cwd=repo_root,
    )
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


def _toolchain_root(repo_root: Path) -> Path:
    rustc = _run(
        ["rustup", "which", "--toolchain", DRIVER_TOOLCHAIN, "rustc"], cwd=repo_root
    ).stdout.strip()
    return Path(rustc).resolve().parent.parent


def _fixture_environment(repo_root: Path, relocated: Path) -> dict[str, str]:
    env = dict(os.environ)
    pair_dir = relocated / "pair"
    toolchain_root = _toolchain_root(repo_root)
    # Keep rustup's proxies on PATH so the fixture's rust-toolchain.toml sets
    # RUSTUP_TOOLCHAIN for dylint-link.  Only the private library directory
    # needs to come from the resolved toolchain itself.
    env["PATH"] = os.pathsep.join([str(pair_dir), env.get("PATH", "")])
    env["DYLINT_DRIVER_PATH"] = str(relocated / "drivers")
    env["RUSTUP_TOOLCHAIN"] = DRIVER_TOOLCHAIN
    env["RUSTUP_HOME"] = str(toolchain_root.parent.parent)
    library_var = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
    if os.name != "nt":
        env[library_var] = os.pathsep.join(
            [str(toolchain_root / "lib"), env.get(library_var, "")]
        )
    return env


def driver_build_command(manifest: Path) -> list[str]:
    """Build the driver for the native host that owns its rustc-private ABI."""
    return [
        "cargo",
        f"+{DRIVER_TOOLCHAIN}",
        "build",
        "--locked",
        "--release",
        "--manifest-path",
        str(manifest),
    ]


def driver_build_environment(work_dir: Path) -> tuple[dict[str, str], Path]:
    """Isolate Cargo target selection so the driver is unconditionally native."""
    env = dict(os.environ)
    env.pop("CARGO_BUILD_TARGET", None)
    target_dir = work_dir / "dylint-driver-target"
    env["CARGO_TARGET_DIR"] = str(target_dir)
    return env, target_dir


def driver_binary_path(target_dir: Path, suffix: str) -> Path:
    """Return Cargo's native-host output path for the release driver."""
    return target_dir / "release" / f"soldr-dylint-driver{suffix}"


def pair_build_command(lane: ReleaseLane) -> list[str]:
    """Build the relocatable CLI pair for the lane's explicit target."""
    return [
        "cargo",
        f"+{PAIR_TOOLCHAIN}",
        "build",
        "--locked",
        "--release",
        "--target",
        lane.target,
        "-p",
        "cargo-dylint",
        "-p",
        "dylint-link",
        "--features=dylint/__driver_from_crates_io",
    ]


def _run_fixture(
    *, repo_root: Path, work_dir: Path, lane: ReleaseLane, pair: Mapping[str, Path], driver: Path
) -> dict[str, object]:
    relocated = work_dir / "relocated"
    if relocated.exists():
        shutil.rmtree(relocated)
    pair_dir = relocated / "pair"
    pair_dir.mkdir(parents=True)
    suffix = ".exe" if lane.shape.startswith("windows-") else ""
    for tool, source in pair.items():
        shutil.copy2(source, pair_dir / f"{tool}{suffix}")

    fixture = relocated / "fixture"
    shutil.copytree(repo_root / "fixtures" / "dylint-release", fixture)
    qualified_toolchain = f"{DRIVER_TOOLCHAIN}-{lane.target}"
    driver_dir = relocated / "drivers" / qualified_toolchain
    driver_dir.mkdir(parents=True)
    installed_driver = driver_dir / "dylint-driver"
    shutil.copy2(driver, installed_driver)
    installed_driver.chmod(0o755)
    before = (_sha256(installed_driver), installed_driver.stat().st_mtime_ns)

    env = _fixture_environment(repo_root, relocated)
    command = [str(pair_dir / f"cargo-dylint{suffix}"), "dylint", "--all"]
    clean = _run(command, cwd=fixture, env=env, check=False)
    print(clean.stdout, end="")
    if clean.returncode != 0:
        raise RuntimeError(f"clean Dylint fixture failed for {lane.target}")

    violating = command + ["--", "--all-targets"]
    first = _run(violating, cwd=fixture, env=env, check=False)
    print(first.stdout, end="")
    lint_name = "release_fixture_forbidden_io"
    if first.returncode == 0 or lint_name not in first.stdout:
        raise RuntimeError(f"known Dylint violation did not fire for {lane.target}")

    warm_env = dict(env)
    warm_env["CARGO_NET_OFFLINE"] = "true"
    warm = _run(violating, cwd=fixture, env=warm_env, check=False)
    print(warm.stdout, end="")
    if warm.returncode == 0 or lint_name not in warm.stdout:
        raise RuntimeError(f"warm Dylint violation did not fire for {lane.target}")
    after = (_sha256(installed_driver), installed_driver.stat().st_mtime_ns)
    if after != before:
        raise RuntimeError(f"warm Dylint rerun replaced or rebuilt the driver for {lane.target}")

    return {
        "result": "passed",
        "fixture": "fixtures/dylint-release",
        "known_violation": lint_name,
        "binaries": [*DYLINT_TOOLS, "dylint-driver"],
        "execution_mode": "native",
        "warm_driver_builds": 0,
        "warm_network": "offline",
        "target": lane.target,
        "github_run_url": (
            f"{os.environ.get('GITHUB_SERVER_URL')}/{os.environ.get('GITHUB_REPOSITORY')}"
            f"/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"
            if os.environ.get("GITHUB_RUN_ID")
            else None
        ),
    }


def build_lane(*, repo_root: Path, dylint_checkout: Path, output_dir: Path, work_dir: Path, shape: str) -> None:
    """Build, relocate, fixture-test, and stage all three lane components."""
    lane = lane_for_shape(shape)
    _verify_dylint_checkout(dylint_checkout)
    _install_toolchains(repo_root)

    pair_command = pair_build_command(lane)
    pair_target_dir = work_dir / "dylint-target"
    pair_env = dict(os.environ)
    pair_env["CARGO_TARGET_DIR"] = str(pair_target_dir)
    pair_result = _run(pair_command, cwd=dylint_checkout, env=pair_env)
    print(pair_result.stdout, end="")

    driver_manifest = repo_root / "dylint-driver" / "Cargo.toml"
    driver_command = driver_build_command(driver_manifest)
    driver_env, driver_target_dir = driver_build_environment(work_dir)
    driver_result = _run(driver_command, cwd=repo_root, env=driver_env)
    print(driver_result.stdout, end="")

    suffix = ".exe" if lane.shape.startswith("windows-") else ""
    pair = {
        tool: pair_target_dir / lane.target / "release" / f"{tool}{suffix}"
        for tool in DYLINT_TOOLS
    }
    driver = driver_binary_path(driver_target_dir, suffix)
    rustc_verbose = _run(
        ["rustc", f"+{DRIVER_TOOLCHAIN}", "-vV"], cwd=repo_root
    ).stdout
    validate_rustc_identity(lane, rustc_verbose)
    binaries = {**pair, "dylint-driver": driver}
    binary_evidence = (
        {
            tool: _inspect_linux_binary(
                repo_root=repo_root, lane=lane, tool=tool, binary=binary
            )
            for tool, binary in binaries.items()
        }
        if lane.shape.startswith("linux-")
        else {}
    )
    smoke = _run_fixture(
        repo_root=repo_root, work_dir=work_dir, lane=lane, pair=pair, driver=driver
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for tool, binary in pair.items():
        _stage_tool_artifact(
            tool=tool,
            version=DYLINT_VERSION,
            built_binary=binary,
            output_dir=output_dir,
            lane=lane,
            smoke=smoke,
            resolution_mode="native-locked-pair",
            extra_manifest={
                "pair_identity": {
                    "dylint_version": DYLINT_VERSION,
                    "source_ref": DYLINT_COMMIT,
                    "target": lane.target,
                },
                **(
                    {"binary_evidence": binary_evidence[tool]}
                    if tool in binary_evidence
                    else {}
                ),
            },
        )
    stage_driver_artifact(
        built_binary=driver,
        output_dir=output_dir,
        lane=lane,
        rustc_verbose=rustc_verbose,
        smoke=smoke,
        binary_evidence=binary_evidence.get("dylint-driver"),
    )


def ingest_release(
    *, artifacts_dir: Path, assets_root: Path, schema: Path, forge_run_id: str
) -> None:
    """Ingest a complete workflow run into a clean local assets checkout."""
    if not artifacts_dir.is_dir():
        raise RuntimeError(f"Dylint workflow artifacts are absent: {artifacts_dir}")
    if not assets_root.is_dir():
        raise RuntimeError(f"assets checkout is absent: {assets_root}")
    branch = _run(["git", "branch", "--show-current"], cwd=assets_root).stdout.strip()
    if branch != "assets":
        raise RuntimeError(f"assets checkout must start on branch 'assets', not {branch!r}")
    dirty = _run(["git", "status", "--porcelain"], cwd=assets_root).stdout.strip()
    if dirty:
        raise RuntimeError(f"assets checkout must be clean before ingest: {dirty}")

    with tempfile.TemporaryDirectory(
        prefix="dylint-ingest-", dir=assets_root.parent
    ) as temporary:
        staged_assets = Path(temporary) / "assets"
        shutil.copytree(
            assets_root,
            staged_assets,
            ignore=shutil.ignore_patterns(".git"),
        )

        for tool, version, shape in catalogue_artifact_matrix():
            result = forge_to_catalogue.main(
                [
                    "--forge-dir",
                    str(artifacts_dir),
                    "--tool",
                    tool,
                    "--version",
                    version,
                    "--shape",
                    shape,
                    "--forge-run-id",
                    forge_run_id,
                    "--assets-root",
                    str(staged_assets),
                    "--schema",
                    str(schema),
                ]
            )
            if result:
                raise RuntimeError(f"failed to ingest {tool} {version} for {shape}")

        index = build_asset_index(staged_assets, branch="assets", offline=True)
        (staged_assets / "asset-index.json").write_text(
            json.dumps(index, indent=2) + "\n", encoding="utf-8"
        )

        dirty = _run(["git", "status", "--porcelain"], cwd=assets_root).stdout.strip()
        if dirty:
            raise RuntimeError(f"assets checkout changed during staged ingest: {dirty}")
        shutil.copytree(staged_assets, assets_root, dirs_exist_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--github-matrix", action="store_true")
    build = subparsers.add_parser("build")
    build.add_argument("--repo-root", type=Path, required=True)
    build.add_argument("--dylint-checkout", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--work-dir", type=Path, required=True)
    build.add_argument("--shape", required=True)
    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--artifacts-dir", type=Path, required=True)
    ingest.add_argument("--assets-root", type=Path, required=True)
    ingest.add_argument("--forge-run-id", required=True)
    ingest.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "schemas"
        / "catalogue.v1.schema.json",
    )
    args = parser.parse_args(argv)
    if args.command == "plan":
        rows = [lane.__dict__ for lane in release_plan()]
        print(json.dumps({"include": rows} if args.github_matrix else rows))
        return 0
    if args.command == "ingest":
        ingest_release(
            artifacts_dir=args.artifacts_dir.resolve(),
            assets_root=args.assets_root.resolve(),
            schema=args.schema.resolve(),
            forge_run_id=args.forge_run_id,
        )
        return 0
    build_lane(
        repo_root=args.repo_root.resolve(),
        dylint_checkout=args.dylint_checkout.resolve(),
        output_dir=args.output_dir.resolve(),
        work_dir=args.work_dir.resolve(),
        shape=args.shape,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
