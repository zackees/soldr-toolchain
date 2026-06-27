# `apple-sdk-universal2`

Forge-built Conan recipe — emits a `MacOSX*.sdk/` tree byte-for-byte,
all architecture slices preserved. The single artifact serves both
`x86_64-apple-darwin` and `aarch64-apple-darwin` cross-build targets.

Tracked in [#14](https://github.com/zackees/soldr-toolchain/issues/14).

## Siblings

- [`recipes/apple-sdk-thin-x86_64`](../apple-sdk-thin-x86_64/) — lipo-thinned to x86_64 only
- [`recipes/apple-sdk-thin-aarch64`](../apple-sdk-thin-aarch64/) — lipo-thinned to arm64 only

All three ship the FULL SDK tree (headers, frameworks, .tbd files) per
the issue #14 full-SDK policy. The thin variants only differ in which
Mach-O slices remain after `lipo -thin` and whether `.tbd` `archs:`
lists are pruned.

## Dispatch

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/apple-sdk-universal2 \
    -f name=apple-sdk-universal2 \
    -f version=14.5 \
    -f macos_arm64=true \
    -f windows_x64=false -f linux_x64=false
```

One macos_arm64 runner is sufficient — `lipo` is bidirectional, so the
arm64 runner produces universal2 output containing both slices (the
recipe just doesn't strip anything).

## SDK version resolution

`sdk_version=auto` (default) uses `xcrun --sdk macosx --show-sdk-path` —
whichever Xcode version is installed on the runner. Explicit versions
try `xcrun --sdk macosx{version}` first; on miss the recipe refuses
to mislabel (see the version-mismatch guard in `_resolve_sdk`).

The recipe writes `package/meta.json` with the captured version,
xcode_version, and target_arch — read by the ingest pipeline for
catalogue provenance.

## Output shape

```
package/
├── sdk/
│   ├── usr/
│   │   ├── include/         ← all architectures' headers
│   │   └── lib/
│   │       ├── libobjc.tbd  ← archs: [ x86_64, arm64 ]
│   │       ├── libSystem.tbd
│   │       └── ...
│   └── System/Library/Frameworks/...
└── meta.json
```

Ingested into the soldr-toolchain catalogue at
`apple-sdk/{version}/darwin-universal2/sdk.tar.zst`.
