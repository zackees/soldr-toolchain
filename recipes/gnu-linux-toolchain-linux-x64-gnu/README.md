# GNU/Linux toolchain for x86_64-unknown-linux-gnu

Reproducibly builds an x86_64 Linux-hosted GCC 13.3.0/binutils 2.42 toolchain
and glibc 2.17 sysroot for `x86_64-unknown-linux-gnu` with crosstool-NG 1.27.0. The producer
runs in a digest-pinned manylinux2014 image so both the host executables and
target libc retain the GLIBC 2.17 floor. Zig is neither installed nor used.

The bundle includes C/C++ drivers, linker, archiver, ranlib, libstdc++, target
headers, CRT objects, libraries, crosstool-NG config, component license files,
and the downloaded corresponding source archives needed for redistribution.
The producer measures the highest GLIBC symbol in the target libc and every
host ELF under `bin/`; publication fails above 2.17.

```sh
gh workflow run forge-conan.yml --repo zackees/forge   -f recipe_repo=zackees/soldr-toolchain -f recipe_ref=<branch>   -f recipe_path=recipes/gnu-linux-toolchain-linux-x64-gnu   -f name=gnu-linux-toolchain-linux-x64-gnu   -f version=gcc-13.3.0-glibc-2.17-1 -f linux_x64=true
```

Catalogue path: `gnu-linux-toolchain/gcc-13.3.0-glibc-2.17-1/linux-x64-gnu/bundle.tar.zst`.
