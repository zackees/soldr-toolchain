"""Conan recipe — OpenSSL libs/headers for Windows MSVC aarch64.

soldr#943 (arm64 half). FireDaemon mirror's `arm64/` subdir. See
sister recipe `openssl-windows-x64/` for source / license / layout
discussion — this recipe just swaps the per-arch subdir.

Dispatch:

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_ref=main \\
        -f recipe_path=recipes/openssl-windows-arm64 \\
        -f name=openssl-windows-arm64 \\
        -f version=3.5.0 \\
        -f linux_x64=true \\
        -f windows_x64=false -f macos_arm64=false
"""

from __future__ import annotations

import sys
from pathlib import Path

from conan import ConanFile
from conan.tools.files import copy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _openssl_firedaemon import extract_arch  # noqa: E402


TARGET_TRIPLE = "aarch64-pc-windows-msvc"
ARCH_DIR = "arm64"


class OpensslWindowsArm64(ConanFile):
    name = "openssl-windows-arm64"
    description = (
        "OpenSSL libs + headers for Windows MSVC aarch64 cross-compile. "
        "Source: FireDaemon's openssl-<ver>.zip mirror of upstream OpenSSL."
    )
    license = "Apache-2.0"
    package_type = "header-library"
    no_copy_source = True
    settings = "os", "arch"

    def build(self):
        extract_arch(
            version=str(self.version),
            arch_dir=ARCH_DIR,
            target_triple=TARGET_TRIPLE,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(
            self,
            "*",
            src=Path(self.build_folder, "package").as_posix(),
            dst=self.package_folder,
        )
        copy(
            self,
            "meta.json",
            src=self.build_folder,
            dst=self.package_folder,
        )
