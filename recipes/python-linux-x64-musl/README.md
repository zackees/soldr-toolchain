# `python-linux-x64-musl`

Forge-built Conan recipe — `x86_64-unknown-linux-musl` Python sysroot from
[astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone).
Closes part of soldr#933 / soldr#997 Phase A.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge     -f recipe_repo=zackees/soldr-toolchain     -f recipe_ref=main     -f recipe_path=recipes/python-linux-x64-musl     -f name=python-linux-x64-musl     -f version=3.13.0     -f linux_x64=true     -f windows_x64=false -f macos_arm64=false
```

Ingested catalogue path: `python/3.13.0/linux-x86_64-musl/sysroot.tar.zst`
