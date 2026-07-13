"""Conan recipe — Apple macOS SDK lipo-thinned to x86_64 only.

Sibling of ``recipes/apple-sdk-universal2/conanfile.py`` and
``recipes/apple-sdk-thin-aarch64/conanfile.py``. See those + the
issue #14 spec for the full architecture.

Forge dispatch:

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_path=recipes/apple-sdk-thin-x86_64 \\
        -f name=apple-sdk-thin-x86_64 \\
        -f version=14.5 -f macos_arm64=true \\
        -f windows_x64=false -f linux_x64=false

Either macOS runner (arm64 or x64) is fine — ``lipo`` is bidirectional.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy

# Helpers live in the recipes/ root so the three thin recipes can
# share them. Conan loads the copied helper from the recipe export directory.

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
thin = _load_recipe_helper("soldr_recipe__apple_sdk_thin", "_apple_sdk_thin.py")
class AppleSDKThinX86_64(ConanFile):

    def export(self):
        copy(
            self,
            "_apple_sdk_thin.py",
            src=Path(__file__).resolve().parent.parent.as_posix(),
            dst=self.export_folder,
        )
    name = "apple-sdk-thin-x86_64"
    description = (
        "Apple macOS SDK lipo-thinned to x86_64. Full SDK tree (headers, "
        "frameworks, .tbd files with archs: rewritten to [x86_64]) per "
        "soldr-toolchain#14 full-SDK policy. No directory trimming."
    )
    license = "Apple-MIT-Style"
    package_type = "header-library"
    no_copy_source = True
    settings = "os", "arch"
    options = {"sdk_version": ("auto", "11.3", "13.3", "14.5", "15.2")}
    default_options = {"sdk_version": "auto"}

    TARGET_ARCH = "x86_64"

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
            shape="thin-x86_64",
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
