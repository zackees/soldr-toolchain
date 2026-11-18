# Darwin compiler-rt 21.1.5, built on Linux x64

The official LLVM **21.1.5** release has no macOS archive. This producer uses the
SHA256-pinned `llvm-project-21.1.5.src.tar.xz`, builds LLVM's Darwin archive/lipo
tools for the Linux host, and cross-builds compiler-rt with the exact managed
21.1.5 clang and an extracted Apple SDK. No Darwin program executes.

Hosted builds provision the published LLVM 21.1.5 Linux bundle and the existing
Apple SDK unit from a verified catalogue-v2/state pair. Every part and final
archive is checked for SHA256 and size. The recipe exports the downloader and
shared reader modules. No workstation paths or installed soldr binary are needed.
CMake, Ninja, zstd, Python 3.12+ and a host C/C++ compiler are producer tools
supplied by Forge. To override provisioning, supply both SOLDR_LLVM_ROOT and
SOLDR_APPLE_SDK_ROOT together.

The package overlays `lib/clang/21/lib/darwin/` and includes ASan/UBSan dynamic
runtimes, the availability builtins archive `libclang_rt.osx.a`, any other built
osx runtimes, sanitizer headers and the LLVM license. Packaging rejects missing
required files, non-arm64 objects, invalid ad-hoc SHA256 signature pages, or
an absent global definition of `__isPlatformVersionAtLeast`. Signature evidence
is retained in meta.json. Local source build, package, sanitizer and availability
link probes passed against the actual SDK 15.5; this is not an SDK 26.5 proof.

```sh
gh workflow run forge-dispatch.yml --repo zackees/soldr-toolchain --ref main \
  -f tool=compiler-rt -f version=21.1.5 -f shape=darwin-arm64 -f recipe_ref=main
```

The dispatcher explicitly selects the
Linux x64 producer even though the artifact shape is `darwin-arm64`.
Catalogue: `compiler-rt/21.1.5/darwin-arm64/bundle.tar.zst`.

See [the cross-build guide](../../docs/LINUX_DARWIN_CROSS.md) for the required link,
architecture and signature proofs.
