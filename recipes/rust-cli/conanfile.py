"""Generic Conan recipe for Rust CLI support binaries.

The forge package name carries both the logical tool and the target
shape, for example ``cargo-chef-linux-x64-gnu`` or
``crgx-windows-arm64``. The shared helper parses that name and builds
the pinned crate into ``package/bin``.
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
import _rust_cli as rust_cli  # noqa: E402


class RustCliSupportBinary(ConanFile):
    description = "Rust CLI support binary bundle for soldr release archives."
    license = "MIT OR Apache-2.0"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    def _tool_shape(self):
        return rust_cli.parse_package_name(str(self.name))

    def validate(self):
        tool, shape = self._tool_shape()
        if str(self.version) not in rust_cli.supported_versions(tool):
            raise ConanInvalidConfiguration(
                f"unsupported {tool} version {self.version}; supported: "
                f"{rust_cli.supported_versions(tool)}"
            )
        if shape not in rust_cli.RUST_CLI_SHAPES:
            raise ConanInvalidConfiguration(
                f"unsupported {tool} shape {shape}; supported: {rust_cli.RUST_CLI_SHAPES}"
            )

    def build(self):
        tool, shape = self._tool_shape()
        rust_cli.build_tool(
            tool=tool,
            version=str(self.version),
            shape=shape,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(self, "*", src=Path(self.build_folder, "package").as_posix(), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
