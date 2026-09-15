# `openssl-linux-arm64-musl`

Forge-built Conan recipe for `openssl` `3.5.8` targeting
`aarch64-unknown-linux-musl`.

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
    -f recipe_path=recipes/openssl-linux-arm64-musl \
    -f name=openssl-linux-arm64-musl \
    -f version=3.5.8 \
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
`openssl/3.5.8/linux-arm64-musl/bundle.tar.zst`
