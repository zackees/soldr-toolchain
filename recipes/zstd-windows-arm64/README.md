# `zstd-windows-arm64`

Forge-built Conan recipe for `zstd` `1.5.7` targeting
`aarch64-pc-windows-msvc`.

The shared implementation is in `recipes/_syslib.py`; this directory is
kept as a thin wrapper so forge can dispatch and cache one package per
`(library, target)` tuple.

## Source

https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/zstd-windows-arm64 \
    -f name=zstd-windows-arm64 \
    -f version=1.5.7 \
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
`zstd/1.5.7/windows-arm64/bundle.tar.zst`
