# `zlib-ng-linux-x64-gnu`

Forge-built Conan recipe for `zlib-ng` `2.2.5` targeting
`x86_64-unknown-linux-gnu`.

The shared implementation is in `recipes/_syslib.py`; this directory is
kept as a thin wrapper so forge can dispatch and cache one package per
`(library, target)` tuple.

## Source

https://github.com/zlib-ng/zlib-ng/archive/refs/tags/2.2.5.tar.gz

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/zlib-ng-linux-x64-gnu \
    -f name=zlib-ng-linux-x64-gnu \
    -f version=2.2.5 \
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
`zlib-ng/2.2.5/linux-x64-gnu/bundle.tar.zst`
