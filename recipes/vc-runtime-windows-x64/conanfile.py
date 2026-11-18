"""Local/authorized private CRT packaging; deliberately no public Forge route."""
from pathlib import Path
import importlib.util
import os

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy


def _load_helper():
    root = Path(__file__).resolve().parent
    for directory in (root, root.parent):
        path = directory/'_windows_cross.py'
        if path.is_file():
            spec = importlib.util.spec_from_file_location('soldr_vc_runtime', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise ImportError('_windows_cross.py was not exported beside the recipe')


helper = _load_helper()


class VcRuntimeWindowsX64(ConanFile):
    name = 'vc-runtime-windows-x64'
    license = 'LicenseRef-Microsoft-Visual-Studio'
    package_type = 'application'
    settings = 'os', 'arch'
    no_copy_source = True

    def export(self):
        copy(self, '_windows_cross.py', src=str(Path(__file__).resolve().parent.parent), dst=self.export_folder)

    def validate(self):
        if str(self.settings.os) != 'Linux' or str(self.settings.arch) != 'x86_64':
            raise ConanInvalidConfiguration('producer requires Linux x86_64')
        if str(self.version) != helper.VC_VERSION:
            raise ConanInvalidConfiguration('unsupported pinned Microsoft CRT version')
        if os.environ.get('SOLDR_ACCEPT_MICROSOFT_LICENSE') != '1':
            raise ConanInvalidConfiguration('Microsoft license acceptance required for local/private use; public mirroring is not authorized')

    def build(self):
        helper.build_runtime(Path(self.build_folder), accept_license=os.environ.get('SOLDR_ACCEPT_MICROSOFT_LICENSE') == '1')

    def package(self):
        copy(self, '*', src=str(Path(self.build_folder)/'package'), dst=self.package_folder)
