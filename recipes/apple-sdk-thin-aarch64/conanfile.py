"""Conan recipe — Apple macOS SDK lipo-thinned to arm64 only.

Sibling of ``recipes/apple-sdk-universal2/conanfile.py`` and
``recipes/apple-sdk-thin-x86_64/conanfile.py``. See those + the
issue #14 spec for the full architecture.

Note: the catalogue platform string is ``darwin-aarch64`` (Rust
target-triple convention) but Apple's lipo arch token is ``arm64``.
Both refer to the same architecture; ``lipo -thin`` requires the
Apple name.

Forge dispatch:

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_path=recipes/apple-sdk-thin-aarch64 \\
        -f name=apple-sdk-thin-aarch64 \\
        -f version=14.5 -f macos_arm64=true \\
        -f windows_x64=false -f linux_x64=false
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
import _apple_sdk_thin as thin  # noqa: E402


class AppleSDKThinAArch64(ConanFile):
    name = "apple-sdk-thin-aarch64"
    description = (
        "Apple macOS SDK lipo-thinned to arm64. Full SDK tree (headers, "
        "frameworks, .tbd files with archs: rewritten to [arm64]) per "
        "soldr-toolchain#14 full-SDK policy. No directory trimming."
    )
    license = "Apple-MIT-Style"
    package_type = "header-library"
    no_copy_source = True
    settings = "os", "arch"
    options = {"sdk_version": ("auto", "11.3", "13.3", "14.5", "15.2")}
    default_options = {"sdk_version": "auto"}

    TARGET_ARCH = "arm64"  # Apple's name; Rust calls this aarch64

    def validate(self):
        if str(self.settings.os) != "Macos":
            raise ConanInvalidConfiguration(
                f"{self.name} runs on macOS runners only; got os={self.settings.os}"
            )

    def build(self):
        sdk_root, captured_version, xcode_version = thin.resolve_sdk(
            self.options, self.output
        )
        self.output.info(f"thinning SDK at {sdk_root} → arch={self.TARGET_ARCH}")
        dest = Path(self.build_folder) / "sdk"
        thin.thin_sdk_tree(Path(sdk_root), self.TARGET_ARCH, dest, self.output)
        thin.write_meta(
            self.build_folder,
            shape="thin-aarch64",
            captured={
                "target_arch": self.TARGET_ARCH,
                "requested_sdk_version": str(self.options.sdk_version),
                "captured_sdk_version": captured_version,
                "xcode_version": xcode_version,
                "runner_arch": str(self.settings.arch),
            },
            log=self.output,
        )

    def package(self):
        copy(
            self,
            "*",
            src=Path(self.build_folder, "sdk").as_posix(),
            dst=Path(self.package_folder, "sdk").as_posix(),
        )
        copy(
            self,
            "meta.json",
            src=self.build_folder,
            dst=self.package_folder,
        )
