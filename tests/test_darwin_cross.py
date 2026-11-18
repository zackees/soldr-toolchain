"""Linux producer contracts for Darwin cross-target toolchain additions."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from recipes import _apple_sdk_thin as sdk
from scripts import forge_to_catalogue as fc


def helper():
    path = Path(__file__).resolve().parents[1] / "recipes" / "_darwin_cross.py"
    spec = importlib.util.spec_from_file_location("darwin_cross", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sdk_dispatch_version_selects_exact_sdk(monkeypatch):
    calls = []
    def output(argv, **kwargs):
        calls.append(argv)
        return {"--show-sdk-path": "/SDKs/MacOSX26.5.sdk", "--show-sdk-version": "26.5"}.get(argv[-1], "Xcode 26.5")
    monkeypatch.setattr(sdk.subprocess, "check_output", output)
    result = sdk.resolve_sdk(SimpleNamespace(sdk_version="auto"), SimpleNamespace(warning=lambda _: None), "26.5")
    assert result[:2] == ("/SDKs/MacOSX26.5.sdk", "26.5")
    assert calls[0][2] == "macosx26.5"
    assert calls[1][2] == result[0]


def test_sdk_rejects_mislabeled_default(monkeypatch):
    monkeypatch.setattr(sdk.subprocess, "check_output", lambda argv, **kw: "/SDKs/14.5.sdk" if argv[-1] == "--show-sdk-path" else "14.5")
    with pytest.raises(ValueError, match="refuse to mislabel"):
        sdk.resolve_sdk(SimpleNamespace(sdk_version="auto"), SimpleNamespace(warning=lambda _: None), "26.5")


def test_dsymutil_alias_is_relocatable_and_linker_is_required(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "llvm-dsymutil").write_text("tool")
    (bin_dir / "ld64.lld").write_text("linker")
    helper().ensure_darwin_tools(tmp_path)
    assert (bin_dir / "dsymutil").read_text() == "tool"
    assert (bin_dir / "dsymutil").readlink() == Path("llvm-dsymutil")
    with pytest.raises(RuntimeError, match="ld64.lld"):
        helper().ensure_darwin_tools(tmp_path / "absent")


def test_swift_floor_rejects_newer_glibc():
    h = helper()
    assert h.glibc_versions("GLIBC_2.2.5 GLIBC_2.17 GLIBC_2.9") == {(2, 2, 5), (2, 17), (2, 9)}
    h.require_glibc_floor("swiftc", {(2, 17)})
    with pytest.raises(RuntimeError, match="2.17"):
        h.require_glibc_floor("swiftc", {(2, 26)})


def test_sources_are_pinned_and_ingest_routes_exist():
    h = helper()
    assert h.LLVM_VERSION == "21.1.5"
    assert len(h.LLVM_SHA256) == len(h.SWIFT_SHA256) == 64
    for tool, shape in [("compiler-rt", "darwin-arm64"), ("swift", "linux-x64-gnu")]:
        assert fc.TOOL_RECIPE_NAME[tool][shape]
        assert fc.DEFAULT_ASSET_NAME[tool] == "bundle.tar.zst"


def test_llvm_bundle_exposes_darwin_linker():
    path = Path(__file__).resolve().parents[1] / "recipes/llvm-tools-linux-x64/conanfile.py"
    assert '"ld64.lld"' in path.read_text()
