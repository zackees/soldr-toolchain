# `ninja-windows-x64`

Forge-built Conan recipe — `windows-x64` Ninja bundle repackaged from
the official upstream prebuilt `ninja-win.zip`
(https://github.com/ninja-build/ninja/releases/tag/v1.13.2).
Pinned to ninja 1.13.2.

Pure download + repackage: the produced artifact is identical no matter
which runner builds it, so every shape dispatches on the cheap
`linux_x64` runner (same trick as the `python-*` recipes).

No musl shapes exist for this tool — the upstream Linux binaries
require glibc.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge     -f recipe_repo=zackees/soldr-toolchain     -f recipe_ref=main     -f recipe_path=recipes/ninja-windows-x64     -f name=ninja-windows-x64     -f version=1.13.2     -f linux_x64=true     -f windows_x64=false -f macos_arm64=false
```

## Catalogue path after ingest

```
ninja/1.13.2/windows-x64/bundle.tar.zst
```

## What it ships

```
package/
├── bin/ninja.exe
└── meta.json   ← { tool, ninja_version, shape, asset_name, source_url }
```
