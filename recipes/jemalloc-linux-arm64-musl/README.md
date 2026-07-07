# `jemalloc-linux-arm64-musl`

Forge-built Conan recipe for `jemalloc` `5.3.0` targeting
`aarch64-unknown-linux-musl`.

The shared implementation is in `recipes/_syslib.py`; this directory is
kept as a thin wrapper so forge can dispatch and cache one package per
`(library, target)` tuple.

## Source

https://github.com/jemalloc/jemalloc/releases/download/5.3.0/jemalloc-5.3.0.tar.bz2

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/jemalloc-linux-arm64-musl \
    -f name=jemalloc-linux-arm64-musl \
    -f version=5.3.0 \
    -f windows_x64=false \
    -f windows_x64_gnu=false \
    -f windows_arm64=false \
    -f linux_x64=false \
    -f linux_arm64=false \
    -f linux_x64_musl=false \
    -f linux_arm64_musl=true \
    -f macos_x64=false \
    -f macos_arm64=false
```

Ingested catalogue path:
`jemalloc/5.3.0/linux-arm64-musl/bundle.tar.zst`
