"""Git-Data-API transaction primitives for the atomic multipart publisher.

This is intentionally a small adapter layer: planning is pure, every remote
write is explicit, and callers can substitute an in-memory transport in tests.
It does not invoke git, LFS, or a shell.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.publication_model import (
    GenerationBinding,
    VerifiedPublicLedger,
    canonical_json_bytes,
    parse_lfs_pointer,
    validate_asset_mapping,
    validate_verified_public_ledger,
    verified_public_ledger,
)


class PublishError(RuntimeError):
    pass


class LeaseRace(PublishError):
    """The stable ref changed after its expected value was captured."""


class RefNotFound(PublishError):
    """A Git ref is absent; bootstrap may create only the expected stable refs."""


class PublicStateAbsent(PublishError):
    """Pages has no stable state at all; the only recoverable bootstrap case."""


class FailureInjector:
    """Deterministic phase-boundary failure injection for recovery tests."""

    def __init__(self, fail_at: Iterable[str] = ()) -> None:
        self.fail_at = set(fail_at)
        self.seen: list[str] = []

    def checkpoint(self, name: str) -> None:
        self.seen.append(name)
        if name in self.fail_at:
            raise PublishError("injected failure at " + name)


class Transport(Protocol):
    def __call__(self, method: str, path: str, body: Mapping[str, Any] | None) -> Any: ...


class LeaseMover(Protocol):
    def __call__(self, ref: str, sha: str, expected: str, source_ref: str | None) -> None: ...


def github_transport(token: str, *, api_url: str = "https://api.github.com") -> Transport:
    """Return the small stdlib-only GitHub JSON transport used by the CLI.

    It is constructed, rather than globally configured, so unit tests always
    inject a fake and a dry run never opens a socket.
    """
    if not token:
        raise PublishError("GITHUB_TOKEN is required for --publish")
    base = api_url.rstrip("/")

    def request(method: str, path: str, body: Mapping[str, Any] | None) -> Any:
        data = None if body is None else canonical_json_bytes(body)
        headers = {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + token, "X-GitHub-Api-Version": "2022-11-28"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(base + path, data=data, headers=headers, method=method), timeout=60) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 404:
                raise RefNotFound(f"GitHub Git Data API {method} {path} returned 404") from exc
            raise PublishError(f"GitHub Git Data API {method} {path} failed: {exc}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise PublishError(f"GitHub Git Data API {method} {path} failed: {exc}") from exc
        return parsed

    return request


def git_force_with_lease(repo_dir: Path, owner: str, repo: str, token: str) -> LeaseMover:
    """Create a server-enforced ref compare-and-swap using Git's lease."""
    checkout = Path(repo_dir).resolve()
    remote = f"https://github.com/{owner}/{repo}.git"
    credential = base64.b64encode(("x-access-token:" + token).encode()).decode("ascii")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + credential,
        }
    )

    def move(ref: str, sha: str, expected: str, source_ref: str | None) -> None:
        fetched = subprocess.run(
            ["git", "fetch", "--no-tags", remote, source_ref or sha],
            cwd=checkout,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if fetched.returncode:
            raise PublishError("cannot fetch immutable publication commit for leased ref move")
        observed = subprocess.run(
            ["git", "rev-parse", "FETCH_HEAD"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
        )
        if observed.returncode or observed.stdout.strip() != sha:
            raise PublishError("fetched publication ref does not resolve to expected commit")
        pushed = subprocess.run(
            ["git", "push", "--porcelain", remote, f"{sha}:{ref}", f"--force-with-lease={ref}:{expected}"],
            cwd=checkout,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if pushed.returncode:
            raise LeaseRace(f"server rejected lease for {ref}")

    return move


class GitDataApi:
    """Minimal GitHub Git Data API adapter with an injectable transport.

    ``dry_run`` records requests but never calls transport, making accidental
    remote mutation impossible in planning tests.
    """

    def __init__(self, owner: str, repo: str, transport: Transport, *, dry_run: bool = False, lease_mover: LeaseMover | None = None) -> None:
        self.prefix = f"/repos/{owner}/{repo}/git"
        self.transport, self.dry_run = transport, dry_run
        self.lease_mover = lease_mover or getattr(transport, "lease_move", None)
        self.requests: list[tuple[str, str, Mapping[str, Any] | None]] = []

    def request(self, method: str, suffix: str, body: Mapping[str, Any] | None = None) -> Any:
        path = self.prefix + suffix
        self.requests.append((method, path, body))
        if not self.dry_run:
            return self.transport(method, path, body)
        # Dry runs are deliberately useful plans, not a collection of malformed
        # API responses.  The identities are deterministic placeholders and no
        # transport (including a GET) is ever invoked.
        encoded = canonical_json_bytes({"method": method, "path": path, "body": body})
        return {"sha": hashlib.sha1(encoded).hexdigest()}

    def ref(self, name: str) -> str:
        result = self.request("GET", "/ref/" + name.removeprefix("refs/"))
        if self.dry_run:
            # Callers which need a lease supply the captured value to
            # ``update_ref``.  A fake GET object must not pretend to be truth.
            return ""
        try:
            return str(result["object"]["sha"])
        except (KeyError, TypeError):
            raise PublishError("malformed ref response") from None

    def optional_ref(self, name: str) -> str | None:
        try:
            return self.ref(name)
        except RefNotFound:
            return None

    def refs(self, prefix: str) -> list[tuple[str, str]]:
        """List a narrow immutable namespace; never use it for stable aliases."""
        response = self.request("GET", "/matching-refs/" + prefix.removeprefix("refs/"))
        rows = response
        if not isinstance(rows, list):
            raise PublishError("malformed refs response")
        result: list[tuple[str, str]] = []
        for row in rows:
            try:
                name, sha = row["ref"], row["object"]["sha"]
            except (KeyError, TypeError):
                raise PublishError("malformed ref row") from None
            if not isinstance(name, str) or not isinstance(sha, str):
                raise PublishError("malformed ref row")
            result.append((name, sha))
        return result

    def blob(self, data: bytes) -> str:
        response = self.request("POST", "/blobs", {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
        return _sha(response, "blob")

    def tree(self, entries: Iterable[Mapping[str, Any]], base_tree: str | None = None) -> str:
        body: dict[str, Any] = {"tree": list(entries)}
        if base_tree:
            body["base_tree"] = base_tree
        return _sha(self.request("POST", "/trees", body), "tree")

    def commit(self, tree: str, message: str, parents: Iterable[str] = ()) -> str:
        # Deliberately parentless by default: generations are immutable snapshots.
        return _sha(self.request("POST", "/commits", {"message": message, "tree": tree, "parents": list(parents)}), "commit")

    def create_ref(self, ref: str, sha: str) -> None:
        self.request("POST", "/refs", {"ref": ref if ref.startswith("refs/") else "refs/" + ref, "sha": sha})

    def commit_tree(self, sha: str) -> str:
        response = self.request("GET", "/commits/" + sha)
        try:
            return str(response["tree"]["sha"])
        except (KeyError, TypeError):
            raise PublishError("malformed commit response") from None

    def tree_entries(self, tree: str) -> list[Mapping[str, Any]]:
        response = self.request("GET", "/trees/" + tree + "?recursive=1")
        entries = response.get("tree")
        if not isinstance(entries, list) or response.get("truncated") is True:
            raise PublishError("malformed or truncated tree response")
        if not all(isinstance(entry, Mapping) for entry in entries):
            raise PublishError("malformed tree entry")
        return entries

    def blob_bytes(self, blob: str) -> bytes:
        response = self.request("GET", "/blobs/" + blob)
        if response.get("encoding") != "base64" or not isinstance(response.get("content"), str):
            raise PublishError("malformed blob response")
        try:
            return base64.b64decode(response["content"], validate=True)
        except ValueError as exc:
            raise PublishError("invalid base64 blob response") from exc

    def update_ref(self, ref: str, sha: str, *, expected: str, source_ref: str | None = None) -> None:
        if self.dry_run:
            self.requests.append(
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
            return
        if self.lease_mover is None:
            raise PublishError("server-enforced ref lease mover is required")
        self.lease_mover(ref, sha, expected, source_ref)


def _sha(response: Mapping[str, Any], what: str) -> str:
    value = response.get("sha")
    if not isinstance(value, str) or len(value) != 40:
        raise PublishError(f"malformed {what} response")
    return value


@dataclass(frozen=True)
class PointerInventory:
    """An unsmudged tree row; only strict LFS pointer blobs become payload work."""

    path: str
    blob_sha: str
    oid_sha256: str
    size_bytes: int


def inventory_unsmudged_tree(entries: Iterable[Mapping[str, Any]], blob_reader: Callable[[str], bytes]) -> list[PointerInventory]:
    result: list[PointerInventory] = []
    for entry in entries:
        if entry.get("type") != "blob" or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha"), str):
            continue
        raw = blob_reader(entry["sha"])
        try:
            oid, size = parse_lfs_pointer(raw)
        except ValueError:
            continue
        result.append(PointerInventory(entry["path"], entry["sha"], oid, size))
    return sorted(result, key=lambda row: row.path)


class ExactOidMaterializer(Protocol):
    def materialize(self, oid_sha256: str, size_bytes: int) -> bytes: ...


def materialize_exact(materializer: ExactOidMaterializer, oid_sha256: str, size_bytes: int) -> bytes:
    data = materializer.materialize(oid_sha256, size_bytes)
    if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != oid_sha256:
        raise PublishError("materializer did not return the declared exact OID")
    return data


def structural_tree(entries: Mapping[str, str]) -> list[dict[str, str]]:
    """Build a complete deterministic tree from already-uploaded blob IDs."""
    if any(not path or path.startswith("/") or ".." in path.split("/") for path in entries):
        raise ValueError("unsafe tree path")
    return [{"path": path, "mode": "100644", "type": "blob", "sha": entries[path]} for path in sorted(entries)]


def verified_reused_entries(
    api: GitDataApi,
    ledger: VerifiedPublicLedger,
) -> dict[str, str]:
    """Validate reusable part paths through Git tree/object metadata only.

    A complete active snapshot must contain every mapping retained by its
    state.  Validation deliberately does not fetch payload bytes: the ledger
    already binds part hashes/sizes, while the Git tree binds path, blob ID,
    object type, and object size.
    """
    validate_verified_public_ledger(ledger)
    observed: dict[str, Mapping[str, Any]] = {}
    for entry in api.tree_entries(ledger.binding.active_tree):
        path = entry.get("path")
        if isinstance(path, str):
            observed[path] = entry
    result: dict[str, str] = {}
    for mapping in ledger.ledger["assets_by_sha256"].values():
        for part in mapping["parts"]:
            path, blob, size = part["path"], part["git_blob"], part["size_bytes"]
            entry = observed.get(path)
            if not isinstance(entry, Mapping) or entry.get("type") != "blob":
                raise PublishError(f"reused part is absent from active tree: {path}")
            if entry.get("sha") != blob or entry.get("size") != size:
                raise PublishError(f"reused part tree metadata does not match ledger: {path}")
            result[path] = blob
    return result


def upload_directory(api: GitDataApi, root: Path, *, reused_entries: Mapping[str, str] = {}) -> dict[str, str]:
    """Upload a finished local snapshot in lexical order.

    This is intentionally separate from the planner: no file is eligible for
    an alias until all of its bytes have become immutable Git blobs.  It is
    also handy for deterministic dry-run tests because :class:`GitDataApi`
    records the exact requests without touching a remote.
    """
    root = Path(root)
    if not root.is_dir():
        raise PublishError("snapshot directory does not exist")
    result: dict[str, str] = dict(reused_entries)
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.name == ".gitattributes" or path.read_bytes().startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            raise PublishError("snapshot contains an LFS control file or pointer")
        if relative in reused_entries:
            # The existing ledger/tree proof owns the blob identity; the new
            # canonical path is independently checked against its filename so
            # cross-asset part dedup never uploads equal bytes twice.
            stem = path.name.removesuffix(".part")
            digest = stem.split("-", 1)[-1]
            if len(digest) != 64 or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise PublishError("local deduplicated part does not match canonical path")
            continue
        result[relative] = api.blob(path.read_bytes())
    return result


def raw_part_verifier(raw_base: str, expected: Mapping[str, tuple[str, int]]) -> Callable[[str, Mapping[str, Any]], bool]:
    """Verify immutable raw bytes by commit before moving any stable ref."""

    def verify(commit: str, _metadata: Mapping[str, Any]) -> bool:
        for path, (digest, size) in sorted(expected.items()):
            url = raw_base.rstrip("/") + "/" + commit + "/" + path
            try:
                with urlopen(Request(url, headers={"Accept": "application/octet-stream"}), timeout=60) as response:
                    actual = response.read()
            except (HTTPError, URLError, TimeoutError):
                return False
            if len(actual) != size or hashlib.sha256(actual).hexdigest() != digest:
                return False
        return True

    return verify


def raw_asset_verifier(
    raw_base: str,
    assets: Mapping[str, Mapping[str, Any]],
    selected: Iterable[str] | None = None,
) -> Callable[[str, Mapping[str, Any]], bool]:
    """Reassemble selected assets from immutable raw URLs and verify both hashes.

    Checking each part is necessary but insufficient: the ordered concatenation
    must also reproduce the source LFS OID and declared full size before a
    stable data alias can move.
    """
    selected_set = set(assets) if selected is None else set(selected)
    if not selected_set.issubset(assets):
        raise PublishError("raw verification selected an unknown asset")

    def verify(commit: str, _metadata: Mapping[str, Any]) -> bool:
        for asset_sha256 in sorted(selected_set):
            mapping = assets[asset_sha256]
            if not validate_asset_mapping(asset_sha256, mapping):
                return False
            full_hasher = hashlib.sha256()
            full_size = 0
            for part in mapping["parts"]:
                part_hasher = hashlib.sha256()
                part_size = 0
                url = raw_base.rstrip("/") + "/" + commit + "/" + part["path"]
                try:
                    with urlopen(Request(url, headers={"Accept": "application/octet-stream"}), timeout=60) as response:
                        while chunk := response.read(1024 * 1024):
                            part_hasher.update(chunk)
                            full_hasher.update(chunk)
                            part_size += len(chunk)
                            full_size += len(chunk)
                except (HTTPError, URLError, TimeoutError):
                    return False
                if part_size != part["size_bytes"] or part_hasher.hexdigest() != part["sha256"]:
                    return False
            if full_size != mapping["size_bytes"] or full_hasher.hexdigest() != asset_sha256:
                return False
        return True

    return verify


def raw_files_verifier(raw_base: str, expected: Mapping[str, bytes]) -> Callable[[str], bool]:
    """Read back metadata from an immutable raw commit before aliasing it."""

    def verify(commit: str) -> bool:
        for path, wanted in sorted(expected.items()):
            try:
                with urlopen(Request(raw_base.rstrip("/") + "/" + commit + "/" + path), timeout=60) as response:
                    if response.read() != wanted:
                        return False
            except (HTTPError, URLError, TimeoutError):
                return False
        return True

    return verify


def _create_immutable_ref(api: GitDataApi, ref: str, commit: str) -> None:
    existing = api.optional_ref(ref)
    if existing is None:
        api.create_ref(ref, commit)
    elif existing != commit:
        raise PublishError("immutable generation ref already exists with different content: " + ref)


def fetch_verified_public_ledger(
    api: GitDataApi,
    *,
    pages_base: str,
    generation: str | None = None,
    require_public_proof: bool = False,
) -> tuple[VerifiedPublicLedger, str, str]:
    """Bind canonical Pages bytes to their immutable generation-www tree.

    The moving ``www`` branch is deliberately not read.  A successful Git ref
    update can precede an unsuccessful Pages artifact/deployment, so treating
    it as public would make an unserved generation eligible for reuse.
    """
    base = pages_base.rstrip("/") + "/"

    def get_json(name: str) -> tuple[Mapping[str, Any], bytes]:
        try:
            with urlopen(Request(base + name, headers={"Accept": "application/json"}), timeout=60) as response:
                raw = response.read()
            parsed = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 404 and name == "publish-state.v1.json":
                raise PublicStateAbsent("public Pages state is absent") from exc
            raise PublishError("cannot fetch immutable public " + name) from exc
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublishError("cannot fetch immutable public " + name) from exc
        if not isinstance(parsed, Mapping) or canonical_json_bytes(parsed) != raw:
            raise PublishError("public " + name + " is not canonical JSON")
        return parsed, raw

    prefix = "" if generation is None else "generations/" + generation + "/"
    state, state_bytes = get_json(prefix + "publish-state.v1.json")
    catalogue, catalogue_bytes = get_json(prefix + "catalogue.v2.json")
    try:
        active, previous, source = state["active"], state["previous"], state["source"]
        binding = GenerationBinding(
            generation=state["generation"],
            source_commit=source["commit"],
            source_tree=source["tree"],
            active_slot=active["slot"],
            active_commit=active["commit"],
            active_tree=active["tree"],
            previous_slot=previous["slot"],
            previous_commit=previous["commit"],
            previous_tree=previous["tree"],
            catalogue_sha256=state["catalogue_sha256"],
        )
        if generation is not None and binding.generation != generation:
            raise ValueError("retained generation path and state disagree")
        generation_state, generation_state_bytes = get_json("generations/" + binding.generation + "/publish-state.v1.json")
        generation_catalogue, generation_catalogue_bytes = get_json("generations/" + binding.generation + "/catalogue.v2.json")
        if generation_state != state or generation_catalogue != catalogue:
            raise ValueError("stable and generation Pages metadata disagree")
        www_commit = api.ref("refs/heads/generations/" + binding.generation + "-www")
        if require_public_proof and api.ref("refs/heads/generations/" + binding.generation + "-public") != www_commit:
            raise ValueError("generation has no matching post-deploy public proof")
        www_tree = api.commit_tree(www_commit)
        entries = {str(item.get("path")): str(item.get("sha")) for item in api.tree_entries(www_tree) if item.get("type") == "blob"}
        for path, expected in {
            "publish-state.v1.json": state_bytes,
            "catalogue.v2.json": catalogue_bytes,
            "generations/" + binding.generation + "/publish-state.v1.json": generation_state_bytes,
            "generations/" + binding.generation + "/catalogue.v2.json": generation_catalogue_bytes,
        }.items():
            if path not in entries or api.blob_bytes(entries[path]) != expected:
                raise ValueError("Pages bytes are not bound to immutable generation www tree")
        if api.commit_tree(binding.active_commit) != binding.active_tree or api.commit_tree(binding.previous_commit) != binding.previous_tree:
            raise ValueError("data commit tree does not agree with state")
        return verified_public_ledger(state, binding, catalogue), www_commit, www_tree
    except (KeyError, TypeError, ValueError) as exc:
        raise PublishError("public state/catalogue binding is corrupt") from exc


def record_public_proof(api: GitDataApi, *, generation: str, www_commit: str) -> None:
    """Create one immutable evidence ref, only after Pages verification succeeds."""
    _create_immutable_ref(api, "refs/heads/generations/" + generation + "-public", www_commit)


def recover_retained_public_ledger(api: GitDataApi) -> VerifiedPublicLedger:
    """Fail-closed recovery when the stable Pages root is corrupt/unavailable.

    Only immutable ``-public`` refs written by the post-deploy verifier are
    candidates.  Their matching ``-www`` state must explicitly retain itself.
    """
    candidates: list[VerifiedPublicLedger] = []
    for ref, commit in api.refs("refs/heads/generations"):
        if not ref.startswith("refs/heads/generations/") or not ref.endswith("-public"):
            continue
        generation = ref.removeprefix("refs/heads/generations/").removesuffix("-public")
        if api.ref("refs/heads/generations/" + generation + "-www") != commit:
            continue
        try:
            ledger, _, _ = fetch_immutable_generation_ledger(api, generation=generation)
            index = ledger.ledger.get("retained_generations", [])
            names = {entry if isinstance(entry, str) else entry.get("generation") for entry in index if isinstance(entry, (str, Mapping))}
            if generation in names:
                candidates.append(ledger)
        except PublishError:
            continue
    if not candidates:
        raise PublishError("no verified retained immutable generation can recover public state")
    return max(candidates, key=lambda item: int(item.ledger.get("published_at", 0)))


def fetch_retained_public_ledgers(
    api: GitDataApi,
    *,
    pages_base: str,
    stable: VerifiedPublicLedger,
) -> list[VerifiedPublicLedger]:
    """Fetch only the canonical retained-generation index rooted in Pages.

    Each listed generation is independently rebound to its immutable www ref;
    a forged/stale list item cannot smuggle parts into a future data tree.
    """
    raw_index = stable.ledger.get("retained_generations", [stable.binding.generation])
    if not isinstance(raw_index, list) or not raw_index:
        raise PublishError("retained generation index is missing or malformed")
    names: list[str] = []
    for item in raw_index:
        name = item if isinstance(item, str) else item.get("generation") if isinstance(item, Mapping) else None
        if not isinstance(name, str) or not name:
            raise PublishError("retained generation index contains an invalid name")
        if name not in names:
            names.append(name)
    if stable.binding.generation not in names:
        raise PublishError("retained generation index excludes the stable generation")
    result: list[VerifiedPublicLedger] = []
    for name in names:
        if name == stable.binding.generation:
            result.append(stable)
        else:
            ledger, _, _ = fetch_immutable_generation_ledger(api, generation=name)
            result.append(ledger)
    return result


def fetch_immutable_generation_ledger(
    api: GitDataApi,
    *,
    generation: str,
) -> tuple[VerifiedPublicLedger, str, str]:
    """Read an explicitly retained ledger solely from its immutable www ref."""
    commit = api.ref("refs/heads/generations/" + generation + "-www")
    tree = api.commit_tree(commit)
    entries = {str(item.get("path")): str(item.get("sha")) for item in api.tree_entries(tree) if item.get("type") == "blob"}

    def document(path: str) -> Mapping[str, Any]:
        sha = entries.get(path)
        if sha is None:
            raise PublishError("immutable generation is missing " + path)
        try:
            raw = api.blob_bytes(sha)
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, PublishError) as exc:
            raise PublishError("immutable generation contains malformed JSON") from exc
        if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
            raise PublishError("immutable generation contains noncanonical JSON")
        return value

    state = document("generations/" + generation + "/publish-state.v1.json")
    catalogue = document("generations/" + generation + "/catalogue.v2.json")
    try:
        active, previous, source = state["active"], state["previous"], state["source"]
        binding = GenerationBinding(
            generation=state["generation"],
            source_commit=source["commit"],
            source_tree=source["tree"],
            active_slot=active["slot"],
            active_commit=active["commit"],
            active_tree=active["tree"],
            previous_slot=previous["slot"],
            previous_commit=previous["commit"],
            previous_tree=previous["tree"],
            catalogue_sha256=state["catalogue_sha256"],
        )
        if binding.generation != generation:
            raise ValueError("immutable generation identity mismatch")
        if api.commit_tree(binding.active_commit) != binding.active_tree or api.commit_tree(binding.previous_commit) != binding.previous_tree:
            raise ValueError("immutable generation data binding mismatch")
        return verified_public_ledger(state, binding, catalogue), commit, tree
    except (KeyError, TypeError, ValueError) as exc:
        raise PublishError("immutable generation ledger is corrupt") from exc


def verified_retained_entries(api: GitDataApi, ledgers: Iterable[VerifiedPublicLedger]) -> dict[str, str]:
    """Return the union of every part still promised by retained generations."""
    result: dict[str, str] = {}
    for ledger in ledgers:
        for path, blob in verified_reused_entries(api, ledger).items():
            old = result.setdefault(path, blob)
            if old != blob:
                raise PublishError("retained generations disagree on immutable part blob: " + path)
    return result


def verified_retained_part_index(api: GitDataApi, ledgers: Iterable[VerifiedPublicLedger]) -> dict[tuple[str, int], str]:
    """Validated repository-wide `(part digest, size) -> Git blob` reuse map."""
    result: dict[tuple[str, int], str] = {}
    for ledger in ledgers:
        # This checks each part path/blob against the immutable active tree.
        verified_reused_entries(api, ledger)
        declared = ledger.ledger.get("parts_by_sha256", {})
        if not isinstance(declared, Mapping):
            raise PublishError("retained ledger lacks canonical parts_by_sha256")
        for digest, row in declared.items():
            if not isinstance(digest, str) or not isinstance(row, Mapping):
                raise PublishError("malformed parts_by_sha256 entry")
            size, blob = row.get("size_bytes"), row.get("git_blob")
            if not isinstance(size, int) or not isinstance(blob, str):
                raise PublishError("malformed parts_by_sha256 entry")
            key = (digest, size)
            previous = result.setdefault(key, blob)
            if previous != blob:
                raise PublishError("retained part index has conflicting blob identity")
    return result


def active_tree_covers(api: GitDataApi, tree: str, desired: Mapping[str, str]) -> bool:
    """True only when every desired path/blob is already in the active tree."""
    observed = {entry.get("path"): entry.get("sha") for entry in api.tree_entries(tree) if entry.get("type") == "blob"}
    return all(observed.get(path) == blob for path, blob in desired.items())


def fetch_staging_ledger(api: GitDataApi, *, generation: str, source_commit: str,
                         source_tree: str) -> tuple[Mapping[str, Any], str] | None:
    """Independently validate interrupted immutable data-generation evidence."""
    commit = api.optional_ref("refs/heads/generations/" + generation + "-data")
    if commit is None:
        return None
    entries = {str(row.get("path")): row for row in api.tree_entries(api.commit_tree(commit))
               if row.get("type") == "blob"}
    marker = entries.get("publish-state.v1.json")
    if not isinstance(marker, Mapping) or not isinstance(marker.get("sha"), str):
        raise PublishError("staging data generation lacks canonical marker")
    try:
        raw = api.blob_bytes(marker["sha"])
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, PublishError) as exc:
        raise PublishError("staging data marker is malformed") from exc
    if not isinstance(state, Mapping) or canonical_json_bytes(state) != raw:
        raise PublishError("staging data marker is not canonical JSON")
    if set(state) != {
        "schema_version", "kind", "generation", "source", "published_at",
        "assets_by_sha256", "logical_assets", "parts_by_sha256",
    }:
        raise PublishError("staging data marker has an unexpected shape")
    if state.get("schema_version") != 1 or state.get("kind") != "staging-data" or state.get("generation") != generation:
        raise PublishError("staging data marker does not match generation")
    if state.get("source") != {"branch": "assets", "commit": source_commit, "tree": source_tree}:
        raise PublishError("staging data marker does not match source")
    assets, logical, index = state.get("assets_by_sha256"), state.get("logical_assets"), state.get("parts_by_sha256")
    if not isinstance(assets, Mapping) or not isinstance(logical, Mapping) or not isinstance(index, Mapping) or not isinstance(state.get("published_at"), int) or state["published_at"] <= 0:
        raise PublishError("staging data marker lacks required ledger fields")
    expected_index: dict[str, dict[str, Any]] = {}
    expected_paths = {"publish-state.v1.json"}
    for full, mapping in assets.items():
        if not validate_asset_mapping(full, mapping):
            raise PublishError("staging data marker has invalid asset mapping")
        for part in mapping["parts"]:
            expected_paths.add(part["path"])
            row = entries.get(part["path"])
            if not isinstance(row, Mapping) or row.get("sha") != part["git_blob"] or row.get("size") != part["size_bytes"]:
                raise PublishError("staging data tree does not bind part metadata")
            proof = {"size_bytes": part["size_bytes"], "git_blob": part["git_blob"]}
            old = expected_index.setdefault(part["sha256"], proof)
            if old != proof:
                raise PublishError("staging data has conflicting part identities")
    referenced_assets: set[str] = set()
    for key, row in logical.items():
        if not isinstance(key, str) or not key or not isinstance(row, Mapping):
            raise PublishError("staging data has malformed logical metadata")
        oid, size = row.get("source_oid_sha256"), row.get("source_size_bytes")
        if not isinstance(oid, str) or not isinstance(size, int) or oid not in assets or assets[oid].get("size_bytes") != size:
            raise PublishError("staging logical metadata is not bound to asset mapping")
        referenced_assets.add(oid)
    if referenced_assets != set(assets):
        raise PublishError("staging data contains an unreferenced asset mapping")
    if dict(index) != expected_index:
        raise PublishError("staging data parts_by_sha256 is incomplete or forged")
    if set(entries) != expected_paths:
        raise PublishError("staging data tree contains undeclared blobs")
    return state, commit


def publish_snapshot_pair(
    api: GitDataApi,
    *,
    public_dir: Path,
    www_dir: Path,
    generation: str,
    active_slot: str,
    raw_verify: Callable[[str, Mapping[str, Any]], bool],
    prepare_www: Callable[[str, str, str, str], None] | None = None,
    injector: FailureInjector | None = None,
    bootstrap: bool = False,
    reused_public_entries: Mapping[str, str] = {},
    raw_verify_www: Callable[[str], bool] | None = None,
    staging_metadata: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Publish verified data first, then move the stable www alias last.

    The two commits are parentless immutable snapshots.  Existing aliases move
    through a server-enforced force-with-lease; callers can retry safely after
    a failed boundary because no stable alias moves before raw verification.
    """
    if active_slot not in {"public-a", "public-b"}:
        raise PublishError("invalid public slot")
    tx = PublisherTransaction(api, injector=injector)
    # ``active_slot`` is the currently *inactive* slot selected for this
    # publication.  The other slot is the verified live generation which must
    # become ``previous`` in the new ledger.  Do not use the old tip of the
    # target slot here: after an interrupted run it can be unrelated history.
    target_ref = "refs/heads/" + active_slot
    previous_ref = "refs/heads/" + ("public-b" if active_slot == "public-a" else "public-a")
    www_ref = "refs/heads/www"
    target_before = api.optional_ref(target_ref) if not api.dry_run else "d" * 40
    previous_commit = api.optional_ref(previous_ref) if not api.dry_run else "e" * 40
    www_before = api.optional_ref(www_ref) if not api.dry_run else "f" * 40
    if not bootstrap and (target_before is None or previous_commit is None):
        raise PublishError("both public slots must exist; use explicit bootstrap to create the first generation")
    data_generation_ref = "refs/heads/generations/" + generation + "-data"
    retained_data = api.optional_ref(data_generation_ref)
    if retained_data is not None:
        # Interrupted retry: immutable staging already exists.  Re-verify it,
        # then perform only the still-pending stable alias operation.
        metadata = staging_metadata or {"generation": generation, "kind": "public-data"}
        if not raw_verify(retained_data, metadata):
            raise PublishError("retained immutable data generation failed raw verification")
        public_commit = retained_data
        if target_before:
            api.update_ref(target_ref, public_commit, expected=target_before, source_ref=data_generation_ref)
        else:
            api.create_ref(target_ref, public_commit)
    else:
        public_entries = upload_directory(api, public_dir, reused_entries=reused_public_entries)
        public_commit = tx.publish(
            GenerationPlan(
                generation,
                data_generation_ref,
                target_ref,
                target_before or "",
                public_entries,
                staging_metadata or {"generation": generation, "kind": "public-data"},
            ),
            raw_verify=raw_verify,
        )
    if bootstrap and previous_commit is None:
        # A first publication initializes both rotating aliases to the same
        # verified parentless snapshot.  The next ordinary run can therefore
        # rotate either slot without a second bootstrap exception.
        api.create_ref(previous_ref, public_commit)
        previous_commit = public_commit
    tx.injector.checkpoint("phase1.data-alias")
    if prepare_www is not None:
        # State is in www, so binding these data identities is not recursive.
        prepare_www(public_commit, api.commit_tree(public_commit), previous_commit or public_commit, api.commit_tree(previous_commit) if previous_commit else api.commit_tree(public_commit))

    data_tree = api.commit_tree(public_commit) if not api.dry_run else "0" * 40
    prior_tree = api.commit_tree(previous_commit) if not api.dry_run and previous_commit else data_tree
    _bind_publication_state(
        www_dir,
        generation=generation,
        active_slot=active_slot,
        active_commit=public_commit,
        active_tree=data_tree,
        previous_slot=previous_ref.rsplit("/", 1)[-1],
        previous_commit=previous_commit or public_commit,
        previous_tree=prior_tree,
    )

    # www is intentionally second: it is the discovery/stable metadata alias.
    immutable_www_ref = "refs/heads/generations/" + generation + "-www"
    retained_www = api.optional_ref(immutable_www_ref)
    if retained_www is not None:
        www_commit = retained_www
    else:
        www_entries = upload_directory(api, www_dir)
        www_tree = api.tree(structural_tree(www_entries))
        tx.metrics.trees_created += 1
        www_commit = api.commit(www_tree, "publish metadata " + generation)
        tx.metrics.commits_created += 1
        _create_immutable_ref(api, immutable_www_ref, www_commit)
    tx.injector.checkpoint("phase1.www-commit")
    if raw_verify_www is not None and not raw_verify_www(www_commit):
        raise PublishError("immutable www generation raw verification failed")
    tx.injector.checkpoint("phase1.www-verified")
    if api.dry_run:
        api.create_ref(www_ref, www_commit)
    else:
        if www_before is None:
            api.create_ref(www_ref, www_commit)
        else:
            api.update_ref(www_ref, www_commit, expected=www_before, source_ref=immutable_www_ref)
    tx.injector.checkpoint("phase1.www-alias")
    return public_commit, www_commit


def publish_www_snapshot(
    api: GitDataApi,
    *,
    www_dir: Path,
    generation: str,
    active_commit: str,
    active_tree: str,
    previous_commit: str,
    previous_tree: str,
    active_slot: str,
    previous_slot: str,
    raw_verify_www: Callable[[str], bool] | None = None,
) -> str:
    """Metadata-only transaction: data refs/blobs are intentionally untouched."""
    www_ref = "refs/heads/www"
    www_before = api.optional_ref(www_ref) if not api.dry_run else "f" * 40
    _bind_publication_state(
        www_dir,
        generation=generation,
        active_slot=active_slot,
        active_commit=active_commit,
        active_tree=active_tree,
        previous_slot=previous_slot,
        previous_commit=previous_commit,
        previous_tree=previous_tree,
    )
    immutable_ref = "refs/heads/generations/" + generation + "-www"
    retained = api.optional_ref(immutable_ref)
    if retained is None:
        entries = upload_directory(api, www_dir)
        commit = api.commit(api.tree(structural_tree(entries)), "publish metadata " + generation)
        _create_immutable_ref(api, immutable_ref, commit)
    else:
        expected = {
            path.relative_to(www_dir).as_posix(): hashlib.sha1(
                b"blob " + str(path.stat().st_size).encode("ascii") + b"\0" + path.read_bytes()
            ).hexdigest()
            for path in www_dir.rglob("*")
            if path.is_file()
        }
        observed = {
            str(row.get("path")): str(row.get("sha"))
            for row in api.tree_entries(api.commit_tree(retained))
            if row.get("type") == "blob"
        }
        if observed != expected:
            raise PublishError("immutable www generation does not match rebuilt metadata")
        commit = retained
    if raw_verify_www is not None and not raw_verify_www(commit):
        raise PublishError("immutable www generation raw verification failed")
    if api.dry_run:
        api.create_ref(www_ref, commit)
    elif www_before is None:
        api.create_ref(www_ref, commit)
    else:
        api.update_ref(www_ref, commit, expected=www_before, source_ref=immutable_ref)
    return commit


def _bind_publication_state(
    www_dir: Path,
    *,
    generation: str,
    active_slot: str,
    active_commit: str,
    active_tree: str,
    previous_slot: str,
    previous_commit: str,
    previous_tree: str,
) -> None:
    """Fill data-ref identities only after the verified data commit exists."""
    paths = [
        www_dir / "publish-state.v1.json",
        www_dir / "generations" / generation / "publish-state.v1.json",
    ]
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        document["active"] = {
            "slot": active_slot,
            "commit": active_commit,
            "tree": active_tree,
        }
        document["previous"] = {
            "slot": previous_slot,
            "commit": previous_commit,
            "tree": previous_tree,
        }
        path.write_bytes(canonical_json_bytes(document))


@dataclass(frozen=True)
class GenerationPlan:
    generation: str
    inactive_ref: str
    active_ref: str
    expected_active: str
    tree_entries: Mapping[str, str]
    metadata: Mapping[str, Any]


@dataclass
class TransactionMetrics:
    blobs_uploaded: int = 0
    blobs_reused: int = 0
    bytes_uploaded: int = 0
    bytes_avoided: int = 0
    trees_created: int = 0
    commits_created: int = 0
    rotations: int = 0
    noops: int = 0


def retention_plan(generations: Iterable[Mapping[str, Any]], now: float) -> dict[str, set[str]]:
    """Keep current plus two prior successes, and anything younger than 14 days."""
    ordered = sorted(generations, key=lambda g: g.get("published_at", 0), reverse=True)
    keep = ordered[:3] + [g for g in ordered if now - g.get("published_at", 0) < 14 * 86400]
    keep_names = {str(g["generation"]) for g in keep}
    return {"keep": keep_names, "delete": {str(g["generation"]) for g in ordered} - keep_names}


def authorize_gc(generations: Iterable[Mapping[str, Any]], now: float, *, confirmed: bool) -> set[str]:
    """GC is a separate, explicitly authorized operation; planning deletes nothing."""
    if not confirmed:
        raise PublishError("GC requires explicit authorization")
    return retention_plan(generations, now)["delete"]


def recover_verified_public_state(fetch: Callable[[], Mapping[str, Any]], verify: Callable[[Mapping[str, Any]], bool]) -> Mapping[str, Any]:
    """Recovery trusts only a currently public, independently verified state."""
    state = fetch()
    if not isinstance(state, Mapping) or not verify(state):
        raise PublishError("public recovery state is absent or corrupt")
    return state


class PublisherTransaction:
    """Publish immutable content first; rotate stable aliases only after raw verify."""

    def __init__(self, api: GitDataApi, *, injector: FailureInjector | None = None) -> None:
        self.api, self.injector, self.metrics = api, injector or FailureInjector(), TransactionMetrics()

    def publish(self, plan: GenerationPlan, *, raw_verify: Callable[[str, Mapping[str, Any]], bool]) -> str:
        self.injector.checkpoint("phase0.captured")
        if not plan.generation or plan.active_ref == plan.inactive_ref:
            raise PublishError("generation and distinct stable aliases are required")
        # Metadata is uploaded bottom-up and is immutable content, too.
        metadata_blob = self.api.blob(canonical_json_bytes(plan.metadata))
        self.metrics.blobs_uploaded += 1
        self.injector.checkpoint("phase0.metadata")
        full_tree = self.api.tree(structural_tree({**plan.tree_entries, "publish-state.v1.json": metadata_blob}))
        self.metrics.trees_created += 1
        self.injector.checkpoint("phase0.tree")
        commit = self.api.commit(full_tree, "publish " + plan.generation)
        self.metrics.commits_created += 1
        self.injector.checkpoint("phase0.commit")
        existing_generation = self.api.optional_ref(plan.inactive_ref) if not self.api.dry_run else None
        if existing_generation is None:
            self.api.create_ref(plan.inactive_ref, commit)
        elif existing_generation != commit:
            raise PublishError(f"immutable generation ref already exists with different content: {plan.inactive_ref}")
        self.injector.checkpoint("phase0.inactive")
        if not raw_verify(commit, plan.metadata):
            raise PublishError("raw verification failed; stable aliases were not moved")
        self.injector.checkpoint("phase0.verified")
        if plan.expected_active:
            self.api.update_ref(plan.active_ref, commit, expected=plan.expected_active, source_ref=plan.inactive_ref)
        elif self.api.optional_ref(plan.active_ref) is None:
            self.api.create_ref(plan.active_ref, commit)
        else:
            raise LeaseRace(f"bootstrap lease lost for {plan.active_ref}")
        self.metrics.rotations += 1
        self.injector.checkpoint("phase0.alias")
        return commit

    def noop(self, *, public_state: Mapping[str, Any], verify: Callable[[Mapping[str, Any]], bool]) -> Mapping[str, Any]:
        """Metadata-only/unchanged runs never rotate an alias without public proof."""
        state = recover_verified_public_state(lambda: public_state, verify)
        self.metrics.noops += 1
        return state

    def rollback(self, *, stable_ref: str, verified_previous_commit: str, expected_active: str) -> None:
        """Rollback points stable only to an already raw-verified immutable commit."""
        self.api.update_ref(stable_ref, verified_previous_commit, expected=expected_active)
        self.metrics.rotations += 1
