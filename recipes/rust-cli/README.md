# `rust-cli`

Generic forge recipe for Rust CLI support binaries that soldr bundles
into release archives.

The forge package name determines the tool and shape:

- `cargo-chef-linux-x64-gnu`
- `cargo-chef-windows-arm64`
- `crgx-linux-arm64-musl`

The recipe installs the pinned Rust toolchain (`RUST_TOOLCHAIN` in
`recipes/_rust_cli.py`, currently `1.98.1`, tracking zackees/soldr's
`rust-toolchain.toml`), exports it as `RUSTUP_TOOLCHAIN`, verifies
`rustc --version`, then runs `cargo install <crate> --version <version>
--target <triple> --locked` and packages the binary as:

```text
package/
  bin/<tool>[.exe]
  meta.json      # records rust_toolchain + rustc_version
```

After ingest, the asset lands at:

```text
<tool>/v<version>/<shape>/bundle.tar.zst
```

The catalog entry is also merged into `<tool>/manifest.json`, so the
GitHub Pages `manifest.json` resolver sees the support binary directly.
