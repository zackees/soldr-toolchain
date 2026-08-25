from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest

from scripts import publisher_transaction as pt
from scripts.publisher_transaction import (
    FailureInjector,
    GenerationPlan,
    GitDataApi,
    LeaseRace,
    RefNotFound,
    PublisherTransaction,
    PublishError,
    authorize_gc,
    inventory_unsmudged_tree,
    materialize_exact,
    publish_www_snapshot,
    publish_snapshot_pair,
    fetch_verified_public_ledger,
    recover_verified_public_state,
    retention_plan,
    structural_tree,
    upload_directory,
    verified_reused_entries,
    verified_retained_entries,
    active_tree_covers,
    fetch_staging_ledger,
    github_transport,
    raw_asset_verifier,
)
from scripts.publication_model import GenerationBinding, canonical_json_bytes, canonical_json_sha256, verified_public_ledger


def test_git_lease_move_clears_checkout_auth_before_fetch_and_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 40
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        stdout = sha + "\n" if command[1:3] == ["rev-parse", "FETCH_HEAD"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(pt.subprocess, "run", run)
    mover = pt.git_force_with_lease(tmp_path, "zackees", "soldr-toolchain", "token")
    mover("refs/heads/www", sha, "b" * 40, "refs/heads/generations/g-www")

    fetch, fetch_kwargs = calls[0]
    assert fetch[:4] == ["git", "-c", "http.https://github.com/.extraheader=", "fetch"]
    assert "env" not in fetch_kwargs
    push_environment = calls[2][1]["env"]
    assert push_environment["GIT_CONFIG_COUNT"] == "2"
    assert push_environment["GIT_CONFIG_VALUE_0"] == ""
    assert push_environment["GIT_CONFIG_VALUE_1"].startswith("AUTHORIZATION: basic ")


class Fake:
    def __init__(self):
        self.calls = []
        self.refs = {
            "refs/heads/public-a": "a" * 40,
            "refs/heads/public-b": "a" * 40,
            "refs/heads/www": "a" * 40,
        }
        self.n = 0
        self.tree_rows = []

    @property
    def active(self):
        return self.refs["refs/heads/public-a"]

    @active.setter
    def active(self, value):
        self.refs["refs/heads/public-a"] = value

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET":
            if "/commits/" in path:
                return {"tree": {"sha": "f" * 40}}
            if "/trees/" in path:
                return {"tree": self.tree_rows, "truncated": False}
            ref = "refs/" + path.split("/ref/", 1)[1]
            if ref not in self.refs:
                raise RefNotFound(ref)
            return {"object": {"sha": self.refs[ref]}}
        if method == "POST" and path.endswith("/refs"):
            self.refs[body["ref"]] = body["sha"]
            return {}
        if method == "PATCH":
            ref = "refs/" + path.split("/refs/", 1)[1]
            self.refs[ref] = body["sha"]
            return {}
        self.n += 1
        return {"sha": f"{self.n:040x}"}

    def lease_move(self, ref, sha, expected, source_ref):
        self.calls.append(
            (
                "LEASE",
                ref,
                {
                    "sha": sha,
                    "expected": expected,
                    "source_ref": source_ref,
                },
            )
        )
        if self.refs.get(ref) != expected:
            raise LeaseRace(ref)
        self.refs[ref] = sha


def test_github_transport_retries_idempotent_object_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    delays: list[int] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"sha":"1111111111111111111111111111111111111111"}'

    def opener(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HTTPError("https://api.github.test", 502, "Bad Gateway", {}, None)
        return Response()

    monkeypatch.setattr("scripts.publisher_transaction.urlopen", opener)
    monkeypatch.setattr("scripts.publisher_transaction.time.sleep", delays.append)

    result = github_transport("token", api_url="https://api.github.test")(
        "POST", "/repos/o/r/git/blobs", {"content": "eA==", "encoding": "base64"},
    )

    assert result["sha"] == "1" * 40
    assert calls == 3
    assert delays == [1, 2]


def test_blob_bytes_accepts_github_wrapped_base64_but_rejects_invalid_data() -> None:
    def wrapped(method, path, body):
        assert (method, path, body) == ("GET", "/repos/o/r/git/blobs/blob", None)
        return {"encoding": "base64", "content": "aGVs\n bG8=\n"}

    assert GitDataApi("o", "r", wrapped).blob_bytes("blob") == b"hello"

    def invalid(*_args):
        return {"encoding": "base64", "content": "aGVs!bG8="}

    with pytest.raises(PublishError, match="invalid base64 blob response"):
        GitDataApi("o", "r", invalid).blob_bytes("blob")


def test_inventory_materializer_retention_and_tree() -> None:
    oid = hashlib.sha256(b"abc").hexdigest()
    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize 3\n".encode()
    assert inventory_unsmudged_tree([{"type": "blob", "path": "x", "sha": "b"}], lambda _: pointer)[0].oid_sha256 == oid

    class M:
        def materialize(self, *_):
            return b"abc"

    assert materialize_exact(M(), oid, 3) == b"abc"
    assert [x["path"] for x in structural_tree({"b": "2" * 40, "a": "1" * 40})] == ["a", "b"]
    assert retention_plan([{"generation": "old", "published_at": 0}, {"generation": "new", "published_at": 99}], 99 + 15 * 86400)["keep"] == {"old", "new"}


def test_stable_alias_is_last_and_failures_leave_it_stable() -> None:
    fake = Fake()
    api = GitDataApi("o", "r", fake)
    plan = GenerationPlan("g", "refs/heads/public-b", "refs/heads/public-a", "a" * 40, {"part": "b" * 40}, {"generation": "g"})
    for point in ("phase0.captured", "phase0.tree", "phase0.commit", "phase0.inactive", "phase0.verified"):
        fake.calls.clear()
        try:
            PublisherTransaction(api, injector=FailureInjector([point])).publish(plan, raw_verify=lambda *_: True)
        except Exception:
            pass
        assert not any(c[0] == "LEASE" for c in fake.calls)
        fake.refs.pop(plan.inactive_ref, None)
    assert PublisherTransaction(api).publish(plan, raw_verify=lambda *_: True)
    assert fake.calls[-1][0] == "LEASE"


def test_noop_recovery_rollback_and_gc_are_explicit() -> None:
    fake = Fake()
    tx = PublisherTransaction(GitDataApi("o", "r", fake))
    state = {"generation": "stable"}
    assert tx.noop(public_state=state, verify=lambda s: s == state) == state
    assert tx.metrics.noops == 1 and not fake.calls
    for bad in ({}, state):
        try:
            recover_verified_public_state(lambda: bad, lambda _: False)
        except PublishError:
            pass
        else:
            assert False
    rows = [{"generation": "g0", "published_at": 0}, {"generation": "g1", "published_at": 10}, {"generation": "g2", "published_at": 20}, {"generation": "g3", "published_at": 30}]
    assert retention_plan(rows, 30 + 15 * 86400)["delete"] == {"g0"}
    try:
        authorize_gc(rows, 30 + 15 * 86400, confirmed=False)
    except PublishError:
        pass
    else:
        assert False
    assert authorize_gc(rows, 30 + 15 * 86400, confirmed=True) == {"g0"}
    tx.rollback(stable_ref="refs/heads/public-a", verified_previous_commit="b" * 40, expected_active="a" * 40)
    assert fake.calls[-1][0] == "LEASE"


def test_raw_failure_and_lease_race_fail_closed() -> None:
    fake = Fake()
    api = GitDataApi("o", "r", fake)
    plan = GenerationPlan("g", "refs/heads/public-b", "refs/heads/public-a", "a" * 40, {}, {})
    try:
        PublisherTransaction(api).publish(plan, raw_verify=lambda *_: False)
    except Exception:
        pass
    assert not any(c[0] == "LEASE" for c in fake.calls)
    fake.active = "b" * 40
    try:
        retry = GenerationPlan("g2", plan.inactive_ref + "-2", plan.active_ref, plan.expected_active, {}, {})
        PublisherTransaction(api).publish(retry, raw_verify=lambda *_: True)
    except LeaseRace:
        pass
    else:
        assert False


def test_dry_run_records_deterministic_upload_and_never_calls_transport(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "b").write_bytes(b"b")
    (root / "a").write_bytes(b"a")
    calls = []
    api = GitDataApi("o", "r", lambda *args: calls.append(args) or {}, dry_run=True)
    assert list(upload_directory(api, root)) == ["a", "nested/b"]
    plan = GenerationPlan("g", "refs/heads/public-b", "refs/heads/public-a", "a" * 40, upload_directory(api, root), {"generation": "g"})
    PublisherTransaction(api).publish(plan, raw_verify=lambda *_: True)
    assert not calls
    assert api.requests[-1][0] == "LEASE"


def test_upload_rejects_pointer_before_a_tree_can_reference_it(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "bad").write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    try:
        upload_directory(GitDataApi("o", "r", Fake()), root)
    except PublishError:
        pass
    else:
        assert False


def test_cross_asset_part_blob_reuse_skips_blob_post_and_active_tree_skip(tmp_path: Path) -> None:
    fake = Fake()
    api = GitDataApi("o", "r", fake)
    root = tmp_path / "dedup-parts"
    root.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()
    first = root / "sha256" / ("a" * 64) / ("0001-" + digest + ".part")
    second = root / "sha256" / ("c" * 64) / ("0001-" + digest + ".part")
    first.parent.mkdir(parents=True, exist_ok=True)
    second.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    reused = {second.relative_to(root).as_posix(): "7" * 40}
    uploaded = upload_directory(api, root, reused_entries=reused)
    assert uploaded[second.relative_to(root).as_posix()] == "7" * 40
    assert sum(1 for method, path, _ in fake.calls if method == "POST" and path.endswith("/blobs")) == 1
    fake.tree_rows = [{"type": "blob", "path": path, "sha": blob} for path, blob in uploaded.items()]
    assert active_tree_covers(api, "f" * 40, uploaded)


def test_staging_ledger_binds_source_marker_and_part_tree() -> None:
    payload = b"x"
    full = hashlib.sha256(payload).hexdigest()
    blob = "7" * 40
    path = f"sha256/{full}/0001-{full}.part"
    state = {
        "schema_version": 1,
        "kind": "staging-data",
        "generation": "source-abc",
        "source": {"branch": "assets", "commit": "1" * 40, "tree": "2" * 40},
        "published_at": 123,
        "assets_by_sha256": {full: {"size_bytes": 1, "partitioner": {"version": 1, "target_bytes": 1}, "parts": [{"number": 1, "sha256": full, "size_bytes": 1, "path": path, "git_blob": blob}]}},
        "logical_assets": {"k": {"source_oid_sha256": full, "source_size_bytes": 1}},
        "parts_by_sha256": {full: {"size_bytes": 1, "git_blob": blob}},
    }
    marker = canonical_json_bytes(state)

    class Api:
        def optional_ref(self, _):
            return "c" * 40

        def commit_tree(self, _):
            return "t" * 40

        def tree_entries(self, _):
            return [{"type": "blob", "path": "publish-state.v1.json", "sha": "m"}, {"type": "blob", "path": path, "sha": blob, "size": 1}]

        def blob_bytes(self, value):
            return marker if value == "m" else payload

    found, commit = fetch_staging_ledger(Api(), generation="source-abc", source_commit="1" * 40, source_tree="2" * 40)  # type: ignore[arg-type]
    assert found["published_at"] == 123 and commit == "c" * 40
    with pytest.raises(PublishError):
        fetch_staging_ledger(Api(), generation="source-abc", source_commit="9" * 40, source_tree="2" * 40)  # type: ignore[arg-type]

    forged = dict(state)
    forged["unexpected"] = True
    forged_marker = canonical_json_bytes(forged)

    class ForgedApi(Api):
        def blob_bytes(self, value):
            return forged_marker if value == "m" else payload

    with pytest.raises(PublishError, match="unexpected shape"):
        fetch_staging_ledger(ForgedApi(), generation="source-abc", source_commit="1" * 40, source_tree="2" * 40)  # type: ignore[arg-type]

    class ExtraBlobApi(Api):
        def tree_entries(self, value):
            return super().tree_entries(value) + [{"type": "blob", "path": "stale.part", "sha": "8" * 40, "size": 1}]

    with pytest.raises(PublishError, match="undeclared blobs"):
        fetch_staging_ledger(ExtraBlobApi(), generation="source-abc", source_commit="1" * 40, source_tree="2" * 40)  # type: ignore[arg-type]


def test_raw_asset_verifier_checks_ordered_full_hash_and_each_part(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = {"p1": b"abc", "p2": b"def"}
    full = hashlib.sha256(b"abcdef").hexdigest()
    paths = {name: f"sha256/{full}/{number:04d}-{hashlib.sha256(chunks[name]).hexdigest()}.part" for number, name in enumerate(("p1", "p2"), 1)}
    mapping = {
        "size_bytes": 6,
        "partitioner": {"version": 1, "target_bytes": 3},
        "parts": [
            {"number": number, "path": paths[name], "size_bytes": 3, "sha256": hashlib.sha256(chunks[name]).hexdigest(), "git_blob": str(number) * 40} for number, name in enumerate(("p1", "p2"), 1)
        ],
    }

    class Response:
        def __init__(self, data):
            self.data, self.offset = data, 0

        def read(self, size=-1):
            if self.offset == len(self.data):
                return b""
            end = len(self.data) if size < 0 else min(len(self.data), self.offset + size)
            value, self.offset = self.data[self.offset : end], end
            return value

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(
        "scripts.publisher_transaction.urlopen",
        lambda request, timeout: Response(next(data for name, data in chunks.items() if request.full_url.endswith(paths[name]))),
    )
    assert raw_asset_verifier("https://raw.test", {full: mapping})("c" * 40, {})
    mapping["parts"].reverse()
    assert not raw_asset_verifier("https://raw.test", {full: mapping})("c" * 40, {})


def test_metadata_recovery_reuses_exact_immutable_www_commit(tmp_path: Path) -> None:
    www = tmp_path / "www"
    generation = "g"
    generation_dir = www / "generations" / generation
    generation_dir.mkdir(parents=True)
    state = {
        "generation": generation,
        "active": {"slot": "public-a", "commit": "a" * 40, "tree": "f" * 40},
        "previous": {"slot": "public-b", "commit": "b" * 40, "tree": "f" * 40},
    }
    for path in (www / "publish-state.v1.json", generation_dir / "publish-state.v1.json"):
        path.write_bytes(canonical_json_bytes(state))
    (www / "catalogue.v2.json").write_text("{}", encoding="utf-8")
    fake = Fake()
    immutable = "c" * 40
    fake.refs["refs/heads/generations/g-www"] = immutable
    fake.refs["refs/heads/www"] = immutable
    fake.tree_rows = []
    for path in www.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            fake.tree_rows.append(
                {
                    "type": "blob",
                    "path": path.relative_to(www).as_posix(),
                    "sha": hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest(),
                    "size": len(data),
                }
            )
    api = GitDataApi("o", "r", fake)
    assert (
        publish_www_snapshot(
            api,
            www_dir=www,
            generation=generation,
            active_commit="a" * 40,
            active_tree="f" * 40,
            previous_commit="b" * 40,
            previous_tree="f" * 40,
            active_slot="public-a",
            previous_slot="public-b",
            raw_verify_www=lambda commit: commit == immutable,
        )
        == immutable
    )
    assert not any(method == "POST" for method, _, _ in fake.calls)
    leases = [body for method, _, body in fake.calls if method == "LEASE"]
    assert leases == [{"sha": immutable, "expected": immutable, "source_ref": "refs/heads/generations/g-www"}]


def test_reused_parts_are_validated_by_tree_metadata_without_payload_reads() -> None:
    full, part = "a" * 64, "b" * 64
    catalogue = {"schema_version": 2, "entries": []}
    digest = canonical_json_sha256(catalogue)
    binding = GenerationBinding("g", "1" * 40, "2" * 40, "public-a", "3" * 40, "4" * 40, "public-b", "5" * 40, "6" * 40, digest)
    path = f"sha256/{full}/0001-{part}.part"
    state = {
        "generation": "g",
        "source": {"commit": "1" * 40, "tree": "2" * 40},
        "active": {"slot": "public-a", "commit": "3" * 40, "tree": "4" * 40},
        "previous": {"slot": "public-b", "commit": "5" * 40, "tree": "6" * 40},
        "catalogue_sha256": digest,
        "assets_by_sha256": {full: {"size_bytes": 3, "partitioner": {"version": 1, "target_bytes": 3}, "parts": [{"number": 1, "sha256": part, "size_bytes": 3, "path": path, "git_blob": "7" * 40}]}},
        "logical_assets": {},
    }
    ledger = verified_public_ledger(state, binding, catalogue)
    fake = Fake()
    fake.tree_rows = [{"path": path, "type": "blob", "sha": "7" * 40, "size": 3}]
    api = GitDataApi("o", "r", fake)
    assert verified_reused_entries(api, ledger) == {path: "7" * 40}
    assert verified_retained_entries(api, [ledger]) == {path: "7" * 40}
    assert not any("/blobs/" in request_path for _, request_path, _ in fake.calls)
    fake.tree_rows[0]["size"] = 2
    try:
        verified_reused_entries(api, ledger)
    except PublishError as exc:
        assert "tree metadata" in str(exc)
    else:
        assert False


def test_data_alias_precedes_www_and_raw_failure_never_publishes_metadata(tmp_path: Path) -> None:
    public = tmp_path / "public"
    www = tmp_path / "www"
    public.mkdir()
    www.mkdir()
    (public / "part").write_bytes(b"part")
    (www / "manifest.json").write_text("{}")
    for generation in ("g", "g2"):
        state = {"generation": generation, "active": {"slot": "public-a"}}
        generation_dir = www / "generations" / generation
        generation_dir.mkdir(parents=True)
        (generation_dir / "publish-state.v1.json").write_text(json.dumps(state))
    (www / "publish-state.v1.json").write_text(json.dumps({"generation": "g"}))
    fake = Fake()
    api = GitDataApi("o", "r", fake)
    try:
        publish_snapshot_pair(api, public_dir=public, www_dir=www, generation="g", active_slot="public-b", raw_verify=lambda *_: False)
    except PublishError:
        pass
    else:
        assert False
    assert not any(method == "LEASE" for method, _, _ in fake.calls)
    fake.calls.clear()
    publish_snapshot_pair(api, public_dir=public, www_dir=www, generation="g2", active_slot="public-b", raw_verify=lambda *_: True)
    patches = [(path, body) for method, path, body in fake.calls if method == "LEASE"]
    assert [path.rsplit("/", 1)[-1] for path, _ in patches] == ["public-b", "www"]
    # The target is the formerly inactive B slot; previous must be A, the
    # currently live peer slot, never B's stale pre-publication tip.
    state = json.loads((www / "publish-state.v1.json").read_text())
    assert state["active"]["slot"] == "public-b"
    assert state["previous"]["slot"] == "public-a"
    raw_state = (www / "publish-state.v1.json").read_bytes()
    assert raw_state == canonical_json_bytes(json.loads(raw_state))


def test_www_lease_is_captured_before_data_mutation_and_stale_publisher_loses(tmp_path: Path) -> None:
    public, www = tmp_path / "public", tmp_path / "www"
    public.mkdir()
    www.mkdir()
    (public / "part").write_bytes(b"part")
    (www / "publish-state.v1.json").write_text('{"generation":"g"}')
    generation_dir = www / "generations" / "g"
    generation_dir.mkdir(parents=True)
    (generation_dir / "publish-state.v1.json").write_text('{"generation":"g"}')

    class InterleavingFake(Fake):
        def lease_move(self, ref, sha, expected, source_ref):
            if ref == "refs/heads/public-b":
                super().lease_move(ref, sha, expected, source_ref)
                self.refs["refs/heads/www"] = "9" * 40
                return
            super().lease_move(ref, sha, expected, source_ref)

    fake = InterleavingFake()
    with pytest.raises(LeaseRace):
        publish_snapshot_pair(
            GitDataApi("o", "r", fake),
            public_dir=public,
            www_dir=www,
            generation="g",
            active_slot="public-b",
            raw_verify=lambda *_: True,
        )
    assert fake.refs["refs/heads/www"] == "9" * 40
    www_leases = [body for method, path, body in fake.calls if method == "LEASE" and path.endswith("/www")]
    assert www_leases[0]["expected"] == "a" * 40


def test_missing_slot_requires_explicit_bootstrap(tmp_path: Path) -> None:
    public, www = tmp_path / "public", tmp_path / "www"
    public.mkdir()
    www.mkdir()
    (public / "part").write_bytes(b"part")
    (www / "publish-state.v1.json").write_text('{"generation":"g"}')
    generation_dir = www / "generations" / "g"
    generation_dir.mkdir(parents=True)
    (generation_dir / "publish-state.v1.json").write_text('{"generation":"g"}')
    fake = Fake()
    fake.refs.pop("refs/heads/public-b")
    try:
        publish_snapshot_pair(GitDataApi("o", "r", fake), public_dir=public, www_dir=www, generation="g", active_slot="public-b", raw_verify=lambda *_: True)
    except PublishError as exc:
        assert "explicit bootstrap" in str(exc)
    else:
        assert False

    fake.refs.pop("refs/heads/public-a")
    publish_snapshot_pair(
        GitDataApi("o", "r", fake),
        public_dir=public,
        www_dir=www,
        generation="g",
        active_slot="public-b",
        raw_verify=lambda *_: True,
        bootstrap=True,
    )
    assert fake.refs["refs/heads/public-a"] == fake.refs["refs/heads/public-b"]


def test_pages_recovery_ignores_newer_www_ref_and_binds_generation_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    catalogue = {"entries": [], "schema_version": 2}
    state = {
        "generation": "old",
        "source": {"commit": "1" * 40, "tree": "2" * 40},
        "active": {"slot": "public-a", "commit": "a" * 40, "tree": "f" * 40},
        "previous": {"slot": "public-b", "commit": "b" * 40, "tree": "f" * 40},
        "catalogue_sha256": canonical_json_sha256(catalogue),
        "assets_by_sha256": {},
        "logical_assets": {},
    }
    files = {
        "publish-state.v1.json": canonical_json_bytes(state),
        "catalogue.v2.json": canonical_json_bytes(catalogue),
        "generations/old/publish-state.v1.json": canonical_json_bytes(state),
        "generations/old/catalogue.v2.json": canonical_json_bytes(catalogue),
    }

    class Api:
        def ref(self, name):
            assert name == "refs/heads/generations/old-www"  # current www must never be read
            return "c" * 40

        def commit_tree(self, commit):
            return "d" * 40 if commit == "c" * 40 else "f" * 40

        def tree_entries(self, tree):
            assert tree == "d" * 40
            return [{"type": "blob", "path": path, "sha": path} for path in files]

        def blob_bytes(self, blob):
            return files[blob]

    class Response:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def opener(request, timeout):
        return Response(files[request.full_url.removeprefix("https://pages.test/")])

    monkeypatch.setattr("scripts.publisher_transaction.urlopen", opener)
    ledger, commit, tree = fetch_verified_public_ledger(Api(), pages_base="https://pages.test")
    assert ledger.binding.generation == "old" and commit == "c" * 40 and tree == "d" * 40
