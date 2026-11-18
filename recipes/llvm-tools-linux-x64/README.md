# LLVM 21.1.5 tools for Linux x64

Builds clang, lld, LLVM binutils and dsymutil from SHA256-pinned LLVM 21.1.5
source inside Forge's digest-pinned GLIBC 2.17 container. The upstream Linux
binary requires GLIBC 2.34 and cannot be republished under the catalogue floor.

Ships X86/AArch64 backends, shared LLVM/Clang libraries, resource headers and
relative tool aliases: clang++, clang-cl, ld.lld, ld64.lld, lld-link, llvm-lib,
llvm-dlltool, llvm-ranlib, llvm-strip, dsymutil and llvm-dsymutil. Every shipped
ELF is measured; relative RPATH finds the package libraries. LLVM notices and
source identity are retained in the package.

```sh
gh workflow run forge-dispatch.yml --repo zackees/soldr-toolchain --ref main \
  -f tool=llvm-tools -f version=21.1.5 -f shape=linux-x64-gnu -f recipe_ref=main
```

Unit: `llvm-tools/21.1.5/linux-x64-gnu/bundle.tar.zst`.
The exact-recipe mode preserves this Linux cross-driver name during planning.
See [the cross-build guide](../../docs/LINUX_DARWIN_CROSS.md).
