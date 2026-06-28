"""Conan recipe - pre-compressed xwin cache for aarch64-pc-windows-msvc.

soldr#1012 PR 3. Produces the arm64 sibling of the existing
`xwin-cache/<date>/windows-x86_64-msvc/xwin-cache.tar.zst` row that
already ships in the catalogue.

The xwin tool (https://github.com/Jake-Shadle/xwin) downloads
Microsoft's freely-redistributable MSVC CRT + SDK headers and library
import stubs into a "splatted" directory layout that cargo-xwin (and
soldr's blessed cross-compile path in soldr#1012 PR 5) point at via
the `XWIN_CACHE_DIR` env var. The aarch64 variant ships the same
shape but with the arm64 import libs instead of x64.

Producing one of these from scratch takes ~3-5 minutes on a vanilla
runner (most of the time is xwin's per-package download). The
pre-compressed `.tar.zst` is ~85 MB (compressed) for x64; the
arm64 cache is similar size since it ships the same set of binaries
just for a different arch.

Source: github.com/Jake-Shadle/xwin (Apache-2.0 / MIT dual).
Output: package/<splatted xwin output tree>/

Dispatch:

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_ref=main \\
        -f recipe_path=recipes/xwin-cache-windows-arm64 \\
        -f name=xwin-cache-windows-arm64 \\
        -f version=$(date -u +%Y-%m-%d) \\
        -f linux_x64=true \\
        -f windows_x64=false -f macos_arm64=false
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

from conan import ConanFile
from conan.tools.files import copy


# Pinned xwin release. Bump when a newer xwin release ships and the
# recipe should pick it up. xwin's release cadence is slow (months
# between releases), so the pin rarely drifts.
XWIN_VERSION = "0.6.5"
XWIN_LINUX_X64_ASSET = f"xwin-{XWIN_VERSION}-x86_64-unknown-linux-musl.tar.gz"
XWIN_URL = (
    f"https://github.com/Jake-Shadle/xwin/releases/download/"
    f"{XWIN_VERSION}/{XWIN_LINUX_X64_ASSET}"
)


class XwinCacheWindowsArm64(ConanFile):
    name = "xwin-cache-windows-arm64"
    description = (
        "Pre-compressed xwin SDK cache for aarch64-pc-windows-msvc "
        "cross-compile. Sibling of the existing x86_64-msvc row in the "
        "soldr-toolchain catalogue. Source: github.com/Jake-Shadle/xwin."
    )
    license = "Apache-2.0 OR MIT"
    package_type = "application"
    no_copy_source = True
    settings = "os", "arch"

    def build(self):
        build_dir = Path(self.build_folder)

        # 1. Fetch xwin binary.
        self.output.info(f"fetching xwin {XWIN_VERSION} from {XWIN_URL}")
        with urllib.request.urlopen(XWIN_URL, timeout=180) as resp:
            tarball = resp.read()
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            tf.extractall(build_dir / "xwin-bin")
        xwin_bin = next(
            (build_dir / "xwin-bin").rglob("xwin"),
            None,
        )
        if xwin_bin is None:
            raise RuntimeError(
                "xwin binary not found in extracted release tarball"
            )
        xwin_bin.chmod(0o755)

        # 2. Run xwin splat for the arm64 arch.
        out_root = build_dir / "package"
        out_root.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(xwin_bin),
            "--accept-license",
            "--arch", "aarch64",
            "splat",
            "--output", str(out_root),
            # `preserve-ms-arch-notation` makes xwin keep the
            # original MS arch-name dirs (`x86`, `x64`, `arm`, `arm64`)
            # instead of normalizing to Rust-style names. cargo-xwin's
            # XWIN_CACHE_DIR consumer expects the MS shape.
            "--preserve-ms-arch-notation",
        ]
        self.output.info(f"running xwin splat for aarch64: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            self.output.error(
                f"xwin splat failed (exit {result.returncode}):\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
            raise RuntimeError(f"xwin splat exited {result.returncode}")
        self.output.info("xwin splat finished")

        # 3. Sanity check: the output dir must contain at least crt/ and
        # sdk/ subtrees (xwin's canonical layout). If not, the upstream
        # tool's output layout changed and the recipe needs updating.
        crt_dir = out_root / "crt"
        sdk_dir = out_root / "sdk"
        if not crt_dir.is_dir() or not sdk_dir.is_dir():
            raise RuntimeError(
                f"xwin splat produced unexpected layout. Found: "
                f"{sorted(p.name for p in out_root.iterdir())}; "
                f"expected `crt/` + `sdk/`."
            )

        meta = {
            "xwin_version": XWIN_VERSION,
            "target_triple": "aarch64-pc-windows-msvc",
            "source_url": XWIN_URL,
            "recipe": "xwin-cache-windows-arm64",
        }
        (build_dir / "meta.json").write_text(
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
