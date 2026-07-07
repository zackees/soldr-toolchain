# mingw-w64-gcc windows-x64-gnu

Pure download + repackage recipe for the WinLibs x86_64 POSIX/SEH
MSVCRT MinGW-w64 GCC toolchain. The package root is the stripped
`mingw64/` tree, so consumers get `bin/gcc.exe`, `bin/g++.exe`,
`bin/ar.exe`, `bin/ranlib.exe`, `bin/ld.exe`, `bin/windres.exe`, and
the `x86_64-w64-mingw32/` sysroot directly under package root.

Manual dispatch:

```sh
gh workflow run forge-conan.yml --repo zackees/forge \
  -f recipe_repo=zackees/soldr-toolchain \
  -f recipe_ref=main \
  -f recipe_path=recipes/mingw-w64-gcc-windows-x64-gnu \
  -f name=mingw-w64-gcc-windows-x64-gnu \
  -f version=15.3.0posix-14.0.0-msvcrt-r1 \
  -f linux_x64=true \
  -f windows_x64=false \
  -f windows_arm64=false \
  -f macos_arm64=false
```

Expected catalogue path after ingest:

```text
mingw-w64-gcc/15.3.0posix-14.0.0-msvcrt-r1/windows-x64-gnu/bundle.tar.zst
```
