import dataclasses
import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from recipes import _syslib
from scripts import generate_syslib_recipes


def test_windows_gnu_shape_is_available_for_core_syslibs() -> None:
    shape = _syslib.SHAPES["windows-x64-gnu"]
    assert shape.target_triple == "x86_64-pc-windows-gnu"
    assert shape.forge_input == "windows_x64_gnu"
    assert shape.cmake_generator == "MinGW Makefiles"

    for tool in ("zstd", "sqlite", "mimalloc", "zlib-ng", "lzma", "bzip2", "openssl"):
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


# --- OpenSSL (soldr#3246) ---------------------------------------------------

OPENSSL_SHA256 = "a8f84a39918ec6415ce765d9b429d313ba97b8143169c172e734b9514464f5b2"
COMMON = ["no-shared", "no-module", "no-tests", "no-docs", "no-apps"]


def test_openssl_library_definition_pins_source() -> None:
    lib = _syslib.LIBRARIES["openssl"]
    assert lib.tool == "openssl"
    assert lib.version == "3.5.8"
    assert lib.source_url == (
        "https://github.com/openssl/openssl/releases/download/openssl-3.5.8/openssl-3.5.8.tar.gz"
    )
    assert lib.license == "Apache-2.0"
    assert lib.custom == "openssl"
    assert lib.source_sha256 == OPENSSL_SHA256
    assert lib.license_file == "LICENSE.txt"
    assert lib.unsupported_shapes == frozenset()
    assert set(_syslib.OPENSSL_CONFIGURE_TARGETS) == set(_syslib.SHAPES)
    assert _syslib.split_recipe_name("openssl-windows-x64-gnu") == (lib, "windows-x64-gnu")


def test_existing_syslibs_do_not_verify_source_sha256() -> None:
    for name, lib in _syslib.LIBRARIES.items():
        if name != "openssl":
            assert lib.source_sha256 is None, name


def _fake_source_archive(tmp_path: Path) -> tuple[Path, str]:
    payload = tmp_path / "payload" / "demo-1.0"
    payload.mkdir(parents=True)
    (payload / "README").write_text("demo", encoding="utf-8")
    archive = tmp_path / "demo-1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(payload, arcname="demo-1.0")
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_fetch_source_verifies_pinned_sha256(tmp_path: Path) -> None:
    archive, digest = _fake_source_archive(tmp_path)
    build_root = tmp_path / "build"
    build_root.mkdir()
    shutil.copy2(archive, build_root / archive.name)
    lib = _syslib.Library(
        tool="demo",
        version="1.0",
        source_url=f"https://example.invalid/{archive.name}",
        license="MIT",
        description="demo",
        source_sha256=digest,
    )
    source = _syslib._fetch_source(lib, build_root)
    assert (source / "README").read_text(encoding="utf-8") == "demo"

    bad = dataclasses.replace(lib, source_sha256="0" * 64)
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        _syslib._fetch_source(bad, build_root)
    # A corrupt cached archive is discarded so the next build re-downloads.
    assert not (build_root / archive.name).exists()


def test_fetch_source_without_pin_skips_verification(tmp_path: Path) -> None:
    archive, _ = _fake_source_archive(tmp_path)
    build_root = tmp_path / "build"
    build_root.mkdir()
    shutil.copy2(archive, build_root / archive.name)
    lib = _syslib.Library(
        tool="demo",
        version="1.0",
        source_url=f"https://example.invalid/{archive.name}",
        license="MIT",
        description="demo",
    )
    assert (_syslib._fetch_source(lib, build_root) / "README").is_file()


