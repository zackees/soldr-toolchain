"""Plan native and cross Conan producers for the reusable Forge workflow."""
from __future__ import annotations

import argparse
import json
import re
from scripts.forge_to_catalogue import TOOL_RECIPE_NAME

FLAGS = ('windows_x64', 'windows_x64_gnu', 'windows_arm64', 'linux_x64',
         'linux_arm64', 'linux_x64_musl', 'linux_arm64_musl', 'macos_x64', 'macos_arm64')
SHAPE_FLAGS = {
    'universal2':'macos_arm64', 'thin-aarch64':'macos_arm64', 'thin-x86_64':'macos_arm64',
    'darwin-arm64':'macos_arm64', 'darwin-x64':'macos_x64',
    'windows-x64':'windows_x64', 'windows-x64-gnu':'windows_x64_gnu', 'windows-arm64':'windows_arm64',
    'linux-x64-gnu':'linux_x64', 'linux-arm64-gnu':'linux_arm64',
    'linux-x64-musl':'linux_x64_musl', 'linux-arm64-musl':'linux_arm64_musl',
}
LINUX_REPACKAGERS = {'compiler-rt', 'swift', 'llvm-tools', 'cmake', 'ninja', 'uv',
                    'mingw-w64-gcc', 'gnu-linux-toolchain', 'musl-linux-toolchain'}


def build_matrix(tool: str, version: str, shape: str) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', version):
        raise ValueError('invalid version')
    if tool not in TOOL_RECIPE_NAME:
        raise ValueError(f'unknown tool {tool}')
    supported = TOOL_RECIPE_NAME[tool]
    selected = list(supported) if shape == 'all' else [shape]
    rows = []
    for candidate in selected:
        if candidate not in supported or candidate not in SHAPE_FLAGS:
            raise ValueError(f'{tool} does not support {candidate}')
        name = supported[candidate]
        flag = 'linux_x64' if tool in LINUX_REPACKAGERS else SHAPE_FLAGS[candidate]
        exact = tool in LINUX_REPACKAGERS or tool in {'cargo-chef','crgx'}
        rows.append({
            'shape':candidate, 'name':name,
            'recipe_path': 'recipes/rust-cli' if tool in {'cargo-chef','crgx'} else f'recipes/{name}',
            'recipe_selection':'exact' if exact else 'platform',
            'macos_arm64_runner':'macos-26' if tool == 'apple-sdk' and int(version.split('.')[0]) >= 26 else 'macos-15',
            **{key: key == flag for key in FLAGS},
        })
    return {'include':rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--shape', required=True)
    parser.add_argument('--dry-run', action='store_true', help='Print the plan; planning never dispatches or publishes.')
    args = parser.parse_args()
    try:
        matrix = build_matrix(args.tool, args.version, args.shape)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(matrix, separators=(',',':')))


if __name__ == '__main__':
    main()
