"""Conan recipe — `windows-arm64` CMake prebuilt bundle.

Pure download + repackage of the official upstream release binaries
(github.com/Kitware/CMake releases). Lets soldr provision cmake on
demand from the toolchain catalogue instead of trusting whatever
cmake happens to be on the user's PATH.

Sibling of the other recipes/cmake-*/ shapes; see
recipes/_cmake.py for the shared download + extraction logic.
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
import _cmake as cmake_helper  # noqa: E402


class CmakeWindowsArm64(ConanFile):
    name = "cmake-windows-arm64"
    description = (
        "Pre-built CMake bundle for windows-arm64. Source: official Kitware/CMake release binaries. Carries bin/cmake + bin/ctest + bin/cpack and the full share/cmake-<maj.min>/ module tree; docs/man dropped."
    )
    license = "BSD-3-Clause"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    SHAPE = "windows-arm64"

    def validate(self):
        if str(self.version) not in cmake_helper.PINNED_VERSIONS:
            raise ConanInvalidConfiguration(
                f"unsupported cmake version {self.version}; supported: "
                f"{sorted(cmake_helper.PINNED_VERSIONS)}"
            )

    def build(self):
        cmake_helper.extract_bundle(
            version=str(self.version),
            shape=self.SHAPE,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(self, "*", src=Path(self.build_folder, "package").as_posix(), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
