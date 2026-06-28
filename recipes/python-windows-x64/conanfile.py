"""Conan recipe — Windows x86_64 Python3 sysroot (python-build-standalone).

Closes the soldr#931 / soldr#997 Phase A blocker for PyO3
cross-compile to ``x86_64-pc-windows-msvc``. The recipe pulls a
specific Python version's pre-built Windows MSVC archive from
[astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone)
and extracts JUST the bits PyO3 + cargo-xwin need at link/compile
time:

  package/
  ├── lib/        ← python3.lib, python313.lib (the stable + versioned import libs)
  └── include/    ← Python.h + all CAPI headers

The recipe runs on any host runner (Linux is preferred for the
forge dispatch since the artifact build is identical on every
runner — pure download + repackage). No native Python execution
required.

Dispatch:

    gh workflow run forge-conan.yml --repo zackees/forge \\
        -f recipe_repo=zackees/soldr-toolchain \\
        -f recipe_ref=main \\
        -f recipe_path=recipes/python-windows-x64 \\
        -f name=python-windows-x64 \\
        -f version=3.13.0 \\
        -f linux_x64=true \\
        -f windows_x64=false -f macos_arm64=false

Output is consumed by:
* soldr's ``crates/soldr-cli/src/fetch/python_sysroot.rs`` (soldr#931
  Phase B) to populate ``~/.soldr/bin/python/<version>/<triple>/``.
* The cargo front door's PyO3 auto-detection (soldr#939) to set
  ``PYO3_CROSS_LIB_DIR`` + ``PYO3_CROSS_PYTHON_VERSION``.

## Versioning

``version`` is the Python release (e.g. ``3.13.0``); the recipe
internally maps to the matching python-build-standalone tag using
the standard ``<py_version>+<pbs_tag>-<triple>-install_only.tar.gz``
naming. The PBS tag is captured into ``package/meta.json`` so
ingest can record provenance.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import urllib.request
from pathlib import Path

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy


# Map Python version → most recent python-build-standalone release tag.
# Update when a new PBS release ships and we want to pin to it.
# https://github.com/astral-sh/python-build-standalone/releases
PBS_TAGS = {
    "3.13.0": "20241016",
    "3.12.7": "20241016",
    "3.11.10": "20241016",
    "3.10.15": "20241016",
}

TARGET_TRIPLE = "x86_64-pc-windows-msvc"


class PythonWindowsX64(ConanFile):
    name = "python-windows-x64"
    description = (
        "Pre-built Windows MSVC Python sysroot for PyO3 cross-compile. "
        "Source: astral-sh/python-build-standalone. Carries lib/python*.lib + "
        "include/* — no interpreter binary."
    )
    license = "PSF-2.0"
    package_type = "header-library"
    no_copy_source = True
    settings = "os", "arch"

    def validate(self):
        if str(self.version) not in PBS_TAGS:
            raise ConanInvalidConfiguration(
                f"unsupported python version {self.version}; supported: "
                f"{sorted(PBS_TAGS.keys())}"
            )

    def build(self):
        pbs_tag = PBS_TAGS[str(self.version)]
        archive_name = f"cpython-{self.version}+{pbs_tag}-{TARGET_TRIPLE}-install_only.tar.gz"
        url = (
            f"https://github.com/astral-sh/python-build-standalone/releases/"
            f"download/{pbs_tag}/{archive_name}"
        )
        self.output.info(f"fetching {url}")
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
        self.output.info(f"downloaded {len(data)} bytes; extracting sysroot subset")

        # PBS `install_only.tar.gz` archives use `python/` as the
        # top-level prefix (NOT `python/install/` — that was a
        # pre-2026-06-28 misreading; the `install/` subdir is only in
        # the `full` archive variants). We keep just:
        #   include/  → Python.h + CAPI headers
        #   libs/     → python3.lib + python313.lib (renamed to lib/
        #              for soldr-side ergonomics — consumer always
        #              looks under lib/)
        # Drops the interpreter binary + DLLs + Lib/ stdlib.
        out_root = Path(self.build_folder) / "package"
        out_root.mkdir(parents=True, exist_ok=True)
        extracted_count = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf:
                name = member.name
                prefix = "python/"
                if not name.startswith(prefix):
                    continue
                rel = name[len(prefix):]
                if rel.startswith("include/"):
                    dest_rel = rel
                elif rel.startswith("libs/"):
                    dest_rel = "lib/" + rel[len("libs/"):]
                else:
                    continue
                target = out_root / dest_rel
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                buf = tf.extractfile(member)
                if buf is None:
                    continue
                target.write_bytes(buf.read())
                extracted_count += 1
        self.output.info(f"extracted {extracted_count} files into package/")
        if extracted_count == 0:
            raise RuntimeError(
                "no files extracted from PBS Windows archive — tarball layout "
                "may have changed (expected `python/include/` + `python/libs/`)."
            )

        meta = {
            "python_version": str(self.version),
            "pbs_tag": pbs_tag,
            "target_triple": TARGET_TRIPLE,
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
