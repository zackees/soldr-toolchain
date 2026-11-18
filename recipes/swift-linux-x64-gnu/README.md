# Swift 6 for Linux x64

Repackages the official Swift **6.2.3 Amazon Linux 2** x86_64 archive, preserving
`bin/../lib`, swiftc, swift-driver, swift-frontend, host plugins, Clang importer
resources, Linux libraries and upstream notices. SHA256 and the pinned Swift 6
PGP signer are both checked before extraction. Every ELF file is measured with
readelf; requirements above GLIBC 2.17 abort packaging. GPG, readelf and Python
3.12+ are producer prerequisites. Swift itself also needs host ncurses/tinfo,
libxml2, libcurl and C/C++ runtime libraries; provision these on the Linux host.

Local real-archive evidence: 114 ELF files, maximum GLIBC 2.17; valid Swift 6
release signature `52BB7E3DE28A71BE22EC05FFEF80A866B47A981F`.

```sh
gh workflow run forge-dispatch.yml --repo zackees/soldr-toolchain --ref main \
  -f tool=swift -f version=6.2.3 -f shape=linux-x64-gnu -f recipe_ref=main
```

This command queues Forge and ingestion; it has **not** been dispatched as part
of recipe development. Catalogue: `swift/6.2.3/linux-x64-gnu/bundle.tar.zst`.

Darwin support requires the separately extracted Apple SDK plus its
`swift-resource-dir/` (target overlays/shims), the managed LLVM ld64.lld and
`dsymutil`. Swift compiler/SDK interface compatibility must be proven by importing
real Metal and AppKit overlays. A Linux archive by itself is not a Darwin SDK.
See [the cross-build guide](../../docs/LINUX_DARWIN_CROSS.md) and
[soldr-toolchain#221](https://github.com/zackees/soldr-toolchain/issues/221).
