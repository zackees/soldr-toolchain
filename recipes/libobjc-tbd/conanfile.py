"""Conan recipe — extract libobjc.tbd from the runner's Xcode SDK.

soldr#988 follow-up: when cargo-zigbuild cross-builds Rust crates for
``*-apple-darwin`` targets, the Apple-SDK shim soldr ships under
``apple-sdk/MacOSX11.3`` MUST carry ``usr/lib/libobjc.tbd``. Stale or
shim-incomplete SDKs fail the link step with::

    error: unable to find dynamic system library 'objc' using strategy
    'paths_first'. searched paths: <build-script out dirs only>

This recipe runs on a macOS runner (where Xcode is naturally available)
and extracts the canonical ``libobjc*.tbd`` files from the active SDK
into the package layout cargo-zigbuild expects::

    pkg/
    └── usr/
        └── lib/
            ├── libobjc.tbd
            ├── libobjc.A.tbd
            └── libobjc-trampolines.tbd

Consumers (the soldr-toolchain ``apple-sdk/<version>/darwin-*`` path)
overlay these on top of an existing SDK to refresh the Objective-C
runtime tbd files without re-vendoring the whole SDK.

Forge invocation (from soldr-toolchain):

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_ref=main \\
        -f recipe_path=recipes/libobjc-tbd \\
        -f name=libobjc-tbd \\
        -f version=14.5 \\
        -f macos_x64=true \\
        -f macos_arm64=true \\
        -f windows_x64=false \\
        -f linux_x64=false

The version string tracks the macOS SDK version the tbd files were
extracted from (e.g. ``14.5`` for MacOSX14.5.sdk). The recipe
discovers this at build time via ``xcrun --sdk macosx --show-sdk-version``.
"""

import os
import shutil

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration


class LibobjcTbdConan(ConanFile):
    name = "libobjc-tbd"
    version = "14.5"
    package_type = "header-library"
    description = (
        "Apple Objective-C runtime tbd (text-based dylib) files extracted "
        "from the Xcode SDK on a macOS runner. Used by soldr-toolchain to "
        "overlay onto cross-compile SDKs that ship an incomplete "
        "usr/lib/libobjc.tbd set."
    )
    license = "Apple-MIT-Style"
    # No source, no build deps — we just read files from the runner's SDK.
    no_copy_source = True
    settings = "os", "arch"

    def validate(self):
        if str(self.settings.os) != "Macos":
            raise ConanInvalidConfiguration(
                "libobjc-tbd extracts from Xcode and only runs on a macOS host; "
                f"got os={self.settings.os}."
            )

    def build(self):
        sdk_path = self._sdk_path()
        self.output.info(f"reading libobjc tbd files from {sdk_path}")
        lib_src = os.path.join(sdk_path, "usr", "lib")
        lib_dst = os.path.join(self.build_folder, "usr", "lib")
        os.makedirs(lib_dst, exist_ok=True)
        copied = 0
        for name in ("libobjc.tbd", "libobjc.A.tbd", "libobjc-trampolines.tbd"):
            src = os.path.join(lib_src, name)
            dst = os.path.join(lib_dst, name)
            if not os.path.isfile(src):
                self.output.warning(f"{name}: not present in SDK, skipping")
                continue
            shutil.copy2(src, dst)
            copied += 1
            self.output.info(f"copied {name}")
        if copied == 0:
            raise ConanInvalidConfiguration(
                f"no libobjc*.tbd files found under {lib_src}; the macOS runner's "
                "Xcode install appears to be incomplete."
            )

    def package(self):
        # Move the assembled `usr/lib/` tree into the package folder.
        src_root = os.path.join(self.build_folder, "usr")
        dst_root = os.path.join(self.package_folder, "usr")
        if os.path.isdir(src_root):
            if os.path.exists(dst_root):
                shutil.rmtree(dst_root)
            shutil.copytree(src_root, dst_root)

    def _sdk_path(self) -> str:
        """Resolve the active macOS SDK path via xcrun."""
        import subprocess

        result = subprocess.run(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
