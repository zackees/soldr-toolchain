# `apple-sdk-thin-aarch64`

Forge-built Conan recipe — emits the macOS SDK tree with every Mach-O
file lipo-thinned to **arm64 only**. Headers, frameworks, and `.tbd`
files are kept (with `.tbd` `archs:` lists rewritten to `[ arm64 ]`).

Note: the catalogue platform string is `darwin-aarch64` (Rust convention)
but Apple's `lipo` arch token is `arm64`. Same architecture, two names.

Tracked in [#14](https://github.com/zackees/soldr-toolchain/issues/14).

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_path=recipes/apple-sdk-thin-aarch64 \
    -f name=apple-sdk-thin-aarch64 \
    -f version=14.5 -f macos_arm64=true \
    -f windows_x64=false -f linux_x64=false
```

Ingested into the catalogue at `apple-sdk/{version}/darwin-aarch64/sdk.tar.zst`.
