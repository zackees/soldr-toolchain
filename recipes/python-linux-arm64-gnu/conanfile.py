"""Conan recipe — aarch64-unknown-linux-gnu Python3 sysroot (python-build-standalone).

soldr#933 / soldr#997 Phase A. Sibling of recipes/python-windows-x64/;
see recipes/_python_pbs.py for the shared extraction logic.
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
pbs = _load_recipe_helper("soldr_recipe__python_pbs", "_python_pbs.py")
class PythonLinuxArm64Gnu(ConanFile):

    def export(self):
        copy(
            self,
            "_python_pbs.py",
            src=Path(__file__).resolve().parent.parent.as_posix(),
            dst=self.export_folder,
        )
    name = "python-linux-arm64-gnu"
    description = (
        "Pre-built aarch64-unknown-linux-gnu Python sysroot for PyO3 cross-compile. "
        "Source: astral-sh/python-build-standalone. lib/ + include/ only."
    )
    license = "PSF-2.0"
    package_type = "header-library"
    no_copy_source = True
    settings = "os", "arch"

    TARGET_TRIPLE = "aarch64-unknown-linux-gnu"

    def validate(self):
        if str(self.version) not in pbs.PBS_TAGS:
            raise ConanInvalidConfiguration(
                f"unsupported python version {self.version}; supported: "
                f"{sorted(pbs.PBS_TAGS.keys())}"
            )

    def build(self):
        pbs.extract_sysroot(
            py_version=str(self.version),
            target_triple=self.TARGET_TRIPLE,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(self, "*", src=Path(self.build_folder, "package").as_posix(), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
