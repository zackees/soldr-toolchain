# `ninja-darwin-arm64`

Forge-built Conan recipe — `darwin-arm64` Ninja bundle repackaged from
the official upstream prebuilt `ninja-mac.zip`
(https://github.com/ninja-build/ninja/releases/tag/v1.13.2).
Pinned to ninja 1.13.2.

The upstream macOS binary is universal — `darwin-x64` and
`darwin-arm64` repackage the same `ninja-mac.zip` asset.

Pure download + repackage: the produced artifact is identical no matter
which runner builds it, so every shape dispatches on the cheap
`linux_x64` runner (same trick as the `python-*` recipes).

No musl shapes exist for this tool — the upstream Linux binaries
require glibc.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge     -f recipe_repo=zackees/soldr-toolchain     -f recipe_ref=main     -f recipe_path=recipes/ninja-darwin-arm64     -f name=ninja-darwin-arm64     -f version=1.13.2     -f linux_x64=true     -f windows_x64=false -f macos_arm64=false
```

## Catalogue path after ingest

```
ninja/1.13.2/darwin-arm64/bundle.tar.zst
```

## What it ships

```
package/
├── bin/ninja
└── meta.json   ← { tool, ninja_version, shape, asset_name, source_url }
```
