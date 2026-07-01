"""Shared Conan recipe plumbing for soldr#1064 syslib bundles.

Each thin recipe directory is named ``<lib>-<shape>`` and imports this
module. Forge builds one recipe per target shape, then the ingest step
places the package at ``<lib>/<version>/<shape>/bundle.tar.zst``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

try:
    from conan.errors import ConanInvalidConfiguration
except ImportError:  # Allow local generators/tests to import metadata without Conan.
    class ConanInvalidConfiguration(Exception):
        pass


@dataclass(frozen=True)
class Shape:
    target_triple: str
    conan_os: str
    conan_arch: str
    forge_input: str
    runner_platform: str
    cmake_arch: str | None = None
    musl: bool = False


SHAPES: dict[str, Shape] = {
    "windows-x64": Shape(
        "x86_64-pc-windows-msvc", "Windows", "x86_64", "windows_x64", "windows-x64", "x64"
    ),
    "windows-arm64": Shape(
        "aarch64-pc-windows-msvc", "Windows", "armv8", "windows_arm64", "windows-arm64", "ARM64"
    ),
    "darwin-x64": Shape(
        "x86_64-apple-darwin", "Macos", "x86_64", "macos_x64", "macos-x64"
    ),
    "darwin-arm64": Shape(
        "aarch64-apple-darwin", "Macos", "armv8", "macos_arm64", "macos-arm64"
    ),
    "linux-x64-gnu": Shape(
        "x86_64-unknown-linux-gnu", "Linux", "x86_64", "linux_x64", "linux-x64"
    ),
    "linux-arm64-gnu": Shape(
        "aarch64-unknown-linux-gnu", "Linux", "armv8", "linux_arm64", "linux-arm64"
    ),
    "linux-x64-musl": Shape(
        "x86_64-unknown-linux-musl", "Linux", "x86_64", "linux_x64_musl", "linux-x64-musl", musl=True
    ),
    "linux-arm64-musl": Shape(
        "aarch64-unknown-linux-musl", "Linux", "armv8", "linux_arm64_musl", "linux-arm64-musl", musl=True
    ),
}


@dataclass(frozen=True)
class Library:
    tool: str
    version: str
    source_url: str
    license: str
    description: str
    pkg_config_name: str | None = None
    pkg_config_lib: str | None = None
    msvc_pkg_config_lib: str | None = None
    cmake_subdir: str = "."
    cmake_defs: dict[str, str] | None = None
    cmake_build_target: str | None = None
    cmake_install_component: str | None = None
    custom: str | None = None
    unsupported_shapes: frozenset[str] = frozenset()


LIBRARIES: dict[str, Library] = {
    "zstd": Library(
        tool="zstd",
        version="1.5.7",
        source_url="https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz",
        license="BSD-3-Clause OR GPL-2.0-only",
        description="libzstd static library and headers for zstd-sys",
        pkg_config_name="libzstd",
        pkg_config_lib="zstd",
        msvc_pkg_config_lib="zstd_static",
        cmake_subdir="build/cmake",
        cmake_defs={
            "ZSTD_BUILD_SHARED": "OFF",
            "ZSTD_BUILD_STATIC": "ON",
            "ZSTD_BUILD_PROGRAMS": "OFF",
            "ZSTD_BUILD_TESTS": "OFF",
            "ZSTD_LEGACY_SUPPORT": "OFF",
            "BUILD_SHARED_LIBS": "OFF",
        },
    ),
    "sqlite": Library(
        tool="sqlite",
        version="3.46.0",
        source_url="https://www.sqlite.org/2024/sqlite-amalgamation-3460000.zip",
        license="blessing",
        description="SQLite amalgamation static library and headers for libsqlite3-sys",
        pkg_config_name="sqlite3",
        pkg_config_lib="sqlite3",
        custom="sqlite",
    ),
    "jemalloc": Library(
        tool="jemalloc",
        version="5.3.0",
        source_url="https://github.com/jemalloc/jemalloc/releases/download/5.3.0/jemalloc-5.3.0.tar.bz2",
        license="BSD-2-Clause",
        description="jemalloc static library and headers for tikv-jemalloc-sys",
        custom="jemalloc",
        unsupported_shapes=frozenset({"windows-x64", "windows-arm64"}),
    ),
    "mimalloc": Library(
        tool="mimalloc",
        version="3.3.2",
        source_url="https://github.com/microsoft/mimalloc/archive/refs/tags/v3.3.2.tar.gz",
        license="MIT",
        description="mimalloc static library and headers",
        cmake_defs={
            "MI_BUILD_SHARED": "OFF",
            "MI_BUILD_STATIC": "ON",
            "MI_BUILD_OBJECT": "OFF",
            "MI_BUILD_TESTS": "OFF",
        },
    ),
    "zlib-ng": Library(
        tool="zlib-ng",
        version="2.2.5",
        source_url="https://github.com/zlib-ng/zlib-ng/archive/refs/tags/2.2.5.tar.gz",
        license="Zlib",
        description="zlib-ng static library and headers",
        pkg_config_name="zlib-ng",
        pkg_config_lib="z-ng",
        msvc_pkg_config_lib="zlibstatic-ng",
        cmake_defs={
            "ZLIB_COMPAT": "OFF",
            "ZLIB_ENABLE_TESTS": "OFF",
            "ZLIBNG_ENABLE_TESTS": "OFF",
            "BUILD_TESTING": "OFF",
            "BUILD_SHARED_LIBS": "OFF",
        },
    ),
    "lzma": Library(
        tool="lzma",
        version="5.6.3",
        source_url="https://github.com/tukaani-project/xz/releases/download/v5.6.3/xz-5.6.3.tar.gz",
        license="0BSD",
        description="liblzma static library and headers for lzma-sys",
        pkg_config_name="liblzma",
        pkg_config_lib="lzma",
        cmake_defs={
            "BUILD_SHARED_LIBS": "OFF",
            "ENABLE_NLS": "OFF",
        },
        cmake_build_target="liblzma",
        cmake_install_component="liblzma_Development",
    ),
    "bzip2": Library(
        tool="bzip2",
        version="1.0.8",
        source_url="https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz",
        license="bzip2-1.0.6",
        description="libbz2 static library and headers for bzip2-sys",
        pkg_config_name="bzip2",
        pkg_config_lib="bz2",
        custom="bzip2",
    ),
}


def split_recipe_name(recipe_name: str) -> tuple[Library, str]:
    for lib_name in sorted(LIBRARIES, key=len, reverse=True):
        prefix = f"{lib_name}-"
        if recipe_name.startswith(prefix):
            shape_name = recipe_name[len(prefix):]
            if shape_name not in SHAPES:
                raise ConanInvalidConfiguration(f"unknown syslib shape: {shape_name}")
            return LIBRARIES[lib_name], shape_name
    raise ConanInvalidConfiguration(f"unknown syslib recipe name: {recipe_name}")


def validate(conanfile) -> None:
    lib, shape_name = split_recipe_name(str(conanfile.name))
    shape = SHAPES[shape_name]
    if shape_name in lib.unsupported_shapes:
        raise ConanInvalidConfiguration(f"{lib.tool} does not support {shape_name}")
    if str(conanfile.version) != lib.version:
        raise ConanInvalidConfiguration(
            f"{conanfile.name} must be dispatched with version {lib.version}"
        )
    if str(conanfile.settings.os) != shape.conan_os:
        raise ConanInvalidConfiguration(
            f"{conanfile.name} must run with os={shape.conan_os}, got {conanfile.settings.os}"
        )
    if str(conanfile.settings.arch) != shape.conan_arch:
        raise ConanInvalidConfiguration(
            f"{conanfile.name} must run with arch={shape.conan_arch}, got {conanfile.settings.arch}"
        )


def build(conanfile) -> None:
    lib, shape_name = split_recipe_name(str(conanfile.name))
    shape = SHAPES[shape_name]
    build_root = Path(conanfile.build_folder)
    package_root = build_root / "package"
    source_root = _fetch_source(lib, build_root)
    package_root.mkdir(parents=True, exist_ok=True)

    if lib.custom == "sqlite":
        _build_sqlite(conanfile, source_root, package_root, shape)
    elif lib.custom == "bzip2":
        _build_bzip2(conanfile, source_root, package_root, shape)
    elif lib.custom == "jemalloc":
        _build_jemalloc(conanfile, source_root, package_root, shape)
    else:
        _build_cmake(conanfile, source_root / lib.cmake_subdir, package_root, shape, lib)

    if lib.tool == "mimalloc":
        _flatten_mimalloc_install_layout(package_root)

    if lib.pkg_config_name and lib.pkg_config_lib:
        _write_pkg_config(package_root, lib, shape, lib.pkg_config_name, lib.pkg_config_lib)

    _write_meta(build_root, lib, shape_name, shape)
    _sanity_check_package(package_root, lib)


def package(conanfile) -> None:
    from conan.tools.files import copy

    copy(conanfile, "*", src=Path(conanfile.build_folder, "package").as_posix(), dst=conanfile.package_folder)
    copy(conanfile, "meta.json", src=conanfile.build_folder, dst=conanfile.package_folder)


def _fetch_source(lib: Library, build_root: Path) -> Path:
    archive = build_root / Path(lib.source_url).name
    if not archive.is_file():
        req = urllib.request.Request(lib.source_url, headers={"User-Agent": "curl/8.5.0"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            archive.write_bytes(resp.read())

    extract_root = build_root / "src"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True)

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_root)
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(extract_root)
    elif archive.name.endswith(".tar.bz2"):
        with tarfile.open(archive, "r:bz2") as tf:
            tf.extractall(extract_root)
    else:
        raise RuntimeError(f"unsupported source archive: {archive}")

    children = [p for p in extract_root.iterdir() if p.is_dir()]
    if len(children) == 1:
        return children[0]
    return extract_root


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True)


def _build_cmake(conanfile, source_dir: Path, package_root: Path, shape: Shape, lib: Library) -> None:
    build_dir = Path(conanfile.build_folder) / "cmake-build"
    args = [
        "cmake",
        "-S",
        str(source_dir),
        "-B",
        str(build_dir),
        f"-DCMAKE_INSTALL_PREFIX={package_root}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
    ]
    if shape.musl:
        args.append("-DCMAKE_C_COMPILER=musl-gcc")
    if shape.cmake_arch:
        args.extend(["-A", shape.cmake_arch])
    for key, value in sorted((lib.cmake_defs or {}).items()):
        args.append(f"-D{key}={value}")
    _run(args, cwd=Path(conanfile.build_folder))
    if lib.cmake_install_component:
        build_args = ["cmake", "--build", str(build_dir), "--config", "Release", "--parallel"]
        if lib.cmake_build_target:
            build_args.extend(["--target", lib.cmake_build_target])
        _run(build_args, cwd=Path(conanfile.build_folder))
        _run(
            [
                "cmake",
                "--install",
                str(build_dir),
                "--config",
                "Release",
                "--component",
                lib.cmake_install_component,
            ],
            cwd=Path(conanfile.build_folder),
        )
    else:
        _run(["cmake", "--build", str(build_dir), "--config", "Release", "--target", "install", "--parallel"], cwd=Path(conanfile.build_folder))


def _build_sqlite(conanfile, source_root: Path, package_root: Path, shape: Shape) -> None:
    cmakelists = source_root / "CMakeLists.txt"
    cmakelists.write_text(
        """cmake_minimum_required(VERSION 3.16)
