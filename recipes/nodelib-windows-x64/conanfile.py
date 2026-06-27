"""Conan recipe — Node.js node.lib import library for Windows x86_64.

soldr#944. Fetches the Node.js Windows headers + import lib from the
official Node.js release for a pinned version; ships just what
`node-bindgen` / `napi-rs` / native-addon crates need at link time.

Source: https://nodejs.org/dist/<version>/node-<version>-headers.tar.gz
        https://nodejs.org/dist/<version>/win-x64/node.lib

Dispatch:

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_ref=main \\
        -f recipe_path=recipes/nodelib-windows-x64 \\
        -f name=nodelib-windows-x64 \\
        -f version=22.10.0 \\
        -f linux_x64=true \\
        -f windows_x64=false -f macos_arm64=false
"""

from __future__ import annotations

import io
import json
import tarfile
import urllib.request
from pathlib import Path

from conan import ConanFile
from conan.tools.files import copy


ARCH_DIR = "win-x64"


class NodelibWindowsX64(ConanFile):
    name = "nodelib-windows-x64"
    description = (
        "Node.js headers + node.lib import library for Windows MSVC x86_64. "
        "Source: nodejs.org/dist."
    )
    license = "MIT"
    package_type = "header-library"
    no_copy_source = True
    settings = "os", "arch"

    def build(self):
        ver = str(self.version)
        out_root = Path(self.build_folder) / "package"
        out_root.mkdir(parents=True, exist_ok=True)

        # Headers — node-vXX.YY.ZZ-headers.tar.gz
        headers_name = f"node-v{ver}-headers.tar.gz"
        headers_url = f"https://nodejs.org/dist/v{ver}/{headers_name}"
        self.output.info(f"fetching {headers_url}")
        with urllib.request.urlopen(headers_url, timeout=180) as resp:
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf:
                rel = "/".join(member.name.split("/")[1:])  # strip top-level dir
                if not rel.startswith("include/"):
                    continue
                target = out_root / rel
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                buf = tf.extractfile(member)
                if buf is None:
                    continue
                target.write_bytes(buf.read())

        # Import lib — win-x64/node.lib
        node_lib_url = f"https://nodejs.org/dist/v{ver}/{ARCH_DIR}/node.lib"
        self.output.info(f"fetching {node_lib_url}")
        with urllib.request.urlopen(node_lib_url, timeout=120) as resp:
            node_lib = resp.read()
        (out_root / "lib").mkdir(parents=True, exist_ok=True)
        (out_root / "lib" / "node.lib").write_bytes(node_lib)

        meta = {
            "node_version": ver,
            "target_triple": "x86_64-pc-windows-msvc",
            "source_headers_url": headers_url,
            "source_node_lib_url": node_lib_url,
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
