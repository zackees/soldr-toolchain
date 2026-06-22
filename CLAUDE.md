# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Managed with [uv](https://docs.astral.sh/uv/) — all Python goes through `uv run`, no manual venv activation.

```sh
# Run the full test suite
uv run --group dev pytest

# Run one test file / one test
uv run --group dev pytest tests/test_build_manifest.py
uv run --group dev pytest tests/test_build_manifest.py::test_derive_platform_key_linux

# Rebuild the catalogue locally (writes into a sibling `assets`-branch checkout)
uv run python scripts/build_manifest.py \
    --output-dir ../soldr-toolchain-assets \
    --repo-root ../soldr

uv run python scripts/build_asset_index.py \
    --manifest-checkout ../soldr-toolchain-assets \
    --output ../soldr-toolchain-assets/asset-index.json \
    --branch assets \
    --offline       # omit to do the SHA256SUMS HTTP fetch pass

# Resolve a single asset URL out of the published manifest
uv run python scripts/tool_query.py --platform linux --arch x86 --extra musl zccache
```

`pyproject.toml` puts `scripts/` on `sys.path` (see `[tool.pytest.ini_options]`), so tests `import build_manifest` directly — no `sys.path.insert` hacks.

## Architecture

This repo is a producer for a public JSON catalogue of third-party tool release assets, consumed by [`zackees/soldr`](https://github.com/zackees/soldr)'s CI. The split is the central design constraint:

### Two-branch layout

| Branch  | Role                       | Contents                                             |
| ------- | -------------------------- | ---------------------------------------------------- |
| `main`  | code only                  | producer scripts, tests, workflows, pyproject        |
| `assets`| data only (orphan, has LFS)| `manifest.json`, per-tool `manifest.json`, `asset-index.json`, LFS blobs under `<tool>/<version>/<platform>/` |

`main` has zero data files and zero LFS. `assets` has zero Python code. Producer scripts on `main` write *into* an on-disk checkout of `assets` passed via `--output-dir`. The nightly workflow checks out both refs into sibling worktrees and runs the scripts across them.

### Why this exists

soldr's parallel CI matrix used to burn the unauthenticated 60-req/hour GitHub Releases API quota on every workflow run. This repo's nightly job queries the API **once per tool** (authenticated, 5000/hr) and writes the resolved `browser_download_url` for every asset into the `assets` branch. Consumer workflows then fetch from `raw.githubusercontent.com` — CDN-served, no API quota.

### Producer scripts (`scripts/`)

- **`build_manifest.py`** — Queries GitHub Releases for each tracked tool (`zccache`, `crgx`, `cargo-chef`, `cargo-zigbuild`, `cargo-xwin`) and writes per-tool `<tool>/manifest.json` plus the top-level index. Pinned tool versions are read **directly from Rust source constants** in a sibling `zackees/soldr` checkout (`crates/soldr-cli/src/fetch/{mod.rs,known_tools.rs}`), so the catalogue can never drift from what soldr would fetch at runtime. `cargo-zigbuild` and `cargo-xwin` are unpinned (always "latest"). Uses `write_if_changed` so unchanged files don't churn `git status`.
- **`build_asset_index.py`** — Rebuilds the flat `asset-index.json` consumed by soldr's `crates/soldr-cli/src/fetch/manifest_lookup.rs`. Two data sources: (1) on-disk LFS blobs hashed locally to sha256, (2) GitHub releases that ship a `SHA256SUMS` asset (fetched + parsed). Releases without `SHA256SUMS` skip silently and the runtime falls back to the live API. `--offline` skips the network pass.
- **`tool_query.py`** — Helper for translating friendly CLI args (`--platform mac --arch arm`) into the manifest's normalized platform keys.

### Schema v5 platform keys

Each release's `platforms` map is keyed `<os>-<arch>[-<extra>]`, with `os ∈ {linux, darwin, windows}`, `arch ∈ {x64, arm64, universal2}`, `extra ∈ {gnu, musl, msvc, gnullvm, …}`. Modern short names (`x64` not `x86_64`, `arm64` not `aarch64`). **32-bit lanes (i686 / armv7) are deliberately not surfaced.** The translation lives in `build_manifest.py::derive_platform_key`; any new upstream tool with idiosyncratic filenames should be handled there rather than at the consumer.

### Vendored entries

The Apple SDK at `apple-sdk/MacOSX11.3/darwin/sdk.tar.zstd` is populated manually (not from GitHub Releases). `preserve_vendored_top_level_entries` in `build_manifest.py` re-adds these to the top-level index on every refresh so the nightly job doesn't wipe them. Entries whose `path` no longer exists on disk are silently dropped.

### Runtime dependency policy

Producer scripts use **stdlib only** (`urllib`, `hashlib`, `json`, `argparse`) — `pyproject.toml` has empty `dependencies = []`. This is deliberate so the nightly workflow runs on stock `python3` with no install step. Don't add runtime deps. `pytest` is the only dev dep.

### Tests (`tests/`)

- `test_build_manifest.py`, `test_build_asset_index.py`, `test_tool_query.py` — pure-function unit tests, run anywhere.
- `test_parity.py` — asserts the checked-in `assets`-branch catalogue agrees with what the producers would re-derive. Auto-discovers an assets checkout via, in order:
  1. `$SOLDR_TOOLCHAIN_ASSETS_DIR`
  2. `../soldr-toolchain-assets`
  3. `../assets`

  If none resolves, parity tests **skip** (don't fail) and the pure-function tests still run. CI (`tests.yml`) sets `SOLDR_TOOLCHAIN_ASSETS_DIR` to a sibling checkout with `lfs: true` so on-disk blob hashes are real.

### Workflows (`.github/workflows/`)

- `refresh-manifest.yml` — Nightly at 06:30 UTC. Checks out three refs (`main`, `assets`, `zackees/soldr@main`), runs both producer scripts, commits any diff back to `assets`. Empty diff → no commit. Auth via `secrets.GITHUB_TOKEN`.
- `tests.yml` — Runs `uv run --group dev pytest` on every push/PR with the `assets` branch checked out for parity tests.