project(sqlite_sysroot C)
add_library(sqlite3 STATIC sqlite3.c)
target_compile_definitions(sqlite3 PRIVATE SQLITE_THREADSAFE=1 SQLITE_ENABLE_COLUMN_METADATA)
install(TARGETS sqlite3 ARCHIVE DESTINATION lib LIBRARY DESTINATION lib RUNTIME DESTINATION bin)
install(FILES sqlite3.h sqlite3ext.h DESTINATION include)
""",
        encoding="utf-8",
    )
    custom_lib = Library(
        tool="sqlite",
        version="",
        source_url="",
        license="",
        description="",
        cmake_defs={"BUILD_SHARED_LIBS": "OFF"},
    )
    _build_cmake(conanfile, source_root, package_root, shape, custom_lib)


def _build_bzip2(conanfile, source_root: Path, package_root: Path, shape: Shape) -> None:
    cmakelists = source_root / "CMakeLists.txt"
    cmakelists.write_text(
        """cmake_minimum_required(VERSION 3.16)
project(bzip2_sysroot C)
add_library(bz2 STATIC blocksort.c huffman.c crctable.c randtable.c compress.c decompress.c bzlib.c)
target_compile_definitions(bz2 PRIVATE _FILE_OFFSET_BITS=64 BZ_NO_STDIO)
if(WIN32)
  target_compile_definitions(bz2 PRIVATE _WIN32 BZ_EXPORT)
