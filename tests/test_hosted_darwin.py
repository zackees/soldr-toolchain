"""Hosted producers resolve portable catalogue inputs, not workstation paths."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import importlib
from recipes import _darwin_cross as cross


def test_sdk_26_uses_extraction_runner_and_own_run_artifacts():
    plan = importlib.import_module('scripts.plan_forge_dispatch')
    row, = plan.build_matrix('apple-sdk', '26.5', 'thin-aarch64')['include']
    assert row['macos_arm64'] is True
    assert row['macos_arm64_runner'] == 'macos-26'
    assert row['linux_x64'] is False
    assert row['recipe_path'] == 'recipes/apple-sdk-thin-aarch64'


def test_cross_producers_keep_exact_recipes_on_linux():
    plan = importlib.import_module('scripts.plan_forge_dispatch')
    for tool, version, shape, name in [
        ('compiler-rt','21.1.5','darwin-arm64','compiler-rt-darwin-arm64'),
        ('llvm-tools','21.1.5','linux-x64-gnu','llvm-tools-linux-x64'),
    ]:
        row, = plan.build_matrix(tool, version, shape)['include']
        assert row['linux_x64'] is True and row['macos_arm64'] is False
        assert row['recipe_selection'] == 'exact'
        assert row['name'] == name and row['recipe_path'] == f'recipes/{name}'


def test_download_unit_rejects_corrupt_parts(tmp_path, monkeypatch):
    provision = importlib.import_module('recipes._provision_darwin')
    payload = b'archive bytes'
    part = {'number': 1, 'size_bytes':len(payload), 'sha256': hashlib.sha256(payload).hexdigest(), 'urls':['https://example.test/part']}
    entry = SimpleNamespace(parts=[part], urls=[], sha256=part['sha256'],size_bytes=len(payload))
    monkeypatch.setattr(provision, 'open_url', lambda url: io.BytesIO(payload))
    assert provision.download_entry(entry, tmp_path / 'good') == tmp_path / 'good'
    monkeypatch.setattr(provision, 'open_url', lambda url: io.BytesIO(b'corrupt'))
    with pytest.raises(ValueError, match='size|SHA256'):
        provision.download_entry(entry, tmp_path / 'bad')


def test_linux_llvm_builds_from_pinned_source_at_floor():
    command = cross.llvm_tools_command(Path('/src'),Path('/build'))
    assert '-DLLVM_ENABLE_PROJECTS=clang;lld' in command
    assert '-DLLVM_TARGETS_TO_BUILD=X86;AArch64' in command
    assert '-DLLVM_ENABLE_LIBXML2=OFF' in command
    assert '-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON' in command


def test_ingest_reads_artifacts_from_caller_repository():
    root = Path(__file__).resolve().parents[1]
    dispatch = (root/'.github/workflows/forge-dispatch.yml').read_text()
    ingest = (root/'.github/workflows/forge-ingest.yml').read_text()
    assert 'uses: forge-ci/forge/.github/workflows/forge-conan.yml@' in dispatch
    assert 'forge_repository=${{ github.repository }}' in dispatch
    assert 'forge_run_id=${{ github.run_id }}' in dispatch
    assert 'FORGE_REPOSITORY: ${{ inputs.forge_repository }}' in ingest
