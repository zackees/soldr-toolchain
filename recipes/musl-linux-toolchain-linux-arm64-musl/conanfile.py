"""Pinned musl.cc aarch64 compiler/sysroot bundle."""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path
from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy

def _load_helper():
    for candidate in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        path = candidate / "_musl_linux_toolchain.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("soldr_recipe__musl_linux_toolchain", path)
            module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
            return module
    raise ImportError("_musl_linux_toolchain.py was not exported beside the recipe")
helper = _load_helper()

class MuslLinuxToolchainArm64(ConanFile):
    name = "musl-linux-toolchain-linux-arm64-musl"
    description = "Digest-pinned musl.cc GCC/binutils/C++ toolchain with static AArch64 musl CRT"
    license = "GPL-3.0-or-later WITH GCC-exception-3.1 AND MIT"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"
    SHAPE = "linux-arm64-musl"
    def export(self):
        copy(self, "_musl_linux_toolchain.py", src=Path(__file__).resolve().parent.parent.as_posix(), dst=self.export_folder)
    def validate(self):
        if str(self.version) not in helper.PINNED_VERSIONS:
            raise ConanInvalidConfiguration(f"unsupported version {self.version}; supported: {helper.PINNED_VERSIONS}")
    def build(self):
        helper.build_bundle(version=str(self.version), shape=self.SHAPE, build_folder=Path(self.build_folder), output=self.output)
    def package(self):
        copy(self, "*", src=Path(self.build_folder, "package").as_posix(), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
