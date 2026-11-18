# Linux-hosted Darwin toolchain production

The 2026-11-18 maintainer decision requires all macOS artifact compilation on
Linux x64. macOS runners may extract SDK/Xcode target resources and execute
completed artifacts. Tracking: [#221](https://github.com/zackees/soldr-toolchain/issues/221),
[obs-studio#5009](https://github.com/obs-rust/obs-studio/issues/5009),
[obs-studio#5008](https://github.com/obs-rust/obs-studio/issues/5008).

## Inputs and producer contracts

| Input | Producer | Installation contract |
| --- | --- | --- |
| LLVM 21.1.5 host tools | llvm-tools-linux-x64 | Linux clang, ld64.lld, dsymutil and llvm-dsymutil aliases |
| Darwin compiler-rt 21.1.5 | compiler-rt-darwin-arm64 on Linux x64 | Overlay lib/clang/21/lib/darwin; never mix LLVM patch versions |
| Swift 6.3.2 host compiler | swift-linux-x64-gnu | Preserve bin/../lib, importer resources and Linux host plugins |
| SDK 26.5 and target Swift resources | existing apple-sdk-* family on extraction runner | sdk/ plus swift-resource-dir/ with Xcode macosx overlays and shims |

The official [LLVM 21.1.5 release](https://github.com/llvm/llvm-project/releases/tag/llvmorg-21.1.5)
has no macOS prebuilt archive. The compiler-rt producer therefore builds pinned
sources on Linux rather than substituting the 21.1.6 macOS archive.

The SDK family accepts 26.0 through 26.5. Its default option resolves the **Conan
package version**, so dispatch version=26.5 requests macosx26.5. The selected SDK
path supplies the measured version; mismatch aborts rather than relabeling a
runner default. 26.x packages additionally preserve Xcode's Swift resource tree.
The consumer must use Linux host plugins and importer headers while selecting
Darwin target resources; captured macOS host plugins cannot execute on Linux.

## Prepared dispatches (not executed)

After the recipe PR is merged:

```sh
# Extraction only; requires the selected Forge runner to have SDK 26.5/Xcode.
gh workflow run forge-dispatch.yml --repo zackees/soldr-toolchain --ref main \
  -f tool=apple-sdk -f version=26.5 -f shape=thin-aarch64 -f recipe_ref=main
# Use shape=universal2 for the full SDK with every architecture slice.

gh workflow run forge-dispatch.yml --repo zackees/soldr-toolchain --ref main \
  -f tool=swift -f version=6.3.2 -f shape=linux-x64-gnu -f recipe_ref=main

gh workflow run forge-dispatch.yml --repo zackees/soldr-toolchain --ref main \
  -f tool=llvm-tools -f version=21.1.5 -f shape=linux-x64-gnu -f recipe_ref=main

gh workflow run forge-dispatch.yml --repo zackees/soldr-toolchain --ref main \
  -f tool=compiler-rt -f version=21.1.5 -f shape=darwin-arm64 -f recipe_ref=main
```

`forge-dispatch.yml` dispatches `forge-ci/forge/forge-conan.yml`, waits, then
queues `forge-ingest.yml`. The new runtime and Swift producers select Linux x64.
Forge currently maps macos_arm64 to **macos-15**, not macos-26; its runner/Xcode
selection must provide the requested SDK before the extraction dispatch can
succeed. The recipe refuses a missing/older SDK. The compiler-rt worker must have
soldr's managed LLVM/SDK available, or explicit SOLDR_LLVM_ROOT and
SOLDR_APPLE_SDK_ROOT. These environment prerequisites are not forwarded by the
dispatcher. Do not claim a successful production dispatch until they are supplied.

For an already completed Forge run, ingestion is:

```sh
gh workflow run forge-ingest.yml --repo zackees/soldr-toolchain --ref main \
  -f forge_run_id=RUN_ID -f tool=apple-sdk -f version=26.5 -f shape=thin-aarch64
```

Use the matching tool/version/shape for the other bundles. Ingestion opens a
catalogue PR; reviewed publication remains a separate step. Preserve immutable
bytes and the current v2 publication policy in [ASSET_CATALOG.md](ASSET_CATALOG.md).

## Required proof before downstream activation

On Linux x64, import AppKit and Metal in Swift 6, emit an arm64 Mach-O dylib
with managed ld64.lld, and independently verify the code signature and CPU slice.
Use a fresh module cache so cached native modules cannot establish a false pass.
Then link independent address+undefined sanitizer and availability-builtin C
probes with LLVM 21.1.5 and its packaged resource directory. Verify signatures
and arm64 slices of all runtime dylibs. No target code executes on Linux.

A source build or packaging test alone does not establish a usable Darwin
Swift/SDK closure, nor OBS's CMake/bridge renderer closure. Provisioning and
CMake/native exposure are tracked in
[soldr#4165](https://github.com/forge-ci/soldr/issues/4165).

## Sources and licensing

Swift.org documents [PGP verification](https://www.swift.org/install/linux/tarball/).
The producer pins both archive SHA256 and the Swift 6 signing fingerprint and
measures all ELF files against the repository GLIBC 2.17 rule. Preserve the
upstream `share/swift/LICENSE.txt` and other notices.

[Swift SDK bundles](https://github.com/swiftlang/swift-sdk-generator) separate
host compiler from target resources.
[xtool's Darwin SDK builder](https://github.com/xtool-org/xtool/blob/main/Sources/XToolSupport/SDKBuilder.swift)
captures Xcode Swift resource paths, uses Linux tools and replaces host-only
components. Its layout is prior art, not proof that an arbitrary Swift/SDK pair
works.

Apple SDKs are proprietary under the
[Xcode and Apple SDKs Agreement](https://www.apple.com/legal/sla/docs/xcode.pdf),
not an MIT-style SDK license. Existing 11.3/14.5 catalogue entries establish the
repository's packaging precedent, not redistribution permission. Preserve Apple
notices and apply the repository's existing entitlement/publication policy;
private/entitled bytes can use the curated-entry mechanism in ASSET_CATALOG.md.
No Apple bytes are committed to main by these recipes.

## Local evidence and limits

Linux source validation built LLVM 21.1.5 compiler-rt against the local SDK whose
SDKSettings.json reports **15.5** (despite its managed cache directory being
named 14.5). All eight produced dylibs are thin arm64, with independently checked
ad-hoc SHA256 CodeDirectory pages. The global availability builtin is defined.
Address+undefined, standalone undefined, and availability-builtin link probes
all produce signed arm64 executables. Ad-hoc integrity verification does not
establish Developer ID identity, notarization, runtime execution or Gatekeeper
approval. Swift's real archive package step passes its signature and ELF gates.
SDK 26.5 extraction and the full Swift/Metal/AppKit link remain activation gates.

Apple lists Swift 6.3 for SDK 26.5 in its
[Xcode release notes](https://developer.apple.com/documentation/xcode-release-notes/xcode-26_6-release-notes).
The host recipe therefore pins the signed Swift 6.3.2 Amazon Linux 2 release;
the older 6.2.3 archive was useful for initial packaging research but does not
establish compatibility with the 26.5 interfaces.
