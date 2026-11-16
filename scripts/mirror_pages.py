#!/usr/bin/env python3
"""Mirror the deployed Pages tree into the forge-ci org site.

The canonical catalogue lives at https://zackees.github.io/soldr-toolchain and
that URL is baked into every released soldr, so it stays primary.  A verbatim
copy is kept at https://forge-ci.github.io/soldr-toolchain, served from the
``soldr-toolchain/`` directory of ``forge-ci/forge-ci.github.io`` (branch
``main``).  Everything outside that directory (the org landing page and the
``soldr-redirect.v1.json`` manifest) belongs to someone else and is never
touched.

Two entry points, both run after the primary deploy has been verified:

``publish``
    A new generation was just deployed: copy ``www-snapshot`` into the mirror,
    commit if anything changed, push, then wait until the mirror serves the
    same ``catalogue.v2.json`` generation.

``converge``
    Nothing was published this run.  If the mirror already serves the live
    generation, do nothing.  Otherwise re-sync it from the immutable
    ``generations/<generation>-public`` branch of the producer repository:
    that branch is the exact tree the post-deploy verifier proved is being
    served by Pages, so it is a byte-exact source that does not depend on the
    Pages artifact (which is not rebuilt on a no-op run) or on crawling the
    live site.

Stdlib only; git authentication comes from the caller's environment
(``GIT_SSH_COMMAND``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable

MIRROR_REPO = "git@github.com:forge-ci/forge-ci.github.io.git"
MIRROR_BRANCH = "main"
MIRROR_SUBDIR = "soldr-toolchain"
MIRROR_PAGES = "https://forge-ci.github.io/soldr-toolchain"
LIVE_PAGES = "https://zackees.github.io/soldr-toolchain"
SOURCE_REPO = "https://github.com/zackees/soldr-toolchain.git"
CATALOGUE = "catalogue.v2.json"

COMMIT_NAME = "soldr-toolchain publisher"
COMMIT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class MirrorError(RuntimeError):
    pass


def _log(message: str) -> None:
    print(f"[mirror] {message}", file=sys.stderr, flush=True)


def _git(cwd: Path | None, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def validate_subdir(subdir: str) -> str:
    """A single normal path segment, so a sync can never reach the site root."""
    path = PurePosixPath(subdir)
    if len(path.parts) != 1 or path.parts[0] in {"", ".", ".."} or path.is_absolute() or path.parts[0] == ".git":
        raise MirrorError(f"mirror subdir must be one plain directory name, got {subdir!r}")
    return path.parts[0]


def generation_of(document: bytes) -> str:
    value = json.loads(document).get("generation")
    if not isinstance(value, str) or not value:
        raise MirrorError("catalogue.v2.json has no generation")
    return value


def source_generation(source_dir: Path) -> str:
    return generation_of((source_dir / CATALOGUE).read_bytes())


def replace_tree(source_dir: Path, checkout: Path, subdir: str) -> None:
    """Make ``checkout/subdir`` an exact copy of ``source_dir`` (deletions too)."""
    subdir = validate_subdir(subdir)
    if not (source_dir / CATALOGUE).is_file():
        raise MirrorError(f"{source_dir} has no {CATALOGUE}; refusing to mirror an incomplete tree")
    for item in source_dir.rglob("*"):
        if item.is_symlink():
            raise MirrorError(f"refusing to mirror symlink {item}")
        if item.name == ".git":
            raise MirrorError(f"refusing to mirror nested git metadata {item}")
    target = checkout / subdir
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    shutil.copytree(source_dir, target)


def commit_snapshot(checkout: Path, subdir: str, message: str) -> bool:
    """Stage only ``subdir`` and commit it; False when there is nothing to commit."""
    subdir = validate_subdir(subdir)
    _git(checkout, "add", "--all", "--", subdir)
    if _git(checkout, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    staged = _git(checkout, "diff", "--cached", "--name-only", "-z").stdout.split("\0")
    outside = [path for path in staged if path and not path.startswith(subdir + "/")]
    if outside:
        raise MirrorError(f"refusing to commit paths outside {subdir}/: {outside}")
    _git(
        checkout,
        "-c", f"user.name={COMMIT_NAME}",
        "-c", f"user.email={COMMIT_EMAIL}",
        "commit", "--quiet", "--no-verify", "-m", message,
    )
    return True


def push_snapshot(
    source_dir: Path,
    *,
    repo: str,
    branch: str,
    subdir: str,
    message: str,
    workdir: Path,
    attempts: int = 3,
    before_push: Callable[[int], None] | None = None,
) -> bool:
    """Clone, replace ``subdir``, commit if changed, push.

    On a rejected push the snapshot is re-applied on top of the freshly fetched
    branch (equivalent to fetch+rebase, but it cannot conflict because the
    whole directory is replaced) and the push retried.  Returns whether a
    commit was pushed.
    """
    subdir = validate_subdir(subdir)
    checkout = workdir / "mirror"
    _git(None, "clone", "--quiet", "--no-tags", "--single-branch", "--branch", branch, repo, str(checkout))
    for attempt in range(1, attempts + 1):
        replace_tree(source_dir, checkout, subdir)
        if not commit_snapshot(checkout, subdir, message):
            _log(f"{subdir}/ already matches the snapshot; nothing to push")
            return False
        if before_push is not None:
            before_push(attempt)
        pushed = _git(checkout, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", check=False)
        if pushed.returncode == 0:
            _log(f"pushed {_git(checkout, 'rev-parse', 'HEAD').stdout.strip()} to {branch}")
            return True
        _log(f"push attempt {attempt}/{attempts} rejected: {pushed.stderr.strip()}")
        if attempt == attempts:
            break
        _git(checkout, "fetch", "--quiet", "origin", branch)
        _git(checkout, "reset", "--quiet", "--hard", "FETCH_HEAD")
    raise MirrorError(f"could not push mirror snapshot after {attempts} attempts")


def fetch_url(url: str, timeout: float = 30.0) -> bytes:
    # A unique query string sidesteps the Pages CDN's 10 minute max-age.
    busted = f"{url}{'&' if '?' in url else '?'}mirror-check={time.time_ns()}"
    request = urllib.request.Request(busted, headers={"Cache-Control": "no-cache", "User-Agent": "soldr-toolchain-mirror"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def served_generation(pages_base: str, fetch: Callable[[str], bytes] = fetch_url) -> str | None:
    try:
        return generation_of(fetch(f"{pages_base.rstrip('/')}/{CATALOGUE}"))
    except (urllib.error.URLError, OSError, ValueError, MirrorError) as exc:
        _log(f"could not read {pages_base}/{CATALOGUE}: {exc}")
        return None


def wait_for_generation(
    pages_base: str,
    expected: str,
    *,
    timeout: float,
    interval: float = 20.0,
    fetch: Callable[[str], bytes] = fetch_url,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = clock() + timeout
    while True:
        seen = served_generation(pages_base, fetch)
        if seen == expected:
            _log(f"{pages_base} serves {expected}")
            return
        if clock() >= deadline:
            raise MirrorError(f"{pages_base} still serves {seen!r}, expected {expected!r} after {timeout:.0f}s")
        _log(f"{pages_base} serves {seen!r}, waiting for {expected!r}")
        sleep(interval)


def export_commit_tree(repo: str, ref: str, expected_commit: str | None, dest: Path) -> None:
    """Materialize the exact tree of ``ref`` (verified against ``expected_commit``)."""
    git_dir = dest.parent / (dest.name + ".git")
    _git(None, "init", "--quiet", "--bare", str(git_dir))
    _git(None, f"--git-dir={git_dir}", "fetch", "--quiet", "--depth=1", "--no-tags", repo, ref)
    commit = _git(None, f"--git-dir={git_dir}", "rev-parse", "FETCH_HEAD^{commit}").stdout.strip()
    if expected_commit and commit != expected_commit:
        raise MirrorError(f"{ref} is {commit}, but the verifier proved {expected_commit}")
    dest.mkdir(parents=True)
    _git(
        None,
        f"--git-dir={git_dir}",
        f"--work-tree={dest}",
        "-c", "core.autocrlf=false",
        "checkout", "--quiet", "--force", "FETCH_HEAD", "--", ".",
    )
    shutil.rmtree(git_dir)


def _commit_message(generation: str, origin: str) -> str:
    return f"soldr-toolchain: mirror generation {generation}\n\nSource: {origin}\n"


def publish(args: argparse.Namespace, source_dir: Path, generation: str, origin: str) -> int:
    actual = source_generation(source_dir)
    if actual != generation:
        raise MirrorError(f"snapshot is generation {actual!r}, expected {generation!r}")
    with tempfile.TemporaryDirectory(prefix="mirror-") as tmp:
        push_snapshot(
            source_dir,
            repo=args.mirror_repo,
            branch=args.mirror_branch,
            subdir=args.mirror_subdir,
            message=_commit_message(generation, origin),
            workdir=Path(tmp),
            attempts=args.push_attempts,
        )
    wait_for_generation(args.mirror_pages, generation, timeout=args.timeout, interval=args.interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mirror-repo", default=MIRROR_REPO)
    parser.add_argument("--mirror-branch", default=MIRROR_BRANCH)
    parser.add_argument("--mirror-subdir", default=MIRROR_SUBDIR)
    parser.add_argument("--mirror-pages", default=MIRROR_PAGES)
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for the mirror to serve the generation")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--push-attempts", type=int, default=3)
    sub = parser.add_subparsers(dest="command", required=True)

    pub = sub.add_parser("publish", help="mirror a freshly deployed www snapshot")
    pub.add_argument("--source-dir", type=Path, required=True)
    pub.add_argument("--generation", required=True)

    con = sub.add_parser("converge", help="re-sync the mirror if it lags the live site")
    con.add_argument("--generation", required=True, help="generation the post-deploy verifier proved live")
    con.add_argument("--www-commit", required=True, help="generation-www commit the verifier bound to that generation")
    con.add_argument("--live-pages", default=LIVE_PAGES)
    con.add_argument("--source-repo", default=SOURCE_REPO)

    args = parser.parse_args(argv)
    args.mirror_subdir = validate_subdir(args.mirror_subdir)
    try:
        if args.command == "publish":
            return publish(args, args.source_dir, args.generation, "www-snapshot Pages artifact")
        live = served_generation(args.live_pages)
        if live != args.generation:
            raise MirrorError(f"{args.live_pages} serves {live!r}, but the verifier proved {args.generation!r}")
        mirrored = served_generation(args.mirror_pages)
        if mirrored == args.generation:
            _log(f"mirror already serves {mirrored}; nothing to do")
            return 0
        _log(f"mirror serves {mirrored!r}, live is {args.generation!r}; re-syncing")
        ref = f"refs/heads/generations/{args.generation}-public"
        with tempfile.TemporaryDirectory(prefix="mirror-src-") as tmp:
            source = Path(tmp) / "www"
            export_commit_tree(args.source_repo, ref, args.www_commit, source)
            return publish(args, source, args.generation, f"{ref} @ {args.www_commit}")
    except (MirrorError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else ""
        print(f"mirror failed: {exc} {detail}".rstrip(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
