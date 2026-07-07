"""Conan recipe: Windows x64 MinGW-w64 GCC bundle.

Pure download + repackage of the WinLibs standalone GCC + MinGW-w64
zip. Produces a relocatable ``bin/`` toolchain bundle consumed by soldr
for Rust's ``x86_64-pc-windows-gnu`` target.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy

_RECIPES_ROOT = Path(__file__).resolve().parent.parent
if str(_RECIPES_ROOT) not in sys.path:
    sys.path.insert(0, str(_RECIPES_ROOT))
import _mingw_w64_gcc as mingw_helper  # noqa: E402


class MingwW64GccWindowsX64Gnu(ConanFile):
    name = "mingw-w64-gcc-windows-x64-gnu"
    description = (
        "Pre-built WinLibs MinGW-w64 GCC bundle for windows-x64-gnu. "
        "Carries gcc/g++/binutils/windres, headers, import libraries, "
        "CRT/runtime files, and target sysroot needed by Rust GNU builds "
        "and cc-rs build scripts."
    )
    license = "GPL-3.0-or-later WITH GCC-exception-3.1"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    SHAPE = "windows-x64-gnu"

    def validate(self):
        if str(self.version) not in mingw_helper.PINNED_VERSIONS:
            raise ConanInvalidConfiguration(
                f"unsupported mingw-w64-gcc version {self.version}; supported: "
                f"{sorted(mingw_helper.PINNED_VERSIONS)}"
            )

    def build(self):
        mingw_helper.extract_bundle(
            version=str(self.version),
            shape=self.SHAPE,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(self, "*", src=Path(self.build_folder, "package").as_posix(), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
