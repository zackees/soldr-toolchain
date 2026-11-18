"""Fetch verified public catalogue units for a portable Linux cross producer."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from scripts.generation_reader import PublishedEntry, read_verified_generation

CATALOGUE_URL = 'https://zackees.github.io/soldr-toolchain/catalogue.v2.json'
LLVM_UNIT = 'llvm-tools/21.1.5/linux-x64-gnu/bundle.tar.zst'
SDK_UNIT = 'apple-sdk/14.5/darwin-aarch64/sdk.tar.zst'


def open_url(url: str):
    if urlsplit(url).scheme != 'https':
        raise ValueError('catalogue transport must be HTTPS')
    return urllib.request.urlopen(url, timeout=180)


def json_url(url: str) -> dict:
    with open_url(url) as response:
        return json.load(response)


def download_entry(entry: PublishedEntry, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    components = entry.parts or [{'number':1, 'sha256':entry.sha256, 'size_bytes':entry.size_bytes, 'urls':entry.urls}]
    whole = hashlib.sha256()
    total = 0
    with destination.open('wb') as output:
        for component in components:
            part = destination.with_name(destination.name + f'.part-{component["number"]}')
            digest = hashlib.sha256()
            size = 0
            with open_url(component['urls'][0]) as response, part.open('wb') as target:
                for block in iter(lambda: response.read(1024 * 1024), b''):
                    size += len(block)
                    if size > component['size_bytes']:
                        raise ValueError('part size exceeds catalogue declaration')
                    digest.update(block)
                    target.write(block)
            if size != component['size_bytes'] or digest.hexdigest() != component['sha256']:
                raise ValueError('part size or SHA256 differs from catalogue')
            with part.open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    whole.update(block)
                    output.write(block)
                    total += len(block)
    if total != entry.size_bytes or whole.hexdigest() != entry.sha256:
        raise ValueError('assembled unit size or SHA256 differs from catalogue')
    return destination


def extract_unit(archive: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    # zstd is a producer tool, not a runtime Python dependency.
    with subprocess.Popen(['zstd','-d','--stdout',str(archive)], stdout=subprocess.PIPE) as decoder:
        try:
            with tarfile.open(fileobj=decoder.stdout, mode='r|') as stream:
                stream.extractall(output, filter='data')
        except BaseException:
            decoder.terminate()
            raise
        # tar EOF may precede padding in the decoded stream. Drain it before
        # waiting, otherwise the decoder can block on a full stdout pipe.
        for _ in iter(lambda: decoder.stdout.read(1024 * 1024), b''):
            pass
        if decoder.wait() != 0:
            raise RuntimeError('zstd unit decompression failed')


def provision(output: Path, catalogue_url: str = CATALOGUE_URL, *, llvm_only: bool = False) -> dict:
    catalogue = json_url(catalogue_url)
    state = json_url(catalogue['publication_state']['url'])
    generation = read_verified_generation(catalogue, state)
    records = []
    inputs = [('llvm',LLVM_UNIT)] if llvm_only else [('llvm',LLVM_UNIT), ('sdk',SDK_UNIT)]
    for kind, unit in inputs:
        matches = [entry for entry in generation.entries if entry.asset == unit]
        if len(matches) != 1:
            raise ValueError(f'expected one published unit {unit}, found {len(matches)}')
        entry = matches[0]
        archive = download_entry(entry, output / f'{kind}.tar.zst')
        extract_unit(archive, output / kind)
        records.append({'id':unit,'sha256':entry.sha256,'size_bytes':entry.size_bytes})
    result = {
        'generation':generation.generation, 'units':records,
        'llvm_root':str((output/'llvm/package').resolve()),
        'sdk_root':str((output/'sdk/package/sdk').resolve()),
    }
    (output/'provenance.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--catalogue-url', default=CATALOGUE_URL)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps({'catalogue':args.catalogue_url,'units':[LLVM_UNIT,SDK_UNIT],'output':str(args.output)}))
        return
    print(json.dumps(provision(args.output, args.catalogue_url)))


if __name__ == '__main__':
    main()
