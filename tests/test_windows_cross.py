"""Linux producers must supply real MSVC builtins, not intrinsic shims."""
import hashlib
import importlib
import io
import struct
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.plan_forge_dispatch import build_matrix
from scripts import forge_to_catalogue as fc


def helper():
    return importlib.import_module('recipes._windows_cross')


def test_windows_builtins_dispatches_exact_recipe_on_linux():
    row, = build_matrix('compiler-rt', '21.1.5', 'windows-x64')['include']
    assert row['linux_x64'] and not row['windows_x64']
    assert row['recipe_path'] == 'recipes/compiler-rt-windows-x64'
    assert row['recipe_selection'] == 'exact'
    assert fc.DEFAULT_ASSET_NAME['compiler-rt'] == 'bundle.tar.zst'


def test_windows_upstream_is_matching_pinned_llvm():
    h = helper()
    assert h.LLVM_VERSION == '21.1.5'
    assert '21.1.5-x86_64-pc-windows-msvc.tar.xz' in h.LLVM_URL
    assert h.LLVM_SHA256 == 'eba824f1379fdb1a385f6dff8d19275a57348f621c752ce93b6d11256741e349'


@pytest.mark.parametrize('code,signer', [(0, 'wrong'), (1, 'pinned')])
def test_llvm_signature_rejects_untrusted_or_failed_verification(tmp_path, monkeypatch, code, signer):
    h = helper()
    signer = h.LLVM_SIGNER if signer == 'pinned' else signer
    monkeypatch.setattr(h.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=code, stdout=f'[GNUPG:] VALIDSIG {signer} rest\n'))
    with pytest.raises(ValueError, match='signature|signer'):
        h.verify_llvm_signature(Path('archive'), Path('sig'), Path('keys'), tmp_path/'gnupg')


def test_windows_provisioning_never_downloads_apple_sdk(tmp_path, monkeypatch):
    from recipes import _provision_darwin as p
    entry = SimpleNamespace(asset=p.LLVM_UNIT, sha256='a'*64, size_bytes=123)
    monkeypatch.setattr(p, 'json_url', lambda *a: {'publication_state': {'url': 'https://example.test/state'}})
    monkeypatch.setattr(p, 'read_verified_generation', lambda *a: SimpleNamespace(entries=[entry], generation='generation'))
    units = []
    monkeypatch.setattr(p, 'download_entry', lambda entry, path: units.append(entry.asset) or path)
    monkeypatch.setattr(p, 'extract_unit', lambda *a: None)
    assert p.provision(tmp_path, llvm_only=True)['units'][0]['id'] == p.LLVM_UNIT
    assert units == [p.LLVM_UNIT]


def test_llvm_only_cli_has_safe_accurate_dry_run(tmp_path, monkeypatch, capsys):
    from recipes import _provision_darwin as p
    monkeypatch.setattr('sys.argv', ['provision', '--llvm-only', '--dry-run', '--output', str(tmp_path/'unused')])
    p.main()
    assert __import__('json').loads(capsys.readouterr().out)['units'] == [p.LLVM_UNIT]
    assert not (tmp_path/'unused').exists()


@pytest.mark.parametrize('architectures,symbols,error', [
    ('Format: COFF-x86-64\nArch: x86_64\nFormat: COFF-x86-64\nArch: x86_64', '00000000 T __udivti3\n', None),
    ('Format: COFF-x86-64\nArch: x86_64\nFormat: COFF-ARM64\nArch: aarch64', '00000000 T __udivti3\n', 'architecture'),
    ('Format: elf64-x86-64\nArch: x86_64', '00000000 T __udivti3\n', 'format'),
    ('', '00000000 T __udivti3\n', 'architecture'),
    ('Format: COFF-x86-64\nArch: x86_64', '         U __udivti3\n', '__udivti3'),
    ('Format: COFF-x86-64\nArch: x86_64', '00000000 T __udivti3_extra\n', '__udivti3'),
])
def test_builtins_require_amd64_and_exact_global_definition(monkeypatch, architectures, symbols, error):
    h = helper()
    monkeypatch.setattr(h.subprocess, 'check_output', lambda cmd, **kw: architectures if '--file-headers' in cmd else symbols)
    if error:
        with pytest.raises(RuntimeError, match=error):
            h.verify_builtins(Path('builtins.lib'), Path('/llvm'))
    else:
        h.verify_builtins(Path('builtins.lib'), Path('/llvm'))


