"""Conan recipe — aarch64-apple-darwin Python3 sysroot (python-build-standalone).

soldr#932 / soldr#997 Phase A. Sibling of recipes/python-windows-x64/;
see recipes/_python_pbs.py for the shared extraction logic.
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
import _python_pbs as pbs  # noqa: E402


class PythonDarwinArm64(ConanFile):
    name = "python-darwin-arm64"
    description = (
        "Pre-built aarch64-apple-darwin Python sysroot for PyO3 cross-compile. "
        "Source: astral-sh/python-build-standalone. lib/ + include/ only."
    )
    license = "PSF-2.0"
    package_type = "header-library"
    no_copy_source = True
    settings = "os", "arch"

    TARGET_TRIPLE = "aarch64-apple-darwin"

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
