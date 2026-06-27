"""Conan recipe — OpenSSL libs/headers for Windows MSVC x86_64.

soldr#943 (x64 half). FireDaemon mirror's `x64/` subdir from the
upstream-built `openssl-<ver>.zip`. Ships:

  bin/libssl-3-x64.dll
  bin/libcrypto-3-x64.dll
  bin/openssl.exe        ← optional, useful for cert gen during build
  lib/libssl.lib         ← MSVC import lib
  lib/libcrypto.lib      ← MSVC import lib
  include/openssl/*.h    ← public headers

Pin via `--version` at dispatch time. As of writing 3.5.x is current,
3.4.x is LTS; both work.

Dispatch:

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_ref=main \\
        -f recipe_path=recipes/openssl-windows-x64 \\
        -f name=openssl-windows-x64 \\
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


TARGET_TRIPLE = "x86_64-pc-windows-msvc"
ARCH_DIR = "x64"


class OpensslWindowsX64(ConanFile):
    name = "openssl-windows-x64"
    description = (
        "OpenSSL libs + headers for Windows MSVC x86_64 cross-compile. "
        "Source: FireDaemon's openssl-<ver>.zip mirror of upstream OpenSSL."
    )
    license = "Apache-2.0"
    package_type = "library"
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
