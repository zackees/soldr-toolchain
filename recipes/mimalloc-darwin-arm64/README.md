# `mimalloc-darwin-arm64`

Forge-built Conan recipe for `mimalloc` `3.3.2` targeting
`aarch64-apple-darwin`.

The shared implementation is in `recipes/_syslib.py`; this directory is
kept as a thin wrapper so forge can dispatch and cache one package per
`(library, target)` tuple.

## Source

https://github.com/microsoft/mimalloc/archive/refs/tags/v3.3.2.tar.gz

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/mimalloc-darwin-arm64 \
    -f name=mimalloc-darwin-arm64 \
    -f version=3.3.2 \
    -f windows_x64=false \
    -f windows_arm64=false \
    -f linux_x64=false \
    -f linux_arm64=false \
    -f linux_x64_musl=false \
    -f linux_arm64_musl=false \
    -f macos_x64=false \
    -f macos_arm64=true
```

Ingested catalogue path:
`mimalloc/3.3.2/darwin-arm64/bundle.tar.zst`
