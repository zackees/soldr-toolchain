# `apple-sdk-thin-x86_64`

Forge-built Conan recipe — emits the macOS SDK tree with every Mach-O
file lipo-thinned to **x86_64 only**. Headers, frameworks, and `.tbd`
files are kept (with `.tbd` `archs:` lists rewritten to `[ x86_64 ]`).

Tracked in [#14](https://github.com/zackees/soldr-toolchain/issues/14).

See the sibling [`apple-sdk-universal2`](../apple-sdk-universal2/) recipe
for the full-SDK shape that doesn't strip; and
[`apple-sdk-thin-aarch64`](../apple-sdk-thin-aarch64/) for the arm64
sibling.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_path=recipes/apple-sdk-thin-x86_64 \
    -f name=apple-sdk-thin-x86_64 \
    -f version=14.5 -f macos_arm64=true \
    -f windows_x64=false -f linux_x64=false
```

Ingested into the catalogue at `apple-sdk/{version}/darwin-x86_64/sdk.tar.zst`.
