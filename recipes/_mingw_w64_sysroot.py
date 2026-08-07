"""Shared helper for the host-neutral ``mingw-w64-sysroot`` recipe.

soldr-toolchain#114. The only mingw asset this catalogue published
before was the Windows-host WinLibs bundle (``mingw-w64-gcc``), whose
payload is Windows ``.exe`` toolchain binaries. A non-Windows consumer
— e.g. ``zackees/reld``'s ``cross-release`` job (Linux ->
``x86_64-pc-windows-gnu``), which brings its own linker and only needs
the sysroot to link a GNU-flavoured PE — has nothing it can fetch.

This helper repackages the **host-neutral** subset of the very same
WinLibs archive: the ``x86_64-w64-mingw32/{include,lib}`` target sysroot
(headers, import libraries, CRT startup objects) plus the gcc runtime
tree (``lib/gcc/x86_64-w64-mingw32/**`` — ``libgcc.a``, ``crtbegin.o``,
``crtend.o``, …). It deliberately drops every host executable
(``bin/``, ``libexec/``, any ``*.exe``/``*.dll``), so the same blob is
consumable on Linux, macOS, or Windows.

Slug: ``windows-x64-gnu`` names the *target* (as the six existing
GNU-shaped C-lib sysroots already do), not the host; the payload
carries no host binaries. See the recipe README for the slug decision.
"""

from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


# Pin the same WinLibs release the ``mingw-w64-gcc`` bundle uses so the
# sysroot and (if a consumer also fetches it) the compiler agree on CRT,
# gcc, and mingw-w64 versions.
PINNED_VERSIONS = ("15.3.0posix-14.0.0-msvcrt-r1",)

SHAPE_ASSETS = {
    "windows-x64-gnu": {
        # Same upstream zip as mingw-w64-gcc-windows-x64-gnu; we keep
        # only the host-neutral members.
        "asset": "winlibs-x86_64-posix-seh-gcc-15.3.0-mingw-w64msvcrt-14.0.0-r1.zip",
        "thread_model": "posix",
        "exception_model": "seh",
        "runtime": "msvcrt",
        "gcc_version": "15.3.0",
        "mingw_w64_version": "14.0.0",
        "target_triple": "x86_64-w64-mingw32",
    },
}

# Members under these top-level dirs are host executables / driver
# tooling and must never appear in a host-neutral sysroot.
_HOST_EXECUTABLE_PREFIXES = ("bin/", "libexec/")

# The sysroot subtrees we keep (relative to the stripped ``mingw64/``
# root). Everything else is dropped.
_SYSROOT_PREFIXES = (
    "x86_64-w64-mingw32/include/",
    "x86_64-w64-mingw32/lib/",
    "lib/gcc/x86_64-w64-mingw32/",
)

# Host-neutral file extensions permitted in the payload. `.o`/`.a` are
# CRT objects + static/import libs; headers and gcc spec/config files
# round out what a linker + cc-rs need. A stray `.exe`/`.dll` inside a
# kept subtree (there should be none) is rejected rather than shipped.
_ALLOWED_SUFFIXES = (
    ".h",
    ".a",
    ".o",
    ".c",
    ".def",
    ".spec",
    ".pc",
    ".inc",
    ".gch",
)
_FORBIDDEN_SUFFIXES = (".exe", ".dll", ".bat", ".cmd", ".ps1")

# Presence gate: the CRT objects + core import libs + a header + the
# gcc runtime a GNU PE link cannot be produced without.
REQUIRED_PATHS = (
    "x86_64-w64-mingw32/lib/crt2.o",
    "x86_64-w64-mingw32/lib/dllcrt2.o",
    "x86_64-w64-mingw32/lib/libmingw32.a",
    "x86_64-w64-mingw32/lib/libmingwex.a",
    "x86_64-w64-mingw32/lib/libmsvcrt.a",
    "x86_64-w64-mingw32/lib/libkernel32.a",
    "x86_64-w64-mingw32/include/stdio.h",
    "lib/gcc/x86_64-w64-mingw32",
)


def supported_shapes() -> tuple[str, ...]:
    return tuple(sorted(SHAPE_ASSETS.keys()))


