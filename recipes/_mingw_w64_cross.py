"""Linux-hosted MinGW-w64 GCC **cross toolchain** (soldr-toolchain#114
Phase 2 / soldr#2336).

Unlike `mingw-w64-gcc` (Windows-host `.exe` toolchain) and
`mingw-w64-sysroot` (host-neutral link inputs), this materializes a
relocatable **Linux-hosted** `x86_64-w64-mingw32-{gcc,g++,ar,ranlib,ld,
dlltool,windres}` cross toolchain, so `soldr build --target
x86_64-pc-windows-gnu` can compile + link win-gnu **from Linux** with a
real gcc driver (the maintainer-chosen path — gcc kept, not zig).

Materialized the same way as `_gnu_linux_toolchain.py`: SHA-locked
conda-forge packages via micromamba-in-Docker. conda-forge's
`gcc_impl_win-64` / `gxx_impl_win-64` are the MinGW-w64 cross gcc used to
build win-64 conda packages from a linux-64 host.

This is a **discovery-first** producer: `EXPECTED_LOCK_SHA256` starts
empty so the first forge build succeeds and emits `conda-plan.json` +
`meta.json` (resolved builds + sha256 + discovered `bin/` layout). Those
values are then pinned here and validation is hardened in a follow-up.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# conda-forge gcc for the win-64 (mingw-w64) target. 15.3.0 matches the
# WinLibs 15.3.0 sysroot pin so the cross driver + host-neutral sysroot
# agree on gcc/CRT versions.
GCC_VERSION = "15.3.0"
PINNED_VERSIONS = (f"mingw-w64-gcc-{GCC_VERSION}",)

MICROMAMBA_IMAGE = (
    "mambaorg/micromamba:2.3.2@"
    "sha256:ce2026639d612fbe837a35ca9d513292a8d776a24a5e6c82cccf9dddf45ad3d2"
)

SHAPE_CONFIG = {
    # Linux x64 host -> windows x64 gnu target.
    "linux-x64-gnu": {
        "host_triple": "x86_64-unknown-linux-gnu",
        "target_triple": "x86_64-pc-windows-gnu",
        "compiler": "x86_64-w64-mingw32",
        "specs": (
            f"gcc_impl_win-64={GCC_VERSION}",
            f"gxx_impl_win-64={GCC_VERSION}",
        ),
    },
}

# Populated after the discovery build reports the resolved builds. Empty
# = capture-only (no lock enforcement) so pass 1 can succeed and reveal
# the plan. Hardened in a follow-up once the SHAs are known.
EXPECTED_LOCK_SHA256: dict[str, dict[str, str]] = {}

# The cross tools a win-gnu compile + link consumes, confirmed present as
# `bin/x86_64-w64-mingw32-<tool>` by the discovery build.
REQUIRED_TOOLS = ("gcc", "g++", "ar", "ranlib", "dlltool", "windres", "ld")

# PE\0\0 machine id for x86-64 (IMAGE_FILE_MACHINE_AMD64), little-endian.
_PE_MACHINE_AMD64 = 0x8664


def _validate_package(package: Path, compiler: str, output) -> None:
    """Assert the cross tools exist and that the toolchain actually
    cross-compiles + links a Windows PE from this Linux host."""
    bin_dir = package / "bin"
    missing = [
        f"bin/{compiler}-{tool}"
        for tool in REQUIRED_TOOLS
        if not (bin_dir / f"{compiler}-{tool}").is_file()
    ]
    if missing:
        raise RuntimeError(
            "mingw cross toolchain missing required tools: " + ", ".join(missing)
        )

    # Link smoke: compile a tiny program to a PE .exe with the cross gcc.
    src = package / "soldr-mingw-smoke.c"
    exe = package / "soldr-mingw-smoke.exe"
    src.write_text(
        "#include <stdio.h>\nint main(void){puts(\"soldr\");return 0;}\n",
        encoding="utf-8",
    )
    gcc = bin_dir / f"{compiler}-gcc"
    try:
        subprocess.run(
            [str(gcc), str(src), "-o", str(exe)],
            check=True,
            capture_output=True,
            text=True,
        )
        machine = _pe_machine(exe)
        if machine != _PE_MACHINE_AMD64:
            raise RuntimeError(
                f"link smoke produced machine 0x{machine:04x}; expected 0x{_PE_MACHINE_AMD64:04x} (amd64 PE)"
            )
        output.info(f"PE link smoke OK: {exe.name} is an x86-64 PE")
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"mingw cross link smoke failed: {detail}") from exc
    finally:
        src.unlink(missing_ok=True)
        exe.unlink(missing_ok=True)


def _pe_machine(path: Path) -> int:
    """Read the COFF machine field from a PE file. DOS header at 0x00
    (`MZ`), PE header offset at 0x3C, `PE\\0\\0` then the 2-byte machine."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise RuntimeError(f"{path.name} is not a PE/MZ image")
    pe_off = int.from_bytes(data[0x3C:0x40], "little")
    if data[pe_off : pe_off + 4] != b"PE\x00\x00":
        raise RuntimeError(f"{path.name} has no PE signature at 0x{pe_off:x}")
    return int.from_bytes(data[pe_off + 4 : pe_off + 6], "little")