@pytest.mark.parametrize(
    ("shape", "target", "extra"),
    [
        ("darwin-x64", "darwin64-x86_64-cc", []),
        ("darwin-arm64", "darwin64-arm64-cc", []),
        ("linux-x64-gnu", "linux-x86_64", []),
        ("linux-arm64-gnu", "linux-aarch64", []),
        ("linux-x64-musl", "linux-x86_64", ["no-async"]),
        ("linux-arm64-musl", "linux-aarch64", ["no-async"]),
    ],
)
def test_openssl_configure_args_unix(shape: str, target: str, extra: list[str]) -> None:
    assert _syslib._openssl_configure_args(shape, "/b/package") == [
        "perl",
        "Configure",
        target,
        *COMMON,
        *extra,
        "--prefix=/b/package",
        "--openssldir=/b/package/ssl",
        "--libdir=lib",
    ]


@pytest.mark.parametrize(("shape", "target"), [("windows-x64", "VC-WIN64A"), ("windows-arm64", "VC-WIN64-ARM")])
def test_openssl_configure_args_msvc(shape: str, target: str) -> None:
    perl = r"C:\Strawberry\perl\bin\perl.exe"
    assert _syslib._openssl_configure_args(shape, r"D:\a\build\package", perl=perl) == [
        perl,
        "Configure",
        target,
        *COMMON,
        "no-asm",
        r"--prefix=D:\a\build\package",
        r"--openssldir=D:\a\build\package\ssl",
        "--libdir=lib",
    ]


@pytest.mark.parametrize(
    ("perl_os", "prefix"),
    [("msys", "/d/a/build/package"), ("cygwin", "/d/a/build/package"), ("MSWin32", "D:/a/build/package")],
)
def test_openssl_configure_args_mingw(perl_os: str, prefix: str) -> None:
    assert _syslib._openssl_configure_args(
        "windows-x64-gnu", r"D:\a\build\package", perl_os=perl_os
    ) == [
        "perl",
        "Configure",
        "mingw64",
        *COMMON,
        "no-asm",
        f"--prefix={prefix}",
        f"--openssldir={prefix}/ssl",
        "--libdir=lib",
    ]


