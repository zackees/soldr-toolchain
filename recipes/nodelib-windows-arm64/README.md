# `nodelib-windows-arm64`

Sibling of `nodelib-windows-x64/`. Same source archive
(`node-v<ver>-headers.tar.gz` from nodejs.org/dist); per-arch
import library is `win-arm64/node.lib` instead of `win-x64/node.lib`.

Closes the arm64 half of soldr#944.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/nodelib-windows-arm64 \
    -f name=nodelib-windows-arm64 \
    -f version=22.10.0 \
    -f linux_x64=true \
    -f windows_x64=false -f macos_arm64=false
```

Ingested catalogue path: `nodelib/22.10.0/windows-aarch64-msvc/bundle.tar.zst`
