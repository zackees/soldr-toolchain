"""Repackage matching Windows compiler-rt builtins on Linux; no target execution."""
from pathlib import Path
import importlib.util
import sys

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy


def _load_helper():
    root = Path(__file__).resolve().parent
    for directory in (root, root.parent):
        path = directory/'_windows_cross.py'
        if path.is_file():
            spec = importlib.util.spec_from_file_location('soldr_windows_cross', path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError('_windows_cross.py was not exported beside the recipe')


helper = _load_helper()


class CompilerRtWindowsX64(ConanFile):
    name = 'compiler-rt-windows-x64'
    license = 'Apache-2.0 WITH LLVM-exception'
    package_type = 'application'
    settings = 'os', 'arch'
    no_copy_source = True

    def export(self):
        root = Path(__file__).resolve().parent.parent
        for name in ('_windows_cross.py', '_provision_darwin.py'):
            copy(self, name, src=str(root), dst=self.export_folder)
        for name in ('__init__.py', 'generation_reader.py', 'catalogue_v2.py', 'publication_model.py'):
            copy(self, name, src=str(root.parent/'scripts'), dst=str(Path(self.export_folder)/'scripts'))

    def validate(self):
        if str(self.settings.os) != 'Linux' or str(self.settings.arch) != 'x86_64':
            raise ConanInvalidConfiguration('producer requires Linux x86_64')
        if str(self.version) != helper.LLVM_VERSION:
            raise ConanInvalidConfiguration('producer requires matching LLVM '+helper.LLVM_VERSION)

    def build(self):
        helper.build_builtins(Path(self.build_folder))

    def package(self):
        copy(self, '*', src=str(Path(self.build_folder)/'package'), dst=self.package_folder)