def test_openssl_build_env_per_shape() -> None:
    base = {"PATH": "/usr/bin", "CC": "cc"}
    for shape in _syslib.SHAPES:
        env = _syslib._openssl_build_env(shape, base)
        expected = dict(base)
        if shape.endswith("-musl"):
            expected["CC"] = "musl-gcc"
        if shape == "darwin-x64":
            expected["MACOSX_DEPLOYMENT_TARGET"] = "10.12"
        if shape == "darwin-arm64":
            expected["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
        assert env == expected, shape
    assert base == {"PATH": "/usr/bin", "CC": "cc"}
    kept = _syslib._openssl_build_env("darwin-arm64", {"MACOSX_DEPLOYMENT_TARGET": "12.0"})
    assert kept["MACOSX_DEPLOYMENT_TARGET"] == "12.0"


def test_openssl_make_commands() -> None:
    for shape in ("windows-x64", "windows-arm64"):
        assert _syslib._openssl_make_commands(shape, 8) == [["nmake", "install_sw"]]
    assert _syslib._openssl_make_commands("linux-x64-gnu", 8) == [
        ["make", "-j8", "build_sw"],
        ["make", "install_sw"],
    ]
    assert _syslib._openssl_make_commands("windows-x64-gnu", 0, "mingw32-make") == [
        ["mingw32-make", "-j1", "build_sw"],
        ["mingw32-make", "install_sw"],
    ]
    nmake = r"C:\VS\VC\Tools\MSVC\bin\Hostx64\x64\nmake.exe"
    assert _syslib._openssl_make_commands("windows-x64", 8, nmake) == [[nmake, "install_sw"]]


def test_openssl_make_program_resolves_nmake_on_the_developer_path(tmp_path: Path) -> None:
    # Forge run 34920971861: a bare "nmake" argv raised FileNotFoundError
    # because CreateProcess searched the parent's PATH, not vcvarsall's.
    tools = tmp_path / "msvc-bin"
    tools.mkdir()
    nmake = tools / "nmake"
    nmake.write_text("")
    nmake.chmod(0o755)
    # A Windows host also needs PATHEXT for shutil.which; POSIX ignores it.
    env = {"Path": str(tools), "PATHEXT": ""}
    for shape in ("windows-x64", "windows-arm64"):
        assert _syslib._openssl_make_program(shape, env) == str(nmake)
    with pytest.raises(RuntimeError, match="nmake"):
        _syslib._openssl_make_program("windows-x64", {"Path": str(tmp_path / "empty")})
    assert _syslib._openssl_make_program("linux-x64-musl", env) == "make"


def test_openssl_perl_prefers_complete_msys2_perl_for_mingw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Forge run 34920992491: Git for Windows' perl lacks Locale::Maketext::Simple.
    msys2_perl = tmp_path / "perl.exe"
    monkeypatch.setattr(_syslib, "_MSYS2_PERL", str(msys2_perl))
    assert _syslib._openssl_perl("windows-x64-gnu", {}) == "perl"
    msys2_perl.write_text("")
    assert _syslib._openssl_perl("windows-x64-gnu", {}) == str(msys2_perl)
    assert _syslib._openssl_perl("linux-x64-gnu", {}) == "perl"
    assert _syslib._openssl_perl("windows-x64-gnu", {"SOLDR_OPENSSL_PERL": "/x/perl"}) == "/x/perl"


def test_perl_shim_env_prepends_an_msys_form_include_path(tmp_path: Path) -> None:
    # Forge run 34921638947: no MSYS2 perl on the runner, only Git's trimmed one.
    # Run 34922722583 then needed ExtUtils::MakeMaker, also stripped by Git.
    shim_root = tmp_path / "perl-shims"
    env = _syslib._perl_shim_env({"PERL5LIB": "/existing"}, shim_root, "msys", ("Locale::Maketext::Simple",))
    assert (shim_root / "Locale" / "Maketext" / "Simple.pm").is_file()
    assert not (shim_root / "ExtUtils").exists(), "only missing modules are shimmed"
    assert env["PERL5LIB"] == f"{_syslib._msys_path(shim_root)}:/existing"
    assert _syslib._msys_path(r"D:\a\_temp\perl-shims") == "/d/a/_temp/perl-shims"
    native = _syslib._perl_shim_env({}, shim_root, "MSWin32", tuple(_syslib._PERL_SHIMS))
    assert (shim_root / "ExtUtils" / "MakeMaker.pm").is_file()
    assert native["PERL5LIB"] == str(shim_root)


@pytest.mark.skipif(shutil.which("perl") is None, reason="needs a perl interpreter")
def test_perl_shims_serve_ipc_cmd_and_its_can_run(tmp_path: Path) -> None:
    shim_root = tmp_path / "shims"
    env = _syslib._perl_shim_env(
        {"PATH": os.environ.get("PATH", "")}, shim_root, "linux", tuple(_syslib._PERL_SHIMS)
    )
    tool = tmp_path / "bin" / "fake-cc"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    script = (
        "use Locale::Maketext::Simple Style => 'gettext'; require ExtUtils::MakeMaker; "
        "print $INC{'Locale/Maketext/Simple.pm'}, \"\\n\", $INC{'ExtUtils/MakeMaker.pm'}, \"\\n\", "
        "loc('%1 of [_2]', 'one', 'two'), \"\\n\", MM->maybe_command($ARGV[0]) // 'undef', \"\\n\", "
        "MM->maybe_command($ARGV[1]) // 'undef', \"\\n\"; "
        # Run 34923015688: configdata.pm's compile-time `use Pod::Usage`.
        "use Pod::Usage; print $INC{'Pod/Usage.pm'}, \"\\n\", defined &pod2usage ? 'pod2usage' : 'missing', \"\\n\";"
    )
    result = subprocess.run(
        ["perl", "-e", script, str(tool), str(tmp_path / "bin")], env=env, capture_output=True, text=True, check=True
    )
    locale, makemaker, rendered, found, directory, pod_usage, exported = result.stdout.splitlines()
    assert Path(pod_usage).resolve() == (shim_root / "Pod" / "Usage.pm").resolve()
    assert exported == "pod2usage"
    assert Path(locale).resolve() == (shim_root / "Locale" / "Maketext" / "Simple.pm").resolve()
    assert Path(makemaker).resolve() == (shim_root / "ExtUtils" / "MakeMaker.pm").resolve()
    assert rendered == "one of two"
    assert found == str(tool)
    assert directory == "undef"
    for name in _syslib._PERL_SHIMS:
        assert _syslib._perl_has_module("perl", name, env)


@pytest.mark.parametrize(("shape", "multiarch"), [("linux-x64-musl", "x86_64-linux-gnu"), ("linux-arm64-musl", "aarch64-linux-gnu")])
def test_musl_kernel_header_dir_exposes_only_kernel_headers(tmp_path: Path, shape: str, multiarch: str) -> None:
    # Forge run 34921049216: musl-gcc could not find <linux/mman.h>.
    system = tmp_path / "usr-include"
    for sub in ("linux", f"{multiarch}/asm", "asm-generic", "sys"):
        (system / sub).mkdir(parents=True)
    header_dir = _syslib._musl_kernel_header_dir(shape, tmp_path / "build", system)
    assert sorted(p.name for p in header_dir.iterdir()) == ["asm", "asm-generic", "linux"]
    assert (header_dir / "asm").resolve() == (system / multiarch / "asm").resolve()
    # Idempotent across a rebuild in the same folder.
    assert _syslib._musl_kernel_header_dir(shape, tmp_path / "build", system) == header_dir
    args = _syslib._openssl_configure_args(shape, "/b/package", include_dirs=(str(header_dir),))
    assert args[args.index("no-async") + 1] == f"-I{header_dir}"
    with pytest.raises(RuntimeError, match="kernel headers missing"):
        _syslib._musl_kernel_header_dir(shape, tmp_path / "other", tmp_path / "nothing")


def test_vcvars_arch_mapping() -> None:
    assert _syslib._vcvars_arch("windows-x64", "AMD64") == "x64"
    assert _syslib._vcvars_arch("windows-arm64", "ARM64") == "arm64"
    assert _syslib._vcvars_arch("windows-arm64", "AMD64") == "x64_arm64"
    assert _syslib._vcvars_arch("windows-x64", "ARM64") == "arm64_amd64"
    with pytest.raises(ValueError):
        _syslib._vcvars_arch("windows-x64-gnu", "AMD64")


def test_vcvars_command_and_env_parsing() -> None:
    vcvarsall = r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvarsall.bat"
    assert _syslib._vcvars_command(vcvarsall, "arm64") == (
        f'cmd.exe /d /s /c ""{vcvarsall}" arm64 >nul && set"'
    )
    parsed = _syslib._parse_set_output(
        "=C:=C:\\work\r\nINCLUDE=C:\\VC\\include;C:\\sdk\r\nPath=C:\\VC\\bin;C:\\Windows\r\nnoise\r\n"
    )
    assert parsed == {"INCLUDE": "C:\\VC\\include;C:\\sdk", "Path": "C:\\VC\\bin;C:\\Windows"}
    assert _syslib._vswhere_path({"ProgramFiles(x86)": r"C:\PF86"}) == (
        r"C:\PF86\Microsoft Visual Studio\Installer\vswhere.exe"
    )


def test_relocate_openssl_pkg_config() -> None:
    installed = """prefix=/home/runner/build/package
exec_prefix=${prefix}
libdir=/home/runner/build/package/lib
includedir=/home/runner/build/package/include
enginesdir=${libdir}/engines-3
modulesdir=${libdir}/ossl-modules

Name: OpenSSL-libcrypto
Description: OpenSSL cryptography library
Version: 3.5.8
Libs: -L${libdir} -lcrypto
Libs.private: -ldl -pthread -L/home/runner/build/package/lib
Cflags: -I${includedir}
"""
    out = _syslib._relocate_pkg_config_text(installed, "/home/runner/build/package")
    assert out.splitlines()[:6] == [
        "prefix=${pcfiledir}/../..",
        "exec_prefix=${prefix}",
        "libdir=${exec_prefix}/lib",
        "includedir=${prefix}/include",
        "enginesdir=${libdir}/engines-3",
        "modulesdir=${libdir}/ossl-modules",
    ]
    assert "/home/runner" not in out
    assert "Libs.private: -ldl -pthread -L${prefix}/lib" in out
    assert "Libs: -L${libdir} -lcrypto" in out


def test_relocate_pkg_config_handles_windows_prefix() -> None:
    installed = "prefix=D:/a/build/package\nlibdir=${exec_prefix}/lib\nLibs: -LD:/a/build/package/lib -lssl\n"
    out = _syslib._relocate_pkg_config_text(installed, r"D:\a\build\package")
    assert out == "prefix=${pcfiledir}/../..\nlibdir=${exec_prefix}/lib\nLibs: -L${prefix}/lib -lssl\n"


def _openssl_package(tmp_path: Path, shape: str) -> Path:
    package = tmp_path / shape
    (package / "include" / "openssl").mkdir(parents=True)
    for header in ("ssl.h", "crypto.h", "opensslv.h"):
        (package / "include" / "openssl" / header).write_text("/* */", encoding="utf-8")
    (package / "lib").mkdir()
    for name in _syslib._openssl_static_lib_names(shape):
        (package / "lib" / name).write_bytes(b"!<arch>\n")
    lib = _syslib.LIBRARIES["openssl"]
    if shape in {"windows-x64", "windows-arm64"}:
        _syslib._write_openssl_pkg_config(package, shape, lib, None)
    else:
        pc_dir = package / "lib" / "pkgconfig"
        pc_dir.mkdir()
        for name in _syslib.OPENSSL_PKG_CONFIG_NAMES:
            (pc_dir / f"{name}.pc").write_text(f"prefix={package}\nlibdir={package}/lib\n", encoding="utf-8")
        _syslib._write_openssl_pkg_config(package, shape, lib, str(package))
    return package


def test_openssl_static_lib_names() -> None:
    for shape in _syslib.SHAPES:
        expected = ("libssl.lib", "libcrypto.lib") if shape in {"windows-x64", "windows-arm64"} else ("libssl.a", "libcrypto.a")
        assert _syslib._openssl_static_lib_names(shape) == expected, shape


@pytest.mark.parametrize("shape", sorted(_syslib.SHAPES))
def test_openssl_sanity_check_accepts_static_package(tmp_path: Path, shape: str) -> None:
    package = _openssl_package(tmp_path, shape)
    _syslib._sanity_check_package(package, _syslib.LIBRARIES["openssl"], shape)
    for name in _syslib.OPENSSL_PKG_CONFIG_NAMES:
        text = (package / "lib" / "pkgconfig" / f"{name}.pc").read_text(encoding="utf-8")
        assert text.startswith("prefix=${pcfiledir}/../..\n")
        assert str(package) not in text


def test_openssl_msvc_pkg_config_links_lib_prefixed_names(tmp_path: Path) -> None:
    package = _openssl_package(tmp_path, "windows-x64")
    pc_dir = package / "lib" / "pkgconfig"
    assert "Libs: -L${libdir} -llibcrypto\n" in (pc_dir / "libcrypto.pc").read_text(encoding="utf-8")
    assert "Libs: -L${libdir} -llibssl\n" in (pc_dir / "libssl.pc").read_text(encoding="utf-8")
    assert "Requires: libssl libcrypto\n" in (pc_dir / "openssl.pc").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "shared",
    ["bin/libcrypto-3-x64.dll", "lib/libssl.dll.a", "lib/libcrypto.dylib", "lib/libssl.so", "lib/libcrypto.so.3"],
)
def test_openssl_sanity_check_rejects_shared_libraries(tmp_path: Path, shared: str) -> None:
    shape = "windows-x64" if "dll" in shared else "linux-x64-gnu"
    package = _openssl_package(tmp_path, shape)
    (package / shared).parent.mkdir(parents=True, exist_ok=True)
    (package / shared).write_bytes(b"MZ")
    with pytest.raises(RuntimeError, match="static-only"):
        _syslib._sanity_check_package(package, _syslib.LIBRARIES["openssl"], shape)


