# `python-windows-x64`

Forge-built Conan recipe that ships a Windows MSVC x86_64 Python
sysroot (lib/ + include/) extracted from
[astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone).

Closes the soldr#931 / soldr#997 Phase A blocker for PyO3
cross-compile to `x86_64-pc-windows-msvc`.

## Sibling recipes (filed per soldr#997 directive)

Pattern repeats for every (target, language) sysroot:

| Soldr issue | Recipe path | Target |
|---|---|---|
| #931 | `recipes/python-windows-x64/` (this) | `x86_64-pc-windows-msvc` |
| #931 | `recipes/python-windows-arm64/` (TBD) | `aarch64-pc-windows-msvc` |
| #932 | `recipes/python-darwin-x64/` (TBD) | `x86_64-apple-darwin` |
| #932 | `recipes/python-darwin-arm64/` (TBD) | `aarch64-apple-darwin` |
| #933 | `recipes/python-linux-x64-gnu/` (TBD) | `x86_64-unknown-linux-gnu` |
| #933 | `recipes/python-linux-arm64-gnu/` (TBD) | `aarch64-unknown-linux-gnu` |
| #933 | `recipes/python-linux-x64-musl/` (TBD) | `x86_64-unknown-linux-musl` |
| #933 | `recipes/python-linux-arm64-musl/` (TBD) | `aarch64-unknown-linux-musl` |

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/python-windows-x64 \
    -f name=python-windows-x64 \
    -f version=3.13.0 \
    -f linux_x64=true \
    -f windows_x64=false -f macos_arm64=false
```

## Catalogue path after ingest

```
python/3.13.0/windows-x86_64-msvc/sysroot.tar.zst
```

## What it ships

```
package/
├── lib/python3.lib       ← stable-ABI import lib for cargo-xwin / lld-link
├── lib/python313.lib     ← versioned import lib (3.13)
├── include/Python.h
├── include/pyconfig.h
├── include/cpython/...
└── meta.json             ← { python_version, pbs_tag, target_triple, source_url }
```
