#!/usr/bin/env bash
# soldr#1064 phase B — direct forge dispatch for every *-sys library
# × shape that still needs an ingested asset.
#
# Use this when the `forge-dispatch.yml` orchestrator can't run
# (e.g. the FORGE_INGEST_TOKEN secret isn't configured on this repo).
# It fans out one `gh workflow run forge-conan.yml` per (tool, shape)
# directly against `zackees/forge`, using whatever auth the caller's
# `gh auth status` reports.
#
# After each dispatched forge run completes, ingest the artifact
# locally with:
#
#   gh run download <FORGE_RUN_ID> --repo zackees/forge --dir /tmp/<lib>-<shape>
#   python scripts/forge_to_catalogue.py \
#       --forge-dir /tmp/<lib>-<shape> \
#       --tool <lib> --version <ver> --shape <shape> \
#       --forge-run-id <FORGE_RUN_ID> \
#       --assets-root /path/to/soldr-toolchain-assets
#
# Already-ingested combos that this script intentionally skips:
#   zstd 1.5.7 × 8 shapes
#   sqlite 3.46.0 × 8 shapes
#
# Versions match the pinned `lib.version` strings in `recipes/_syslib.py`.

set -euo pipefail

ALL_SHAPES=(
    windows-x64
    windows-arm64
    darwin-x64
    darwin-arm64
    linux-x64-gnu
    linux-arm64-gnu
    linux-x64-musl
    linux-arm64-musl
)

# jemalloc skips windows: tikv-jemalloc-sys recipes don't ship Windows
# binaries upstream — autotools doesn't build natively under MSVC.
JEMALLOC_SHAPES=(
    darwin-x64
    darwin-arm64
    linux-x64-gnu
    linux-arm64-gnu
    linux-x64-musl
    linux-arm64-musl
)

declare -A VERSIONS=(
    [jemalloc]=5.3.0
    [mimalloc]=3.3.2
    [zlib-ng]=2.2.5
    [lzma]=5.6.3
    [bzip2]=1.0.8
)

dry_run=0
selected_lib=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry_run=1 ;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) selected_lib="$1" ;;
    esac
    shift
done

shapes_for() {
    case "$1" in
        jemalloc) printf '%s\n' "${JEMALLOC_SHAPES[@]}" ;;
        *)        printf '%s\n' "${ALL_SHAPES[@]}" ;;
    esac
}

# Map a shape to forge-conan's per-platform input booleans. forge-conan
# accepts one boolean per platform; exactly one of them gets `true`.
shape_to_flags() {
    local shape="$1"
    declare -A flags=(
        [windows-x64]=windows_x64
        [windows-arm64]=windows_arm64
        [darwin-x64]=macos_x64
        [darwin-arm64]=macos_arm64
        [linux-x64-gnu]=linux_x64
        [linux-arm64-gnu]=linux_arm64
        [linux-x64-musl]=linux_x64_musl
        [linux-arm64-musl]=linux_arm64_musl
    )
    local on="${flags[$shape]:-}"
    [ -z "$on" ] && { echo "unknown shape: $shape" >&2; return 1; }
    for k in windows_x64 windows_arm64 macos_x64 macos_arm64 linux_x64 linux_arm64 linux_x64_musl linux_arm64_musl; do
        if [ "$k" = "$on" ]; then
            printf -- '-f %s=true ' "$k"
        else
            printf -- '-f %s=false ' "$k"
        fi
    done
}

dispatch_one() {
    local tool="$1" version="$2" shape="$3"
    local recipe_path="recipes/${tool}-${shape}"
    local name="${tool}-${shape}"
    local flags
    flags=$(shape_to_flags "$shape")
    local cmd="gh workflow run forge-conan.yml --repo zackees/forge \
        -f recipe_repo=zackees/soldr-toolchain \
        -f recipe_ref=main \
        -f recipe_path=$recipe_path \
        -f name=$name \
        -f version=$version \
        $flags"
    if [ "$dry_run" = "1" ]; then
        echo "$cmd"
    else
        echo "dispatch: $tool $version $shape"
        eval "$cmd" && sleep 1
    fi
}

for tool in "${!VERSIONS[@]}"; do
    if [ -n "$selected_lib" ] && [ "$selected_lib" != "$tool" ]; then
        continue
    fi
    version="${VERSIONS[$tool]}"
    while IFS= read -r shape; do
        dispatch_one "$tool" "$version" "$shape"
    done < <(shapes_for "$tool")
done
