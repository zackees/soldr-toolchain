"""Contracts for locked GNU/Linux compiler + glibc 2.17 recipes."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from scripts import forge_to_catalogue as fc

RECIPES_DIR = Path(__file__).resolve().parents[1] / "recipes"
EXPECTED_SHAPES = {"linux-x64-gnu", "linux-arm64-gnu"}


def _load_helper():
    path = RECIPES_DIR / "_gnu_linux_toolchain.py"
    spec = importlib.util.spec_from_file_location("_gnu_linux_toolchain", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pins_exact_conda_artifacts_and_glibc_floor():
    helper = _load_helper()
    assert helper.PINNED_VERSIONS == ("gcc-13.3.0-glibc-2.17-1",)
    assert helper.MICROMAMBA_IMAGE.startswith("mambaorg/micromamba:2.3.2@sha256:")
    assert helper.GLIBC_VERSION == "2.17"
    assert helper.GCC_VERSION == "13.3.0"
    assert set(helper.SHAPE_CONFIG) == EXPECTED_SHAPES
    assert {cfg["target"] for cfg in helper.SHAPE_CONFIG.values()} == {
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
    }
    assert {cfg["compiler"] for cfg in helper.SHAPE_CONFIG.values()} == {
        "x86_64-conda-linux-gnu",
        "aarch64-conda-linux-gnu",
    }
    for shape, expected in helper.EXPECTED_LOCK_SHA256.items():
        assert expected
        assert all(len(value) == 64 for value in expected.values()), shape
        assert any("sysroot" in name for name in expected)


def test_lock_rejects_solver_drift():
    helper = _load_helper()
    payload = {
        "actions": {
            "LINK": [
                {"name": name, "sha256": "0" * 64}
                for name in helper.EXPECTED_LOCK_SHA256["linux-x64-gnu"]
            ]
        }
    }
    try:
        helper._locked_rows(payload, "linux-x64-gnu")
    except RuntimeError as exc:
        assert "locked conda artifact mismatch" in str(exc)
    else:
        raise AssertionError("solver drift must not be accepted")


def test_recipe_and_catalogue_wiring_exists_for_both_targets():
    for shape in EXPECTED_SHAPES:
        recipe_dir = RECIPES_DIR / f"gnu-linux-toolchain-{shape}"
        assert (recipe_dir / "conanfile.py").is_file()
        assert (recipe_dir / "README.md").is_file()
        text = (recipe_dir / "conanfile.py").read_text(encoding="utf-8")
        assert f'name = "gnu-linux-toolchain-{shape}"' in text
        assert f'SHAPE = "{shape}"' in text

    assert set(fc.TOOL_RECIPE_NAME["gnu-linux-toolchain"]) == EXPECTED_SHAPES
    assert fc.DEFAULT_ASSET_NAME["gnu-linux-toolchain"] == "bundle.tar.zst"


def test_host_scan_includes_non_executable_elf(tmp_path):
    helper = _load_helper()
    elf = tmp_path / "plugin.so"
    elf.write_bytes(b"\x7fELFpayload")
    elf.chmod(0o644)
    assert helper._is_elf(elf)
