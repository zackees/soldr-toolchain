# `python-darwin-arm64`

Forge-built Conan recipe — `aarch64-apple-darwin` Python sysroot from
[astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone).
Closes part of soldr#932 / soldr#997 Phase A.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge     -f recipe_repo=zackees/soldr-toolchain     -f recipe_ref=main     -f recipe_path=recipes/python-darwin-arm64     -f name=python-darwin-arm64     -f version=3.13.0     -f linux_x64=true     -f windows_x64=false -f macos_arm64=false
```

Ingested catalogue path: `python/3.13.0/darwin-aarch64/sysroot.tar.zst`
