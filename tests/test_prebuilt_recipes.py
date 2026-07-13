"""Structural tests for the cmake-* / ninja-* / uv-* prebuilt-repackage
recipes.

The recipes are pure download+repackage (no live dispatch here); these
tests pin the wiring invariants instead:

  * the shared helpers (`recipes/_cmake.py`, `recipes/_ninja.py`,
    `recipes/_uv.py`) cover exactly the expected shapes — six
    glibc/msvc/darwin shapes for cmake/ninja (no musl, Kitware/ninja
    upstream binaries require glibc), all eight for uv (upstream ships
    musl builds);
  * every shape in the helper table has a matching thin recipe dir with
    a conanfile.py + README.md;
  * `scripts/forge_to_catalogue.py` knows how to ingest every shape and
    uses the standard `bundle.tar.zst` asset name.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from scripts import forge_to_catalogue as fc

RECIPES_DIR = Path(__file__).resolve().parents[1] / "recipes"

EXPECTED_SHAPES = {
    "windows-x64",
    "windows-arm64",
    "darwin-x64",
    "darwin-arm64",
    "linux-x64-gnu",
    "linux-arm64-gnu",
}

# uv ships musl Linux builds upstream — all eight shapes.
UV_EXPECTED_SHAPES = EXPECTED_SHAPES | {"linux-x64-musl", "linux-arm64-musl"}
RUST_CLI_EXPECTED_SHAPES = {
    "windows-x64",
    "windows-arm64",
    "darwin-x64",
    "darwin-arm64",
    "linux-x64-gnu",
    "linux-arm64-gnu",
    "linux-x64-musl",
    "linux-arm64-musl",
}

TOOL_SHAPES = {
    "cmake": EXPECTED_SHAPES,
    "ninja": EXPECTED_SHAPES,
    "uv": UV_EXPECTED_SHAPES,
    "mingw-w64-gcc": {"windows-x64-gnu"},
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


def test_uv_helper_shapes_all_eight():
    uv = _load_helper("_uv")
    assert set(uv.SHAPE_ASSETS) == UV_EXPECTED_SHAPES
    assert uv.PINNED_VERSIONS == ("0.11.26",)
    # Every shape repackages a distinct upstream asset (no universal
    # binaries in uv's release matrix).
    assets = list(uv.SHAPE_ASSETS.values())
    assert len(assets) == len(set(assets))
    # Windows shapes are zips; everything else is tar.gz.
    for shape, asset in uv.SHAPE_ASSETS.items():
        if shape.startswith("windows-"):
            assert asset.endswith(".zip"), (shape, asset)
        else:
            assert asset.endswith(".tar.gz"), (shape, asset)


def test_mingw_w64_gcc_helper_shape():
    mingw = _load_helper("_mingw_w64_gcc")
    assert set(mingw.SHAPE_ASSETS) == {"windows-x64-gnu"}
    assert mingw.PINNED_VERSIONS == ("15.3.0posix-14.0.0-msvcrt-r1",)
    asset = mingw.SHAPE_ASSETS["windows-x64-gnu"]["asset"]
    assert asset.endswith(".zip")
    assert "x86_64-posix-seh" in asset
    assert "mingw-w64msvcrt" in asset


def test_rust_cli_helper_shapes_release_matrix():
    rust_cli = _load_helper("_rust_cli")
    assert set(rust_cli.RUST_CLI_SHAPES) == RUST_CLI_EXPECTED_SHAPES
    assert rust_cli.TOOL_CONFIG["cargo-chef"]["versions"] == ("0.1.73",)
    assert rust_cli.TOOL_CONFIG["crgx"]["versions"] == ("0.1.0",)
    assert rust_cli.parse_package_name("cargo-chef-linux-arm64-musl") == (
        "cargo-chef",
        "linux-arm64-musl",
    )
    assert rust_cli.parse_package_name("crgx-windows-arm64") == (
        "crgx",
        "windows-arm64",
    )


def test_recipe_dirs_exist_per_shape():
    for tool, shapes in TOOL_SHAPES.items():
        for shape in shapes:
            recipe_dir = RECIPES_DIR / f"{tool}-{shape}"
            assert (recipe_dir / "conanfile.py").is_file(), recipe_dir
            assert (recipe_dir / "README.md").is_file(), recipe_dir
            conanfile = (recipe_dir / "conanfile.py").read_text(encoding="utf-8")
            assert f'name = "{tool}-{shape}"' in conanfile
            assert f'SHAPE = "{shape}"' in conanfile
    rust_recipe = RECIPES_DIR / "rust-cli"
    assert (rust_recipe / "conanfile.py").is_file()
    assert (rust_recipe / "README.md").is_file()


def test_recipe_entrypoints_load_helpers_without_search_path_mutation():
    for conanfile in RECIPES_DIR.glob("*/conanfile.py"):
        text = conanfile.read_text(encoding="utf-8")
        assert "sys.path.insert" not in text, conanfile
        if "_load_recipe_helper" in text or "_load_helper" in text:
            assert "importlib.util.spec_from_file_location" in text, conanfile
            assert "def export" in text, conanfile


def test_forge_to_catalogue_wiring():
    for tool, shapes in TOOL_SHAPES.items():
        assert set(fc.TOOL_RECIPE_NAME[tool]) == shapes
        for shape, recipe_name in fc.TOOL_RECIPE_NAME[tool].items():
            assert recipe_name == f"{tool}-{shape}"
            # Every ingest shape must resolve to a catalogue platform.
            assert shape in fc.SHAPE_TO_PLATFORM
        assert fc.DEFAULT_ASSET_NAME[tool] == "bundle.tar.zst"
    for tool in ("cargo-chef", "crgx", "cargo-binstall", "cargo-nextest"):
        assert set(fc.TOOL_RECIPE_NAME[tool]) == RUST_CLI_EXPECTED_SHAPES
        for shape, recipe_name in fc.TOOL_RECIPE_NAME[tool].items():
            assert recipe_name == f"{tool}-{shape}"
            assert shape in fc.SHAPE_TO_PLATFORM
        assert fc.DEFAULT_ASSET_NAME[tool] == "bundle.tar.zst"
