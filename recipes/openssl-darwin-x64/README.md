# `openssl-darwin-x64`

Forge-built Conan recipe for `openssl` `3.5.8` targeting
`x86_64-apple-darwin`.

The shared implementation is in `recipes/_syslib.py`; this directory is
kept as a thin wrapper so forge can dispatch and cache one package per
`(library, target)` tuple.

## Source

https://github.com/openssl/openssl/releases/download/openssl-3.5.8/openssl-3.5.8.tar.gz

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/openssl-darwin-x64 \
    -f name=openssl-darwin-x64 \
    -f version=3.5.8 \
    -f windows_x64=false \
    -f windows_x64_gnu=false \
    -f windows_arm64=false \
    -f linux_x64=false \
    -f linux_arm64=false \
    -f linux_x64_musl=false \
    -f linux_arm64_musl=false \
    -f macos_x64=true \
    -f macos_arm64=false
```

Ingested catalogue path:
`openssl/3.5.8/darwin-x64/bundle.tar.zst`
