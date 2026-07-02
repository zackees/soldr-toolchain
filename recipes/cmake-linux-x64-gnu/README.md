# `cmake-linux-x64-gnu`

Forge-built Conan recipe — `linux-x64-gnu` CMake bundle repackaged from
the official upstream prebuilt `cmake-4.3.4-linux-x86_64.tar.gz`
(https://github.com/Kitware/CMake/releases/tag/v4.3.4).
Pinned to cmake 4.3.4.

Pure download + repackage: the produced artifact is identical no matter
which runner builds it, so every shape dispatches on the cheap
`linux_x64` runner (same trick as the `python-*` recipes).

No musl shapes exist for this tool — the upstream Linux binaries
require glibc.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge     -f recipe_repo=zackees/soldr-toolchain     -f recipe_ref=main     -f recipe_path=recipes/cmake-linux-x64-gnu     -f name=cmake-linux-x64-gnu     -f version=4.3.4     -f linux_x64=true     -f windows_x64=false -f macos_arm64=false
```

## Catalogue path after ingest

```
cmake/4.3.4/linux-x64-gnu/bundle.tar.zst
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
