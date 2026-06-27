# `nodelib-windows-x64`

Forge-built Conan recipe — Node.js `node.lib` + headers for Windows
MSVC x86_64. Closes part of soldr#944.

Sibling `nodelib-windows-arm64/` (TBD) for `aarch64-pc-windows-msvc`.
Source for both: official `nodejs.org/dist/v<ver>/` releases.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/nodelib-windows-x64 \
    -f name=nodelib-windows-x64 \
    -f version=22.10.0 \
    -f linux_x64=true \
    -f windows_x64=false -f macos_arm64=false
```

Ingested catalogue path: `nodelib/22.10.0/windows-x86_64-msvc/bundle.tar.zst`
