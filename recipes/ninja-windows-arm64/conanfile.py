"""Conan recipe — `windows-arm64` Ninja prebuilt bundle.

Pure download + repackage of the official upstream release binaries
(github.com/ninja-build/ninja releases). Lets soldr provision ninja on
demand from the toolchain catalogue instead of trusting whatever
ninja happens to be on the user's PATH.

Sibling of the other recipes/ninja-*/ shapes; see
recipes/_ninja.py for the shared download + extraction logic.
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
import _ninja as ninja_helper  # noqa: E402


class NinjaWindowsArm64(ConanFile):
    name = "ninja-windows-arm64"
    description = (
        "Pre-built Ninja bundle for windows-arm64. Source: official ninja-build/ninja release binaries. Carries just bin/ninja."
    )
    license = "Apache-2.0"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    SHAPE = "windows-arm64"

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
