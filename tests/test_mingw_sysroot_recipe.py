"""Tests for the host-neutral ``mingw-w64-sysroot`` recipe
(soldr-toolchain#114).

The extraction is exercised against a synthetic WinLibs-shaped zip (no
network): a ``mingw64/`` tree mixing host executables with the target
sysroot. The filter must keep every host-neutral sysroot member and drop
every host executable, and the presence gate + wiring must line up with
the ``mingw-w64-gcc`` sibling.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import forge_to_catalogue as fc

RECIPES_DIR = Path(__file__).resolve().parents[1] / "recipes"


def _load_helper(name: str):
    path = RECIPES_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Members that must be KEPT (host-neutral sysroot), all under mingw64/.
_SYSROOT_MEMBERS = (
    "x86_64-w64-mingw32/lib/crt2.o",
    "x86_64-w64-mingw32/lib/dllcrt2.o",
    "x86_64-w64-mingw32/lib/libmingw32.a",
    "x86_64-w64-mingw32/lib/libmingwex.a",
    "x86_64-w64-mingw32/lib/libmsvcrt.a",
    "x86_64-w64-mingw32/lib/libkernel32.a",
    "x86_64-w64-mingw32/include/stdio.h",
    "x86_64-w64-mingw32/include/windows.h",
    "lib/gcc/x86_64-w64-mingw32/15.3.0/libgcc.a",
    "lib/gcc/x86_64-w64-mingw32/15.3.0/crtbegin.o",
    "lib/gcc/x86_64-w64-mingw32/15.3.0/crtend.o",
)

# Members that must be DROPPED (host executables / non-sysroot).
_HOST_MEMBERS = (
    "bin/gcc.exe",
    "bin/g++.exe",
    "bin/ld.exe",
    "bin/dlltool.exe",
    "bin/windres.exe",
    "libexec/gcc/x86_64-w64-mingw32/15.3.0/cc1.exe",
    "lib/gcc/x86_64-w64-mingw32/15.3.0/libgcc_s.dll",  # forbidden suffix inside a kept subtree
    "include/README.txt",  # not under a kept sysroot subtree
)


def _synthetic_winlibs_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in _SYSROOT_MEMBERS + _HOST_MEMBERS:
            # Non-empty payload so the >0-byte / min-size expectations hold.
            zf.writestr(f"mingw64/{rel}", b"\x00payload\x00")
    return buf.getvalue()


def test_extract_keeps_sysroot_drops_host_executables(tmp_path):
    helper = _load_helper("_mingw_w64_sysroot")
    out_root = tmp_path / "package"
    out_root.mkdir()

    count = helper.extract_sysroot_payload(_synthetic_winlibs_zip(), out_root)
    assert count == len(_SYSROOT_MEMBERS)

    written = {p.relative_to(out_root).as_posix() for p in out_root.rglob("*") if p.is_file()}
    assert written == set(_SYSROOT_MEMBERS)

    # No host executables, no forbidden suffixes anywhere.
    for rel in _HOST_MEMBERS:
        assert not (out_root / rel).exists(), rel
    assert not any(p.suffix.lower() in {".exe", ".dll"} for p in out_root.rglob("*"))

    # The presence gate + host-executable assertion both pass on a good tree.
    helper._validate_package(out_root, "windows-x64-gnu")


def test_validate_rejects_missing_crt(tmp_path):
    helper = _load_helper("_mingw_w64_sysroot")
    out_root = tmp_path / "package"
    out_root.mkdir()
    # Everything except crt2.o.
    for rel in _SYSROOT_MEMBERS:
        if rel.endswith("crt2.o"):
            continue
        p = out_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="missing required paths"):
        helper._validate_package(out_root, "windows-x64-gnu")


def test_validate_rejects_leaked_host_executable(tmp_path):
    helper = _load_helper("_mingw_w64_sysroot")
    out_root = tmp_path / "package"
    out_root.mkdir()
    for rel in _SYSROOT_MEMBERS:
        p = out_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    # Sneak a host exe past extraction (simulating a regression).
    leaked = out_root / "bin" / "gcc.exe"
    leaked.parent.mkdir(parents=True, exist_ok=True)
    leaked.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="host executable"):
        helper._validate_package(out_root, "windows-x64-gnu")


def test_helper_shape_and_pin():
    helper = _load_helper("_mingw_w64_sysroot")
    assert set(helper.SHAPE_ASSETS) == {"windows-x64-gnu"}
    assert helper.PINNED_VERSIONS == ("15.3.0posix-14.0.0-msvcrt-r1",)
    # Same upstream archive as the compiler bundle → CRT/gcc/mingw agree.
    gcc = _load_helper("_mingw_w64_gcc")
    assert (
        helper.SHAPE_ASSETS["windows-x64-gnu"]["asset"]
        == gcc.SHAPE_ASSETS["windows-x64-gnu"]["asset"]
    )


def test_recipe_dir_and_forge_wiring():
    recipe_dir = RECIPES_DIR / "mingw-w64-sysroot-windows-x64-gnu"
    assert (recipe_dir / "conanfile.py").is_file()
    assert (recipe_dir / "README.md").is_file()
    conanfile = (recipe_dir / "conanfile.py").read_text(encoding="utf-8")
    assert 'name = "mingw-w64-sysroot-windows-x64-gnu"' in conanfile
    assert 'SHAPE = "windows-x64-gnu"' in conanfile

    assert fc.TOOL_RECIPE_NAME["mingw-w64-sysroot"] == {
        "windows-x64-gnu": "mingw-w64-sysroot-windows-x64-gnu",
    }
    assert fc.SHAPE_TO_PLATFORM["windows-x64-gnu"]  # resolves to a catalogue platform
    assert fc.DEFAULT_ASSET_NAME["mingw-w64-sysroot"] == "bundle.tar.zst"
