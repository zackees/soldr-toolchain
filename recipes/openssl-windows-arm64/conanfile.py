"""Conan recipe — OpenSSL libs/headers for Windows MSVC aarch64.

soldr#943 (arm64 half). FireDaemon mirror's `arm64/` subdir. See
sister recipe `openssl-windows-x64/` for source / license / layout
discussion — this recipe just swaps the per-arch subdir.

Dispatch:

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_ref=main \\
        -f recipe_path=recipes/openssl-windows-arm64 \\
        -f name=openssl-windows-arm64 \\
        -f version=3.5.0 \\
        -f linux_x64=true \\
        -f windows_x64=false -f macos_arm64=false
"""

from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from pathlib import Path

from conan import ConanFile
from conan.tools.files import copy


TARGET_TRIPLE = "aarch64-pc-windows-msvc"
ARCH_DIR = "arm64"
# Inlined helper — sister recipe `openssl-windows-x64` has the same
# code path. Inlining keeps the recipe self-contained for conan's
# export cache (sibling-helper imports fail there).
FIREDAEMON_URL_TPL = (
    "https://download.firedaemon.com/FireDaemon-OpenSSL/openssl-{version}.zip"
)


def _extract_arch(*, version, arch_dir, target_triple, build_folder, output):
    url = FIREDAEMON_URL_TPL.format(version=version)
    output.info(f"fetching {url}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "curl/8.5.0"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    output.info(f"downloaded {len(data)} bytes; extracting {arch_dir}/")

    out_root = build_folder / "package"
    out_root.mkdir(parents=True, exist_ok=True)
    prefix = f"{arch_dir}/"
    keep_subdirs = ("bin/", "lib/", "include/")
    extracted = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not any(rel.startswith(p) for p in keep_subdirs):
                continue
            if info.is_dir():
                (out_root / rel).mkdir(parents=True, exist_ok=True)
                continue
            target = out_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src:
                target.write_bytes(src.read())
            extracted += 1
    if extracted == 0:
        raise RuntimeError(
            f"no files extracted for arch_dir={arch_dir!r}; "
            f"FireDaemon zip layout may have changed."
        )
    output.info(f"extracted {extracted} files into package/")
    (build_folder / "meta.json").write_text(
        json.dumps(
            {
                "openssl_version": version,
                "target_triple": target_triple,
                "source_url": url,
                "arch_dir": arch_dir,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class OpensslWindowsArm64(ConanFile):
    name = "openssl-windows-arm64"
    description = (
        "OpenSSL libs + headers for Windows MSVC aarch64 cross-compile. "
        "Source: FireDaemon's openssl-<ver>.zip mirror of upstream OpenSSL."
    )
    license = "Apache-2.0"
    package_type = "header-library"
    no_copy_source = True
    settings = "os", "arch"

    def build(self):
        _extract_arch(
            version=str(self.version),
            arch_dir=ARCH_DIR,
            target_triple=TARGET_TRIPLE,
            build_folder=Path(self.build_folder),
            output=self.output,
        )

    def package(self):
        copy(
            self,
            "*",
            src=Path(self.build_folder, "package").as_posix(),
            dst=self.package_folder,
        )
        copy(
            self,
            "meta.json",
            src=self.build_folder,
            dst=self.package_folder,
        )