endif()
install(TARGETS bz2 ARCHIVE DESTINATION lib LIBRARY DESTINATION lib RUNTIME DESTINATION bin)
install(FILES bzlib.h DESTINATION include)
""",
        encoding="utf-8",
    )
    custom_lib = Library(
        tool="bzip2",
        version="",
        source_url="",
        license="",
        description="",
        cmake_defs={"BUILD_SHARED_LIBS": "OFF"},
    )
    _build_cmake(conanfile, source_root, package_root, shape, custom_lib)


def _build_jemalloc(conanfile, source_root: Path, package_root: Path, shape: Shape) -> None:
    if shape.conan_os == "Windows":
        raise ConanInvalidConfiguration("jemalloc does not support Windows")
    env = os.environ.copy()
    if shape.musl:
        env["CC"] = "musl-gcc"
    configure = source_root / "configure"
    if not configure.exists():
        _run(["./autogen.sh"], cwd=source_root, env=env)
    _run(
        [
            str(configure),
            f"--prefix={package_root}",
            "--disable-shared",
            "--enable-static",
        ],
        cwd=source_root,
        env=env,
    )
    _run(["make", "-j2"], cwd=source_root, env=env)
    _run(["make", "install"], cwd=source_root, env=env)


def _flatten_mimalloc_install_layout(package_root: Path) -> None:
    for parent_name in ("lib", "include"):
        parent = package_root / parent_name
        if not parent.is_dir():
            continue
        for child in parent.glob("mimalloc-*"):
            if not child.is_dir():
                continue
            for item in child.iterdir():
                target = parent / item.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                shutil.move(str(item), str(target))
            try:
                child.rmdir()
            except OSError:
                pass


def _write_pkg_config(package_root: Path, lib: Library, shape: Shape, pc_name: str, link_lib: str) -> None:
    if shape.conan_os == "Windows" and lib.msvc_pkg_config_lib:
        link_lib = lib.msvc_pkg_config_lib
    pc_dir = package_root / "lib" / "pkgconfig"
    pc_dir.mkdir(parents=True, exist_ok=True)
    (pc_dir / f"{pc_name}.pc").write_text(
        f"""prefix=${{pcfiledir}}/../..
exec_prefix=${{prefix}}
libdir=${{prefix}}/lib
includedir=${{prefix}}/include

Name: {pc_name}
Description: {lib.description}
Version: {lib.version}
Libs: -L${{libdir}} -l{link_lib}
Cflags: -I${{includedir}}
""",
        encoding="utf-8",
    )


def _write_meta(build_root: Path, lib: Library, shape_name: str, shape: Shape) -> None:
    (build_root / "meta.json").write_text(
        json.dumps(
            {
                "lib": lib.tool,
                "lib_version": lib.version,
                "target_triple": shape.target_triple,
                "shape": shape_name,
                "source_url": lib.source_url,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _sanity_check_package(package_root: Path, lib: Library) -> None:
    if not (package_root / "include").is_dir():
        raise RuntimeError(f"{lib.tool}: package missing include/")
    if not (package_root / "lib").is_dir():
        raise RuntimeError(f"{lib.tool}: package missing lib/")
    libs = list((package_root / "lib").glob("*.a")) + list((package_root / "lib").glob("*.lib"))
    if not libs:
        raise RuntimeError(f"{lib.tool}: package missing static library in lib/")
