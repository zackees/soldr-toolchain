#!/usr/bin/env bash
# soldr#1010 Phase 3 — direct forge dispatch for the four new tool
# families (python / nodelib / openssl / llvm-tools) plus the
# xwin-cache windows-arm64 ingest gap and the mimalloc retry sweep
# from soldr#1064 Phase B.
#
# Same dispatch pattern as `dispatch_forge_syslibs.sh` in
# zackees/soldr-toolchain: fans out one `gh workflow run
# forge-conan.yml` per (recipe, shape) tuple, using the caller's
# `gh auth status` token (does NOT depend on FORGE_INGEST_TOKEN).

set -euo pipefail

declare -A VERSIONS=(
    [python]=3.13.14
    [nodelib]=22.18.0
    [openssl]=3.5.0
    [llvm-tools]=20.1.7
    [xwin-cache]=2026-06-22
    [mimalloc]=3.3.2
)

# Which shapes each tool builds. xwin-cache is special: x64 was
# ingested earlier; only arm64 needs producing. mimalloc gets a full
# retry because the soldr#1064 first sweep saw cross-compile failures
# in zig-cc compose against the autotools build.
PYTHON_SHAPES=( windows-x64 windows-arm64 darwin-x64 darwin-arm64 linux-x64-gnu linux-arm64-gnu linux-x64-musl linux-arm64-musl )
NODELIB_SHAPES=( windows-x64 windows-arm64 )
OPENSSL_SHAPES=( windows-x64 windows-arm64 )
LLVM_TOOLS_SHAPES=( linux-x64-gnu )
XWIN_CACHE_SHAPES=( windows-arm64 )
MIMALLOC_SHAPES=( windows-x64 windows-arm64 darwin-x64 darwin-arm64 linux-x64-gnu linux-arm64-gnu linux-x64-musl linux-arm64-musl )

dry_run=0
selected_lib=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry_run=1 ;;
        -h|--help)
            echo "usage: $0 [--dry-run] [<tool>]"
            echo ""
            echo "Tools: ${!VERSIONS[@]}"
            exit 0
            ;;
        *) selected_lib="$1" ;;
    esac
    shift
done

shapes_for() {
    case "$1" in
        python) printf '%s\n' "${PYTHON_SHAPES[@]}" ;;
        nodelib) printf '%s\n' "${NODELIB_SHAPES[@]}" ;;
        openssl) printf '%s\n' "${OPENSSL_SHAPES[@]}" ;;
        llvm-tools) printf '%s\n' "${LLVM_TOOLS_SHAPES[@]}" ;;
        xwin-cache) printf '%s\n' "${XWIN_CACHE_SHAPES[@]}" ;;
        mimalloc) printf '%s\n' "${MIMALLOC_SHAPES[@]}" ;;
        *) echo "unknown tool: $1" >&2; return 1 ;;
    esac
}

# Map shape → forge-conan host platform flag (only one flag flips true).
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

# llvm-tools and xwin-cache use shorter recipe-folder names than the
# (tool, shape) concatenation. Map them explicitly.
recipe_path_for() {
    local tool="$1" shape="$2"
    case "$tool" in
        llvm-tools)
            # recipes/llvm-tools-linux-x64 (no trailing -gnu)
            echo "recipes/llvm-tools-linux-x64"
            ;;
        xwin-cache)
            # recipes/xwin-cache-windows-arm64
            echo "recipes/xwin-cache-${shape}"
            ;;
        *)
            echo "recipes/${tool}-${shape}"
            ;;
    esac
}

dispatch_one() {
    local tool="$1" version="$2" shape="$3"
    local recipe_path
    recipe_path=$(recipe_path_for "$tool" "$shape")
    local name="${tool}-${shape}"
    # llvm-tools uses a non-standard recipe name.
    if [ "$tool" = "llvm-tools" ]; then
        name="llvm-tools-linux-x64"
    elif [ "$tool" = "xwin-cache" ]; then
        name="xwin-cache-${shape}"
    fi
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
