# `uv-linux-x64-musl`

Forge-built Conan recipe — `linux-x64-musl` uv bundle repackaged from
the official upstream prebuilt `uv-x86_64-unknown-linux-musl.tar.gz`
(https://github.com/astral-sh/uv/releases/tag/0.11.26 — note: uv
release tags have no `v` prefix).
Pinned to uv 0.11.26.

Pure download + repackage: the produced artifact is identical no matter
which runner builds it, so every shape dispatches on the cheap
`linux_x64` runner (same trick as the `python-*` recipes).

All eight shapes exist for this tool — unlike cmake/ninja, uv ships
musl Linux builds upstream.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge     -f recipe_repo=zackees/soldr-toolchain     -f recipe_ref=main     -f recipe_path=recipes/uv-linux-x64-musl     -f name=uv-linux-x64-musl     -f version=0.11.26     -f linux_x64=true     -f windows_x64=false -f macos_arm64=false
```

## Catalogue path after ingest

```
uv/0.11.26/linux-x64-musl/bundle.tar.zst
```

## What it ships

```
package/
├── bin/uv
├── bin/uvx
└── meta.json   ← { tool, uv_version, shape, asset_name, source_url }
```
