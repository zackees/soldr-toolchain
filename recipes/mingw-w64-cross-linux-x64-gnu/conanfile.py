"""Linux-hosted MinGW-w64 GCC cross toolchain (x64 host -> win-gnu).

soldr-toolchain#114 Phase 2 / soldr#2336. Materializes a relocatable
`x86_64-w64-mingw32-*` cross toolchain from conda-forge so
`soldr build --target x86_64-pc-windows-gnu` can compile + link win-gnu
from a Linux host with a real gcc driver.
"""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path
from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy


def _load_helper():
    recipe_dir = Path(__file__).resolve().parent
    for candidate in (recipe_dir, recipe_dir.parent):
        path = candidate / "_mingw_w64_cross.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("soldr_recipe__mingw_w64_cross", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError("_mingw_w64_cross.py was not exported beside the recipe")


helper = _load_helper()


class MingwW64CrossLinuxX64Gnu(ConanFile):
    name = "mingw-w64-cross-linux-x64-gnu"
    description = (
        "Linux x86_64-host MinGW-w64 GCC cross toolchain "
        "(x86_64-w64-mingw32-gcc/g++/ar/ranlib/dlltool/windres) for building "
        "Rust's x86_64-pc-windows-gnu target from Linux with a real gcc driver."
    )
    license = "GPL-3.0-or-later WITH GCC-exception-3.1 AND LGPL-2.1-or-later"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"
    SHAPE = "linux-x64-gnu"

    def export(self):
        copy(
            self,
            "_mingw_w64_cross.py",
            src=Path(__file__).resolve().parent.parent.as_posix(),
            dst=self.export_folder,
        )

    def validate(self):
        if str(self.version) not in helper.PINNED_VERSIONS:
            raise ConanInvalidConfiguration(
                f"unsupported version {self.version}; supported: {helper.PINNED_VERSIONS}"
            )

    def build(self):
        helper.build_bundle(
            version=str(self.version),
            shape=self.SHAPE,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(self, "*", src=Path(self.build_folder, "package").as_posix(), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
