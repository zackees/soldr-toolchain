# `rust-cli`

Generic forge recipe for Rust CLI support binaries that soldr bundles
into release archives.

The forge package name determines the tool and shape:

- `cargo-chef-linux-x64-gnu`
- `cargo-chef-windows-arm64`
- `crgx-linux-arm64-musl`

The recipe runs `cargo install <crate> --version <version> --target
<triple> --locked` and packages the binary as:

```text
package/
  bin/<tool>[.exe]
  meta.json
```

After ingest, the asset lands at:

```text
<tool>/v<version>/<shape>/bundle.tar.zst
```

The catalog entry is also merged into `<tool>/manifest.json`, so the
GitHub Pages `manifest.json` resolver sees the support binary directly.
