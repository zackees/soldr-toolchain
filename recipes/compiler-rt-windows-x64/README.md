# Windows MSVC x64 compiler-rt builtins, produced on Linux

Pinned LLVM **21.1.5**, matching `llvm-tools-linux-x64`. Repackages only
`lib/clang/21/lib/windows/clang_rt.builtins-x86_64.lib` from the official
LLVM Windows x64 archive. Host Windows executables are never installed or run.
Checks the pinned archive SHA256, detached GPG signature and release signer,
every COFF member's architecture, and a global `__udivti3` definition. Includes
the matching full compiler-rt license. This is a builtins overlay, not the
Windows sanitizer runtime family.

The Linux producer needs `gpg` and managed LLVM 21.1.5 (`llvm-readobj`, `llvm-nm`).
`SOLDR_LLVM_ROOT` can select an existing managed root; otherwise the recipe
fetches and verifies the published Linux LLVM unit. It does not fetch an Apple
SDK. Merge the overlay into the managed LLVM root; retain its existing headers
and Linux/Darwin resources. Clang's intrinsic headers must precede xwin headers.

```sh
gh workflow run forge-dispatch.yml -R zackees/soldr-toolchain --ref main \
  -f tool=compiler-rt -f version=21.1.5 -f shape=windows-x64 -f recipe_ref=main
```

The existing dispatcher selects the exact recipe on Linux x64 and queues
Forge ingest. Expected unit: `compiler-rt/21.1.5/windows-x64/bundle.tar.zst`.
See [the Windows producer/consumer contract](../../docs/LINUX_WINDOWS_CROSS.md).