@pytest.mark.parametrize('suffix', ['lib/clang/21/lib/windows/clang_rt.builtins-x86_64.lib',
                                  '../lib/clang/21/lib/windows/clang_rt.builtins-x86_64.lib'])
def test_package_selects_only_expected_resource_path(tmp_path, monkeypatch, suffix):
    h = helper()
    archive = tmp_path / 'llvm.tar.xz'
    with tarfile.open(archive, 'w:xz') as tf:
        for name, data in [('LLVM/'+suffix, b'builtins'), ('LLVM/bin/clang.exe', b'host executable')]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tf.addfile(member, io.BytesIO(data))
    monkeypatch.setattr(h, 'verify_builtins', lambda *a: None)
    license_path = tmp_path/'LICENSE.TXT'
    license_path.write_bytes(b'license')
    if '..' in suffix:
        with pytest.raises(ValueError, match='path|missing'):
            h.package_builtins(archive, tmp_path/'package', Path('/llvm'), license_path)
    else:
        h.package_builtins(archive, tmp_path/'package', Path('/llvm'), license_path)
        assert (tmp_path/'package'/suffix).read_bytes() == b'builtins'
        assert not (tmp_path/'package/bin').exists()
        assert (tmp_path/'package/LICENSE.TXT').read_bytes() == b'license'


def pe(machine=0x8664):
    image = bytearray(256)
    image[:2] = b'MZ'
    struct.pack_into('<I', image, 0x3c, 64)
    image[64:68] = b'PE\0\0'
    struct.pack_into('<H', image, 68, machine)
    struct.pack_into('<H', image, 86, 0x2000)
    struct.pack_into('<H', image, 88, 0x20b)
    return bytes(image)


@pytest.mark.parametrize('bad', ['debug', 'arm64', 'duplicate', 'missing', 'not-pe', 'unsafe'])
def test_runtime_rejects_unqualified_payload(tmp_path, bad):
    h = helper()
    archive = tmp_path/'runtime.vsix'
    names = ['vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll']
    if bad == 'missing':
        names.pop()
    with zipfile.ZipFile(archive, 'w') as z:
        for name in names:
            z.writestr(h.CRT_PREFIX+name, b'bad' if bad == 'not-pe' else pe(0xaa64 if bad == 'arm64' else 0x8664))
        if bad == 'debug':
            z.writestr(h.CRT_PREFIX+'vcruntime140d.dll', pe())
        if bad == 'duplicate':
            z.writestr(h.CRT_PREFIX+'VCRUNTIME140.dll', pe())
        if bad == 'unsafe':
            z.writestr(h.CRT_PREFIX+'../vcruntime140.dll', pe())
    with pytest.raises(ValueError):
        h.package_runtime(archive, tmp_path/'out', accept_license=True)


def test_runtime_license_gate_and_unmodified_release_dlls(tmp_path):
    h = helper()
    archive = tmp_path/'runtime.vsix'
    with zipfile.ZipFile(archive, 'w') as z:
        for name in ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll'):
            z.writestr(h.CRT_PREFIX+name, pe())
    with pytest.raises(ValueError, match='license'):
        h.package_runtime(archive, tmp_path/'blocked', accept_license=False)
    h.package_runtime(archive, tmp_path/'out', accept_license=True)
    assert (tmp_path/'out/bin/vcruntime140.dll').read_bytes() == pe()
    assert not (tmp_path/'out/bin/ucrtbase.dll').exists()
    # Local/private recipe is intentionally absent from the public dispatcher.
    assert 'vc-runtime' not in fc.TOOL_RECIPE_NAME


def test_download_rejects_wrong_source_digest(tmp_path, monkeypatch):
    h = helper()
    monkeypatch.setattr(h.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'changed'))
    with pytest.raises(ValueError, match='SHA256'):
        h.download('https://example.test/archive', tmp_path/'archive', hashlib.sha256(b'expected').hexdigest())
