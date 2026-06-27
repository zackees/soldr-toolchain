"""Shared helpers for the apple-sdk-thin-{x86_64, aarch64} recipes.

Imported from each sibling recipe's conanfile.py — Conan recipes can
sit next to a non-conanfile.py module in the same directory and import
from it because Conan adds the recipe dir to sys.path during build.
The two thin recipes only differ in which arch token they pass to the
thin-walk routine, so factor the walk out here.

See ``recipes/apple-sdk-thin-x86_64/conanfile.py`` and
``recipes/apple-sdk-thin-aarch64/conanfile.py`` for the consumers, and
``recipes/apple-sdk-universal2/conanfile.py`` for the full-shape sibling.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable


# Mach-O magic bytes (32-bit + 64-bit, little + big endian, + fat).
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",  # MH_MAGIC      (32-bit BE)
    b"\xce\xfa\xed\xfe",  # MH_CIGAM      (32-bit LE)
    b"\xfe\xed\xfa\xcf",  # MH_MAGIC_64   (64-bit BE)
    b"\xcf\xfa\xed\xfe",  # MH_CIGAM_64   (64-bit LE)
    b"\xca\xfe\xba\xbe",  # FAT_MAGIC     (fat BE)
    b"\xbe\xba\xfe\xca",  # FAT_CIGAM     (fat LE)
    b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64
    b"\xbf\xba\xfe\xca",  # FAT_CIGAM_64
}


def looks_like_macho(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return head in MACHO_MAGICS


def lipo_thin_in_place(path: Path, arch: str, log) -> None:
    """Replace ``path`` with its ``arch``-only slice. No-op when the
    file is already thin and matches ``arch``; logs + skips when the
    file does not contain ``arch``."""
    try:
        archs_line = subprocess.check_output(
            ["lipo", "-archs", str(path)], text=True
        ).strip()
    except subprocess.CalledProcessError:
        log.warning(f"{path}: lipo -archs failed; leaving as-is")
        return
    archs = archs_line.split()
    if arch not in archs:
        log.warning(f"{path}: missing slice for {arch} (has {archs}); skipping")
        return
    if archs == [arch]:
        return  # already thin for this arch
    tmp = path.with_suffix(path.suffix + ".lipo-tmp")
    try:
        subprocess.check_call(
            ["lipo", "-thin", arch, "-output", str(tmp), str(path)]
        )
        tmp.replace(path)
    except subprocess.CalledProcessError as exc:
        log.warning(f"{path}: lipo -thin {arch} failed: {exc}; leaving as-is")
        if tmp.exists():
            tmp.unlink()


def prune_tbd_archs(path: Path, keep_arch: str, log) -> None:
    """Rewrite a ``.tbd`` (text-based dylib) file so its top-level
    ``archs:`` list contains only ``keep_arch`` (and ``arm64`` is kept
    if ``keep_arch == arm64``). YAML edit done by line scan to avoid
    pulling PyYAML — the .tbd format is line-oriented and the
    ``archs:`` key always appears at top level."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        log.warning(f"{path}: not utf-8 text; skipping tbd prune")
        return
    new_lines = []
    in_archs = False
    for line in text.splitlines():
        if line.startswith("archs:"):
            new_lines.append(f"archs:           [ {keep_arch} ]")
            in_archs = False
            continue
        if line.lstrip().startswith("archs:") and ":" in line:
            indent = line[: len(line) - len(line.lstrip())]
            new_lines.append(f"{indent}archs:           [ {keep_arch} ]")
            continue
        new_lines.append(line)
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def thin_sdk_tree(sdk_root: Path, arch: str, dest: Path, log) -> None:
    """Copy ``sdk_root`` into ``dest`` and lipo-thin every Mach-O along
    the way. Symlinks are preserved (the SDK has many cross-version
    framework symlinks)."""
    import shutil

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(sdk_root, dest, symlinks=True)

    files_walked = 0
    files_thinned = 0
    files_pruned = 0
    for path in dest.rglob("*"):
        if path.is_symlink() or path.is_dir():
            continue
        files_walked += 1
        if path.suffix == ".tbd":
            prune_tbd_archs(path, arch, log)
            files_pruned += 1
            continue
        if looks_like_macho(path):
            lipo_thin_in_place(path, arch, log)
            files_thinned += 1
    log.info(
        f"thinned {files_thinned} Mach-O files and pruned {files_pruned} "
        f".tbd files across {files_walked} total entries (target arch: {arch})"
    )


def write_meta(build_folder: str, shape: str, captured: dict, log) -> None:
    meta = {"shape": shape, **captured}
    Path(build_folder, "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    log.info(f"meta.json: {json.dumps(meta)}")


def resolve_sdk(options, log) -> tuple[str, str, str]:
    """Same logic as apple-sdk-universal2's _resolve_sdk."""
    requested = str(options.sdk_version)
    sdk_arg = "macosx" if requested == "auto" else f"macosx{requested}"
    try:
        sdk_path = subprocess.check_output(
            ["xcrun", "--sdk", sdk_arg, "--show-sdk-path"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        if requested == "auto":
            raise
        log.warning(f"requested SDK {requested} missing; falling back to default")
        sdk_path = subprocess.check_output(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True
        ).strip()
    captured = subprocess.check_output(
        ["xcrun", "--sdk", "macosx", "--show-sdk-version"], text=True
    ).strip()
    try:
        xcode_version = (
            subprocess.check_output(["xcodebuild", "-version"], text=True)
            .splitlines()[0]
            .split(" ", 1)[-1]
            .strip()
        )
    except Exception:
        xcode_version = "unknown"
    return sdk_path, captured, xcode_version
