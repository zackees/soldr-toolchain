# soldr-toolchain `assets` branch

This is the long-lived **orphan branch** that hosts the public catalogue
of third-party tool releases consumed by `zackees/soldr`'s CI. It
shares no history with `main` — `main` holds the producer scripts +
tests, and `assets` holds only the catalogue data.

## Layout

```
/                                       # repo root (assets branch)
├── .gitattributes                      # LFS rules
├── manifest.json                       # top-level index: tools -> subdir
├── asset-index.json                    # flat (owner, repo, tag, asset, url, sha256) index
├── apple-sdk/
│   ├── manifest.json                   # vendored release entries
│   └── MacOSX11.3/                     # version
│       └── darwin/                     # platform
│           └── sdk.tar.zstd            # blob (LFS, ~51 MB)
├── zccache/
│   └── manifest.json                   # release metadata (URLs point to GitHub)
├── crgx/
│   └── manifest.json
├── cargo-chef/
│   └── manifest.json
├── cargo-zigbuild/
│   └── manifest.json
└── cargo-xwin/
    └── manifest.json
```

Locally-hosted blobs follow `<tool>/<version>/<platform>/<file>` —
variants for the same OS+arch (e.g. gnu/musl on `linux-x64/`) live as
flat siblings inside the platform folder, the filename carries the
ABI.

## Refreshed by

`scripts/build_manifest.py` and `scripts/build_asset_index.py` on the
[`main`](https://github.com/zackees/soldr-toolchain/tree/main) branch,
driven by `.github/workflows/refresh-manifest.yml` nightly at 06:30
UTC.

## Consumer URL form

```
https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/<path>
```

For LFS-tracked blobs use the `/media/` endpoint instead so the LFS
pointer is followed transparently:

```
https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/<path>
```

Both forms are CDN-served and NOT subject to the GitHub Releases API
rate limit.

See the [main branch README](https://github.com/zackees/soldr-toolchain/blob/main/README.md)
for the full project documentation.
