import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "recipes"))

import _syslib  # noqa: E402


def test_windows_gnu_shape_is_available_for_core_syslibs() -> None:
    shape = _syslib.SHAPES["windows-x64-gnu"]
    assert shape.target_triple == "x86_64-pc-windows-gnu"
    assert shape.forge_input == "windows_x64_gnu"
    assert shape.cmake_generator == "MinGW Makefiles"

    for tool in ("zstd", "sqlite", "mimalloc", "zlib-ng", "lzma", "bzip2"):
        assert "windows-x64-gnu" not in _syslib.LIBRARIES[tool].unsupported_shapes

    assert "windows-x64-gnu" in _syslib.LIBRARIES["jemalloc"].unsupported_shapes


def test_windows_gnu_pkg_config_keeps_gnu_library_name(tmp_path: Path) -> None:
    lib = _syslib.LIBRARIES["zstd"]

    msvc_root = tmp_path / "msvc"
    _syslib._write_pkg_config(
        msvc_root,
        lib,
        _syslib.SHAPES["windows-x64"],
        "libzstd",
        lib.pkg_config_lib,
    )
    assert "-lzstd_static" in (msvc_root / "lib" / "pkgconfig" / "libzstd.pc").read_text(
        encoding="utf-8"
    )

    gnu_root = tmp_path / "gnu"
    _syslib._write_pkg_config(
        gnu_root,
        lib,
        _syslib.SHAPES["windows-x64-gnu"],
        "libzstd",
        lib.pkg_config_lib,
    )
    assert "-lzstd\n" in (gnu_root / "lib" / "pkgconfig" / "libzstd.pc").read_text(
        encoding="utf-8"
    )


def test_flatten_mimalloc_install_layout_moves_versioned_libs_and_headers(tmp_path: Path) -> None:
    package = tmp_path / "package"
    versioned_lib = package / "lib" / "mimalloc-3.3"
    versioned_include = package / "include" / "mimalloc-3.3"
    versioned_lib.mkdir(parents=True)
    versioned_include.mkdir(parents=True)
    (versioned_lib / "libmimalloc.a").write_text("archive", encoding="utf-8")
    (versioned_include / "mimalloc.h").write_text("header", encoding="utf-8")

    _syslib._flatten_mimalloc_install_layout(package)

    assert (package / "lib" / "libmimalloc.a").read_text(encoding="utf-8") == "archive"
    assert (package / "include" / "mimalloc.h").read_text(encoding="utf-8") == "header"
    assert not versioned_lib.exists()
    assert not versioned_include.exists()
    _syslib._sanity_check_package(package, _syslib.LIBRARIES["mimalloc"])
