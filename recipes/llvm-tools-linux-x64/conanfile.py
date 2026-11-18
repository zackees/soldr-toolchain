"""LLVM 21.1.5 Linux x64 host tools built inside Forge's glibc-2.17 image.

The official Linux archive requires GLIBC2.34 and cannot satisfy the catalogue
floor. Build clang/lld/binutils and shared LLVM/Clang libraries from pinned
source instead; the output includes Darwin tool aliases and resource headers.
"""

from __future__ import annotations

import io
import importlib.util
import json
import lzma
import tarfile
import urllib.request
from pathlib import Path

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy


LLVM_VERSION_DEFAULT = "21.1.5"
# LLVM upstream renamed the linux archives in the 21.x cycle:
#   old (≤ 20.x): clang+llvm-<ver>-x86_64-linux-gnu-ubuntu-<distro>.tar.xz
#   new (≥ 21.x): LLVM-<ver>-Linux-X64.tar.xz
# Recipe targets the new format; pin a ≥21 release at dispatch time.
ASSET_NAME_TPL = "LLVM-{ver}-Linux-X64.tar.xz"


def _load_recipe_helper():
    root = Path(__file__).resolve().parent
    for directory in (root, root.parent):
        path = directory / "_darwin_cross.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("soldr_recipe__darwin_cross", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise ImportError("_darwin_cross.py was not exported beside the recipe")


class LlvmToolsLinuxX64(ConanFile):
    name = "llvm-tools-linux-x64"
    description = (
        "LLVM toolchain bundle (clang/clang-cl/lld-link/llvm-lib/llvm-rc/"
        "llvm-dlltool/llvm-strip + libLLVM.so) for Linux x64 cross-compile "
        "drivers. Covers what cargo-xwin / rust-objcopy need beyond xwin's "
        "MSVC CRT cache."
    )
    license = "Apache-2.0 WITH LLVM-exception"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    WHITELIST_BIN = (
        "clang",
        "clang++",
        "clang-cl",
        "lld",
        "lld-link",
        "ld.lld",
        "ld64.lld",
        "llvm-lib",
        "llvm-rc",
        "llvm-dlltool",
        "llvm-strip",
        "llvm-objcopy",
        "llvm-ar",
        "llvm-readobj",
        "dsymutil",
        "llvm-dsymutil",
    )

    def export(self):
        copy(self, "_darwin_cross.py", src=str(Path(__file__).resolve().parent.parent), dst=self.export_folder)

    def validate(self):
        if str(self.settings.os) != "Linux" or str(self.settings.arch) != "x86_64":
            raise ConanInvalidConfiguration("LLVM host producer requires Linux x86_64")

    def build(self):
        if str(self.version) != "21.1.5":
            raise ConanInvalidConfiguration("source producer is pinned to LLVM 21.1.5")
        _load_recipe_helper().build_llvm_tools(Path(self.build_folder))

    def package(self):
        copy(
            self,
            "*",
            src=Path(self.build_folder, "package").as_posix(),
            dst=self.package_folder,
        )
        copy(
            self,
            "meta.json",
            src=self.build_folder,
            dst=self.package_folder,
        )
