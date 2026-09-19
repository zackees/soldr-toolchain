"""Shared helpers for Rust CLI support-binary recipes.

The generic ``recipes/rust-cli`` Conan recipe builds small Rust command
line tools that soldr bundles into release archives. The output is a
standard catalogue bundle:

    package/
      bin/<tool>[.exe]
      meta.json

The recipe intentionally uses the host runner selected by forge for the
requested shape. Linux musl shapes rely on forge's existing musl-tools
setup before Conan invokes the recipe.

The compiler is pinned: ``RUST_TOOLCHAIN`` is installed explicitly and
exported as ``RUSTUP_TOOLCHAIN`` for the build, so the published binary
never depends on whatever ``stable`` the runner image happens to carry.
It tracks the toolchain pinned by zackees/soldr's ``rust-toolchain.toml``
and forge's ``forge-rust.yml`` default.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


# Exact rustc release every rust-cli bundle is compiled with. Never a
# floating channel (``stable``/``beta``/``nightly``).
RUST_TOOLCHAIN = "1.98.1"

RUST_CLI_SHAPES = (
    "windows-x64",
    "windows-arm64",
    "darwin-x64",
    "darwin-arm64",
    "linux-x64-gnu",
    "linux-arm64-gnu",
    "linux-x64-musl",
    "linux-arm64-musl",
)

TOOL_CONFIG = {
    "cargo-chef": {
        "crate": "cargo-chef",
        "binary": "cargo-chef",
        "versions": ("0.1.73",),
    },
    "crgx": {
        "crate": "crgx",
        "binary": "crgx",
        "versions": ("0.1.0",),
    },
    "cargo-binstall": {
        "crate": "cargo-binstall",
        "binary": "cargo-binstall",
        "versions": ("1.20.1",),
    },
    "cargo-nextest": {
        "crate": "cargo-nextest",
        "binary": "cargo-nextest",
        "versions": ("0.9.140",),
    },
}

TARGET_TRIPLES = {
    "windows-x64": "x86_64-pc-windows-msvc",
    "windows-arm64": "aarch64-pc-windows-msvc",
    "darwin-x64": "x86_64-apple-darwin",
    "darwin-arm64": "aarch64-apple-darwin",
    "linux-x64-gnu": "x86_64-unknown-linux-gnu",
    "linux-arm64-gnu": "aarch64-unknown-linux-gnu",
    "linux-x64-musl": "x86_64-unknown-linux-musl",
    "linux-arm64-musl": "aarch64-unknown-linux-musl",
}


def parse_package_name(package_name: str) -> tuple[str, str]:
    """Return ``(tool, shape)`` from names like
    ``cargo-chef-linux-x64-gnu``.
    """
    for tool in sorted(TOOL_CONFIG, key=len, reverse=True):
        prefix = f"{tool}-"
        if package_name.startswith(prefix):
            shape = package_name[len(prefix):]
            if shape not in RUST_CLI_SHAPES:
                raise ValueError(
                    f"unsupported {tool} shape {shape}; supported: {RUST_CLI_SHAPES}"
                )
            return tool, shape
    raise ValueError(
        f"unsupported rust CLI package name {package_name}; "
        f"expected one of: {', '.join(TOOL_CONFIG)}"
    )


def supported_versions(tool: str) -> tuple[str, ...]:
    return TOOL_CONFIG[tool]["versions"]


def build_tool(
    *,
    tool: str,
    version: str,
    shape: str,
    build_folder: Path,
    output,
) -> dict:
    config = TOOL_CONFIG[tool]
    if version not in config["versions"]:
        raise ValueError(
            f"unsupported {tool} version {version}; supported: {config['versions']}"
        )
    target = TARGET_TRIPLES[shape]
    crate = config["crate"]
    binary = config["binary"]
    exe = ".exe" if shape.startswith("windows-") else ""

    staging_root = build_folder / "cargo-install-root"
    cargo_home = build_folder / "cargo-home"
    target_dir = build_folder / "target"
    package_root = build_folder / "package"
    bin_dir = package_root / "bin"
    for path in (staging_root, cargo_home, target_dir, bin_dir):
        path.mkdir(parents=True, exist_ok=True)

    _run(toolchain_install_command(target), output=output)

    env = os.environ.copy()
    # RUSTUP_TOOLCHAIN outranks rustup's default and any rust-toolchain.toml,
    # so every rustc/cargo proxy below resolves to the pinned release.
    env["RUSTUP_TOOLCHAIN"] = RUST_TOOLCHAIN
    rustc_version = verify_rustc_version(
        subprocess.run(
            ["rustc", "--version"], check=True, env=env, capture_output=True, text=True
        ).stdout
    )
    output.info(rustc_version)
    env["CARGO_HOME"] = str(cargo_home)
    env["CARGO_TARGET_DIR"] = str(target_dir)
    if shape == "linux-x64-musl":
        env.setdefault("CC_x86_64_unknown_linux_musl", "musl-gcc")
        env.setdefault("CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER", "musl-gcc")
    elif shape == "linux-arm64-musl":
        env.setdefault("CC_aarch64_unknown_linux_musl", "musl-gcc")
        env.setdefault("CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER", "musl-gcc")

    _run(
        [
            "cargo",
            "install",
            crate,
            "--version",
            version,
            "--target",
            target,
            "--root",
            str(staging_root),
            "--force",
            "--locked",
        ],
        env=env,
        output=output,
    )

    built = staging_root / "bin" / f"{binary}{exe}"
    if not built.is_file():
        raise RuntimeError(f"cargo install did not produce expected binary: {built}")
    final = bin_dir / f"{binary}{exe}"
    shutil.copy2(built, final)
    final.chmod(0o755)

    meta = {
        "tool": tool,
        "crate": crate,
        "version": version,
        "shape": shape,
        "target_triple": target,
        "binary": f"bin/{binary}{exe}",
        "source": f"crates.io:{crate}@{version}",
        "rust_toolchain": RUST_TOOLCHAIN,
        "rustc_version": rustc_version,
    }
    (build_folder / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta


def toolchain_install_command(target: str) -> list[str]:
    """Install the pinned toolchain plus ``target`` (idempotent)."""
    return [
        "rustup",
        "toolchain",
        "install",
        RUST_TOOLCHAIN,
        "--profile",
        "minimal",
        "--target",
        target,
        "--no-self-update",
    ]


def verify_rustc_version(rustc_version: str) -> str:
    """Require ``rustc --version`` output to report exactly RUST_TOOLCHAIN."""
    line = rustc_version.strip()
    if not line.startswith(f"rustc {RUST_TOOLCHAIN} "):
        raise RuntimeError(
            f"compiler reported {line!r}, expected rustc {RUST_TOOLCHAIN}"
        )
    return line


def _run(cmd: list[str], *, output, env: dict[str, str] | None = None) -> None:
    output.info("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True, env=env)
