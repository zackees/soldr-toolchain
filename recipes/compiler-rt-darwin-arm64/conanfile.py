"""Linux-hosted producer for Darwin cross compilation."""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path
from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy


def _load_helper():
    root = Path(__file__).resolve().parent
    for directory in (root, root.parent):
        path = directory / "_darwin_cross.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("soldr_recipe__darwin_cross", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError("_darwin_cross.py was not exported beside the recipe")


helper = _load_helper()


class CompilerRtDarwinArm64(ConanFile):
    name = "compiler-rt-darwin-arm64"
    license = "Apache-2.0 WITH LLVM-exception"
    package_type = "application"
    settings = "os", "arch"
    no_copy_source = True

    def export(self):
        root = Path(__file__).resolve().parent.parent
        for filename in ("_darwin_cross.py", "_provision_darwin.py"):
            copy(self, filename, src=str(root), dst=self.export_folder)
        for filename in ("__init__.py", "generation_reader.py", "catalogue_v2.py", "publication_model.py"):
            copy(self, filename, src=str(root.parent / "scripts"), dst=str(Path(self.export_folder) / "scripts"))

    def validate(self):
        if str(self.settings.os) != "Linux" or str(self.settings.arch) != "x86_64":
            raise ConanInvalidConfiguration("producer requires Linux x86_64")
        if str(self.version) != helper.LLVM_VERSION:
            raise ConanInvalidConfiguration("unsupported version; producer is pinned to " + helper.LLVM_VERSION)

    def build(self):
        helper.build_compiler_rt(Path(self.build_folder))

    def package(self):
        copy(self, "*", src=str(Path(self.build_folder) / "package"), dst=self.package_folder)
        copy(self, "meta.json", src=self.build_folder, dst=self.package_folder)
