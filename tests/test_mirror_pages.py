from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import mirror_pages as mp

WORKFLOW = Path(mp.__file__).resolve().parents[1] / ".github" / "workflows" / "publish-multipart.yml"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, check=True, text=True, capture_output=True,
    ).stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _snapshot(root: Path, generation: str, extra: dict[str, str] | None = None) -> Path:
    _write(root, {"catalogue.v2.json": json.dumps({"generation": generation}), ".nojekyll": "", **(extra or {})})
    return root


@pytest.fixture
def mirror_remote(tmp_path: Path) -> Path:
    """Bare remote seeded like forge-ci.github.io: root files plus an old mirror dir."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main")
    _write(seed, {
        ".nojekyll": "",
        "index.html": "<html>org</html>",
        "soldr-redirect.v1.json": "{}",
        "soldr-toolchain/catalogue.v2.json": json.dumps({"generation": "old"}),
        "soldr-toolchain/generations/old/stale.json": "{}",
    })
    _git(seed, "add", "--all")
    _git(seed, "commit", "--quiet", "-m", "seed")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(remote))
    return remote


def _remote_files(remote: Path, tmp_path: Path, name: str) -> dict[str, str]:
    out = tmp_path / name
    _git(tmp_path, "clone", "--quiet", str(remote), str(out))
    return {
        p.relative_to(out).as_posix(): p.read_text(encoding="utf-8")
        for p in out.rglob("*") if p.is_file() and ".git" not in p.relative_to(out).parts
    }


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "/abs", ".git"])
def test_subdir_must_be_single_plain_segment(bad: str) -> None:
    with pytest.raises(mp.MirrorError):
        mp.validate_subdir(bad)


def test_replace_tree_deletes_stale_files_and_keeps_siblings(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _write(checkout, {"index.html": "keep", "soldr-toolchain/stale.json": "x", "soldr-toolchain/catalogue.v2.json": "{}"})
    source = _snapshot(tmp_path / "src", "g2", {"generations/g2/catalogue.v2.json": "{}"})
    mp.replace_tree(source, checkout, "soldr-toolchain")
    assert (checkout / "index.html").read_text() == "keep"
    assert not (checkout / "soldr-toolchain" / "stale.json").exists()
    assert (checkout / "soldr-toolchain" / "generations" / "g2" / "catalogue.v2.json").is_file()
    assert (checkout / "soldr-toolchain" / ".nojekyll").is_file()


def test_replace_tree_refuses_incomplete_or_symlinked_source(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(mp.MirrorError):
        mp.replace_tree(empty, tmp_path / "checkout", "soldr-toolchain")
    source = _snapshot(tmp_path / "src", "g")
    (source / "link").symlink_to("/etc/passwd")
    with pytest.raises(mp.MirrorError):
        mp.replace_tree(source, tmp_path / "checkout", "soldr-toolchain")


def test_push_snapshot_replaces_only_subdir_and_is_idempotent(tmp_path: Path, mirror_remote: Path) -> None:
    source = _snapshot(tmp_path / "src", "g2", {"generations/g2/x.json": "{}"})
    assert mp.push_snapshot(source, repo=str(mirror_remote), branch="main", subdir="soldr-toolchain",
                            message="mirror g2", workdir=tmp_path / "w1")
    files = _remote_files(mirror_remote, tmp_path, "after")
    assert files["index.html"] == "<html>org</html>"
    assert files["soldr-redirect.v1.json"] == "{}"
    assert ".nojekyll" in files
    assert json.loads(files["soldr-toolchain/catalogue.v2.json"]) == {"generation": "g2"}
    assert "soldr-toolchain/generations/old/stale.json" not in files
    assert "soldr-toolchain/generations/g2/x.json" in files
    assert "mirror g2" in _git(mirror_remote, "log", "-1", "--format=%B")
    # Same tree again: no commit, no push.
    head = _git(mirror_remote, "rev-parse", "main")
    assert not mp.push_snapshot(source, repo=str(mirror_remote), branch="main", subdir="soldr-toolchain",
                                message="mirror g2", workdir=tmp_path / "w2")
    assert _git(mirror_remote, "rev-parse", "main") == head


def test_push_snapshot_retries_after_concurrent_root_change(tmp_path: Path, mirror_remote: Path) -> None:
    other = tmp_path / "other"
    _git(tmp_path, "clone", "--quiet", str(mirror_remote), str(other))

    def race(attempt: int) -> None:
        if attempt == 1:
            _write(other, {"index.html": "<html>updated by someone else</html>"})
            _git(other, "commit", "--quiet", "-am", "concurrent")
            _git(other, "push", "--quiet", "origin", "main")

    source = _snapshot(tmp_path / "src", "g3")
    assert mp.push_snapshot(source, repo=str(mirror_remote), branch="main", subdir="soldr-toolchain",
                            message="mirror g3", workdir=tmp_path / "w", before_push=race)
    files = _remote_files(mirror_remote, tmp_path, "after")
    assert files["index.html"] == "<html>updated by someone else</html>"
    assert json.loads(files["soldr-toolchain/catalogue.v2.json"]) == {"generation": "g3"}


def test_push_snapshot_gives_up_after_bounded_attempts(tmp_path: Path, mirror_remote: Path) -> None:
    other = tmp_path / "other"
    _git(tmp_path, "clone", "--quiet", str(mirror_remote), str(other))

    def always_race(attempt: int) -> None:
        _write(other, {"index.html": f"race {attempt}"})
        _git(other, "commit", "--quiet", "-am", f"race {attempt}")
        _git(other, "push", "--quiet", "origin", "main")

    with pytest.raises(mp.MirrorError):
        mp.push_snapshot(_snapshot(tmp_path / "src", "g4"), repo=str(mirror_remote), branch="main",
                         subdir="soldr-toolchain", message="m", workdir=tmp_path / "w",
                         attempts=2, before_push=always_race)


def test_export_commit_tree_checks_expected_commit(tmp_path: Path, mirror_remote: Path) -> None:
    head = _git(mirror_remote, "rev-parse", "main")
    dest = tmp_path / "exported"
    mp.export_commit_tree(str(mirror_remote), "refs/heads/main", head, dest)
    assert (dest / "soldr-toolchain" / "catalogue.v2.json").is_file()
    assert not (dest / ".git").exists()
    with pytest.raises(mp.MirrorError):
        mp.export_commit_tree(str(mirror_remote), "refs/heads/main", "0" * 40, tmp_path / "other")


def test_wait_for_generation_polls_until_match() -> None:
    answers = iter([b"not json", json.dumps({"generation": "old"}).encode(), json.dumps({"generation": "new"}).encode()])
    now = [0.0]
    mp.wait_for_generation("https://m", "new", timeout=100, interval=10,
                           fetch=lambda url: next(answers), clock=lambda: now[0],
                           sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert now[0] == 20


def test_wait_for_generation_times_out() -> None:
    now = [0.0]
    with pytest.raises(mp.MirrorError):
        mp.wait_for_generation("https://m", "new", timeout=60, interval=20,
                               fetch=lambda url: json.dumps({"generation": "old"}).encode(),
                               clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert now[0] == 60


def test_workflow_mirror_runs_after_verifier_with_pinned_host_key() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    verify = text.index("name: Verify deployed Pages evidence")
    mirror = text.index("name: Mirror Pages to forge-ci.github.io")
    assert verify < mirror
    assert "steps.verify.outcome == 'success'" in text
    assert "FORGE_CI_MIRROR_DEPLOY_KEY" in text
    assert "IdentitiesOnly=yes" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" in text
    assert "ssh-keyscan" not in text
