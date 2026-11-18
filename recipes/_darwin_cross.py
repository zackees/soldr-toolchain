"""Linux-only producers for Darwin cross targets. No target code is executed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import struct
import tarfile
import urllib.request
from pathlib import Path

LLVM_VERSION = "21.1.5"
LLVM_SHA256 = "1794be4bf974e99a3fe1da4b2b9b1456c02ae9479c942f365441d8d207bd650c"
SWIFT_VERSION = "6.3.2"
SWIFT_SHA256 = "648daccc9062045cb42431f6c7d620858b729abccb6bb9075a6b990e6259e897"
SWIFT_KEY = "52BB7E3DE28A71BE22EC05FFEF80A866B47A981F"
SWIFT_URL = (
    f"https://download.swift.org/swift-{SWIFT_VERSION}-release/amazonlinux2/"
    f"swift-{SWIFT_VERSION}-RELEASE/swift-{SWIFT_VERSION}-RELEASE-amazonlinux2.tar.gz"
)
LLVM_URL = (
    f"https://github.com/llvm/llvm-project/releases/download/llvmorg-{LLVM_VERSION}/"
    f"llvm-project-{LLVM_VERSION}.src.tar.xz"
)


def download(url: str, path: Path, sha256: str | None = None) -> None:
    if sha256 is not None and path.is_file() and digest(path) == sha256:
        return
    with urllib.request.urlopen(url, timeout=600) as response, path.open("wb") as out:
        shutil.copyfileobj(response, out)
    if sha256 is not None and digest(path) != sha256:
        raise RuntimeError(f"SHA256 mismatch: {url}")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract(archive: Path, dest: Path) -> None:
    # Conan uses modern Python on Forge; reject unsafe links and traversal before
    # materializing anything. data_filter is available on Python 3.12+.
    if not hasattr(tarfile, "data_filter"):
        raise RuntimeError("safe archive extraction requires Python 3.12 or newer")
    with tarfile.open(archive) as tf:
        tf.extractall(dest, filter="data")


def ensure_darwin_tools(root: Path) -> None:
    tools = root / "bin"
    if not (tools / "ld64.lld").is_file():
        raise RuntimeError("LLVM bundle is missing ld64.lld")
    plain, prefixed = tools / "dsymutil", tools / "llvm-dsymutil"
    if not plain.is_file() and prefixed.is_file():
        plain.symlink_to("llvm-dsymutil")
    if not prefixed.is_file() and plain.is_file():
        prefixed.symlink_to("dsymutil")
    if not plain.is_file():
        raise RuntimeError("LLVM bundle is missing dsymutil/llvm-dsymutil")


def glibc_versions(text: str) -> set[tuple[int, ...]]:
    return {tuple(map(int, v.split("."))) for v in re.findall(r"GLIBC_([0-9.]+)", text)}


def require_glibc_floor(name: str, versions: set[tuple[int, ...]]) -> None:
    if versions and max(versions) > (2, 17):
        raise RuntimeError(f"{name}: GLIBC {max(versions)} exceeds catalogue floor 2.17; rebuild from source")


def verify_elf_tree(root: Path) -> dict:
    report = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as src:
            if src.read(4) != b"\x7fELF":
                continue
        info = subprocess.check_output(["readelf", "--version-info", str(path)], text=True)
        versions = glibc_versions(info)
        require_glibc_floor(str(path), versions)
        report[str(path.relative_to(root))] = max(versions) if versions else None
    if not report:
        raise RuntimeError("Swift archive contains no ELF tools")
    return report


def verify_swift_signature(archive: Path, signature: Path, keys: Path, home: Path) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(["gpg", "--homedir", str(home), "--batch", "--import", str(keys)], check=True)
    status = subprocess.check_output([
        "gpg", "--homedir", str(home), "--batch", "--status-fd", "1",
        "--verify", str(signature), str(archive),
    ], text=True)
    valid = [line.split() for line in status.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    if len(valid) != 1 or SWIFT_KEY not in (valid[0][2], valid[0][-1]):
        raise RuntimeError("Swift archive was not signed by the pinned Swift 6 release key")
    if any(f"[GNUPG:] {bad}" in status for bad in ("BADSIG", "EXPKEYSIG", "EXPSIG", "REVKEYSIG")):
        raise RuntimeError("Swift release signature is invalid or expired")


def build_swift(work: Path) -> None:
    archive, signature, keys = work / "swift.tar.gz", work / "swift.sig", work / "swift-keys.asc"
    download(SWIFT_URL, archive, SWIFT_SHA256)
    download(SWIFT_URL + ".sig", signature)
    # Swift.org may serve this response gzip-encoded even without Accept-Encoding.
    download("https://www.swift.org/keys/all-keys.asc", keys)
    if keys.read_bytes()[:2] == b"\x1f\x8b":
        import gzip
        keys.write_bytes(gzip.decompress(keys.read_bytes()))
    verify_swift_signature(archive, signature, keys, work / "gnupg")
    extract(archive, work / "unpacked")
    source = work / "unpacked" / f"swift-{SWIFT_VERSION}-RELEASE-amazonlinux2" / "usr"
    # Preserve the upstream bin/../lib layout, host plugins, driver, licenses and
    # Clang importer resources. Target resources are a separate SDK input.
    shutil.copytree(source, work / "package", symlinks=True)
    report = verify_elf_tree(work / "package")
    (work / "meta.json").write_text(json.dumps({
        "swift_version": SWIFT_VERSION, "source_url": SWIFT_URL,
        "source_sha256": SWIFT_SHA256, "signer": SWIFT_KEY,
        "elf_glibc_versions": report,
    }, indent=2) + "\n")


def compiler_rt_command(source: Path, build: Path, llvm: Path, sdk: Path, sdk_version: str) -> list[str]:
    flags = {
        "CMAKE_SYSTEM_NAME": "Darwin", "CMAKE_BUILD_TYPE": "Release",
        "CMAKE_C_COMPILER": build.parent / "target-clang", "CMAKE_CXX_COMPILER": build.parent / "target-clang++",
        "CMAKE_C_COMPILER_TARGET": "arm64-apple-darwin", "CMAKE_CXX_COMPILER_TARGET": "arm64-apple-darwin",
        "CMAKE_OSX_ARCHITECTURES": "arm64", "CMAKE_OSX_SYSROOT": sdk,
        "CMAKE_OSX_DEPLOYMENT_TARGET": "11.0", "CMAKE_C_FLAGS": "-fuse-ld=lld",
        "CMAKE_CXX_FLAGS": "-fuse-ld=lld", "CMAKE_LINKER": llvm / "bin/ld64.lld",
        "CMAKE_AR": llvm / "bin/llvm-ar", "CMAKE_RANLIB": llvm / "bin/llvm-ranlib",
        "CMAKE_LIBTOOL": build.parent / "host-tools/bin/llvm-libtool-darwin",
        "CMAKE_LIPO": build.parent / "host-tools/bin/llvm-lipo",
        "DARWIN_macosx_CACHED_SYSROOT": sdk, "DARWIN_osx_ARCHS": "arm64",
        "DARWIN_osx_BUILTIN_ARCHS": "arm64", "DARWIN_macosx_OVERRIDE_SDK_VERSION": sdk_version,
        "COMPILER_RT_INCLUDE_TESTS": "OFF", "COMPILER_RT_BUILD_XRAY": "OFF",
        "COMPILER_RT_BUILD_LIBFUZZER": "OFF", "COMPILER_RT_BUILD_PROFILE": "OFF",
        "COMPILER_RT_BUILD_ORC": "OFF", "COMPILER_RT_ENABLE_IOS": "OFF",
        "COMPILER_RT_ENABLE_WATCHOS": "OFF", "COMPILER_RT_ENABLE_TVOS": "OFF",
        "COMPILER_RT_ENABLE_VISIONOS": "OFF", "COMPILER_RT_ENABLE_MACCATALYST": "OFF",
    }
    return ["cmake", "-G", "Ninja", "-S", str(source / "compiler-rt"), "-B", str(build)] + [
        f"-D{key}={value}" for key, value in flags.items()
    ]


def build_compiler_rt(work: Path) -> None:
    llvm_value, sdk_value = os.environ.get("SOLDR_LLVM_ROOT"), os.environ.get("SOLDR_APPLE_SDK_ROOT")
    if bool(llvm_value) != bool(sdk_value):
        raise RuntimeError("SOLDR_LLVM_ROOT and SOLDR_APPLE_SDK_ROOT must be supplied together")
    if llvm_value and sdk_value:
        llvm, sdk = Path(llvm_value).resolve(), Path(sdk_value).resolve()
    else:
        provisioner = Path(__file__).with_name("_provision_darwin.py")
        args = [sys.executable, str(provisioner), "--output", str(work / "inputs")]
        if os.environ.get("SOLDR_TOOLCHAIN_CATALOGUE_URL"):
            args += ["--catalogue-url", os.environ["SOLDR_TOOLCHAIN_CATALOGUE_URL"]]
        inputs = json.loads(subprocess.check_output(args, text=True))
        llvm, sdk = Path(inputs["llvm_root"]), Path(inputs["sdk_root"])
    version = subprocess.check_output([str(llvm / "bin/clang"), "--version"], text=True)
    if not re.search(r"clang version 21\.1\.5\b", version):
        raise RuntimeError("compiler-rt must match managed LLVM 21.1.5 exactly")
    sdk_version = json.loads((sdk / "SDKSettings.json").read_text())["Version"]
    archive = work / "llvm-project.tar.xz"
    download(LLVM_URL, archive, LLVM_SHA256)
    extract(archive, work / "source")
    source = work / "source" / f"llvm-project-{LLVM_VERSION}.src"
    host_build = work / "host-tools"
    subprocess.run([
        "cmake", "-G", "Ninja", "-S", str(source / "llvm"), "-B", str(host_build),
        "-DCMAKE_BUILD_TYPE=Release", "-DLLVM_TARGETS_TO_BUILD=",
        "-DLLVM_INCLUDE_TESTS=OFF", "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_ENABLE_LIBXML2=OFF", "-DLLVM_ENABLE_ZSTD=OFF", "-DLLVM_ENABLE_ZLIB=OFF",
    ], check=True)
    subprocess.run(["cmake", "--build", str(host_build), "--target", "llvm-libtool-darwin", "llvm-lipo",
                    "--parallel", str(os.cpu_count() or 2)], check=True)
    # compiler-rt has custom assembly rules that ignore CMAKE_*_COMPILER_TARGET.
    # Its lower-case .s files also require preprocessing on Linux clang.
    for name in ("clang", "clang++"):
        driver = work / ("target-" + name)
        compiler = shlex.quote(str(llvm / "bin" / name))
        driver.write_text(
            '#!/bin/sh\nfor arg in "$@"; do\n'
            ' case "$arg" in *.s) exec ' + compiler +
            ' --target=arm64-apple-macos11 -x assembler-with-cpp "$@";; esac\ndone\n'
            'exec ' + compiler + ' --target=arm64-apple-macos11 "$@"\n'
        )
        driver.chmod(0o755)
    build = work / "rt-build"
    subprocess.run(compiler_rt_command(source, build, llvm, sdk, sdk_version), check=True)
    subprocess.run(["cmake", "--build", str(build), "--parallel", str(os.cpu_count() or 2)], check=True)
    package_compiler_rt(work, build, llvm, source, sdk_version)


def package_compiler_rt(work: Path, build: Path, llvm: Path, source: Path, sdk_version: str) -> None:
    dest = work / "package/lib/clang/21/lib/darwin"
    dest.mkdir(parents=True)
    for path in (build / "lib/darwin").glob("libclang_rt.*osx*"):
        shutil.copy2(path, dest / path.name)
    required = ("libclang_rt.osx.a", "libclang_rt.asan_osx_dynamic.dylib", "libclang_rt.ubsan_osx_dynamic.dylib")
    for name in required:
        if not (dest / name).is_file():
            raise RuntimeError(f"compiler-rt build did not produce {name}")
    for path in dest.iterdir():
        info = subprocess.check_output([str(llvm / "bin/llvm-readobj"), "--file-headers", str(path)], text=True)
        if "Arch: aarch64" not in info or re.search(r"Arch: (?!aarch64)", info):
            raise RuntimeError(f"unexpected compiler-rt architecture: {path}")
    signatures = {path.name: verify_macho_signature(path) for path in dest.glob("*.dylib")}
    verify_builtins(dest / "libclang_rt.osx.a", llvm / "bin/llvm-nm")
    shutil.copytree(source / "compiler-rt/include/sanitizer", work / "package/lib/clang/21/include/sanitizer")
    shutil.copy2(source / "compiler-rt/LICENSE.TXT", work / "package/LICENSE.TXT")
    (work / "meta.json").write_text(json.dumps({
        "llvm_version": LLVM_VERSION, "source_url": LLVM_URL, "source_sha256": LLVM_SHA256,
        "sdk_version": sdk_version, "target": "aarch64-apple-darwin",
        "dylib_signatures": signatures, "availability_builtin_defined": True,
        "managed_inputs": json.loads((work / "inputs/provenance.json").read_text()) if (work / "inputs/provenance.json").is_file() else None,
    }, indent=2) + "\n")


def verify_builtins(archive: Path, nm: Path) -> None:
    symbols = subprocess.check_output([str(nm), "--defined-only", "--extern-only", str(archive)], text=True)
    if not re.search(r"\b___isPlatformVersionAtLeast$", symbols, re.MULTILINE):
        raise RuntimeError("Darwin builtins lack a definition of __isPlatformVersionAtLeast")


def verify_macho_signature(path: Path) -> dict:
    """Verify every SHA256 CodeDirectory page of our thin arm64 ad-hoc images.

    Linker signatures have no CMS certificate. This verifies integrity, not a
    Developer ID identity or Gatekeeper approval. Reject formats we don't emit.
    """
    data = path.read_bytes()

    def require(ok, message):
        if not ok:
            raise RuntimeError(f"{path}: {message}")

    require(len(data) >= 32 and data[:4] == b"\xcf\xfa\xed\xfe", "expected thin Mach-O 64")
    require(struct.unpack_from("<I", data, 4)[0] == 0x100000C, "expected arm64")
    count, size = struct.unpack_from("<II", data, 16)
    require(32 + size <= len(data) and count <= size // 8, "invalid load commands")
    cursor, signature = 32, None
    for _ in range(count):
        require(cursor + 8 <= 32 + size, "truncated load command")
        command, length = struct.unpack_from("<II", data, cursor)
        require(length >= 8 and cursor + length <= 32 + size, "invalid load command size")
        if command == 0x1D:
            require(length == 16 and signature is None, "invalid LC_CODE_SIGNATURE")
            signature = struct.unpack_from("<II", data, cursor + 8)
        cursor += length
    require(signature is not None, "missing code signature")
    offset, length = signature
    require(offset >= 32 + size and offset + length <= len(data) and length >= 20, "signature outside image")
    blob = data[offset:offset + length]
    magic, total, entries = struct.unpack_from(">III", blob)
    require(magic == 0xFADE0CC0 and total <= length and 12 + entries * 8 <= total, "invalid signature superblob")
    require(entries == 1, "expected only a primary CodeDirectory")
    directories = []
    for i in range(entries):
        kind, start = struct.unpack_from(">II", blob, 12 + 8 * i)
        require(12 + entries * 8 <= start <= total - 8, "invalid signature slot")
        if kind == 0:
            directories.append(start)
    require(len(directories) == 1, "expected one primary CodeDirectory")
    start = directories[0]
    cd_magic, cd_length = struct.unpack_from(">II", blob, start)
    require(cd_magic == 0xFADE0C02 and cd_length >= 44 and start + cd_length <= total, "invalid CodeDirectory")
    cd = blob[start:start + cd_length]
    version, flags, hashes, identifier, special, slots, limit = struct.unpack_from(">7I", cd, 8)
    hash_size, hash_type, _, page_shift = struct.unpack_from(">4B", cd, 36)
    require(0x20000 <= version <= 0x20400 and flags & 2, "expected ad-hoc CodeDirectory")
    header_size = next(size for minimum, size in (
        (0x20400, 88), (0x20300, 64), (0x20200, 52), (0x20100, 48), (0x20000, 44)
    ) if version >= minimum)
    require(cd_length >= header_size, "truncated versioned CodeDirectory header")
    if version >= 0x20100:
        require(struct.unpack_from(">I", cd, 44)[0] == 0, "unsupported scatter signature")
    if version >= 0x20200:
        require(struct.unpack_from(">I", cd, 48)[0] == 0, "unexpected ad-hoc team identifier")
    if version >= 0x20300:
        require(struct.unpack_from(">Q", cd, 56)[0] in (0, limit), "unsupported 64-bit signed extent")
    require(hash_size == 32 and hash_type == 2 and 0 < page_shift <= 20, "unsupported signature hash")
    page_size = 1 << page_shift
    require(limit == offset and slots == (limit + page_size - 1) // page_size, "invalid signed extent")
    require(special == 0 and header_size <= identifier < hashes and hashes + slots * 32 <= cd_length, "invalid signature hash table")
    for i in range(slots):
        actual = hashlib.sha256(data[i * page_size:min((i + 1) * page_size, limit)]).digest()
        require(actual == cd[hashes + i * 32:hashes + (i + 1) * 32], f"signature page {i} mismatch")
    return {"arch": "arm64", "signature": "ad-hoc-sha256", "pages": slots, "sha256": digest(path)}


def llvm_tools_command(source: Path, build: Path) -> list[str]:
    flags = {
        "CMAKE_BUILD_TYPE":"Release", "LLVM_ENABLE_PROJECTS":"clang;lld",
        "LLVM_TARGETS_TO_BUILD":"X86;AArch64", "LLVM_INCLUDE_TESTS":"OFF",
        "LLVM_ENABLE_RUNTIMES":"compiler-rt", "LLVM_ENABLE_PER_TARGET_RUNTIME_DIR":"OFF",
        "COMPILER_RT_DEFAULT_TARGET_ONLY":"ON", "COMPILER_RT_INCLUDE_TESTS":"OFF",
        "LLVM_INCLUDE_BENCHMARKS":"OFF", "LLVM_INCLUDE_EXAMPLES":"OFF",
        "LLVM_BUILD_LLVM_DYLIB":"ON", "LLVM_LINK_LLVM_DYLIB":"ON",
        "CLANG_LINK_CLANG_DYLIB":"ON", "LLVM_ENABLE_LIBXML2":"OFF",
        "LLVM_ENABLE_ZSTD":"OFF", "LLVM_ENABLE_LIBEDIT":"OFF",
        "CMAKE_BUILD_WITH_INSTALL_RPATH":"ON", "CMAKE_INSTALL_RPATH":"$ORIGIN/../lib",
        "CMAKE_EXE_LINKER_FLAGS":"-static-libstdc++ -static-libgcc",
        "CMAKE_SHARED_LINKER_FLAGS":"-static-libstdc++ -static-libgcc",
    }
    return ["cmake","-G","Ninja","-S",str(source/"llvm"),"-B",str(build)] + [
        f"-D{key}={value}" for key,value in flags.items()
    ]


def build_llvm_tools(work: Path) -> None:
    archive = work / "llvm-project.tar.xz"
    download(LLVM_URL, archive, LLVM_SHA256)
    extract(archive, work / "source")
    source = work / "source" / f"llvm-project-{LLVM_VERSION}.src"
    build = work / "llvm-build"
    subprocess.run(llvm_tools_command(source,build),check=True)
    tools = ["clang","lld","llvm-ar","llvm-rc","llvm-objcopy","llvm-readobj","dsymutil","clang-resource-headers","runtimes"]
    subprocess.run(["cmake","--build",str(build),"--target",*tools,"--parallel",str(os.cpu_count() or 2)],check=True)
    package = work / "package"
    (package/"bin").mkdir(parents=True)
    (package/"lib").mkdir()
    for name in tools[:-2]:
        shutil.copy2(build/"bin"/name,package/"bin"/name)
    for path in (build/"lib").iterdir():
        if path.is_file() and path.name.startswith(("libLLVM","libclang-cpp")) and '.so' in path.name:
            destination = package / "lib" / path.name
            if path.is_symlink():
                destination.symlink_to(path.readlink())
            else:
                shutil.copy2(path, destination)
    aliases = {"clang++":"clang","clang-cl":"clang","clang-cpp":"clang",
               "ld.lld":"lld","ld64.lld":"lld","lld-link":"lld",
               "llvm-lib":"llvm-ar","llvm-dlltool":"llvm-ar","llvm-ranlib":"llvm-ar",
               "llvm-strip":"llvm-objcopy","llvm-dsymutil":"dsymutil"}
    for alias,target in aliases.items():
        (package/"bin"/alias).symlink_to(target)
    shutil.copytree(build/"lib/clang/21",package/"lib/clang/21")
    for name in ("libclang_rt.asan-x86_64.a", "libclang_rt.ubsan_standalone-x86_64.a"):
        if not (package / "lib/clang/21/lib/linux" / name).is_file():
            raise RuntimeError(f"missing native Linux runtime {name}")
    shutil.copy2(source/"llvm/LICENSE.TXT",package/"LLVM-LICENSE.TXT")
    ensure_darwin_tools(package)
    floors = verify_elf_tree(package)
    version = subprocess.check_output([str(package/"bin/clang"),"--version"],text=True)
    if not re.search(r"clang version 21\.1\.5\b",version):
        raise RuntimeError("packaged clang version mismatch")
    (work/"meta.json").write_text(json.dumps({"llvm_version":LLVM_VERSION,"source_url":LLVM_URL,
        "source_sha256":LLVM_SHA256,"elf_glibc_versions":floors},indent=2)+'\n')
