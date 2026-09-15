"""Shared Conan recipe plumbing for soldr#1064 syslib bundles.

Each thin recipe directory is named ``<lib>-<shape>`` and imports this
module. Forge builds one recipe per target shape, then the ingest step
places the package at ``<lib>/<version>/<shape>/bundle.tar.zst``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping

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
    cmake_generator: str | None = None
    musl: bool = False


SHAPES: dict[str, Shape] = {
    "windows-x64": Shape(
        "x86_64-pc-windows-msvc", "Windows", "x86_64", "windows_x64", "windows-x64", "x64"
    ),
    "windows-x64-gnu": Shape(
        "x86_64-pc-windows-gnu",
        "Windows",
        "x86_64",
        "windows_x64_gnu",
        "windows-x64-gnu",
        cmake_generator="MinGW Makefiles",
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
    # Lowercase hex sha256 of the source archive. Verified when set.
    source_sha256: str | None = None
    # Source-relative license file copied into the package root when set.
    license_file: str | None = None


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
        unsupported_shapes=frozenset({"windows-x64", "windows-x64-gnu", "windows-arm64"}),
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
    # soldr#3246: static, source-built OpenSSL for openssl-sys. Replaces the
    # FireDaemon DLL repackage recipes (soldr#943) for new versions.
    "openssl": Library(
        tool="openssl",
        version="3.5.8",
        source_url="https://github.com/openssl/openssl/releases/download/openssl-3.5.8/openssl-3.5.8.tar.gz",
        license="Apache-2.0",
        description="OpenSSL libssl and libcrypto static libraries and headers for openssl-sys",
        custom="openssl",
        source_sha256="a8f84a39918ec6415ce765d9b429d313ba97b8143169c172e734b9514464f5b2",
        license_file="LICENSE.txt",
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
    elif lib.custom == "openssl":
        _build_openssl(source_root, package_root, shape_name, lib)
    else:
        _build_cmake(conanfile, source_root / lib.cmake_subdir, package_root, shape, lib)

    if lib.tool == "mimalloc":
        _flatten_mimalloc_install_layout(package_root)

    if lib.pkg_config_name and lib.pkg_config_lib:
        _write_pkg_config(package_root, lib, shape, lib.pkg_config_name, lib.pkg_config_lib)

    if lib.license_file:
        shutil.copy2(source_root / lib.license_file, package_root / Path(lib.license_file).name)

    _write_meta(build_root, lib, shape_name, shape)
    _sanity_check_package(package_root, lib, shape_name)


def package(conanfile) -> None:
    from conan.tools.files import copy

    copy(conanfile, "*", src=Path(conanfile.build_folder, "package").as_posix(), dst=conanfile.package_folder)
    copy(conanfile, "meta.json", src=conanfile.build_folder, dst=conanfile.package_folder)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_source_sha256(lib: Library, archive: Path) -> None:
    if not lib.source_sha256:
        return
    actual = _sha256_file(archive)
    if actual != lib.source_sha256.lower():
        raise RuntimeError(
            f"{lib.tool}: source sha256 mismatch for {lib.source_url}: "
            f"expected {lib.source_sha256}, got {actual}"
        )


def _fetch_source(lib: Library, build_root: Path) -> Path:
    archive = build_root / Path(lib.source_url).name
    if not archive.is_file():
        req = urllib.request.Request(lib.source_url, headers={"User-Agent": "curl/8.5.0"})
        partial = archive.with_name(archive.name + ".part")
        with urllib.request.urlopen(req, timeout=600) as resp:
            partial.write_bytes(resp.read())
        partial.replace(archive)
    try:
        _verify_source_sha256(lib, archive)
    except RuntimeError:
        archive.unlink()
        raise

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
    if shape.cmake_generator:
        args.extend(["-G", shape.cmake_generator])
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


# ---------------------------------------------------------------------------
# OpenSSL (soldr#3246)
#
# The command construction below is pure so tests can assert the exact argv
# and environment per shape without running Perl, make or vcvarsall.
# ---------------------------------------------------------------------------

OPENSSL_CONFIGURE_TARGETS: dict[str, str] = {
    "windows-x64": "VC-WIN64A",
    "windows-arm64": "VC-WIN64-ARM",
    "windows-x64-gnu": "mingw64",
    "darwin-x64": "darwin64-x86_64-cc",
    "darwin-arm64": "darwin64-arm64-cc",
    "linux-x64-gnu": "linux-x86_64",
    "linux-arm64-gnu": "linux-aarch64",
    "linux-x64-musl": "linux-x86_64",
    "linux-arm64-musl": "linux-aarch64",
}

# ``no-module`` links the legacy provider into libcrypto. Without it,
# ``no-shared`` still installs ``lib/ossl-modules/legacy.so`` (``.dll`` on
# Windows), which a static-only bundle must not ship.
OPENSSL_COMMON_OPTIONS: tuple[str, ...] = ("no-shared", "no-module", "no-tests", "no-docs", "no-apps")

# Match rustc's default deployment targets so linking the archives into a
# Rust binary does not warn about objects built for a newer macOS.
OPENSSL_MACOSX_DEPLOYMENT_TARGETS: dict[str, str] = {
    "darwin-x64": "10.12",
    "darwin-arm64": "11.0",
}

OPENSSL_PKG_CONFIG_NAMES: tuple[str, ...] = ("libcrypto", "libssl", "openssl")

# System libraries libcrypto needs on Windows (mirrors OpenSSL's ex_libs).
OPENSSL_WINDOWS_SYSTEM_LIBS: tuple[str, ...] = ("ws2_32", "gdi32", "crypt32", "advapi32", "user32")

_STRAWBERRY_PERL = r"C:\Strawberry\perl\bin\perl.exe"
# Git for Windows trims its MSYS perl (no Locale::Maketext::Simple, which
# OpenSSL's Configure needs through IPC::Cmd). The runner images' MSYS2 perl
# is complete and is the Perl OpenSSL documents for mingw builds.
_MSYS2_PERL = r"C:\msys64\usr\bin\perl.exe"
# When that perl is absent, Git for Windows' perl is all there is, and it
# ships without core_perl/Locale/ and core_perl/ExtUtils/
# and core_perl/Pod/ (git-for-windows/build-extra make-file-list.sh).
# OpenSSL's Configure reaches Locale and ExtUtils only through IPC::Cmd:
# Params::Check and Module::Load::Conditional call loc() with %N or [_N]
# placeholders, and can_run() loads ExtUtils::MakeMaker solely for
# MM->maybe_command. Configure then runs configdata.pm to write the Makefile,
# and configdata.pm loads Pod::Usage at compile time.
_LOCALE_MAKETEXT_SIMPLE_SHIM = r"""package Locale::Maketext::Simple;
use strict;
use warnings;
our $VERSION = '0.21';

