# `sqlite-windows-arm64`

Forge-built Conan recipe for `sqlite` `3.46.0` targeting
`aarch64-pc-windows-msvc`.

The shared implementation is in `recipes/_syslib.py`; this directory is
kept as a thin wrapper so forge can dispatch and cache one package per
`(library, target)` tuple.

## Source

https://www.sqlite.org/2024/sqlite-amalgamation-3460000.zip

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/sqlite-windows-arm64 \
    -f name=sqlite-windows-arm64 \
    -f version=3.46.0 \
    -f windows_x64=false \
    -f windows_x64_gnu=false \
    -f windows_arm64=true \
    -f linux_x64=false \
    -f linux_arm64=false \
    -f linux_x64_musl=false \
    -f linux_arm64_musl=false \
    -f macos_x64=false \
    -f macos_arm64=false
```

Ingested catalogue path:
`sqlite/3.46.0/windows-arm64/bundle.tar.zst`
