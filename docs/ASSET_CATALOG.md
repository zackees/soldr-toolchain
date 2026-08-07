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

Run `uv run --group dev python -m scripts.lint_assets --assets-dir path/to/assets/` to check.
The linter enforces every rule in this document. Exit code 0 = clean.

## Schema: `catalogue.v1.json` (soldr#988 Phase 1)

The flat `(owner, repo, tag, asset, url, sha256)` shape produced by
`build_asset_index.py` is formalized under the v1 namespace. GitHub
release rows are looked up by `(owner, repo, tag, asset)`; locally hosted
platform bundles may reuse a stable filename like `bundle.tar.zst`, so
their unique identity is the URL:

- **Schema**: [`schemas/catalogue.v1.schema.json`](../schemas/catalogue.v1.schema.json) (JSON Schema Draft 2020-12)
- **Sample**: [`examples/catalogue.v1.json`](../examples/catalogue.v1.json)
- **Validator**: `uv run --group dev python -m scripts.validate_catalogue <path>` (requires
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

## External / curated entries (private-bytes tools)

`catalogue.v1.json` is the single public discovery manifest for **every**
soldr tool, including tools whose bytes cannot be publicly redistributed.
The manifest entry (owner/repo/tag/asset/url/sha256) is not sensitive —
it's a pointer plus a checksum — so it is safe to publish even when the
`url` resolves to a private release asset that a request without a token
cannot download.

### Why `msvc` is like this

The MSVC toolchain (cl.exe, link.exe, the MSVC STL, Windows SDK headers)
is licensed by Microsoft and cannot be redistributed from a public
location under the terms soldr-toolchain otherwise vendors tools with.
The bundle is instead published as a **private** GitHub release asset on
`zackees/soldr-toolchain-private`, and only the manifest row — including
its sha256 — is exposed via the public `catalogue.v1.json`. This keeps
`msvc` discoverable and verifiable through the same catalogue every other
tool uses, without redistributing bytes soldr-toolchain doesn't have the
right to host publicly. Consumers who are entitled to the toolchain (e.g.
via their own Visual Studio / Build Tools license) authenticate with a
token to fetch the private asset; everyone else can still see that the
tool exists, which version, and what its verified hash is.

### Mechanism: `external-entries.v1.json` on `main`

`.github/workflows/refresh-manifest.yml` regenerates `catalogue.v1.json`
every night from two sources: GitHub-release inventories
(`build_manifest.py`) and locally vendored blobs discovered by walking the
`assets` tree. Neither source can produce a curated entry like `msvc` — it
has no public release inventory and no on-disk blob. A hand-edit of the
generated `assets/catalogue.v1.json` would therefore be **silently dropped
by the next nightly run**.

Instead, curated entries are checked into
[`external-entries.v1.json`](../external-entries.v1.json) on `main`. It
uses the exact same top-level shape as `catalogue.v1.json`
(`{"schema_version": 1, "entries": [...]}]`) and validates against the
same [`schemas/catalogue.v1.schema.json`](../schemas/catalogue.v1.schema.json)
via the same validator: `uv run --group dev python -m scripts.validate_catalogue external-entries.v1.json`.

`scripts/build_catalogue_v1.py` accepts an optional `--external-entries
<path>` flag. When given, it loads the document
(`load_external_entries()`), and `transform()` appends each entry after
the generated entries — **inside the generator**, not as a post-hoc patch
— so curated entries survive the nightly regeneration by construction.
`refresh-manifest.yml` always passes
`--external-entries main/external-entries.v1.json` (the `main` checkout
of this repo is already available in that job).

Duplicate safety: `transform()` tracks every generated entry's `url` and
raises `ValueError` (the CLI then exits 1) if an external entry's `url`
collides with one already produced by the generator, rather than silently
emitting a duplicate row. Add a new curated entry by appending to
`external-entries.v1.json`'s `entries` array; CI
(`.github/workflows/catalogue-schema.yml`) schema-validates the file on
every PR, and `tests/test_build_catalogue_v1.py` covers the merge +
duplicate-rejection contract plus a drift guard on the checked-in file's
current contents.

Note for `scripts/lint_assets.py`: its R8/R9 rules only resolve URLs that
point at a soldr-toolchain CDN host (`CDN_HOSTS` in that script). A
private `api.github.com` URL like the `msvc` entry's doesn't match any of
those hosts, so `_url_to_rel()` returns `None` and the entry is skipped as
"external URL — out of scope" — the linter already tolerates
catalogue entries with no corresponding on-disk file, no changes needed.

### Consumer auth contract

A request for a private release asset URL
(`https://api.github.com/repos/<owner>/<repo>/releases/assets/<id>`)
must be authenticated:

- Set `SOLDR_TOOLCHAIN_AUTH_TOKEN` to a GitHub token with read access to
  the private repo (e.g. a fine-grained PAT scoped to
  `zackees/soldr-toolchain-private`, or a GitHub App installation token).
- Send `Authorization: Bearer $SOLDR_TOOLCHAIN_AUTH_TOKEN` and
  `Accept: application/octet-stream` on the request to the `url` field.
- Without a token (or with an unauthorized one), GitHub returns `404 Not
  Found` — not `401`/`403` — for private release assets. Treat any
  non-2xx response from this endpoint as "not entitled," not as a broken
  catalogue entry.
- On success, the API responds with a `302` redirect to a signed,
  time-limited `objects.githubusercontent.com` URL. **Do not forward the
  `Authorization` header when following that redirect** — GitHub's signed
  object URLs reject requests that carry it. Modern HTTP clients strip
  `Authorization` by default on a cross-host redirect (curl ≥7.58's
  built-in redirect handling, Python `requests`, and fetch-spec-compliant
  clients all do this), so the common case is already safe. Still verify
  your client actually does this before relying on it, especially if
  you're using a raw/manual redirect follower (e.g. reading the `Location`
  header and issuing the second request yourself) rather than the
  client's built-in redirect handling — in that case you must explicitly
  drop the header on the second request rather than assuming it's
  stripped for you.
- Verify the downloaded bytes against the catalogue entry's `sha256`
  before use, exactly as for any other catalogue entry.
