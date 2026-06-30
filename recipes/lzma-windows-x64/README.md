# `lzma-windows-x64`

Forge-built Conan recipe for `lzma` `5.6.3` targeting
`x86_64-pc-windows-msvc`.

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
    -f recipe_path=recipes/lzma-windows-x64 \
    -f name=lzma-windows-x64 \
    -f version=5.6.3 \
    -f windows_x64=true \
    -f windows_arm64=false \
    -f linux_x64=false \
    -f linux_arm64=false \
    -f linux_x64_musl=false \
    -f linux_arm64_musl=false \
    -f macos_x64=false \
    -f macos_arm64=false
```

Ingested catalogue path:
`lzma/5.6.3/windows-x64/bundle.tar.zst`
