"""Materialize SHA-locked conda-forge GNU/Linux toolchains."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

PINNED_VERSIONS = ("gcc-13.3.0-glibc-2.17-1",)
MICROMAMBA_IMAGE = (
    "mambaorg/micromamba:2.3.2@"
    "sha256:ce2026639d612fbe837a35ca9d513292a8d776a24a5e6c82cccf9dddf45ad3d2"
)
GCC_VERSION = "13.3.0"
GLIBC_VERSION = "2.17"
BINUTILS_VERSION = "2.40"

SHAPE_CONFIG = {
    "linux-x64-gnu": {
        "target": "x86_64-unknown-linux-gnu",
        "compiler": "x86_64-conda-linux-gnu",
        "specs": (
            "gcc_impl_linux-64=13.3.0=h1e990d8_2",
            "gxx_impl_linux-64=13.3.0=hae580e1_2",
            "binutils_impl_linux-64=2.40=ha1999f0_7",
            "sysroot_linux-64=2.17=h0157908_18",
            "libgcc=13.3.0=h767d61c_2",
            "libstdcxx=13.3.0=h8f9b012_2",
            "libgomp=13.3.0=h767d61c_2",
        ),
    },
    "linux-arm64-gnu": {
        "target": "aarch64-unknown-linux-gnu",
        "compiler": "aarch64-conda-linux-gnu",
        "specs": (
            "gcc_impl_linux-aarch64=13.3.0=h7df2b2f_2",
            "gxx_impl_linux-aarch64=13.3.0=h4b86524_2",
            "binutils_impl_linux-aarch64=2.40=hdca1da1_7",
            "sysroot_linux-aarch64=2.17=h68829e0_18",
        ),
    },
}

EXPECTED_LOCK_SHA256 = {
    "linux-x64-gnu": {
        "gcc_impl_linux-64": "c3e9f243ea8292eecad78bb200d8f5b590e0f82bf7e7452a3a7c8df4eea6f774",
        "gxx_impl_linux-64": "7cb36526a5c3e75ae07452aee5c9b6219f62fad9f85cc6d1dab5b21d1c4cc996",
        "binutils_impl_linux-64": "230f3136d17fdcf0e6da3a3ae59118570bc18106d79dd29bf2f341338d2a42c4",
        "sysroot_linux-64": "69ab5804bdd2e8e493d5709eebff382a72fab3e9af6adf93a237ccf8f7dbd624",
        "libgcc": "b22f7567e776f17930ecfcd8c4ae376f1874abd4b78817a34886eb16425d4a0b",
        "libstdcxx": "8177dfd0869b722237863bbeced9c79b1657b8ce21dce0d9f38157c5f9daed01",
        "libgomp": "7f839503e20bb3655c751abc6e4ac05878cf2cdec6b48d19c42a71e0ac20bdfe",
    },
    "linux-arm64-gnu": {
        "gcc_impl_linux-aarch64": "0684f8a490c534977f6a1593f6e42f31faf71f3f8495a30741ca85fde986e626",
        "gxx_impl_linux-aarch64": "920d0e944f8d168725e2601b995865b82b1be5eae7e1e75f85aa7a5390b30dc1",
        "binutils_impl_linux-aarch64": "d599b1823d569f16ddd660bf6ba2240efbcc6e63b83c7f87f9400d13dbf82680",
        "sysroot_linux-aarch64": "1e478bfd87c296829e62f0cae37e591568c2dcfc90ee6228c285bb1c7130b915",
    },
}

REQUIRED_TOOLS = ("gcc", "g++", "ar", "ranlib", "ld", "readelf", "strip", "objcopy")
_GLIBC_RE = re.compile(r"GLIBC_(\d+)\.(\d+)")


def supported_shapes():
    return tuple(sorted(SHAPE_CONFIG))


def _locked_rows(payload, shape):
    rows = payload.get("actions", {}).get("LINK", [])
    actual = {row["name"]: row.get("sha256") for row in rows}
    for name, expected in EXPECTED_LOCK_SHA256[shape].items():
        if actual.get(name) != expected:
            raise RuntimeError(
                f"locked conda artifact mismatch for {name}: "
                f"expected {expected}, got {actual.get(name)}"
            )
    return rows


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
    plan = work / "conda-plan.json"
    command = [
        "docker", "run", "--rm", "--user", "0", "-v", f"{work}:/work",
        MICROMAMBA_IMAGE, "micromamba", "create", "--yes", "--prefix",
        "/work/package", "--channel", "conda-forge", "--json", *cfg["specs"],
    ]
    output.info(f"materializing locked conda-forge artifacts for {cfg['target']}")
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
            "-R", "a+rX,u+w", "/work/package",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = _locked_rows(payload, shape)
    plan.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    measured = validate_package(package, cfg["compiler"])
    shutil.copy2(plan, package / "conda-plan.json")
    meta = {
        "tool": "gnu-linux-toolchain",
        "version": version,
        "shape": shape,
        "host_triple": "x86_64-unknown-linux-gnu",
        "target_triple": cfg["target"],
        "compiler_triple": cfg["compiler"],
        "gcc_version": GCC_VERSION,
        "binutils_version": BINUTILS_VERSION,
        "libc": "glibc",
        "glibc_version": GLIBC_VERSION,
        "measured_max_glibc": measured,
        "solver_image": MICROMAMBA_IMAGE,
        "packages": [
            {key: row.get(key) for key in ("name", "version", "build", "url", "sha256", "license")}
            for row in rows
        ],
        "no_zig": True,
    }
    (work / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def _versions(path, readelf):
    try:
        result = subprocess.run(
            [readelf, "--version-info", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"could not measure GLIBC versions in {path}: {exc}") from exc
    return {(int(a), int(b)) for a, b in _GLIBC_RE.findall(result.stdout)}


def _is_elf(path):
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError:
        return False


def validate_package(package, compiler):
    missing = [
        f"bin/{compiler}-{tool}"
        for tool in REQUIRED_TOOLS
        if not (package / "bin" / f"{compiler}-{tool}").is_file()
    ]
    sysroot = package / compiler / "sysroot"
    for rel in ("usr/include", "usr/lib"):
        if not (sysroot / rel).exists():
            missing.append(f"{compiler}/sysroot/{rel}")
    if missing:
        raise RuntimeError("toolchain missing required paths: " + ", ".join(missing))

    target_readelf = str(package / "bin" / f"{compiler}-readelf")
    libc_versions = set()
    for libc in sysroot.rglob("libc.so.6"):
        libc_versions.update(_versions(libc, target_readelf))
    if not libc_versions:
        raise RuntimeError("could not measure target libc GLIBC versions")
    measured = max(libc_versions)
    if measured > (2, 17):
        raise RuntimeError(f"target libc exports GLIBC_{measured}; required <= GLIBC_2.17")

    host_readelf = shutil.which("readelf")
    if not host_readelf:
        raise RuntimeError("host readelf is required")
    host_versions = set()
    for path in package.rglob("*"):
        if path.is_file() and _is_elf(path):
            host_versions.update(_versions(path, host_readelf))
    if host_versions and max(host_versions) > (2, 17):
        raise RuntimeError(f"host tool requires GLIBC_{max(host_versions)}")

    return f"{measured[0]}.{measured[1]}"
