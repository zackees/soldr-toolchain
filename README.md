# soldr-toolchain — assets branch

Public catalogue of third-party tool releases consumed by
[`zackees/soldr`](https://github.com/zackees/soldr) CI.

**Schema:** conforms to the [manifest.json v1](https://github.com/zackees/manifest.json)
format (proto-defined, JSON-serialized). Use the
[`manifest_json`](https://github.com/zackees/manifest.json) Python package to
resolve and validate.

## Layout

```
/                              # this branch (orphan; data only, no code)
├── index.html                 # GitHub Pages landing page
├── .nojekyll                  # serve HTML/JSON raw, no Jekyll preprocessing
├── manifest.json              # Index (federated top-level)
├── asset-index.json           # flat (owner, repo, tag, asset, url, sha256) — debugging only
├── apple-sdk/manifest.json    # Catalog
├── cargo-chef/manifest.json   # Catalog
├── cargo-xwin/manifest.json   # Catalog
├── cargo-zigbuild/manifest.json
├── crgx/manifest.json
├── zccache/manifest.json
└── apple-sdk/MacOSX11.3/darwin/sdk.tar.zstd   # LFS blob (only locally-hosted asset)
```

## Live URLs

- GitHub Pages: <https://zackees.github.io/soldr-toolchain/>
- Raw: `https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/<path>`

Either base works; Pages is preferred for human discovery.

## Resolve an asset

```python
import json, urllib.request
from manifest_json import resolve_in_catalog

url = "https://zackees.github.io/soldr-toolchain/zccache/manifest.json"
catalog = json.loads(urllib.request.urlopen(url).read())
asset = resolve_in_catalog(
    catalog,
    tool="zccache",
    platform={"os": "linux", "arch": "x86_64", "libc": "musl"},
    channel="latest-stable",
)
print(asset["urls"][0])
```

## Channels exposed per tool

| Channel | Meaning |
|---|---|
| `latest-stable` | Newest tracked upstream release |
| `stable` | Alias for `latest-stable` |
| `pinned` | The version `zackees/soldr` actually consumes today (when distinct from latest) |

## Migration history

This branch was migrated from a custom schema v5 to manifest.json v1. The
producer scripts on the `main` branch were updated to emit v1 directly. See
[scripts/convert_v5_to_v1.py](https://github.com/zackees/soldr-toolchain/blob/main/scripts/convert_v5_to_v1.py)
for the one-shot migration logic.
