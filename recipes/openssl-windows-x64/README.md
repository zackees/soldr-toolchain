# `openssl-windows-x64`

Forge-built Conan recipe — OpenSSL libs + headers for Windows MSVC
x86_64 cross-compile. Closes the x64 half of soldr#943.

## Source

FireDaemon's `openssl-<ver>.zip` at
`https://download.firedaemon.com/FireDaemon-OpenSSL/openssl-<ver>.zip`.
The zip contains `x64/`, `arm64/`, `x86/` subdirs; this recipe extracts
just `x64/` (sister `openssl-windows-arm64/` extracts `arm64/`).

License: Apache-2.0 (upstream OpenSSL 3.x).

## What's inside

```
package/
├── bin/
│   ├── libssl-3-x64.dll
│   ├── libcrypto-3-x64.dll
│   └── openssl.exe       ← cert / hash CLI, useful at build-time
├── lib/
│   ├── libssl.lib         ← MSVC import lib
│   └── libcrypto.lib
└── include/openssl/*.h
```

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/openssl-windows-x64 \
    -f name=openssl-windows-x64 \
    -f version=3.5.0 \
    -f linux_x64=true \
    -f windows_x64=false -f macos_arm64=false
```

Ingested catalogue path: `openssl/3.5.0/windows-x86_64-msvc/bundle.tar.zst`
