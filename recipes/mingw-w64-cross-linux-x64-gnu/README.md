# mingw-w64-cross linux-x64-gnu

Linux x86_64-host **MinGW-w64 GCC cross toolchain** for Rust's
`x86_64-pc-windows-gnu` target (soldr-toolchain#114 Phase 2 / soldr#2336).

Materialized the same way as `gnu-linux-toolchain`: SHA-locked
conda-forge packages (`gcc_impl_win-64`, `gxx_impl_win-64` @ 15.3.0)
via micromamba-in-Docker, producing a relocatable prefix with
`bin/x86_64-w64-mingw32-{gcc,g++,ar,ranlib,dlltool,windres}`. Pinned to
gcc 15.3.0 to agree with the WinLibs `mingw-w64-sysroot` pin.

This is the maintainer-chosen "keep the gcc driver, just relocate it to
Linux" path — the alternative (zig + sysroot) was rejected to preserve
soldr's documented win-gnu = gcc stance.

## Discovery-first

`_mingw_w64_cross.py` ships with `EXPECTED_LOCK_SHA256 = {}` (capture
only). The **first** forge build succeeds and emits `conda-plan.json` +
`meta.json` recording the resolved builds, sha256s, and the discovered
`bin/` layout. Those are then pinned in the helper and validation is
hardened (link-smoke a PE, assert machine + tools) in a follow-up
commit — mirroring how `_gnu_linux_toolchain.py` pins its glibc-2.17
floor.

Discovery dispatch (needs Docker on the forge Linux worker):

```sh
gh workflow run forge-conan.yml --repo zackees/forge \
  -f recipe_repo=https://github.com/zackees/soldr-toolchain \
  -f recipe_ref=feat/mingw-cross-linux \
  -f recipe_path=recipes/mingw-w64-cross-linux-x64-gnu \
  -f name=mingw-w64-cross-linux-x64-gnu \
  -f version=mingw-w64-gcc-15.3.0 \
  -f linux_x64=true \
  -f windows_x64=false -f windows_x64_gnu=false \
  -f linux_x64_musl=false -f macos_arm64=false
```

Expected catalogue path after ingest:

```text
mingw-w64-cross/mingw-w64-gcc-15.3.0/linux-x64-gnu/bundle.tar.zst
```
