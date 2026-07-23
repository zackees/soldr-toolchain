# `llvm-tools-linux-x64`

Forge-built Conan recipe — LLVM toolchain bundle for a Linux x64
cross-compile driver host. Closes soldr#934 + soldr#942.

## What's inside

Whitelist-extracted from the official `clang+llvm-<ver>-x86_64-linux-gnu-ubuntu-22.04.tar.xz`
release archive at `github.com/llvm/llvm-project/releases`:

```
package/
├── bin/
│   ├── clang / clang++       ← clang driver
│   ├── clang-cl              ← MSVC-mode driver (cc-rs target for Windows MSVC cross)
│   ├── lld / lld-link        ← LLVM linker (Mach-O/PE/ELF — replaces lib.exe + link.exe + ld)
│   ├── llvm-lib              ← MSVC lib.exe replacement
│   ├── llvm-rc / llvm-dlltool ← rc.exe / dlltool replacements
│   ├── llvm-strip            ← strip replacement
│   ├── llvm-objcopy          ← rust-objcopy underlying tool (closes #934 strip-fail)
│   └── llvm-dsymutil         ← packed Darwin DWARF materialization
├── lib/libLLVM.so.<ver>      ← runtime lib rust-objcopy needs at exec time
└── include/clang/...          ← C/C++ headers cc-rs uses when compiling deps
```

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/llvm-tools-linux-x64 \
    -f name=llvm-tools-linux-x64 \
    -f version=18.1.8 \
    -f linux_x64=true \
    -f windows_x64=false -f macos_arm64=false
```

Ingested catalogue path: `llvm-tools/18.1.8/linux-x86_64-gnu/bundle.tar.zst`
