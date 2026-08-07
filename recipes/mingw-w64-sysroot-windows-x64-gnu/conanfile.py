"""Conan recipe: host-neutral MinGW-w64 sysroot for windows-x64-gnu.

soldr-toolchain#114. Repackages only the host-neutral subset of the
WinLibs standalone GCC + MinGW-w64 zip — the ``x86_64-w64-mingw32``
target sysroot (headers, import libraries, CRT startup objects) plus the
``lib/gcc/x86_64-w64-mingw32`` runtime tree — and drops every host
executable. The result is a relocatable sysroot bundle a linker (e.g.
``zackees/reld``, which brings its own engine and bridges to ``lld`` for
PE-COFF) can fetch from any host to link Rust's ``x86_64-pc-windows-gnu``
target.
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


sysroot_helper = _load_recipe_helper(
    "soldr_recipe__mingw_w64_sysroot", "_mingw_w64_sysroot.py"
)


class MingwW64SysrootWindowsX64Gnu(ConanFile):

    def export(self):
        copy(
            self,
            "_mingw_w64_sysroot.py",
            src=Path(__file__).resolve().parent.parent.as_posix(),
            dst=self.export_folder,
        )

    name = "mingw-w64-sysroot-windows-x64-gnu"
    description = (
        "Host-neutral MinGW-w64 sysroot for windows-x64-gnu. Carries the "
        "x86_64-w64-mingw32 headers, import libraries, CRT startup objects, "
        "and the gcc runtime (libgcc.a, crtbegin.o, crtend.o) needed to link "
        "Rust's x86_64-pc-windows-gnu target from any host. No host "
        "executables."
    )
    license = "GPL-3.0-or-later WITH GCC-exception-3.1"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    SHAPE = "windows-x64-gnu"

    def validate(self):
        if str(self.version) not in sysroot_helper.PINNED_VERSIONS:
            raise ConanInvalidConfiguration(
                f"unsupported mingw-w64-sysroot version {self.version}; supported: "
                f"{sorted(sysroot_helper.PINNED_VERSIONS)}"
            )

    def build(self):
        sysroot_helper.extract_sysroot(
            version=str(self.version),
            shape=self.SHAPE,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(self, "*", src=Path(self.build_folder, "package").as_posix(), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
