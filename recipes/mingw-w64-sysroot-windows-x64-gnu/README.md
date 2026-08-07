# mingw-w64-sysroot windows-x64-gnu

Host-neutral MinGW-w64 **sysroot** for Rust's `x86_64-pc-windows-gnu`
target (soldr-toolchain#114). Repackages only the host-neutral subset of
the same WinLibs zip the `mingw-w64-gcc-windows-x64-gnu` recipe consumes:

- `x86_64-w64-mingw32/include/**` — headers
- `x86_64-w64-mingw32/lib/**` — import libraries (`libmingw32.a`,
  `libmingwex.a`, `libmsvcrt.a`, `libkernel32.a`, …) and CRT startup
  objects (`crt2.o`, `dllcrt2.o`)
- `lib/gcc/x86_64-w64-mingw32/**` — gcc runtime (`libgcc.a`,
  `crtbegin.o`, `crtend.o`)

Every host executable (`bin/`, `libexec/`, any `*.exe`/`*.dll`) is
dropped, so Linux, macOS, and Windows consumers materialize the **same
blob**. This is what `zackees/reld`'s `cross-release` lane needs: reld
brings its own linker engine (bridging to `lld` for PE-COFF) and only
needs the sysroot, not `gcc.exe`.

## Slug decision

Published under the **`windows-x64-gnu`** slug, which names the *target*
— consistent with the six GNU-shaped C-lib sysroots (`zstd`, `sqlite`,
`mimalloc`, `zlib-ng`, `lzma`, `bzip2`) already catalogued under that
slug and consumed from any host. The payload carries no host binaries,
so a separate `noarch`/`any` catalogue platform (which would require a
schema change) is unnecessary for Phase 1. Revisit if a genuinely
host-keyed variant is ever added.

## Runtime (msvcrt vs UCRT)

Pinned to **msvcrt** (`…-msvcrt-r1`), matching Rust's default
`x86_64-pc-windows-gnu` (msvcrt) and the existing `mingw-w64-gcc` pin.
Serving reld's native **UCRT64** world would be a second asset variant
(`-ucrt`), not a change to this pin — tracked as a follow-up on #114.

Manual dispatch (extraction needs no compiler, so it builds on the cheap
Linux worker):

```sh
gh workflow run forge-conan.yml --repo zackees/forge \
  -f recipe_repo=zackees/soldr-toolchain \
  -f recipe_ref=main \
  -f recipe_path=recipes/mingw-w64-sysroot-windows-x64-gnu \
  -f name=mingw-w64-sysroot-windows-x64-gnu \
  -f version=15.3.0posix-14.0.0-msvcrt-r1 \
  -f linux_x64=true \
  -f windows_x64=false \
  -f windows_arm64=false \
  -f macos_arm64=false
```

Expected catalogue path after ingest:

```text
mingw-w64-sysroot/15.3.0posix-14.0.0-msvcrt-r1/windows-x64-gnu/bundle.tar.zst
```
