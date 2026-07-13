"""Conan recipe — `linux-arm64-gnu` Ninja prebuilt bundle.

Pure download + repackage of the official upstream release binaries
(github.com/ninja-build/ninja releases). Lets soldr provision ninja on
demand from the toolchain catalogue instead of trusting whatever
ninja happens to be on the user's PATH.

Sibling of the other recipes/ninja-*/ shapes; see
recipes/_ninja.py for the shared download + extraction logic.
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
ninja_helper = _load_recipe_helper("soldr_recipe__ninja", "_ninja.py")
class NinjaLinuxArm64Gnu(ConanFile):

    def export(self):
        copy(
            self,
            "_ninja.py",
            src=Path(__file__).resolve().parent.parent.as_posix(),
            dst=self.export_folder,
        )
    name = "ninja-linux-arm64-gnu"
    description = (
        "Pre-built Ninja bundle for linux-arm64-gnu. Source: official ninja-build/ninja release binaries. Carries just bin/ninja."
    )
    license = "Apache-2.0"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    SHAPE = "linux-arm64-gnu"

    def validate(self):
        if str(self.version) not in ninja_helper.PINNED_VERSIONS:
            raise ConanInvalidConfiguration(
                f"unsupported ninja version {self.version}; supported: "
                f"{sorted(ninja_helper.PINNED_VERSIONS)}"
            )

    def build(self):
        ninja_helper.extract_bundle(
            version=str(self.version),
            shape=self.SHAPE,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(self, "*", src=Path(self.build_folder, "package").as_posix(), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
