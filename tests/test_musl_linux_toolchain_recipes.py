"""Contracts for the catalogue-backed, non-Zig musl compiler bundles."""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path
import tarfile
from scripts import forge_to_catalogue as fc

RECIPES_DIR = Path(__file__).resolve().parents[1] / "recipes"
EXPECTED_SHAPES = {"linux-x64-musl", "linux-arm64-musl"}

def _load_helper():
    spec = importlib.util.spec_from_file_location("_musl_linux_toolchain", RECIPES_DIR / "_musl_linux_toolchain.py")
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    return module

def test_musl_bundles_pin_complete_non_zig_compilers_and_startup_objects():
    helper = _load_helper()
    assert helper.PINNED_VERSIONS == ("gcc-11.2.1-musl-20211123-1",)
    assert set(helper.SHAPE_CONFIG) == EXPECTED_SHAPES
    assert {cfg["target"] for cfg in helper.SHAPE_CONFIG.values()} == {"x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl"}
    for shape, config in helper.SHAPE_CONFIG.items():
        assert len(config["sha256"]) == 64, shape
        assert config["archive"].endswith(".tgz"), shape
        assert helper.source_url(config).startswith("https://musl.cc/")
    assert {"lib/crt1.o", "lib/rcrt1.o"} <= set(helper.REQUIRED_TARGET_PATHS)
    assert {"gcc", "g++", "ar", "ranlib", "ld", "readelf"} <= set(helper.REQUIRED_TOOLS)

def test_recipe_and_catalogue_wiring_exists_for_both_musl_targets():
    for shape in EXPECTED_SHAPES:
        recipe = RECIPES_DIR / f"musl-linux-toolchain-{shape}"
        assert (recipe / "conanfile.py").is_file()
        assert (recipe / "README.md").is_file()
        text = (recipe / "conanfile.py").read_text(encoding="utf-8")
        assert f'name = "musl-linux-toolchain-{shape}"' in text
        assert f'SHAPE = "{shape}"' in text
    assert set(fc.TOOL_RECIPE_NAME["musl-linux-toolchain"]) == EXPECTED_SHAPES
    assert fc.DEFAULT_ASSET_NAME["musl-linux-toolchain"] == "bundle.tar.zst"

def test_known_musl_loader_alias_is_the_only_allowed_absolute_link():
    helper = _load_helper()
    allowed = tarfile.TarInfo("x86_64-linux-musl-cross/x86_64-linux-musl/lib/ld-musl-x86_64.so.1")
    allowed.type = tarfile.SYMTYPE
    allowed.linkname = "/lib/libc.so"
    assert helper._is_musl_loader_link(allowed, "x86_64-linux-musl-cross")
    denied = tarfile.TarInfo("x86_64-linux-musl-cross/x86_64-linux-musl/lib/other")
    denied.type = tarfile.SYMTYPE
    denied.linkname = "/lib/libc.so"
    assert not helper._is_musl_loader_link(denied, "x86_64-linux-musl-cross")
