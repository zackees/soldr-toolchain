"""Conan recipe — Apple macOS SDK (universal2 fat shape).

soldr-toolchain#14: ships a versioned macOS SDK as a forge-built Conan
package. ``universal2`` shape carries every architecture slice the
runner's installed SDK contains (typically ``x86_64 + arm64``), so the
single artifact serves both ``x86_64-apple-darwin`` and
``aarch64-apple-darwin`` cross-build targets.

Sibling recipes ``apple-sdk-thin-x86_64`` and ``apple-sdk-thin-aarch64``
emit lipo-thinned per-arch variants of the SAME SDK content. All three
use byte-for-byte the same headers / .tbd / framework Resources;
the only difference is which Mach-O slices remain after ``lipo -thin``
and whether ``.tbd`` ``archs:`` lists are pruned.

## Full-SDK policy

This recipe (and its thin siblings) ship the ENTIRE ``MacOSX*.sdk``
directory tree verbatim. No trimming of headers, frameworks, or
rarely-used directories. A future ``apple-sdk-minimal`` recipe may
strip headers from frameworks soldr's cross-compile path never
touches — that's a follow-up issue, not part of #14.

## SDK version resolution

``sdk_version=auto`` (the default) uses
``xcrun --sdk macosx --show-sdk-path`` — whichever Xcode version is
installed on the runner. The recipe ALSO writes the captured version
to ``package/meta.json`` so the ingest pipeline can record provenance.

Explicit versions try ``xcrun --sdk macosx{version} --show-sdk-path``
first; on miss (the runner doesn't have that exact SDK installed)
the recipe falls back to the default and records the actual version
captured. Catalogue ingest treats version mismatch between dispatched
and captured versions as a fatal — version slippage in this
direction is too easy to ignore otherwise.

## Forge dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \\
    -f recipe_repo=zackees/soldr-toolchain \\
    -f recipe_ref=main \\
    -f recipe_path=recipes/apple-sdk-universal2 \\
    -f name=apple-sdk-universal2 \\
    -f version=14.5 \\
    -f macos_arm64=true \\
    -f windows_x64=false -f linux_x64=false
```

One macos_arm64 runner is sufficient — `lipo -thin` is bidirectional,
so an arm64 runner can produce universal2 output containing both
slices (the recipe just doesn't strip anything).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from conan import ConanFile
from conan.errors import ConanInvalidConfiguration
from conan.tools.files import copy

_RECIPES_ROOT = Path(__file__).resolve().parent.parent
if str(_RECIPES_ROOT) not in sys.path:
    sys.path.insert(0, str(_RECIPES_ROOT))
from _apple_sdk_thin import prune_manpages  # noqa: E402


SUPPORTED_SDK_VERSIONS = ("auto", "11.3", "13.3", "14.5", "15.2")


class AppleSDKUniversal2(ConanFile):
    name = "apple-sdk-universal2"
    description = (
        "Apple macOS SDK packaged for cross-compile consumers. Universal2 "
        "shape — all architecture slices the runner's SDK ships, no "
        "lipo-thinning. Full SDK tree (headers, frameworks, .tbd files) "
        "is included verbatim per soldr-toolchain#14 full-SDK policy."
    )
    license = "Apple-MIT-Style"
    package_type = "header-library"
    no_copy_source = True
    settings = "os", "arch"
    options = {"sdk_version": SUPPORTED_SDK_VERSIONS}
    default_options = {"sdk_version": "auto"}

    def validate(self):
        if str(self.settings.os) != "Macos":
            raise ConanInvalidConfiguration(
                "apple-sdk-universal2 runs on macOS runners only "
                f"(got os={self.settings.os})."
            )

    def build(self):
        sdk_root, captured_version, xcode_version = self._resolve_sdk()
        self.output.info(f"copying full SDK from {sdk_root}")
        self.output.info(f"captured SDK version: {captured_version}")
        self.output.info(f"Xcode version: {xcode_version}")

        # Copy the entire `MacOSX*.sdk` directory tree byte-for-byte
        # into the build folder under `sdk/`.
        dest = Path(self.build_folder) / "sdk"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(sdk_root, dest, symlinks=True)
        prune_manpages(dest, self.output)

        # Provenance metadata for the catalogue ingest.
        meta = {
            "shape": "universal2",
            "requested_sdk_version": str(self.options.sdk_version),
            "captured_sdk_version": captured_version,
            "xcode_version": xcode_version,
            "runner_arch": str(self.settings.arch),
        }
        Path(self.build_folder, "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )

    def package(self):
        # Move `sdk/` + `meta.json` into the Conan package folder. Forge
        # wraps the package_folder in tar.gz; ingest re-tars + zstd-
        # compresses for the catalogue.
        copy(
            self,
            "*",
            src=Path(self.build_folder, "sdk").as_posix(),
            dst=Path(self.package_folder, "sdk").as_posix(),
        )
        copy(
            self,
            "meta.json",
            src=self.build_folder,
            dst=self.package_folder,
        )

    # ----- helpers --------------------------------------------------

    def _resolve_sdk(self) -> tuple[str, str, str]:
        """Return (sdk_path, captured_version, xcode_version)."""
        requested = str(self.options.sdk_version)
        if requested == "auto":
            sdk_arg = "macosx"
        else:
            sdk_arg = f"macosx{requested}"

        try:
            sdk_path = subprocess.check_output(
                ["xcrun", "--sdk", sdk_arg, "--show-sdk-path"],
                text=True,
            ).strip()
        except subprocess.CalledProcessError as exc:
            if requested == "auto":
                raise ConanInvalidConfiguration(
                    f"xcrun --sdk macosx failed; no SDK on runner: {exc}"
                ) from exc
            self.output.warning(
                f"requested SDK {requested} not on runner; falling back to default"
            )
            sdk_path = subprocess.check_output(
                ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
                text=True,
            ).strip()

        captured_version = subprocess.check_output(
            ["xcrun", "--sdk", "macosx", "--show-sdk-version"],
            text=True,
        ).strip()
        if requested not in ("auto",) and not captured_version.startswith(requested):
            raise ConanInvalidConfiguration(
                f"requested SDK version {requested} but runner returned "
                f"{captured_version}; refuse to mislabel."
            )

        try:
            xcode_version = subprocess.check_output(
                ["xcodebuild", "-version"], text=True
            ).splitlines()[0].split(" ", 1)[-1].strip()
        except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
            xcode_version = "unknown"

        return sdk_path, captured_version, xcode_version
