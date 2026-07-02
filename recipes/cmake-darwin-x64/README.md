# `cmake-darwin-x64`

Forge-built Conan recipe — `darwin-x64` CMake bundle repackaged from
the official upstream prebuilt `cmake-4.3.4-macos-universal.tar.gz`
(https://github.com/Kitware/CMake/releases/tag/v4.3.4).
Pinned to cmake 4.3.4.

The upstream macOS archive is a universal binary — `darwin-x64` and
`darwin-arm64` repackage the same asset. The `CMake.app/Contents/`
prefix is stripped so the package root is `bin/` + `share/`.

Pure download + repackage: the produced artifact is identical no matter
which runner builds it, so every shape dispatches on the cheap
`linux_x64` runner (same trick as the `python-*` recipes).

No musl shapes exist for this tool — the upstream Linux binaries
require glibc.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge     -f recipe_repo=zackees/soldr-toolchain     -f recipe_ref=main     -f recipe_path=recipes/cmake-darwin-x64     -f name=cmake-darwin-x64     -f version=4.3.4     -f linux_x64=true     -f windows_x64=false -f macos_arm64=false
```

## Catalogue path after ingest

```
cmake/4.3.4/darwin-x64/bundle.tar.zst
```

## What it ships

```
package/
├── bin/cmake
├── bin/ctest
├── bin/cpack
├── share/cmake-4.3/   ← FULL module tree (Modules/, Templates/, ...)
└── meta.json          ← { tool, cmake_version, shape, asset_name, source_url }
```

`doc/` and `man/` are dropped to keep the bundle small.