def _is_host_neutral(rel: str) -> bool:
    """True when ``rel`` (a stripped, POSIX-style member path) belongs
    in the host-neutral sysroot: under a kept subtree, not a host
    executable dir, and without a forbidden suffix."""
    if rel.startswith(_HOST_EXECUTABLE_PREFIXES):
        return False
    if not rel.startswith(_SYSROOT_PREFIXES):
        return False
    lowered = rel.lower()
    if lowered.endswith(_FORBIDDEN_SUFFIXES):
        return False
    return True


def extract_sysroot_payload(data: bytes, out_root: Path) -> int:
    """Pure extraction — no network. Given the raw WinLibs ``.zip``
    bytes, write only the host-neutral sysroot members under
    ``out_root`` (with the ``mingw64/`` prefix stripped). Returns the
    number of files written. Kept separate from :func:`extract_sysroot`
    so the filter is unit-testable against a synthetic archive."""
    extracted_count = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith("mingw64/"):
                continue
            rel = name[len("mingw64/") :]
            if not rel:
                continue
            if info.is_dir():
                continue
            if not _is_host_neutral(rel):
                continue
            rel_path = PurePosixPath(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise RuntimeError(f"unsafe path in {name!r}")
            target = out_root.joinpath(*rel_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            extracted_count += 1
    return extracted_count


def _assert_no_host_executables(out_root: Path) -> None:
    for path in out_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(out_root).as_posix()
        if rel.startswith(_HOST_EXECUTABLE_PREFIXES):
            raise RuntimeError(
                f"host executable leaked into sysroot payload: {rel}"
            )
        if rel.lower().endswith(_FORBIDDEN_SUFFIXES):
            raise RuntimeError(
                f"forbidden host-binary suffix in sysroot payload: {rel}"
            )


def _validate_package(out_root: Path, shape: str) -> None:
    missing = [rel for rel in REQUIRED_PATHS if not (out_root / rel).exists()]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"mingw-w64-sysroot bundle for {shape} is missing required paths: {joined}"
        )
    _assert_no_host_executables(out_root)


def extract_sysroot(
    *,
    version: str,
    shape: str,
    build_folder: Path,
    output,
) -> dict:
    """Fetch the WinLibs archive for ``shape`` and repackage only its
    host-neutral sysroot into ``build_folder/package``."""

    cfg = SHAPE_ASSETS.get(shape)
    if cfg is None:
        raise ValueError(
            f"unsupported mingw-w64-sysroot shape {shape}; supported: {supported_shapes()}"
        )
    if version not in PINNED_VERSIONS:
        raise ValueError(
            f"unsupported mingw-w64-sysroot version {version}; supported: {PINNED_VERSIONS}"
        )

    asset_name = cfg["asset"]
    url = (
        "https://github.com/brechtsanders/winlibs_mingw/releases/download/"
        f"{version}/{asset_name}"
    )
    output.info(f"fetching {url}")
    with urllib.request.urlopen(url, timeout=600) as resp:
        data = resp.read()
    output.info(
        f"downloaded {len(data)} bytes; extracting host-neutral sysroot"
    )

    out_root = build_folder / "package"
    out_root.mkdir(parents=True, exist_ok=True)
    extracted_count = extract_sysroot_payload(data, out_root)
    output.info(f"extracted {extracted_count} host-neutral files into package/")
    if extracted_count == 0:
        raise RuntimeError(
            f"no sysroot files extracted from {asset_name}; expected a mingw64/ archive root"
        )

    _validate_package(out_root, shape)

    meta = {
        "tool": "mingw-w64-sysroot",
        "version": version,
        "shape": shape,
        "asset_name": asset_name,
        "source_url": url,
        "upstream": "brechtsanders/winlibs_mingw",
        "host_neutral": True,
        "contains_host_executables": False,
        "target_triple": cfg["target_triple"],
        "gcc_version": cfg["gcc_version"],
        "mingw_w64_version": cfg["mingw_w64_version"],
        "thread_model": cfg["thread_model"],
        "exception_model": cfg["exception_model"],
        "runtime": cfg["runtime"],
    }
    (build_folder / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta
