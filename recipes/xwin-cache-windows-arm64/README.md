# `xwin-cache-windows-arm64`

Forge-built Conan recipe — pre-compressed xwin SDK cache for
`aarch64-pc-windows-msvc` cross-compile. soldr#1012 PR 3.

Sibling of the existing `xwin-cache/<date>/windows-x86_64-msvc/`
row in the catalogue. soldr's blessed cross-compile path
(soldr#1012 PR 5) consumes this bundle via `XWIN_CACHE_DIR` env
var so cargo-xwin's slow live MS download is bypassed.

## Source

The xwin tool (https://github.com/Jake-Shadle/xwin) downloads
Microsoft's freely-redistributable MSVC CRT + SDK headers and
library import stubs. The recipe runs `xwin --arch aarch64 splat`
to produce the splatted output tree, then forge wraps it as the
catalogue bundle.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/xwin-cache-windows-arm64 \
    -f name=xwin-cache-windows-arm64 \
    -f version=$(date -u +%Y-%m-%d) \
    -f linux_x64=true \
    -f windows_x64=false -f macos_arm64=false
```

Ingested catalogue path:
`xwin-cache/<version>/windows-aarch64-msvc/xwin-cache.tar.zst`
