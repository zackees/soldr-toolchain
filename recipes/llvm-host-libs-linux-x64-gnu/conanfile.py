"""Host runtime libraries for the managed LLVM lld (soldr#3774)."""

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
        path = candidate / "_llvm_host_libs.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("soldr_recipe__llvm_host_libs", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError("_llvm_host_libs.py was not exported beside the recipe")


helper = _load_helper()


class LlvmHostLibsX64(ConanFile):
    name = "llvm-host-libs-linux-x64-gnu"
    description = "glibc-2.17 libxml2.so.2 + libz.so.1 for the managed LLVM lld (linux-x64-gnu host)"
    license = "MIT AND Zlib"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"
    SHAPE = "linux-x64-gnu"

    def export(self):
        copy(
            self,
            "_llvm_host_libs.py",
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
