# `zstd-linux-x64-gnu`

Forge-built Conan recipe for `zstd` `1.5.7` targeting
`x86_64-unknown-linux-gnu`.

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
    -f recipe_path=recipes/zstd-linux-x64-gnu \
    -f name=zstd-linux-x64-gnu \
    -f version=1.5.7 \
    -f windows_x64=false \
    -f windows_arm64=false \
    -f linux_x64=true \
    -f linux_arm64=false \
    -f linux_x64_musl=false \
    -f linux_arm64_musl=false \
    -f macos_x64=false \
    -f macos_arm64=false
```

Ingested catalogue path:
`zstd/1.5.7/linux-x64-gnu/bundle.tar.zst`
