# llvm-host-libs for linux-x64-gnu hosts

Runtime libraries for the managed LLVM bundle's `lld` (zackees/soldr#3774).
`lld` NEEDs `libxml2.so.2` and `libz.so.1` with `RUNPATH $ORIGIN/../lib`;
hosts such as ubuntu-26.04 have no `libxml2.so.2`. This bundle builds
libxml2 2.13.9 (no ICU/lzma/iconv/python) and zlib 1.3.1 from pinned
sources inside digest-pinned manylinux2014 (glibc 2.17) and verifies each
library's soname, GLIBC floor, and NEEDED closure. Build natively on the
matching runner.

```sh
gh workflow run forge-conan.yml --repo zackees/forge \
  -f recipe_repo=zackees/soldr-toolchain -f recipe_ref=main \
  -f recipe_path=recipes/llvm-host-libs-linux-x64-gnu -f name=llvm-host-libs-linux-x64-gnu \
  -f version=libxml2-2.13.9-zlib-1.3.1 -f linux_x64=true
```

Catalogue path: `llvm-host-libs/libxml2-2.13.9-zlib-1.3.1/linux-x64-gnu/bundle.tar.zst`.
