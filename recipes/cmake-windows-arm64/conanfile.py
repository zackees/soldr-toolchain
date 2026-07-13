"""Conan recipe — `windows-arm64` CMake prebuilt bundle.

Pure download + repackage of the official upstream release binaries
(github.com/Kitware/CMake releases). Lets soldr provision cmake on
demand from the toolchain catalogue instead of trusting whatever
cmake happens to be on the user's PATH.

Sibling of the other recipes/cmake-*/ shapes; see
recipes/_cmake.py for the shared download + extraction logic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy


def _load_recipe_helper(module_name: str, filename: str):
    recipe_dir = Path(__file__).resolve().parent
    for candidate in (recipe_dir, recipe_dir.parent):
        helper_path = candidate / filename
        if not helper_path.is_file():
            continue
        spec = importlib.util.spec_from_file_location(module_name, helper_path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    raise ImportError(f"Conan recipe helper {filename} was not exported beside the recipe")
cmake_helper = _load_recipe_helper("soldr_recipe__cmake", "_cmake.py")
class CmakeWindowsArm64(ConanFile):

    def export(self):
        copy(
            self,
            "_cmake.py",
            src=Path(__file__).resolve().parent.parent.as_posix(),
            dst=self.export_folder,
        )
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