@pytest.mark.parametrize("missing", ["libssl.a", "libcrypto.a"])
def test_openssl_sanity_check_requires_both_libraries(tmp_path: Path, missing: str) -> None:
    package = _openssl_package(tmp_path, "linux-arm64-musl")
    (package / "lib" / missing).unlink()
    with pytest.raises(RuntimeError, match=f"lib/{missing}"):
        _syslib._sanity_check_package(package, _syslib.LIBRARIES["openssl"], "linux-arm64-musl")


def test_openssl_sanity_check_rejects_msvc_package_with_unix_names(tmp_path: Path) -> None:
    package = _openssl_package(tmp_path, "linux-x64-gnu")
    with pytest.raises(RuntimeError, match="libssl.lib"):
        _syslib._sanity_check_package(package, _syslib.LIBRARIES["openssl"], "windows-arm64")


def test_openssl_sanity_check_rejects_absolute_pkg_config(tmp_path: Path) -> None:
    package = _openssl_package(tmp_path, "darwin-arm64")
    (package / "lib" / "pkgconfig" / "libssl.pc").write_text("prefix=/opt/build\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not relocatable"):
        _syslib._sanity_check_package(package, _syslib.LIBRARIES["openssl"], "darwin-arm64")


def test_prune_openssl_install(tmp_path: Path) -> None:
    package = tmp_path / "package"
    for rel in ("bin", "ssl/misc", "lib/engines-3", "lib/ossl-modules", "lib/cmake/OpenSSL"):
        (package / rel).mkdir(parents=True)
    (package / "bin" / "c_rehash").write_text("#!", encoding="utf-8")
    _syslib._prune_openssl_install(package)
    assert sorted(p.relative_to(package).as_posix() for p in package.rglob("*")) == [
        "lib",
        "lib/cmake",
        "lib/cmake/OpenSSL",
    ]


