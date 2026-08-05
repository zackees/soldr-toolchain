# musl Linux x64 toolchain

Pinned `musl.cc` GCC 11.2.1 for `x86_64-unknown-linux-musl`. The producer verifies the source SHA-256, requires compiler/binutils, C/C++ runtime and musl CRT objects including `crt1.o`/`rcrt1.o`, then links a static C++ smoke binary. Zig is not installed or used.

Catalogue path: `musl-linux-toolchain/gcc-11.2.1-musl-20211123-1/linux-x64-musl/bundle.tar.zst`.
