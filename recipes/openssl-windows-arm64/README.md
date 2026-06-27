# `openssl-windows-arm64`

Sibling of `openssl-windows-x64/`. Same source zip; selects `arm64/`
subdir instead of `x64/`. Closes the arm64 half of soldr#943.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/openssl-windows-arm64 \
    -f name=openssl-windows-arm64 \
    -f version=3.5.0 \
    -f linux_x64=true \
    -f windows_x64=false -f macos_arm64=false
```

Ingested catalogue path: `openssl/3.5.0/windows-aarch64-msvc/bundle.tar.zst`
