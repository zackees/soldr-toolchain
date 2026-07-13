#!/usr/bin/env python3
"""Provider-neutral immutable blob upload and public verification helpers."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

def digest_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size

def object_key(digest: str, filename: str) -> str:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("digest must be lowercase sha256")
    safe = Path(filename).name
    if safe != filename or not safe:
        raise ValueError("filename must be a single path component")
    return f"sha256/{digest[:2]}/{digest}/{safe}"

def public_url(origin: str, key: str) -> str:
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("blob origin must be HTTPS")
    return urljoin(origin.rstrip("/") + "/", key)

def verify_public(url: str, expected_digest: str, expected_size: int) -> None:
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    h = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=120) as response:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    if h.hexdigest() != expected_digest or size != expected_size:
        raise ValueError(f"public blob verification failed for {url}")

def upload(path: Path, *, origin: str, helper: str) -> dict:
    digest, size = digest_file(path)
    key = object_key(digest, path.name)
    url = public_url(origin, key)
    request = {"source_path": str(path), "object_key": key, "sha256": digest,
               "size_bytes": size, "content_type": "application/octet-stream", "create_only": True}
    proc = subprocess.run([helper], input=json.dumps(request), text=True,
                          capture_output=True, check=False)
    if proc.returncode:
        raise RuntimeError("blob upload helper failed")
    try:
        response = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("blob upload helper returned invalid JSON") from exc
    returned = response.get("url")
    if returned != url or not returned.startswith(origin.rstrip("/") + "/") or key not in returned:
        raise ValueError("upload helper returned a URL outside the immutable origin")
    verify_public(returned, digest, size)
    return {"url": returned, "object_key": key, "sha256": digest, "size_bytes": size}

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--origin", default=os.environ.get("SOLDR_BLOB_PUBLIC_ORIGIN", ""))
    parser.add_argument("--helper", default=os.environ.get("SOLDR_BLOB_UPLOAD_HELPER", ""))
    args = parser.parse_args()
    if not args.origin or not args.helper:
        raise SystemExit("--origin and --helper (or SOLDR_BLOB_*) are required")
    print(json.dumps(upload(args.path, origin=args.origin, helper=args.helper), sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
