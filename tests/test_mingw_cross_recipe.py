"""Structural tests for the Linux-hosted mingw cross toolchain recipe
(soldr-toolchain#114 Phase 2). The build itself materializes conda-forge
packages via Docker (exercised on forge), so these pin the wiring only.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from scripts import forge_to_catalogue as fc

RECIPES_DIR = Path(__file__).resolve().parents[1] / "recipes"


def _load_helper(name: str):
    path = RECIPES_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_helper_shape_and_specs():
    helper = _load_helper("_mingw_w64_cross")
    assert set(helper.SHAPE_CONFIG) == {"linux-x64-gnu"}
    cfg = helper.SHAPE_CONFIG["linux-x64-gnu"]
    assert cfg["host_triple"] == "x86_64-unknown-linux-gnu"
    assert cfg["target_triple"] == "x86_64-pc-windows-gnu"
    assert cfg["compiler"] == "x86_64-w64-mingw32"
    # conda-forge mingw cross gcc, pinned to match the WinLibs sysroot gcc.
    assert helper.GCC_VERSION == "15.3.0"
    assert any("gcc_impl_win-64" in s for s in cfg["specs"])
    assert any("gxx_impl_win-64" in s for s in cfg["specs"])
    assert helper.PINNED_VERSIONS == ("mingw-w64-gcc-15.3.0",)


def test_recipe_dir_and_forge_wiring():
    recipe_dir = RECIPES_DIR / "mingw-w64-cross-linux-x64-gnu"
    assert (recipe_dir / "conanfile.py").is_file()
    assert (recipe_dir / "README.md").is_file()
    conanfile = (recipe_dir / "conanfile.py").read_text(encoding="utf-8")
    assert 'name = "mingw-w64-cross-linux-x64-gnu"' in conanfile
    assert 'SHAPE = "linux-x64-gnu"' in conanfile

    assert fc.TOOL_RECIPE_NAME["mingw-w64-cross"] == {
        "linux-x64-gnu": "mingw-w64-cross-linux-x64-gnu",
    }
    # Host platform slug must resolve to a catalogue platform.
    assert fc.SHAPE_TO_PLATFORM["linux-x64-gnu"]
    assert fc.DEFAULT_ASSET_NAME["mingw-w64-cross"] == "bundle.tar.zst"


def test_discovery_mode_lock_is_capture_only():
    # Phase-2 ships lock-empty so the first forge build can reveal the
    # resolved builds/SHAs; hardening pins them afterward.
    helper = _load_helper("_mingw_w64_cross")
    assert helper.EXPECTED_LOCK_SHA256 == {}
