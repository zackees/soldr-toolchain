# `libobjc-tbd` — Apple Objective-C runtime tbd files

Conan recipe consumed by [`zackees/forge`](https://github.com/zackees/forge)
to extract the Objective-C runtime tbd files from a macOS runner's
Xcode SDK and republish them as a soldr-toolchain catalogue entry.

See `conanfile.py` for the recipe itself and the rationale section
in the docstring.

## Dispatch from `gh`

```bash
gh workflow run forge-conan.yml --repo zackees/forge \
    -f recipe_repo=zackees/soldr-toolchain \
    -f recipe_ref=main \
    -f recipe_path=recipes/libobjc-tbd \
    -f name=libobjc-tbd \
    -f version=14.5 \
    -f macos_x64=true \
    -f macos_arm64=true \
    -f windows_x64=false \
    -f linux_x64=false
```

## Why this exists

`soldr-toolchain`'s `apple-sdk/MacOSX11.3/darwin-universal2/sdk.tar.zstd`
is vintage Xcode 12. The `usr/lib/libobjc.tbd` it ships covers most
crates but newer cargo-zigbuild releases expect the symbol set the
14.x SDK ships. This recipe is the upstream-fix path's complement:
when the zig + cargo-zigbuild version bump in soldr#995 still misses
edge cases, an in-catalogue `libobjc-tbd` artifact lets us overlay
the newer tbd files without re-vendoring the whole SDK.
