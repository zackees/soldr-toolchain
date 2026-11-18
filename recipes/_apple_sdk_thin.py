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
import os
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
    # Xcode 26 SDK stubs can be read-only. This is the copied working tree;
    # leave the installed SDK untouched while permitting the thinning edit.
    path.chmod(path.stat().st_mode | 0o200)
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def thin_sdk_tree(sdk_root: Path, arch: str, dest: Path, log) -> None:
    """Copy ``sdk_root`` into ``dest`` and lipo-thin every Mach-O along
    the way. Symlinks are preserved (the SDK has many cross-version
    framework symlinks)."""
    import shutil

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(sdk_root, dest, symlinks=True)
    prune_manpages(dest, log)

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


def prune_manpages(sdk_root: Path, log) -> int:
    """Remove optional manpage documentation from a copied SDK tree.

    The Apple SDK's manpage filenames include colon-bearing names such as
    ``ttk::progressbar.ntcl``.  Those names are valid on macOS but cannot be
    materialized by Windows tar extraction.  Headers, libraries, frameworks,
    and every other SDK build input remain untouched.
    """
    import shutil

    man_dir = sdk_root / "usr" / "share" / "man"
    if not man_dir.exists():
        return 0
    removed = sum(1 for _ in man_dir.rglob("*"))
    shutil.rmtree(man_dir)
    log.info(f"removed {removed} optional Apple SDK manpage entries")
    return removed


def write_meta(build_folder: str, shape: str, captured: dict, log) -> None:
    meta = {"shape": shape, **captured}
    Path(build_folder, "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    log.info(f"meta.json: {json.dumps(meta)}")


def copy_swift_resources(sdk_root: Path, dest: Path) -> None:
    """Capture Xcode's target resource directory for SDK 26.x without compiling.

    SDK module interfaces alone omit toolchain-owned Darwin overlays/shims.
    Preserve their relative layout; the Linux consumer supplies host plugins.
    """
    import shutil

    developer = next(
        (p for p in sdk_root.resolve().parents if p.name == "Developer" and p.parent.name == "Contents"),
        None,
    )
    if developer is None:
        raise ValueError(f"SDK 26.x requires an Xcode Developer directory: {sdk_root}")
    resources = developer / "Toolchains/XcodeDefault.xctoolchain/usr/lib/swift"
    for required in ("macosx", "shims"):
        if not (resources / required).is_dir():
            raise ValueError(f"Xcode Swift resources missing {resources / required}")
    shutil.copytree(resources, dest, symlinks=True)


def versioned_sdk_paths(version: str) -> list[Path]:
    paths = Path("/Applications").glob(
        f"Xcode*.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX{version}.sdk"
    )
    return sorted((path for path in paths if path.is_dir()),
                  key=lambda path: (f"Xcode_{version}.app" not in str(path), str(path)))


def resolve_sdk(options, log, package_version=None) -> tuple[str, str, str]:
    """Same logic as apple-sdk-universal2's _resolve_sdk."""
    requested = str(options.sdk_version)
    if requested == "auto" and package_version is not None:
        requested = str(package_version)
    candidates = versioned_sdk_paths(requested) if requested != "auto" else []
    if candidates:
        # Several Xcodes may ship the same SDK version. Prefer its matching
        # Xcode so SDK interfaces and toolchain-owned Swift resources agree.
        sdk_path = str(candidates[0])
    else:
        sdk_path = None
    sdk_arg = "macosx" if requested == "auto" else f"macosx{requested}"
    try:
        if sdk_path is None:
            sdk_path = subprocess.check_output(
                ["xcrun", "--sdk", sdk_arg, "--show-sdk-path"], text=True
            ).strip()
    except subprocess.CalledProcessError:
        if requested == "auto":
            raise
        log.warning(f"requested SDK {requested} missing; checking default before rejecting mismatch")
        sdk_path = subprocess.check_output(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True
        ).strip()
    developer = next((p for p in Path(sdk_path).resolve().parents if p.name == "Developer" and p.parent.name == "Contents"), None)
    selected_env = {**os.environ, "DEVELOPER_DIR": str(developer)} if developer else dict(os.environ)
    captured = subprocess.check_output(
        ["xcrun", "--sdk", sdk_path, "--show-sdk-version"], text=True, env=selected_env
    ).strip()
    if requested != "auto" and captured != requested:
        raise ValueError(
            f"requested SDK version {requested} but runner returned {captured}; refuse to mislabel"
        )
    try:
        xcodebuild = str(developer / "usr/bin/xcodebuild") if developer else "xcodebuild"
        xcode_version = (
            subprocess.check_output([xcodebuild, "-version"], text=True, env=selected_env)
            .splitlines()[0]
            .split(" ", 1)[-1]
            .strip()
        )
    except Exception:
        xcode_version = "unknown"
    return sdk_path, captured, xcode_version
