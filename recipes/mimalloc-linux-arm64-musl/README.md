# `mimalloc-linux-arm64-musl`

Forge-built Conan recipe for `mimalloc` `3.0.4` targeting
`aarch64-unknown-linux-musl`.

The shared implementation is in `recipes/_syslib.py`; this directory is
kept as a thin wrapper so forge can dispatch and cache one package per
`(library, target)` tuple.

## Source

https://github.com/microsoft/mimalloc/archive/refs/tags/v3.0.4.tar.gz

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/mimalloc-linux-arm64-musl \
    -f name=mimalloc-linux-arm64-musl \
    -f version=3.0.4 \
    -f windows_x64=false \
    -f windows_arm64=false \
    -f linux_x64=false \
    -f linux_arm64=false \
    -f linux_x64_musl=false \
    -f linux_arm64_musl=true \
    -f macos_x64=false \
    -f macos_arm64=false
```

Ingested catalogue path:
`mimalloc/3.0.4/linux-arm64-musl/bundle.tar.zst`
