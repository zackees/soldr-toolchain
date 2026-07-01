import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "recipes"))

import _syslib  # noqa: E402


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
