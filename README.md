# soldr-toolchain

Producer scripts + tests for a public catalogue of third-party tool
releases consumed by [`zackees/soldr`](https://github.com/zackees/soldr)'s
CI.

## Two-branch layout

The repo splits cleanly into a **code branch** and a **data branch**:

| Branch | Role | Contents |
|---|---|---|
| `main` | code only | producer scripts, tests, workflows, pyproject |
| `assets` | data only (orphan branch) | catalogue JSON + locally-hosted blobs (LFS) |

`main` has no manifest data, no LFS, no `deps/`. `assets` has no Python
code, no tests, no workflows.

### `assets` branch layout

```
/                                       # assets branch (orphan)
├── README.md
├── .gitattributes                      # LFS rules for deps/** blobs
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

Locally-hosted blobs follow `<tool>/<version>/<platform>/<file>`:

- **`<tool>`** — directory name matches the top-level `tools` key.
- **`<version>`** — the release tag, used as-is (e.g. `1.12.9`, `v0.23.0`,
  `MacOSX11.3`).
- **`<platform>`** — the OS+arch part of the schema's platform key
  (e.g. `linux-x64`, `darwin-arm64`, `darwin`). No ABI suffix here.
- **variants flat inside the platform folder** — when a tool ships
  multiple ABIs for the same OS+arch (e.g. `linux-x64-gnu` and
  `linux-x64-musl`), both files live as flat siblings inside
  `<tool>/<version>/<platform>/`. The filename carries the ABI.

Today, the only blob actually hosted in-repo is the Apple SDK at
`apple-sdk/MacOSX11.3/darwin/sdk.tar.zstd`. The structure is set up
so the build scripts can host more tools locally in the future without
schema changes.

## Why this repo exists

soldr's `cross-compile-all-targets.yml` workflow used to have every
parallel matrix lane independently resolve tool download URLs against
the GitHub Releases REST API. With 7+ lanes hitting the same runner
IP, the unauthenticated 60-req/hour quota burned out fast and the
workflow 403-ed.

This repo breaks that:

1. A nightly workflow on `main` (`.github/workflows/refresh-manifest.yml`)
   runs `scripts/build_manifest.py`, which queries the GitHub API
   **once per tool** (authenticated, 5000 req/hour, `per_page=100`)
   and writes the resolved `browser_download_url` for every asset
   into the per-tool files on the `assets` branch.
2. The workflow commits any diff back to `assets`.
3. Consumer workflows fetch from
   `https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/<path>`
   — that URL is CDN-served and **not** subject to the API rate limit.

Per-tool files only change when upstream actually publishes something
new (the script uses content equality, not timestamps). `git log` on
the `assets` branch is the source of truth for "when did this tool's
release set last change."

The same daily refresh publishes `rust-nightly-versions.v1.json`, a
metadata-only map from `nightly-YYYY-MM-DD` to the corresponding rustc
release and full commit identity. The map is a SHA-bearing asset in
`catalogue.v1.json`; it contains no toolchain archives and is stored as
ordinary Git JSON rather than LFS. Only a newly observed nightly is
downloaded with the minimal profile and queried for its verbose version;
known nightlies are never downloaded or probed again. The reverse
`versions` index lists nightlies newest-first and selects index zero.
If a scheduled run is missed, each later refresh checks up to eight
oldest unprocessed dates, records dates on which no nightly was
published, and eventually closes the gap without repeating prior work.
The incremental backfill begins at `nightly-2025-12-06`, the first observed
nightly in the Rust 1.94 train, and includes `nightly-2026-01-18`, its newest
observed nightly, so consumers pinned to Rust 1.94.x can
use the map immediately.

## Getting an asset by platform (dead simple, schema v5)

Each release in the per-tool manifest carries a normalized `platforms`
map keyed by `<os>-<arch>[-<extra>]`. Consumers ask for the host they
care about — no need to deal with each upstream tool's idiosyncratic
asset filename quirks.

```python
import json, urllib.request
m = json.loads(urllib.request.urlopen(
    "https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/zccache/manifest.json"
).read())

latest = m[0]                                  # newest-first array
url    = latest["platforms"]["linux-x64-musl"]["url"]
```

Or via the bundled helper:

```sh
uv run --group dev python -m scripts.tool_query --platform linux --arch x86 --extra musl zccache
```

## Platform key shape

```
os    ∈ { linux, darwin, windows }
arch  ∈ { x64, arm64, universal2 }         # 32-bit lanes are not surfaced
extra ∈ { gnu, musl, msvc, gnullvm, … }    # only when meaningful
```

| Key | Meaning |
|---|---|
| `linux-x64-gnu` | Standard Linux x64 glibc build — **floor must be glibc 2.17** |
| `linux-x64-musl` | Linux x64 musl (static; runs on glibc hosts too) |
| `linux-arm64-gnu` | Linux aarch64 glibc — **floor must be glibc 2.17** |
| `linux-arm64-musl` | Linux aarch64 musl |
| `darwin-x64` | macOS x86_64 |
| `darwin-arm64` | macOS Apple Silicon |
| `darwin-universal2` | macOS fat binary |
| `windows-x64-msvc` | Windows x64, official MSVC ABI |
| `windows-arm64-msvc` | Windows ARM64, official MSVC ABI |
| `windows-x64-gnu` | Windows x64, GNU ABI |
| `windows-arm64-gnullvm` | Windows ARM64, gnullvm ABI |

Modern arch names (`x64` not `x86_64`; `arm64` not `aarch64`) match
the npm/Node.js convention. **32-bit binaries (i686 / armv7) are not
surfaced** — every modern process is 64-bit, the schema doesn't need
to fragment to track them.

## Linux glibc floor: 2.17, always

**Every catalogued Linux `-gnu` asset MUST have a glibc floor of 2.17 or
lower.** This applies to assets this repo builds *and* to prebuilt ones it
merely fetches and republishes.

This repo is the middle of a chain — `zackees/forge` builds the tools,
soldr-toolchain catalogues them, and `zackees/soldr` bundles them into its
release archive. The floor of that archive is the **highest** floor of any
binary in it, so a single 2.39 asset here caps everything downstream. That is
exactly what happened: `crgx` and `cargo-chef` are fetched prebuilt at glibc
2.39, which pins soldr's Linux archive at 2.39 no matter how soldr compiles
itself, and makes it unusable on RHEL 8 / Debian 10 (both 2.28).

**Deps currently above the floor must be recompiled**, not documented around.
Rebuilding them is upstream work in `zackees/forge`, whose recipes are now
required to build against glibc 2.17 — see that repo's README.

Rules:

- **musl assets are exempt** — statically linked, no glibc to floor.
- **Verify before cataloguing.** `readelf -V <binary> | grep -o 'GLIBC_[0-9.]*'
  | sort -Vu | tail -1` must print `GLIBC_2.17` or lower for every `-gnu`
  asset. Asserting the floor in a manifest is not the same as measuring it.
- **No zig, no `cargo-zigbuild`.** Both are being purged in favour of the
  blessed toolchain. Reaching 2.17 is a matter of building in an old sysroot,
  which needs no zig.

The `gnu-linux-toolchain` Forge recipes are that blessed source. They build GCC
13.3.0 and binutils 2.42 with crosstool-NG 1.27.0 in a digest-pinned
manylinux2014 x86_64 producer, for x86_64 and aarch64 targets. Each package
contains its crosstool-NG config, license files, and downloaded corresponding
source archives. The recipe measures both target `libc.so.6` and host tool ELF
version requirements and refuses any result above GLIBC 2.17.

## Tracked tools

| Tool | Upstream | Pin source |
|---|---|---|
| zccache | `zackees/zccache` | soldr `MANAGED_ZCCACHE_VERSION` |
| crgx | `yfedoseev/crgx` | soldr `MANAGED_CRGX_VERSION` |
| cargo-chef | `LukeMathWalker/cargo-chef` | soldr `CARGO_CHEF_PINNED_VERSION` |
| cargo-zigbuild | `rust-cross/cargo-zigbuild` | latest |
| cargo-xwin | `rust-cross/cargo-xwin` | latest |
| mingw-w64-gcc | `brechtsanders/winlibs_mingw` | pinned WinLibs release |
| gnu-linux-toolchain | crosstool-NG-built GCC/binutils/glibc/Linux | `gcc-13.3.0-glibc-2.17-1`; reproducible config + source archives included |
| apple-sdk | vendored under `apple-sdk/MacOSX11.3/darwin/` | manual |

The `scripts/build_manifest.py` script reads the three pinned versions
directly from a checkout of `zackees/soldr` so the manifest can never
drift from what soldr would fetch.

## Local development

This project is managed with [uv](https://docs.astral.sh/uv/). All
Python invocations go through `uv run` — no manual virtualenv
activation needed.

### Cloning both branches

```sh
git clone https://github.com/zackees/soldr-toolchain.git
git clone -b assets https://github.com/zackees/soldr-toolchain.git soldr-toolchain-assets
# The parity test auto-discovers ../soldr-toolchain-assets.
```

### Running the tests

```sh
uv run --group dev pytest
```

The parity test (`tests/test_parity.py`) auto-discovers the assets
checkout in this order:

1. `$SOLDR_TOOLCHAIN_ASSETS_DIR` environment variable
2. `../soldr-toolchain-assets` (sibling clone)
3. `../assets`

If none of those resolves to a directory containing `manifest.json`,
the parity tests skip — the pure-function tests still run.

### Producing the Dylint 6.0.3 triplet

The `build and validate native Dylint release` workflow produces
`cargo-dylint`, `dylint-link`, and the exact-nightly `dylint-driver` together on
all eight canonical hosts. Windows ARM64 and both macOS architectures use native
hosted runners; GNU binaries use digest-pinned manylinux2014 containers; musl
binaries and drivers use digest-pinned Alpine containers on matching CPU
architectures.

The driver identity is immutable and includes Dylint `6.0.3`,
`nightly-2026-05-28`, rustc release `1.98.0-nightly`, full rustc commit
`57d06900fd7d9ee06d3a7f323bb77f17ab3cfaf8`, and the host triple. Every lane
relocates all three binaries, runs a clean custom-lint case, observes the known
`release_fixture_forbidden_io` violation, and repeats the violation with Cargo
offline. The warm run must leave the driver hash and timestamp unchanged.

The driver bundle intentionally contains only `dylint-driver`; rustc-private
libraries remain owned by the exact installed nightly. Consumers must place the
driver at
`$DYLINT_DRIVER_PATH/nightly-2026-05-28-<host>/dylint-driver` and expose that
nightly's private libraries while invoking Dylint: prepend the toolchain's
`bin` directory to `PATH` on Windows, its `lib` directory to `LD_LIBRARY_PATH`
on Linux, or its `lib` directory to `DYLD_LIBRARY_PATH` on macOS. Soldr owns
that runtime environment contract; the release fixture uses the same layout
and never treats the driver archive as a standalone rustc distribution.

After a green workflow run, download all eight workflow artifacts into one
directory and ingest the complete 24-bundle transaction into a clean sibling
checkout of the `assets` branch:

```sh
gh run download <run-id> --dir ../dylint-release-artifacts
uv run --frozen --group dev python -m scripts.produce_dylint_release ingest \
    --artifacts-dir ../dylint-release-artifacts \
    --assets-root ../soldr-toolchain-assets \
    --forge-run-id <run-id>
```

Ingest fails closed if any tool/host artifact, payload hash, real-lint evidence,
or driver identity is missing or inconsistent. Review the resulting assets diff,
run `scripts.lint_assets`, and publish it as one PR against `assets`; never merge
a partial target set. Unsupported nightlies are remediated by adding and proving
a new exact driver identity in this producer before a Soldr consumer pin changes.

### Rebuilding the catalogue locally

```sh
# Needs both a soldr checkout (for pinned versions) and an assets-branch
# checkout (the destination tree).
uv run --group dev python -m scripts.build_manifest \
    --output-dir ../soldr-toolchain-assets \
    --repo-root ../soldr

uv run --group dev python -m scripts.build_asset_index \
    --manifest-checkout ../soldr-toolchain-assets \
    --output ../soldr-toolchain-assets/asset-index.json \
    --branch assets \
    --offline   # omit for the SHA256SUMS HTTP fetch pass
```
