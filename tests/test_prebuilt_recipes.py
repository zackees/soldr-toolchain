"""Structural tests for the cmake-* / ninja-* prebuilt-repackage recipes.

The recipes are pure download+repackage (no live dispatch here); these
tests pin the wiring invariants instead:

  * the shared helpers (`recipes/_cmake.py`, `recipes/_ninja.py`) cover
    exactly the six glibc/msvc/darwin shapes — no musl (Kitware/ninja
    upstream binaries require glibc);
  * every shape in the helper table has a matching thin recipe dir with
    a conanfile.py + README.md;
  * `scripts/forge_to_catalogue.py` knows how to ingest every shape and
    uses the standard `bundle.tar.zst` asset name.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import forge_to_catalogue as fc

RECIPES_DIR = Path(__file__).resolve().parents[1] / "recipes"

EXPECTED_SHAPES = {
    "windows-x64",
    "windows-arm64",
    "darwin-x64",
    "darwin-arm64",
    "linux-x64-gnu",
    "linux-arm64-gnu",
}


def _load_helper(name: str):
    path = RECIPES_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_cmake_helper_shapes_no_musl():
    cmake = _load_helper("_cmake")
    assert set(cmake.SHAPE_ASSETS) == EXPECTED_SHAPES
    assert cmake.PINNED_VERSIONS == ("4.3.4",)
    # Both darwin shapes repackage the same universal archive.
    assert (
        cmake.SHAPE_ASSETS["darwin-x64"] == cmake.SHAPE_ASSETS["darwin-arm64"]
    )


def test_ninja_helper_shapes_no_musl():
    ninja = _load_helper("_ninja")
    assert set(ninja.SHAPE_ASSETS) == EXPECTED_SHAPES
    assert ninja.PINNED_VERSIONS == ("1.13.2",)
    assert (
        ninja.SHAPE_ASSETS["darwin-x64"] == ninja.SHAPE_ASSETS["darwin-arm64"]
    )


def test_recipe_dirs_exist_per_shape():
    for tool in ("cmake", "ninja"):
        for shape in EXPECTED_SHAPES:
            recipe_dir = RECIPES_DIR / f"{tool}-{shape}"
            assert (recipe_dir / "conanfile.py").is_file(), recipe_dir
            assert (recipe_dir / "README.md").is_file(), recipe_dir
            conanfile = (recipe_dir / "conanfile.py").read_text(encoding="utf-8")
            assert f'name = "{tool}-{shape}"' in conanfile
            assert f'SHAPE = "{shape}"' in conanfile


def test_forge_to_catalogue_wiring():
    for tool in ("cmake", "ninja"):
        assert set(fc.TOOL_RECIPE_NAME[tool]) == EXPECTED_SHAPES
        for shape, recipe_name in fc.TOOL_RECIPE_NAME[tool].items():
            assert recipe_name == f"{tool}-{shape}"
            # Every ingest shape must resolve to a catalogue platform.
            assert shape in fc.SHAPE_TO_PLATFORM
        assert fc.DEFAULT_ASSET_NAME[tool] == "bundle.tar.zst"
