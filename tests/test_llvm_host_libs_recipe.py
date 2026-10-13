"""Contracts for the llvm-host-libs recipe (zackees/soldr#3774).

The managed LLVM bundle's lld NEEDs libxml2.so.2 and libz.so.1 from the host
(RUNPATH $ORIGIN/../lib). ubuntu-26.04 has no libxml2.so.2, so soldr ships
both next to lld from this glibc-2.17 bundle.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from scripts import forge_to_catalogue as fc

RECIPES_DIR = Path(__file__).resolve().parents[1] / "recipes"
SHAPES = ("linux-arm64-gnu", "linux-x64-gnu")


def _load_helper():
    path = RECIPES_DIR / "_llvm_host_libs.py"
    spec = importlib.util.spec_from_file_location("_llvm_host_libs", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pins_sources_images_and_version():
    helper = _load_helper()
    assert helper.PINNED_VERSIONS == ("libxml2-2.13.9-zlib-1.3.1",)
    assert helper.SOURCES["libxml2"] == (
        "https://download.gnome.org/sources/libxml2/2.13/libxml2-2.13.9.tar.xz",
        "a2c9ae7b770da34860050c309f903221c67830c86e4a7e760692b803df95143a",
    )
    assert helper.SOURCES["zlib"] == (
        "https://zlib.net/fossils/zlib-1.3.1.tar.gz",
        "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23",
    )
    assert set(helper.MANYLINUX2014_IMAGES) == set(SHAPES)
    for shape, image in helper.MANYLINUX2014_IMAGES.items():
        arch = "aarch64" if "arm64" in shape else "x86_64"
        assert image.startswith(f"quay.io/pypa/manylinux2014_{arch}:")
        assert "@sha256:" in image


def test_build_script_is_minimal_shared_libxml2_and_zlib():
    helper = _load_helper()
    argv = helper.docker_command("linux-x64-gnu", Path("/b"), uid_gid="1000:1000")
    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert "/b:/work" in argv
    assert helper.MANYLINUX2014_IMAGES["linux-x64-gnu"] in argv
    script = argv[-1]
    for flag in (
        "--enable-shared",
        "--without-python",
        "--without-lzma",
        "--without-icu",
        "--with-zlib=/work/stage",
        "libxml2.la",
    ):
        assert flag in script
    assert script.index("zlib-1.3.1") < script.index("libxml2-2.13.9")
    with pytest.raises(ValueError):
        helper.docker_command("linux-x64-musl", Path("/b"), uid_gid="0:0")


def test_parses_needed_and_glibc_versions():
    helper = _load_helper()
    dynamic = """
 0x0000000000000001 (NEEDED)             Shared library: [libdl.so.2]
 0x0000000000000001 (NEEDED)             Shared library: [libz.so.1]
 0x000000000000000e (SONAME)             Library soname: [libxml2.so.2]
"""
    assert helper.parse_needed(dynamic) == ["libdl.so.2", "libz.so.1"]
    assert helper.parse_soname(dynamic) == "libxml2.so.2"
    assert helper.max_glibc("GLIBC_2.2.5 GLIBC_2.14 GLIBC_2.3") == (2, 14)


@pytest.mark.parametrize(
    ("needed", "ok"),
    [
        (["libc.so.6", "libm.so.6", "libdl.so.2", "libz.so.1"], True),
        (["libc.so.6", "liblzma.so.5"], False),
        (["libicuuc.so.70"], False),
    ],
)
def test_needed_closure_allows_only_glibc_and_bundled_libs(needed, ok):
    helper = _load_helper()
    assert helper.unexpected_needed(needed) == ([] if ok else [n for n in needed if n not in helper.ALLOWED_NEEDED])


def test_recipe_dirs_and_ingest_mapping():
    for shape in SHAPES:
        recipe = RECIPES_DIR / f"llvm-host-libs-{shape}"
        conanfile = (recipe / "conanfile.py").read_text(encoding="utf-8")
        assert f'name = "llvm-host-libs-{shape}"' in conanfile
        assert f'SHAPE = "{shape}"' in conanfile
    assert fc.TOOL_RECIPE_NAME["llvm-host-libs"] == {
        shape: f"llvm-host-libs-{shape}" for shape in SHAPES
    }
    assert fc.DEFAULT_ASSET_NAME["llvm-host-libs"] == "bundle.tar.zst"
