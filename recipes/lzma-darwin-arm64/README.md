# `lzma-darwin-arm64`

Forge-built Conan recipe for `lzma` `5.6.3` targeting
`aarch64-apple-darwin`.

The shared implementation is in `recipes/_syslib.py`; this directory is
kept as a thin wrapper so forge can dispatch and cache one package per
`(library, target)` tuple.

## Source

https://github.com/tukaani-project/xz/releases/download/v5.6.3/xz-5.6.3.tar.gz

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/lzma-darwin-arm64 \
    -f name=lzma-darwin-arm64 \
    -f version=5.6.3 \
    -f windows_x64=false \
    -f windows_x64_gnu=false \
    -f windows_arm64=false \
    -f linux_x64=false \
    -f linux_arm64=false \
    -f linux_x64_musl=false \
    -f linux_arm64_musl=false \
    -f macos_x64=false \
    -f macos_arm64=true
```

Ingested catalogue path:
`lzma/5.6.3/darwin-arm64/bundle.tar.zst`
