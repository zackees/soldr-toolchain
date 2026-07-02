"""Conan recipe — `linux-arm64-musl` uv prebuilt bundle.

Pure download + repackage of the official upstream release binaries
(github.com/astral-sh/uv releases). Lets soldr provision uv on
demand from the toolchain catalogue instead of trusting whatever
uv happens to be on the user's PATH.

Sibling of the other recipes/uv-*/ shapes; see
recipes/_uv.py for the shared download + extraction logic.
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
import _uv as uv_helper  # noqa: E402


class UvLinuxArm64Musl(ConanFile):
    name = "uv-linux-arm64-musl"
    description = (
        "Pre-built uv bundle for linux-arm64-musl. Source: official astral-sh/uv release binaries. Carries bin/uv + bin/uvx."
    )
    license = "Apache-2.0 OR MIT"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    SHAPE = "linux-arm64-musl"

    def validate(self):
        if str(self.version) not in uv_helper.PINNED_VERSIONS:
            raise ConanInvalidConfiguration(
                f"unsupported uv version {self.version}; supported: "
                f"{sorted(uv_helper.PINNED_VERSIONS)}"
            )

    def build(self):
        uv_helper.extract_bundle(
            version=str(self.version),
            shape=self.SHAPE,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(self, "*", src=Path(self.build_folder, "package").as_posix(), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