def supported_shapes():
    return tuple(sorted(SHAPE_CONFIG))


def _locked_rows(payload, shape):
    rows = payload.get("actions", {}).get("LINK", [])
    expected = EXPECTED_LOCK_SHA256.get(shape, {})
    if expected:
        actual = {row["name"]: row.get("sha256") for row in rows}
        for name, want in expected.items():
            if actual.get(name) != want:
                raise RuntimeError(
                    f"locked conda artifact mismatch for {name}: "
                    f"expected {want}, got {actual.get(name)}"
                )
    return rows


def _discover_compiler_bin(package: Path) -> list[str]:
    """List candidate cross-gcc driver basenames under bin/ so the
    discovery build reports the real prefix (conda's exact spelling of
    `x86_64-w64-mingw32-gcc`)."""
    bin_dir = package / "bin"
    if not bin_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in bin_dir.iterdir()
        if p.is_file() and (p.name.endswith("gcc") or p.name.endswith("gcc.exe"))
    )


def build_bundle(*, version, shape, build_folder, output):
    if version not in PINNED_VERSIONS:
        raise ValueError(f"unsupported version {version}; supported: {PINNED_VERSIONS}")
    cfg = SHAPE_CONFIG.get(shape)
    if cfg is None:
        raise ValueError(f"unsupported shape {shape}; supported: {supported_shapes()}")
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required")

    work = Path(build_folder).resolve()
    package = work / "package"
    if package.exists():
        shutil.rmtree(package)
    work.mkdir(parents=True, exist_ok=True)

    command = [
        "docker", "run", "--rm", "--user", "0", "-v", f"{work}:/work",
        MICROMAMBA_IMAGE, "micromamba", "create", "--yes", "--prefix",
        "/work/package", "--channel", "conda-forge", "--json", *cfg["specs"],
    ]
    output.info(
        f"materializing conda-forge mingw cross toolchain for {cfg['target_triple']}"
    )
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"micromamba materialization failed: {detail}") from exc
    payload = json.loads(result.stdout)

    subprocess.run(
        [
            "docker", "run", "--rm", "--user", "0", "-v", f"{work}:/work",
            "--entrypoint", "/bin/chmod", MICROMAMBA_IMAGE,
            "-R", "a+rwX", "/work/package",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = _locked_rows(payload, shape)
    (package / "conda-plan.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )

    discovered_gcc = _discover_compiler_bin(package)
    if not discovered_gcc:
        raise RuntimeError(
            "no *-gcc driver found under bin/ in the materialized conda prefix; "
            "the conda-forge mingw cross package layout is not as expected"
        )
    output.info(f"discovered cross gcc driver(s): {discovered_gcc}")

    # Functional gate (confirmed layout `bin/x86_64-w64-mingw32-*`): the
    # tools exist AND the toolchain links a real x86-64 PE from Linux.
    _validate_package(package, cfg["compiler"], output)

    meta = {
        "tool": "mingw-w64-cross",
        "version": version,
        "shape": shape,
        "host_triple": cfg["host_triple"],
        "target_triple": cfg["target_triple"],
        "compiler_triple": cfg["compiler"],
        "gcc_version": GCC_VERSION,
        "solver_image": MICROMAMBA_IMAGE,
        "discovered_gcc_drivers": discovered_gcc,
        "required_tools": list(REQUIRED_TOOLS),
        "no_zig": True,
        "packages": [
            {k: row.get(k) for k in ("name", "version", "build", "url", "sha256", "license")}
            for row in rows
        ],
    }
    (work / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta
