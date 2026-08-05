"""Materialize digest-pinned musl.cc cross compiler bundles without Zig."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

PINNED_VERSIONS = ("gcc-11.2.1-musl-20211123-1",)
GCC_VERSION = "11.2.1"
MUSL_VERSION = "1.2.2"
SHAPE_CONFIG = {
    "linux-x64-musl": {
        "target": "x86_64-unknown-linux-musl", "compiler": "x86_64-linux-musl",
        "archive": "x86_64-linux-musl-cross.tgz",
        "sha256": "c5d410d9f82a4f24c549fe5d24f988f85b2679b452413a9f7e5f7b956f2fe7ea",
        "machine": "Advanced Micro Devices X86-64",
    },
    "linux-arm64-musl": {
        "target": "aarch64-unknown-linux-musl", "compiler": "aarch64-linux-musl",
        "archive": "aarch64-linux-musl-cross.tgz",
        "sha256": "c909817856d6ceda86aa510894fa3527eac7989f0ef6e87b5721c58737a06c38",
        "machine": "AArch64",
    },
}
REQUIRED_TOOLS = ("gcc", "g++", "ar", "ranlib", "ld", "readelf", "strip", "objcopy")
REQUIRED_TARGET_PATHS = ("include", "lib", "lib/crt1.o", "lib/rcrt1.o", "lib/crti.o", "lib/crtn.o", "lib/libc.a", "lib/libstdc++.a")


def supported_shapes() -> tuple[str, ...]:
    return tuple(sorted(SHAPE_CONFIG))


def source_url(config: dict[str, str]) -> str:
    return f"https://musl.cc/{config['archive']}"


def _download_verified(url: str, expected_sha256: str, destination: Path) -> None:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=600) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"musl toolchain archive sha256 mismatch: expected {expected_sha256}, got {actual}")


def _safe_extract(archive: Path, destination: Path, expected_root: str) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != expected_root:
                raise RuntimeError(f"unsafe or unexpected path in musl toolchain archive: {member.name!r}")
            if (member.issym() or member.islnk()) and PurePosixPath(member.linkname).is_absolute() and not _is_musl_loader_link(member, expected_root):
                raise RuntimeError(f"absolute link in musl toolchain archive: {member.name!r}")
        tar.extractall(destination)


def _is_musl_loader_link(member: tarfile.TarInfo, expected_root: str) -> bool:
    """Allow musl.cc's known loader alias, which is normalized after extraction."""
    path = PurePosixPath(member.name)
    return (
        member.linkname == "/lib/libc.so"
        and len(path.parts) == 4
        and path.parts[0] == expected_root
        and path.parts[1].endswith("-linux-musl")
        and path.parts[2] == "lib"
        and path.name.startswith("ld-musl-")
        and path.name.endswith(".so.1")
    )


def _normalize_loader_link(package: Path, compiler: str) -> None:
    loader = next((package / compiler / "lib").glob("ld-musl-*.so.1"), None)
    if loader is None or not loader.is_symlink():
        raise RuntimeError("musl toolchain is missing its dynamic-loader link")
    if loader.readlink().as_posix() != "/lib/libc.so":
        raise RuntimeError(f"unexpected musl dynamic-loader link: {loader.readlink()}")
    loader.unlink()
    loader.symlink_to("libc.so")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
        raise RuntimeError(f"musl toolchain command failed: {' '.join(command)}: {detail}") from exc


def validate_package(package: Path, config: dict[str, str]) -> None:
    compiler = config["compiler"]
    missing = [f"bin/{compiler}-{tool}" for tool in REQUIRED_TOOLS if not (package / "bin" / f"{compiler}-{tool}").is_file()]
    target_root = package / compiler
    missing.extend(rel for rel in REQUIRED_TARGET_PATHS if not (target_root / rel).exists())
    if missing:
        raise RuntimeError("musl toolchain missing required paths: " + ", ".join(missing))
    source, output = package / "soldr-musl-smoke.cc", package / "soldr-musl-smoke"
    source.write_text("#include <iostream>\nint main() { std::cout << \"soldr\\n\"; }\n", encoding="utf-8")
    try:
        _run([str(package / "bin" / f"{compiler}-g++"), "-static", str(source), "-o", str(output)])
        header = _run([str(package / "bin" / f"{compiler}-readelf"), "--file-header", str(output)]).stdout
        if f"Machine:                           {config['machine']}" not in header:
            raise RuntimeError(f"musl smoke target machine mismatch: expected {config['machine']}")
        program_headers = _run([str(package / "bin" / f"{compiler}-readelf"), "--program-headers", str(output)]).stdout
        dynamic = _run([str(package / "bin" / f"{compiler}-readelf"), "--dynamic", str(output)]).stdout
        if "INTERP" in program_headers or "NEEDED" in dynamic:
            raise RuntimeError("musl smoke artifact is dynamically linked")
    finally:
        source.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def build_bundle(*, version: str, shape: str, build_folder: Path, output) -> dict:
    if version not in PINNED_VERSIONS:
        raise ValueError(f"unsupported version {version}; supported: {PINNED_VERSIONS}")
    config = SHAPE_CONFIG.get(shape)
    if config is None:
        raise ValueError(f"unsupported shape {shape}; supported: {supported_shapes()}")
    work, package, staging = Path(build_folder).resolve(), Path(build_folder).resolve() / "package", Path(build_folder).resolve() / "extract"
    archive = work / config["archive"]
    shutil.rmtree(package, ignore_errors=True); shutil.rmtree(staging, ignore_errors=True); work.mkdir(parents=True, exist_ok=True)
    url = source_url(config); output.info(f"downloading pinned musl.cc toolchain {url}")
    _download_verified(url, config["sha256"], archive)
    _safe_extract(archive, staging, f"{config['compiler']}-cross")
    shutil.move(str(staging / f"{config['compiler']}-cross"), package); archive.unlink(missing_ok=True)
    _normalize_loader_link(package, config["compiler"])
    validate_package(package, config)
    meta = {"tool": "musl-linux-toolchain", "version": version, "shape": shape, "host_triple": "x86_64-unknown-linux-gnu", "target_triple": config["target"], "compiler_triple": config["compiler"], "gcc_version": GCC_VERSION, "libc": "musl", "musl_version": MUSL_VERSION, "source_url": url, "source_sha256": config["sha256"], "static_link_verified": True, "no_zig": True}
    (work / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta
