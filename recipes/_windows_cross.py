"""Linux-hosted Windows runtime producers; never execute target binaries."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

LLVM_VERSION = '21.1.5'
LLVM_URL = ('https://github.com/llvm/llvm-project/releases/download/llvmorg-21.1.5/'
            'clang%2Bllvm-21.1.5-x86_64-pc-windows-msvc.tar.xz')
LLVM_SHA256 = 'eba824f1379fdb1a385f6dff8d19275a57348f621c752ce93b6d11256741e349'
LLVM_SIGNER = 'B6C8F98282B944E3B0D5C2530FC3042E345AD05D'
LLVM_LICENSE_URL = 'https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-21.1.5/compiler-rt/LICENSE.TXT'
LLVM_LICENSE_SHA256 = '1a8f1058753f1ba890de984e48f0242a3a5c29a6a8f2ed9fd813f36985387e8d'
BUILTINS_PATH = 'lib/clang/21/lib/windows/clang_rt.builtins-x86_64.lib'

# Only the exact Microsoft release CRT directory is eligible. Never include
# debug_nonredist, import libs, SDK headers, or arbitrary host system DLLs.
VC_VERSION = '14.52.37029'
VC_URL = ('https://download.visualstudio.microsoft.com/download/pr/aad09a18-fd5d-45d3-91e5-9fc87e932d9f/'
          '40d09d7e3ee369b84c7fb47cb76e773c54f2614e24d72fbcbf38a3821f67f86b/'
          'Microsoft.VC.14.52.CRT.Redist.X64.base.vsix')
VC_SHA256 = '40d09d7e3ee369b84c7fb47cb76e773c54f2614e24d72fbcbf38a3821f67f86b'
CRT_PREFIX = 'Contents/VC/Redist/MSVC/14.52.37025/x64/Microsoft.VC145.CRT/'
VC_LICENSE_URL = 'https://visualstudio.microsoft.com/license-terms/vs2026-ga-visualcpp-v14-redist-runtime/'
VC_LICENSE_DOCUMENT = ('https://visualstudio.microsoft.com/wp-content/uploads/2025/10/'
                       'Visual-C-V14-License-Redistributable_and_Runtime_ENU.docx')
VC_LICENSE_SHA256 = '08651651a7602fc7c0e2763de0fde1ff9f868df2780597cd1775ee9d6441c783'
VC_REDIST_URL = 'https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution'
VC_DLLS = {
    'vcruntime140.dll', 'vcruntime140_1.dll', 'vcruntime140_threads.dll',
    'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
    'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll',
    'concrt140.dll', 'vccorlib140.dll',
}
REQUIRED_VC_DLLS = {'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll'}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def download(url: str, destination: Path, expected: str | None = None) -> None:
    if not url.startswith('https://'):
        raise ValueError('source transport must be HTTPS')
    if expected and destination.is_file() and sha256(destination) == expected:
        return
    with urllib.request.urlopen(url, timeout=600) as source, destination.open('wb') as out:
        shutil.copyfileobj(source, out)
    if expected and sha256(destination) != expected:
        raise ValueError(f'SHA256 mismatch: {url}')


def verify_llvm_signature(archive: Path, signature: Path, keys: Path, home: Path) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(['gpg', '--homedir', str(home), '--batch', '--import', str(keys)], check=True)
    result = subprocess.run(['gpg', '--homedir', str(home), '--batch', '--status-fd', '1',
                             '--verify', str(signature), str(archive)], text=True, capture_output=True)
    valid = re.findall(r'^\[GNUPG:\] VALIDSIG ([A-F0-9]+) ', result.stdout, re.MULTILINE)
    if result.returncode or valid != [LLVM_SIGNER] or re.search(r'\b(BADSIG|ERRSIG|EXPKEYSIG|EXPSIG|REVKEYSIG)\b', result.stdout):
        raise ValueError('invalid LLVM release signature or unexpected signer')


def llvm_root(work: Path) -> Path:
    if os.environ.get('SOLDR_LLVM_ROOT'):
        root = Path(os.environ['SOLDR_LLVM_ROOT'])
    else:
        # Exported Conan recipes carry this stdlib catalogue reader next to the
        # helper. Fetch LLVM only: Windows builtins do not depend on an Apple SDK.
        directory = Path(__file__).resolve().parent
        sys.path.insert(0, str(directory))
        try:
            spec = importlib.util.spec_from_file_location('soldr_windows_provision', directory/'_provision_darwin.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            root = Path(module.provision(work/'inputs', llvm_only=True)['llvm_root'])
        finally:
            sys.path.pop(0)
    for tool in ('clang', 'llvm-readobj', 'llvm-nm'):
        if not (root/'bin'/tool).is_file():
            raise ValueError(f'managed LLVM lacks {tool}')
    version = subprocess.check_output([str(root/'bin/clang'), '--version'], text=True)
    if not re.search(r'clang version 21\.1\.5\b', version):
        raise ValueError('compiler-rt requires matching managed LLVM 21.1.5')
    return root


def verify_builtins(archive: Path, llvm: Path) -> None:
    info = subprocess.check_output([str(llvm/'bin/llvm-readobj'), '--file-headers', str(archive)], text=True)
    architectures = re.findall(r'^Arch: (\S+)', info, re.MULTILINE)
    if not architectures or set(architectures) != {'x86_64'}:
        raise RuntimeError('unexpected or missing compiler-rt COFF architecture')
    symbols = subprocess.check_output([str(llvm/'bin/llvm-nm'), '--defined-only', '--extern-only', str(archive)], text=True)
    if not re.search(r'^\s*(?:[0-9a-fA-F]+\s+)?T __udivti3$', symbols, re.MULTILINE):
        raise RuntimeError('Windows builtins lack a global definition of __udivti3')


def package_builtins(archive: Path, destination: Path, llvm: Path, license_path: Path) -> None:
    found = False
    target = destination/BUILTINS_PATH
    with tarfile.open(archive, 'r|xz') as stream:
        for member in stream:
            parts = PurePosixPath(member.name).parts
            if '..' in parts or member.name.startswith('/'):
                raise ValueError('unsafe upstream archive path')
            if len(parts) != 7 or '/'.join(parts[1:]) != BUILTINS_PATH:
                continue
            if found or not member.isfile() or not 0 < member.size <= 64 * 1024 * 1024:
                raise ValueError('duplicate or invalid compiler-rt archive member')
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source, target.open('wb') as out:
                shutil.copyfileobj(source, out)
            found = True
    if not found:
        raise ValueError('missing Windows builtins at expected clang resource path')
    verify_builtins(target, llvm)
    shutil.copy2(license_path, destination/'LICENSE.TXT')


def build_builtins(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    archive, signature, keys = work/'llvm.tar.xz', work/'llvm.sig', work/'llvm-keys.asc'
    download(LLVM_URL, archive, LLVM_SHA256)
    download(LLVM_URL+'.sig', signature)
    download('https://releases.llvm.org/release-keys.asc', keys)
    verify_llvm_signature(archive, signature, keys, work/'gnupg')
    license_path = work/'LICENSE.TXT'
    download(LLVM_LICENSE_URL, license_path, LLVM_LICENSE_SHA256)
    root = llvm_root(work)
    package_builtins(archive, work/'package', root, license_path)
    (work/'package/meta.json').write_text(json.dumps({
        'llvm_version': LLVM_VERSION, 'target': 'x86_64-pc-windows-msvc',
        'source_url': LLVM_URL, 'source_sha256': LLVM_SHA256, 'signer': LLVM_SIGNER,
        'license_url': LLVM_LICENSE_URL, 'license_sha256': LLVM_LICENSE_SHA256,
        'resource_path': BUILTINS_PATH, 'udivti3_defined': True,
        'managed_inputs': json.loads((work/'inputs/provenance.json').read_text()) if (work/'inputs/provenance.json').is_file() else None,
    }, indent=2)+'\n')


def verify_pe_dll(data: bytes) -> None:
    if len(data) < 64 or data[:2] != b'MZ':
        raise ValueError('not a PE DLL')
    offset = struct.unpack_from('<I', data, 0x3c)[0]
    if offset < 64 or offset + 26 > len(data) or data[offset:offset+4] != b'PE\0\0':
        raise ValueError('invalid PE header')
    machine = struct.unpack_from('<H', data, offset+4)[0]
    flags = struct.unpack_from('<H', data, offset+22)[0]
    magic = struct.unpack_from('<H', data, offset+24)[0]
    if machine != 0x8664 or magic != 0x20b or not flags & 0x2000:
        raise ValueError('runtime must be an AMD64 PE32+ DLL')


def package_runtime(archive: Path, destination: Path, *, accept_license: bool) -> None:
    if not accept_license:
        raise ValueError('Microsoft license acceptance required for local use; public mirroring is not authorized')
    selected = {}
    with zipfile.ZipFile(archive) as stream:
        for member in stream.infolist():
            path = PurePosixPath(member.filename)
            if '..' in path.parts or path.is_absolute() or '\\' in member.filename:
                raise ValueError('unsafe Microsoft payload path')
            if not member.filename.startswith(CRT_PREFIX) or member.is_dir():
                continue
            name = member.filename[len(CRT_PREFIX):]
            if '/' in name or name not in VC_DLLS or name.casefold() in selected:
                raise ValueError('unexpected, debug or duplicate CRT DLL')
            if not 0 < member.file_size <= 32 * 1024 * 1024:
                raise ValueError('invalid CRT DLL size')
            data = stream.read(member)
            verify_pe_dll(data)
            selected[name.casefold()] = data
    if not REQUIRED_VC_DLLS <= selected.keys():
        raise ValueError('missing required VC runtime DLLs')
    output = destination/'bin'
    output.mkdir(parents=True, exist_ok=True)
    for name, data in selected.items():
        (output/name).write_bytes(data)
    (destination/'meta.json').write_text(json.dumps({
        'version': VC_VERSION, 'target': 'x86_64-pc-windows-msvc',
        'source_url': VC_URL, 'source_sha256': VC_SHA256,
        'distribution': 'local-or-authorized-private-only',
        'license_url': VC_LICENSE_URL, 'license_document': VC_LICENSE_DOCUMENT,
        'license_sha256': VC_LICENSE_SHA256, 'redist_list_url': VC_REDIST_URL,
        'license_accepted': True, 'ucrt': 'Windows 10+ OS component; not bundled',
        'dll_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(selected.items())},
    }, indent=2)+'\n')


def build_runtime(work: Path, *, accept_license: bool) -> None:
    if not accept_license:
        raise ValueError('Microsoft license acceptance required before downloading')
    work.mkdir(parents=True, exist_ok=True)
    archive = work/'runtime.vsix'
    download(VC_URL, archive, VC_SHA256)
    package_runtime(archive, work/'package', accept_license=accept_license)
    download(VC_LICENSE_DOCUMENT, work/'package/LICENSE.docx', VC_LICENSE_SHA256)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool', choices=['compiler-rt', 'vc-runtime'])
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--accept-microsoft-license', action='store_true')
    args = parser.parse_args()
    if args.tool == 'compiler-rt':
        build_builtins(args.work)
    else:
        build_runtime(args.work, accept_license=args.accept_microsoft_license)


if __name__ == '__main__':
    main()