def test_generator_emits_all_nine_openssl_recipes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generate_syslib_recipes, "REPO", tmp_path)
    assert generate_syslib_recipes.main() == 0
    generated = sorted(p.name for p in (tmp_path / "recipes").glob("openssl-*"))
    assert generated == sorted(f"openssl-{shape}" for shape in _syslib.SHAPES)
    for shape, spec in _syslib.SHAPES.items():
        recipe = tmp_path / "recipes" / f"openssl-{shape}"
        conanfile = (recipe / "conanfile.py").read_text(encoding="utf-8")
        readme = (recipe / "README.md").read_text(encoding="utf-8")
        assert f'name = "openssl-{shape}"' in conanfile
        assert 'license = "Apache-2.0"' in conanfile
        assert "-f version=3.5.8" in readme
        assert f"`openssl/3.5.8/{shape}/bundle.tar.zst`" in readme
        assert f"-f {spec.forge_input}=true" in readme
        assert "firedaemon" not in (conanfile + readme).lower()
        # The checked-in wrapper matches the generator output.
        checked_in = Path(__file__).resolve().parents[1] / "recipes" / f"openssl-{shape}"
        assert (checked_in / "conanfile.py").read_text(encoding="utf-8") == conanfile
        assert (checked_in / "README.md").read_text(encoding="utf-8") == readme