sub import {
    my $caller = caller;
    no strict 'refs';
    *{"${caller}::loc"} = sub {
        my ($text, @args) = @_;
        $text =~ s{%(\d+)|\[_(\d+)\]}{$args[(defined $1 ? $1 : $2) - 1] // ''}ge;
        return $text;
    };
    *{"${caller}::loc_lang"} = sub { 1 };
}

1;
"""
_EXTUTILS_MAKEMAKER_SHIM = r"""package ExtUtils::MakeMaker;
use strict;
use warnings;
our $VERSION = '7.70';

package MM;

# IPC::Cmd::can_run's only use of MakeMaker: an executable regular file, with
# the Windows executable suffixes MSYS resolves implicitly.
sub maybe_command {
    my ($self, $file) = @_;
    for my $candidate ($file, map { "$file$_" } qw(.exe .com .bat .cmd)) {
        return $candidate if -f $candidate && -x _;
    }
    return;
}

1;
"""
_POD_USAGE_SHIM = r"""package Pod::Usage;
use strict;
use warnings;
use Exporter 'import';
our $VERSION = '2.03';
our @EXPORT = qw(pod2usage);

# configdata.pm loads Pod::Usage at compile time but calls pod2usage only for
# --help or an unknown option, neither of which the recipe passes.
sub pod2usage {
    my %args = @_ == 1 && ref $_[0] eq 'HASH' ? %{$_[0]}
             : @_ == 1                        ? (-exitval => $_[0])
             :                                  @_;
    my $message = defined $args{-message} ? $args{-message} : $args{-msg};
    print STDERR "$message\n" if defined $message;
    my $exit = defined $args{-exitval} ? $args{-exitval} : 2;
    exit($exit) unless $exit eq 'NOEXIT';
    return;
}

1;
"""
_PERL_SHIMS: dict[str, str] = {
    "Locale::Maketext::Simple": _LOCALE_MAKETEXT_SIMPLE_SHIM,
    "ExtUtils::MakeMaker": _EXTUTILS_MAKEMAKER_SHIM,
    "Pod::Usage": _POD_USAGE_SHIM,
}

# musl-gcc's specs drop the host include path, which also hides the kernel
# UAPI headers OpenSSL includes on Linux (crypto/mem_sec.c: <linux/mman.h>).
_MUSL_KERNEL_MULTIARCH: dict[str, str] = {
    "linux-x64-musl": "x86_64-linux-gnu",
    "linux-arm64-musl": "aarch64-linux-gnu",
}


def _is_msvc_shape(shape_name: str) -> bool:
    return SHAPES[shape_name].target_triple.endswith("-msvc")


def _openssl_static_lib_names(shape_name: str) -> tuple[str, str]:
    if _is_msvc_shape(shape_name):
        return ("libssl.lib", "libcrypto.lib")
    # OpenSSL's Unix and mingw platforms both use the ".a" static extension.
    return ("libssl.a", "libcrypto.a")


def _openssl_prefix_arg(shape_name: str, package_root: Path | str, perl_os: str | None = None) -> str:
    """Render the install prefix in the form the selected Perl accepts.

    MSVC Configure runs under native Windows Perl and gets a native path. The
    mingw build may run under MSYS Perl, whose Unix File::Spec rejects
    ``D:/...`` as non-absolute, so drive paths become ``/d/...`` there.
    """
    raw = str(package_root)
    if _is_msvc_shape(shape_name):
        return str(PureWindowsPath(raw))
    if SHAPES[shape_name].conan_os == "Windows":
        posix = raw.replace("\\", "/")
        if perl_os in {"msys", "cygwin"} and len(posix) >= 2 and posix[1] == ":":
            posix = f"/{posix[0].lower()}{posix[2:]}"
        return posix
    return str(PurePosixPath(raw))


def _openssl_configure_args(
    shape_name: str,
    package_root: Path | str,
    *,
    perl: str = "perl",
    perl_os: str | None = None,
    include_dirs: tuple[str, ...] = (),
) -> list[str]:
    shape = SHAPES[shape_name]
    prefix = _openssl_prefix_arg(shape_name, package_root, perl_os)
    sep = "\\" if _is_msvc_shape(shape_name) else "/"
    args = [perl, "Configure", OPENSSL_CONFIGURE_TARGETS[shape_name], *OPENSSL_COMMON_OPTIONS]
    if shape.conan_os == "Windows":
        # Forge's Windows images have no NASM.
        args.append("no-asm")
    if shape.musl:
        # musl lacks the ucontext API OpenSSL's async jobs use.
        args.append("no-async")
    args.extend(f"-I{include_dir}" for include_dir in include_dirs)
    args.extend(
        [
            f"--prefix={prefix}",
            f"--openssldir={prefix}{sep}ssl",
            "--libdir=lib",
        ]
    )
    return args


def _openssl_build_env(shape_name: str, base_env: Mapping[str, str]) -> dict[str, str]:
    shape = SHAPES[shape_name]
    env = dict(base_env)
    if shape.musl:
        env["CC"] = "musl-gcc"
    deployment_target = OPENSSL_MACOSX_DEPLOYMENT_TARGETS.get(shape_name)
    if deployment_target and not env.get("MACOSX_DEPLOYMENT_TARGET"):
        env["MACOSX_DEPLOYMENT_TARGET"] = deployment_target
    return env


def _musl_kernel_header_dir(
    shape_name: str, build_root: Path, system_include: Path = Path("/usr/include")
) -> Path:
    """Expose only the host's kernel UAPI headers to musl-gcc.

    Adding ``/usr/include`` itself would put glibc's headers behind musl's;
    a directory holding just ``linux``, ``asm`` and ``asm-generic`` does not.
    """
    multiarch = _MUSL_KERNEL_MULTIARCH[shape_name]
    sources = {
        "linux": system_include / "linux",
        "asm": system_include / multiarch / "asm",
        "asm-generic": system_include / "asm-generic",
    }
    missing = [str(path) for path in sources.values() if not path.is_dir()]
    if missing:
        raise RuntimeError(f"openssl: kernel headers missing for {shape_name}: {', '.join(missing)}")
    header_dir = build_root / "musl-kernel-headers"
    header_dir.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        link = header_dir / name
        if not link.is_symlink():
            link.symlink_to(source, target_is_directory=True)
    return header_dir


def _openssl_make_commands(shape_name: str, jobs: int, make: str | None = None) -> list[list[str]]:
    if _is_msvc_shape(shape_name):
        return [[make or "nmake", "install_sw"]]
    make = make or "make"
    return [[make, f"-j{max(1, jobs)}", "build_sw"], [make, "install_sw"]]


def _vcvars_arch(shape_name: str, host_machine: str) -> str:
    """vcvarsall.bat architecture argument for a target shape and host CPU.

    ``host_machine`` is ``platform.machine()``: ``AMD64`` or ``ARM64``.
    Forge builds natively, so the expected pairs are x64/x64 and arm64/arm64.
    """
    host_is_arm64 = host_machine.upper() in {"ARM64", "AARCH64"}
    if shape_name == "windows-x64":
        return "arm64_amd64" if host_is_arm64 else "x64"
    if shape_name == "windows-arm64":
        return "arm64" if host_is_arm64 else "x64_arm64"
    raise ValueError(f"{shape_name} is not an MSVC shape")


def _vcvars_command(vcvarsall: str, arch: str) -> str:
    # Passed to subprocess as a string: Python's list quoting would emit \"
    # escapes that cmd.exe does not understand. /s strips the outer quotes.
    return f'cmd.exe /d /s /c ""{vcvarsall}" {arch} >nul && set"'


def _parse_set_output(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("="):
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _vswhere_path(env: Mapping[str, str]) -> str:
    base = env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    return str(PureWindowsPath(base, "Microsoft Visual Studio", "Installer", "vswhere.exe"))


def _find_vcvarsall(env: Mapping[str, str]) -> str:
    vswhere = _vswhere_path(env)
    if not Path(vswhere).is_file():
        raise RuntimeError(f"openssl: vswhere.exe not found at {vswhere}")
    result = subprocess.run(
        [vswhere, "-latest", "-products", "*", "-find", r"VC\Auxiliary\Build\vcvarsall.bat"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("openssl: vswhere did not locate VC\\Auxiliary\\Build\\vcvarsall.bat")


def _msvc_env(shape_name: str, base_env: Mapping[str, str]) -> dict[str, str]:
    vcvarsall = _find_vcvarsall(base_env)
    arch = _vcvars_arch(shape_name, platform.machine())
    command = _vcvars_command(vcvarsall, arch)
    print(f"+ {command}", flush=True)
    result = subprocess.run(command, capture_output=True, text=True, env=dict(base_env), check=True)
    env = _parse_set_output(result.stdout)
    upper = {key.upper() for key in env}
    if not {"INCLUDE", "LIB", "PATH"} <= upper:
        raise RuntimeError(f"openssl: vcvarsall.bat {arch} did not produce a developer environment")
    return env


def _openssl_perl(shape_name: str, env: Mapping[str, str]) -> str:
    override = env.get("SOLDR_OPENSSL_PERL")
    if override:
        return override
    # OpenSSL's VC targets need native Windows Perl; Git's MSYS perl can shadow
    # Strawberry Perl on PATH inside a Git Bash step.
    if _is_msvc_shape(shape_name) and Path(_STRAWBERRY_PERL).is_file():
        return _STRAWBERRY_PERL
    if shape_name == "windows-x64-gnu" and Path(_MSYS2_PERL).is_file():
        return _MSYS2_PERL
    return "perl"


def _perl_os(perl: str, env: Mapping[str, str]) -> str:
    result = subprocess.run(
        [perl, "-e", "print $^O"], capture_output=True, text=True, env=dict(env), check=True
    )
    return result.stdout.strip()


def _msys_path(path: Path | str) -> str:
    posix = str(path).replace("\\", "/")
    if len(posix) >= 2 and posix[1] == ":":
        posix = f"/{posix[0].lower()}{posix[2:]}"
    return posix


def _perl_has_module(perl: str, module: str, env: Mapping[str, str]) -> bool:
    probe = subprocess.run([perl, f"-M{module}", "-e", "1"], capture_output=True, env=dict(env), check=False)
    return probe.returncode == 0


def _perl_shim_env(
    env: Mapping[str, str], shim_root: Path, perl_os: str | None, modules: tuple[str, ...]
) -> dict[str, str]:
    """Put shims for the named core modules first on a trimmed perl's @INC."""
    for name in modules:
        module = shim_root.joinpath(*name.split("::")).with_suffix(".pm")
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(_PERL_SHIMS[name], encoding="utf-8", newline="\n")
    # MSYS perl splits PERL5LIB on ':', so a "C:/..." entry would break apart.
    unix_like = perl_os != "MSWin32"
    entry = _msys_path(shim_root) if unix_like else str(shim_root)
    separator = ":" if unix_like else ";"
    shimmed = dict(env)
    shimmed["PERL5LIB"] = separator.join(part for part in (entry, env.get("PERL5LIB")) if part)
    if unix_like:
        # OpenSSL's Makefile re-invokes make; when MSYS sh starts that native
        # make.exe, the MSYS2 runtime rewrites path-like variables to "C:/...",
        # which MSYS perl then splits on ':' (forge run 34923387721). Keep
        # PERL5LIB out of that environment conversion.
        excluded = [part for part in env.get("MSYS2_ENV_CONV_EXCL", "").split(";") if part]
        if "PERL5LIB" not in excluded:
            excluded.append("PERL5LIB")
        shimmed["MSYS2_ENV_CONV_EXCL"] = ";".join(excluded)
    return shimmed


def _env_path(env: Mapping[str, str]) -> str | None:
    # vcvarsall's `set` output spells it "Path"; POSIX environments use "PATH".
    return next((value for key, value in env.items() if key.upper() == "PATH"), None)


def _openssl_make_program(shape_name: str, env: Mapping[str, str]) -> str:
    if SHAPES[shape_name].conan_os != "Windows":
        return "make"
    # Windows process creation searches the *parent's* PATH, not the child
    # env's, so a program that only the developer environment provides
    # (nmake) must be spawned by absolute path.
    candidates = ("nmake",) if _is_msvc_shape(shape_name) else ("make", "mingw32-make", "gmake")
    path = _env_path(env)
    for candidate in candidates:
        found = shutil.which(candidate, path=path)
        if found:
            return found
    raise RuntimeError(f"openssl: no make program ({', '.join(candidates)}) on the build PATH")


def _build_openssl(source_root: Path, package_root: Path, shape_name: str, lib: Library) -> None:
    env = _openssl_build_env(shape_name, os.environ)
    perl = _openssl_perl(shape_name, env)
    perl_os = _perl_os(perl, env) if shape_name == "windows-x64-gnu" else None
    if shape_name == "windows-x64-gnu":
        # Configure records $ENV{PERL} (else $^X, an MSYS path that Git Bash
        # would resolve to its own trimmed perl) for the Makefile's recipes.
        env["PERL"] = perl.replace("\\", "/")
        missing = tuple(name for name in _PERL_SHIMS if not _perl_has_module(perl, name, env))
        if missing:
            print(f"openssl: shimming perl modules absent from {perl}: {', '.join(missing)}", flush=True)
            env = _perl_shim_env(env, source_root.parent / "perl-shims", perl_os, missing)
    if _is_msvc_shape(shape_name):
        env = _msvc_env(shape_name, env)
    make = _openssl_make_program(shape_name, env)
    include_dirs: tuple[str, ...] = ()
    if SHAPES[shape_name].musl:
        include_dirs = (str(_musl_kernel_header_dir(shape_name, source_root.parent)),)
    _run(
        _openssl_configure_args(
            shape_name, package_root, perl=perl, perl_os=perl_os, include_dirs=include_dirs
        ),
        cwd=source_root,
        env=env,
    )
    for command in _openssl_make_commands(shape_name, os.cpu_count() or 2, make):
        _run(command, cwd=source_root, env=env)
    _prune_openssl_install(package_root)
    _write_openssl_pkg_config(package_root, shape_name, lib, str(package_root))


def _prune_openssl_install(package_root: Path) -> None:
    """Drop install_sw leftovers a static link bundle does not need."""
    for name in ("bin", "ssl"):
        path = package_root / name
        if path.is_dir():
            shutil.rmtree(path)
    for name in ("engines-3", "ossl-modules"):
        path = package_root / "lib" / name
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def _relocate_pkg_config_text(text: str, abs_prefix: str | None = None) -> str:
    """Rewrite an installed OpenSSL .pc file to be relocatable."""
    replacements = {
        "prefix": "${pcfiledir}/../..",
        "exec_prefix": "${prefix}",
        "libdir": "${exec_prefix}/lib",
        "includedir": "${prefix}/include",
    }
    prefixes: list[str] = []
    if abs_prefix:
        for form in (abs_prefix, abs_prefix.replace("\\", "/")):
            if form not in prefixes:
                prefixes.append(form)
    out: list[str] = []
    for line in text.splitlines():
        key, sep, _ = line.partition("=")
        if sep and key.strip() in replacements and ":" not in key:
            out.append(f"{key.strip()}={replacements[key.strip()]}")
            continue
        for form in prefixes:
            line = line.replace(form, "${prefix}")
        out.append(line)
    return "\n".join(out) + "\n"


def _openssl_msvc_pkg_config(version: str) -> dict[str, str]:
    """pkg-config files for MSVC, where OpenSSL's nmake install writes none."""
    header = (
        "prefix=${pcfiledir}/../..\n"
        "exec_prefix=${prefix}\n"
        "libdir=${exec_prefix}/lib\n"
        "includedir=${prefix}/include\n\n"
    )
    system_libs = " ".join(f"-l{name}" for name in OPENSSL_WINDOWS_SYSTEM_LIBS)
    return {
        "libcrypto": header
        + "Name: OpenSSL-libcrypto\n"
        + "Description: OpenSSL cryptography library\n"
        + f"Version: {version}\n"
        + "Libs: -L${libdir} -llibcrypto\n"
        + f"Libs.private: {system_libs}\n"
        + "Cflags: -I${includedir}\n",
        "libssl": header
        + "Name: OpenSSL-libssl\n"
        + "Description: Secure Sockets Layer and cryptography libraries\n"
        + f"Version: {version}\n"
        + "Requires.private: libcrypto\n"
        + "Libs: -L${libdir} -llibssl\n"
        + "Cflags: -I${includedir}\n",
        "openssl": header
        + "Name: OpenSSL\n"
        + "Description: Secure Sockets Layer and cryptography libraries and tools\n"
        + f"Version: {version}\n"
        + "Requires: libssl libcrypto\n",
    }


def _write_openssl_pkg_config(
    package_root: Path, shape_name: str, lib: Library, abs_prefix: str | None
) -> None:
    pc_dir = package_root / "lib" / "pkgconfig"
    pc_dir.mkdir(parents=True, exist_ok=True)
    if _is_msvc_shape(shape_name):
        for name, text in _openssl_msvc_pkg_config(lib.version).items():
            (pc_dir / f"{name}.pc").write_text(text, encoding="utf-8")
        return
    for name in OPENSSL_PKG_CONFIG_NAMES:
        pc_file = pc_dir / f"{name}.pc"
        if not pc_file.is_file():
            raise RuntimeError(f"openssl: install_sw did not produce lib/pkgconfig/{name}.pc")
        text = pc_file.read_text(encoding="utf-8")
        pc_file.write_text(_relocate_pkg_config_text(text, abs_prefix), encoding="utf-8")


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
    if shape.target_triple.endswith("-msvc") and lib.msvc_pkg_config_lib:
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
    meta = {
        "lib": lib.tool,
        "lib_version": lib.version,
        "target_triple": shape.target_triple,
        "shape": shape_name,
        "source_url": lib.source_url,
    }
    if lib.source_sha256:
        meta["source_sha256"] = lib.source_sha256
    (build_root / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n",
        encoding="utf-8",
    )


def _is_shared_library(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.endswith((".dll", ".dylib", ".so", ".dll.a"))
        or ".so." in name
    )


def _sanity_check_openssl(package_root: Path, shape_name: str | None) -> None:
    if shape_name is None:
        raise RuntimeError("openssl: sanity check needs the target shape")
    for header in ("ssl.h", "crypto.h", "opensslv.h"):
        if not (package_root / "include" / "openssl" / header).is_file():
            raise RuntimeError(f"openssl: package missing include/openssl/{header}")
    for name in _openssl_static_lib_names(shape_name):
        if not (package_root / "lib" / name).is_file():
            raise RuntimeError(f"openssl: package missing static library lib/{name}")
    shared = sorted(
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file() and _is_shared_library(path)
    )
    if shared:
        raise RuntimeError(f"openssl: package must be static-only, found {', '.join(shared)}")
    for name in OPENSSL_PKG_CONFIG_NAMES:
        pc_file = package_root / "lib" / "pkgconfig" / f"{name}.pc"
        if not pc_file.is_file():
            raise RuntimeError(f"openssl: package missing lib/pkgconfig/{name}.pc")
        if "prefix=${pcfiledir}/../..\n" not in pc_file.read_text(encoding="utf-8"):
            raise RuntimeError(f"openssl: lib/pkgconfig/{name}.pc is not relocatable")


def _sanity_check_package(package_root: Path, lib: Library, shape_name: str | None = None) -> None:
    if not (package_root / "include").is_dir():
        raise RuntimeError(f"{lib.tool}: package missing include/")
    if not (package_root / "lib").is_dir():
        raise RuntimeError(f"{lib.tool}: package missing lib/")
    libs = list((package_root / "lib").glob("*.a")) + list((package_root / "lib").glob("*.lib"))
    if not libs:
        raise RuntimeError(f"{lib.tool}: package missing static library in lib/")
    if lib.tool == "openssl":
        _sanity_check_openssl(package_root, shape_name)
