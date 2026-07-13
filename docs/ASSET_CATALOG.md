# Asset catalog layout (canonical)

The `assets` branch hosts both the v1 manifest tree AND any binaries the
producer chooses to vendor in-repo (instead of linking to GitHub releases
or upstream CDNs). This document is the source of truth for the directory
structure and naming conventions that `scripts/lint_assets.py` enforces.

## Tree shape

```
/                                       # root of the `assets` branch
├── manifest.json                       # v1 Index (federated top-level)
├── asset-index.json                    # legacy flat (owner, repo, tag, asset, url, sha256) index
├── README.md
├── .nojekyll                           # GitHub Pages: serve files literally
├── .gitattributes                      # LFS filters for vendored binaries
├── index.html                          # GitHub Pages landing page
└── <tool>/                             # one per top-level Index.tools key
    ├── manifest.json                   # v1 Catalog
    └── <version>/                      # only when the tool ships vendored binaries
        └── <platform>/                 # `<os>-<arch>[-<libc-or-abi>]`
            └── <filename>              # the binary (matches Asset.filename)
```

Tools that pull all their assets from upstream CDNs (GitHub releases,
etc.) have NO `<version>/` directories — only their `manifest.json`.

## Naming rules

### `<tool>`

- Lowercase, kebab-case (`apple-sdk`, `cargo-chef`, `xwin-cache`)
- Matches the key in top-level `manifest.json` `tools` map
- Matches the `tool` field inside the per-tool Catalog
- One per directory entry under the assets root

### `<version>`

- The canonical version string from the Catalog's `Release.version` field
- For GitHub-derived tools, typically the upstream tag verbatim (`v0.1.73`)
- For vendored SDKs, a producer-chosen stable label (`MacOSX11.3`, `2026-06-22`)

### `<platform>`

- The flat string produced by `manifest_json.flatten_platform(platform_dict)`:
  - `<os>-<arch>` always
  - Optional `-<libc-or-abi>` segment (libc preferred over abi when both set)
  - Lowercased

Examples:

| Platform tuple | Directory name |
|---|---|
| `{os: darwin,  arch: universal2}` | `darwin-universal2` |
| `{os: darwin,  arch: aarch64}` | `darwin-aarch64` |
| `{os: linux,   arch: x86_64, libc: musl}` | `linux-x86_64-musl` |
| `{os: linux,   arch: x86_64, libc: glibc}` | `linux-x86_64-glibc` |
| `{os: windows, arch: x86_64, abi: msvc}` | `windows-x86_64-msvc` |
| `{os: windows, arch: aarch64, abi: gnullvm}` | `windows-aarch64-gnullvm` |

Producers must use canonical arch + os names (`x86_64`/`aarch64`,
`darwin`/`windows`) on the producer side. Caller-side aliases
(`x64`/`arm64`/`mac`) are normalized by the resolver but MUST NOT
appear in stored values.

### `<filename>`

- Whatever the upstream chose, byte-for-byte
- MUST match the `Asset.filename` field in the Catalog
- Examples: `sdk.tar.zstd`, `cargo-chef-0.1.73-x86_64-unknown-linux-musl.tar.zst`

## URL convention

Vendored binaries are served from one of the soldr-toolchain CDNs or from a
provider-neutral immutable HTTPS blob origin configured by Forge:

- `https://zackees.github.io/soldr-toolchain/<rel>`              (Pages)
- `https://raw.githubusercontent.com/zackees/soldr-toolchain/assets/<rel>`  (raw)
- `https://media.githubusercontent.com/media/zackees/soldr-toolchain/assets/<rel>`  (LFS-backed)

Off-site objects use the content-addressed form
`https://<origin>/sha256/<first-two>/<sha256>/<filename>`. Forge performs an
unauthenticated GET and verifies the digest before publishing the catalogue;
the upstream URL and build provenance are retained in `forge-assets.json`.

Where `<rel>` MUST equal `<tool>/<version>/<platform>/<filename>` (per the
naming rules above). The linter rejects any URL whose path diverges from
this convention.

## Validation

Run `python scripts/lint_assets.py --assets-dir path/to/assets/` to check.
The linter enforces every rule in this document. Exit code 0 = clean.

## Schema: `catalogue.v1.json` (soldr#988 Phase 1)

The flat `(owner, repo, tag, asset, url, sha256)` shape produced by
`build_asset_index.py` is formalized under the v1 namespace. GitHub
release rows are looked up by `(owner, repo, tag, asset)`; locally hosted
platform bundles may reuse a stable filename like `bundle.tar.zst`, so
their unique identity is the URL:

- **Schema**: [`schemas/catalogue.v1.schema.json`](../schemas/catalogue.v1.schema.json) (JSON Schema Draft 2020-12)
- **Sample**: [`examples/catalogue.v1.json`](../examples/catalogue.v1.json)
- **Validator**: `python scripts/validate_catalogue.py <path>` (requires
  `jsonschema`, installed in CI via `uv pip install jsonschema`)
- **CI gate**: [`.github/workflows/catalogue-schema.yml`](../.github/workflows/catalogue-schema.yml)

The legacy `asset-index.json` document on the `assets` branch carries
`schema_version: 5` and a near-identical shape; the v1 catalogue starts
fresh so the catalogue product can evolve independently of the
asset-index legacy. The downstream soldr migration tracked in
[zackees/soldr#988](https://github.com/zackees/soldr/issues/988) Phase 2
flips soldr's resolver from `asset-index.json` over to `catalogue.v1.json`.

## Adding a new vendored asset

1. Decide the canonical tool name, version, platform.
2. Place the binary at `<tool>/<version>/<platform>/<filename>` (LFS-tracked).
3. Either:
   - Create or update `<tool>/manifest.json` (v1 Catalog) with a Release
     for `<version>` and a ReleasePlatform pointing at the file's CDN URL,
     OR
   - Use the producer pipeline if the tool is GitHub-derived.
4. Add the tool to top-level `manifest.json` (Index) if not already present.
5. Run `scripts/lint_assets.py` to confirm.
