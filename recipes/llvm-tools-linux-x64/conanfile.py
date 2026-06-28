"""Conan recipe — LLVM toolchain bundle for Linux x64 cross-compile hosts.

soldr#934 + soldr#942. Bundles the LLVM-side binutils replacements that
cargo-xwin / lld-link / `rust-objcopy` rely on but that aren't covered
by xwin's MSVC CRT cache:

  bin/
  ├── clang             ← clang/clang++ driver
  ├── clang++
  ├── clang-cl          ← MSVC-mode driver (cargo-xwin cc-rs target)
  ├── lld-link          ← MSVC linker replacement (cargo-xwin linker target)
  ├── lld               ← unified lld
  ├── llvm-lib          ← MSVC `lib.exe` replacement (cc-rs static-lib path)
  ├── llvm-rc           ← `rc.exe` replacement (windows-sys resource path)
  ├── llvm-dlltool      ← `dlltool` replacement
  ├── llvm-strip        ← strip (rust-objcopy strip path; #934)
  └── llvm-objcopy      ← rust-objcopy underlying tool
  lib/
  └── libLLVM.so.<ver>   ← required by `rust-objcopy --strip-debug`
  include/llvm/...       ← optional; whitelisted for downstream cc-rs use

Source: official LLVM project release archives. The recipe pins a
specific LLVM version per the `LLVM_VERSION` constant and downloads
the matching prebuilt for the runner's host.

Dispatch (Linux x64 runner):

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_ref=main \\
        -f recipe_path=recipes/llvm-tools-linux-x64 \\
        -f name=llvm-tools-linux-x64 \\
        -f version=18.1.8 \\
        -f linux_x64=true \\
        -f windows_x64=false -f macos_arm64=false

The same recipe shape repeats per host (linux-arm64, macos-arm64,
windows-x64, …) when soldr's bootstrap supports those hosts as
cross-compile drivers. For now linux-x64 is the canonical host
per the docker-cross-all design.
"""

from __future__ import annotations

import io
import json
import lzma
import tarfile
import urllib.request
from pathlib import Path

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy


LLVM_VERSION_DEFAULT = "22.1.8"
# LLVM upstream renamed the linux archives in the 21.x cycle:
#   old (≤ 20.x): clang+llvm-<ver>-x86_64-linux-gnu-ubuntu-<distro>.tar.xz
#   new (≥ 21.x): LLVM-<ver>-Linux-X64.tar.xz
# Recipe targets the new format; pin a ≥21 release at dispatch time.
ASSET_NAME_TPL = "LLVM-{ver}-Linux-X64.tar.xz"


class LlvmToolsLinuxX64(ConanFile):
    name = "llvm-tools-linux-x64"
    description = (
        "LLVM toolchain bundle (clang/clang-cl/lld-link/llvm-lib/llvm-rc/"
        "llvm-dlltool/llvm-strip + libLLVM.so) for Linux x64 cross-compile "
        "drivers. Covers what cargo-xwin / rust-objcopy need beyond xwin's "
        "MSVC CRT cache."
    )
    license = "Apache-2.0 WITH LLVM-exception"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    WHITELIST_BIN = (
        "clang",
        "clang++",
        "clang-cl",
        "lld",
        "lld-link",
        "ld.lld",
        "llvm-lib",
        "llvm-rc",
        "llvm-dlltool",
        "llvm-strip",
        "llvm-objcopy",
        "llvm-ar",
        "llvm-readobj",
    )

    def build(self):
        ver = str(self.version)
        archive_name = ASSET_NAME_TPL.format(ver=ver)
        url = (
            f"https://github.com/llvm/llvm-project/releases/download/"
            f"llvmorg-{ver}/{archive_name}"
        )
        self.output.info(f"fetching {url}")
        # Stream the download → xz decompressor → tarfile reader. The
        # LLVM 22.x Linux X64 archive is ~1.9 GB compressed, ~6 GB
        # decompressed; holding either fully in memory OOM-killed the
        # forge runner. Stream-mode tarfile iterates once front-to-back
        # which is enough for our whitelist filter.
        out_root = Path(self.build_folder) / "package"
        out_root.mkdir(parents=True, exist_ok=True)
        self.output.info("streaming tar.xz → whitelist filter → package/")
        with urllib.request.urlopen(url, timeout=600) as resp:
            xz_reader = lzma.LZMAFile(resp, mode="rb")
            with tarfile.open(fileobj=xz_reader, mode="r|") as tf:
                for member in tf:
                    parts = member.name.split("/", 1)
                    if len(parts) < 2:
                        continue
                    rel = parts[1]
                    keep = False
                    if rel.startswith("bin/") and Path(rel).name in self.WHITELIST_BIN:
                        keep = True
                    elif rel.startswith("lib/") and (
                        Path(rel).name.startswith("libLLVM")
                        or Path(rel).name.startswith("libclang")
                        or Path(rel).name.startswith("liblldb")
                    ):
                        keep = True
                    elif rel.startswith("lib/clang/") and (
                        rel.endswith(".h") or "lib/" in rel
                    ):
                        # The `lib/clang/<ver>/include/` headers are needed
                        # by clang-cl when compiling C/C++ deps.
                        keep = True
                    if not keep:
                        continue
                    target = out_root / rel
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Streaming tar mode (`r|`) cannot extract symlinks
                    # via `extractfile()` — calling it raises
                    # `StreamError: cannot extract (sym)link as file
                    # object`. LLVM's archive uses lots of symlinks
                    # (e.g. clang++ → clang). Materialize symlinks
                    # directly via os.symlink instead of trying to read
                    # them as files. Hardlinks get the same treatment.
                    if member.issym() or member.islnk():
                        link_target = member.linkname
                        try:
                            if target.exists() or target.is_symlink():
                                target.unlink()
                            target.symlink_to(link_target)
                        except OSError:
                            pass
                        continue
                    buf = tf.extractfile(member)
                    if buf is None:
                        continue
                    target.write_bytes(buf.read())
                    # Preserve executable mode.
                    if member.mode & 0o111:
                        target.chmod(0o755)

        meta = {
            "llvm_version": ver,
            "asset_name": archive_name,
            "source_url": url,
        }
        (Path(self.build_folder) / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
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
