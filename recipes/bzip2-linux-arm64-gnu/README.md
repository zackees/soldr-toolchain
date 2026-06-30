# `bzip2-linux-arm64-gnu`

Forge-built Conan recipe for `bzip2` `1.0.8` targeting
`aarch64-unknown-linux-gnu`.

The shared implementation is in `recipes/_syslib.py`; this directory is
kept as a thin wrapper so forge can dispatch and cache one package per
`(library, target)` tuple.

## Source

https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/bzip2-linux-arm64-gnu \
    -f name=bzip2-linux-arm64-gnu \
    -f version=1.0.8 \
    -f windows_x64=false \
    -f windows_arm64=false \
    -f linux_x64=false \
    -f linux_arm64=true \
    -f linux_x64_musl=false \
    -f linux_arm64_musl=false \
    -f macos_x64=false \
    -f macos_arm64=false
```

Ingested catalogue path:
`bzip2/1.0.8/linux-arm64-gnu/bundle.tar.zst`
