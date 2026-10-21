"""Front-door fixture world for the internal catalog visibility e2e tests.

Shared by Phase 3 (in-process TestClient), Phase 4 (live uvicorn) and Phase 6
(PostgreSQL-backed uvicorn).  Everything is driven through REST and MCP; the
only direct filesystem write is the catalog content itself, which a real
server produces with its own generators and which is seeded here from the
neutral unit fixture (``tests/shared/cidx_meta_visibility_fixture.py``).

World:
- golden repositories ``alpha``, ``beta-team-sample`` (the canary), ``gamma``
  and the short alias ``zed``, registered from tiny local git repositories;
- a restricted power user in a group granted only ``alpha``;
- the catalog seeded with descriptions, a dependency map (visible and hidden
  domains, an edge through a hidden repository, intra-domain, roles and
  frontmatter lists), memories, X-Ray patterns and a visible/hidden asset
  pair, then refreshed so the semantic and full-text indexes serve it.
"""

from __future__ import annotations

import copy
import functools
import json
import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

import httpx

from tests.e2e.helpers import GIT_SUBPROCESS_TIMEOUT, wait_for_job
from tests.e2e.server.conftest import AdminTokenProvider
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    GAMMA,
    SHORT_ALIAS,
    write_audit_domain,
    write_catalog_content,
)

CATALOG = "cidx-meta"
CATALOG_GLOBAL = "cidx-meta-global"
WORLD_REPOS = (ALPHA, CANARY, GAMMA, SHORT_ALIAS)
RESTRICTED_USER = "e2e-catalog-viewer"
RESTRICTED_GROUP = "e2e-catalog-alpha"
VISIBLE_ASSET = f"{ALPHA}.svg"
HIDDEN_ASSET = f"{CANARY}.svg"
GIT_HEAD = ".git/HEAD"
# A real internal metadata file of the indexed catalog (same exclusion rule
# as .git in the file doors).
INTERNAL_FILE = ".code-indexer/config.json"

JOB_TIMEOUT_SECONDS = 300.0
JOB_POLL_SECONDS = 1.0
# The server defers a catalog refresh by 30 s (CidxMetaRefreshDebouncer).
CATALOG_DEBOUNCE_GRACE_SECONDS = 45.0
INDEX_READY_TIMEOUT_SECONDS = 240.0
INDEX_READY_POLL_SECONDS = 2.0
REPLY_SNIPPET = 600
# Long enough that a search hit on it exceeds the 2000-character preview and
# is returned with a cache handle.
GUIDELINE_LINES = 80
# Repository internals never snapshotted or restored by the teardown.
_INTERNAL_DIRS = (".code-indexer", ".git", ".locks", ".snapshot-reader-leases")

_GAMMA_WORD = re.compile(rf"(?<![a-z0-9_-]){GAMMA}(?![a-z0-9_])", re.IGNORECASE)
_SHORT_VALUES = frozenset({SHORT_ALIAS, f"{SHORT_ALIAS}-global", f"{SHORT_ALIAS}.md"})

EchoPath = Tuple[str, ...]


class HeaderSource(Protocol):
    def get_headers(self) -> dict: ...


@dataclass(frozen=True)
class Reply:
    """One front-door reply.

    ``text`` is the complete HTTP body; ``envelope`` its parsed JSON (None
    for HTML); ``items`` every decoded MCP content item (the REST body as
    the single item); ``body`` the first item.
    """

    status: int
    body: Any
    text: str
    envelope: Any = None
    items: Tuple[Any, ...] = ()

    @property
    def ok(self) -> bool:
        if self.status >= 400:
            return False
        if isinstance(self.body, dict) and "error" in self.body:
            return not self.body.get("error") and self.body.get("success") is not False
        if isinstance(self.body, dict) and self.body.get("success") is False:
            return False
        return True

    def snippet(self) -> str:
        return f"HTTP {self.status}: {self.text[:REPLY_SNIPPET]}"


def _decode_item(item: Any) -> Any:
    if isinstance(item, dict) and isinstance(item.get("text"), str):
        try:
            return json.loads(item["text"])
        except ValueError:
            return {"text": item["text"]}
    return item


def mcp(client: httpx.Client, headers: dict, tool: str, args: Dict[str, Any]) -> Reply:
    """Call one MCP tool; ``items`` holds every decoded content item, or the
    JSON-RPC error object when the dispatcher refused the call."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
        headers=headers,
    )
    try:
        envelope = resp.json()
    except ValueError:
        return Reply(resp.status_code, None, resp.text)
    if isinstance(envelope, dict) and "error" in envelope:
        refusal = {"success": False, "error": envelope["error"]}
        return Reply(resp.status_code, refusal, resp.text, envelope, (refusal,))
    result = envelope.get("result", {}) if isinstance(envelope, dict) else {}
    content = result.get("content", []) if isinstance(result, dict) else []
    items = tuple(_decode_item(item) for item in content) or (result,)
    return Reply(resp.status_code, items[0], resp.text, envelope, items)


def rest(
    client: httpx.Client, method: str, path: str, headers: dict, **kwargs: Any
) -> Reply:
    resp = client.request(method, path, headers=headers, **kwargs)
    try:
        body: Any = resp.json()
    except ValueError:
        return Reply(resp.status_code, None, resp.text)
    return Reply(resp.status_code, body, resp.text, body, (body,))


@dataclass(frozen=True)
class Echo:
    """Where a reply may echo the request, and the request texts it may echo.

    A value at one of ``paths`` is ignored by the leak scan only when it is
    exactly one of ``submitted``; any other value there is scanned."""

    paths: Tuple[EchoPath, ...]
    submitted: Tuple[str, ...]


def _drop_echo(value: Any, path: EchoPath, submitted: Tuple[str, ...]) -> None:
    """Remove the key at exactly *path* when its value is a submitted text."""
    node = value
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return
        node = node[key]
    if isinstance(node, dict) and node.get(path[-1]) in submitted:
        del node[path[-1]]


def _full_payload(reply: Reply, echo: Echo) -> Any:
    """The complete reply with every MCP content item decoded in place and
    only the exact request echoes removed from each item."""
    items = [copy.deepcopy(item) for item in reply.items]
    for item in items:
        for path in echo.paths:
            _drop_echo(item, path, echo.submitted)
    envelope = copy.deepcopy(reply.envelope)
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        result["content"] = items
        return envelope
    return items


def _structured_short_alias(value: Any, where: str, out: List[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _structured_short_alias(key, f"{where}.<key>", out)
            _structured_short_alias(item, f"{where}.{key}", out)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _structured_short_alias(item, f"{where}[{index}]", out)
    elif isinstance(value, str):
        segments = {s.lower() for s in re.split(r"[/\\]", value.strip())}
        if value.strip().lower() in _SHORT_VALUES or segments & _SHORT_VALUES:
            out.append(f"short alias as a structured value at {where}")


def leaks(reply: Reply, echo: Optional[Echo] = None) -> List[str]:
    """Every way *reply* names a repository the restricted user cannot see.

    The canary and ``gamma`` are searched in the COMPLETE reply: the raw
    HTTP body, or, when *echo* is given (a request that itself names the
    canary), the complete payload with only the exact echoes of the
    submitted request removed.  The short alias counts as a whole
    structured value or path segment in any decoded item (3-character
    aliases in prose are an accepted residual).
    """
    found: List[str] = []
    if reply.envelope is None:
        text = reply.text
    else:
        payload = _full_payload(reply, echo or Echo((), ()))
        _structured_short_alias(payload, "$", found)
        text = json.dumps(payload) if echo else reply.text
    if CANARY in text.lower():
        found.append(f"canary {CANARY!r} present")
    if _GAMMA_WORD.search(text):
        found.append(f"{GAMMA!r} present")
    return found


def mentions(reply: Reply, needle: str) -> bool:
    return needle.lower() in reply.text.lower()


CleanupStep = Tuple[str, Callable[[], None]]


def run_cleanup_steps(steps: Sequence[CleanupStep]) -> List[str]:
    """Run every step in order, whatever fails; one report per failure."""
    failures: List[str] = []
    for name, step in steps:
        try:
            step()
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    return failures


@dataclass
class WorldLedger:
    """What provisioning created, recorded as it goes, so a failed setup and
    the teardown remove exactly that."""

    client: httpx.Client
    admin: HeaderSource
    catalog_root: Path
    snapshot: Optional[Dict[str, bytes]] = None
    repos: List[str] = field(default_factory=list)
    user_created: bool = False
    group_id: Optional[str] = None
    catalog_seeded: bool = False


@dataclass
class CatalogWorld:
    client: httpx.Client
    admin: HeaderSource
    restricted: HeaderSource
    restricted_password: str
    group_id: str
    web_client_factory: Callable[[], httpx.Client]
    catalog_root: Path
    ledger: WorldLedger
    # (sent at, X-Correlation-ID) of every door call, for log claims.
    issued_calls: List[Tuple[datetime, str]] = field(default_factory=list)

    def calls_since(self, started: datetime) -> FrozenSet[str]:
        return frozenset(cid for sent, cid in self.issued_calls if sent >= started)

    def admin_mcp(self, tool: str, args: Dict[str, Any]) -> Reply:
        return mcp(self.client, self.admin.get_headers(), tool, args)

    def admin_token(self) -> str:
        return _token(self.admin)

    def set_alpha_grant(self, granted: bool) -> None:
        action = "add" if granted else "remove"
        args: Dict[str, Any] = {"action": action, "group_id": self.group_id}
        if granted:
            args["repos"] = [ALPHA]
        else:
            args["repo_name"] = ALPHA
        reply = self.admin_mcp("manage_group_repos", args)
        assert reply.ok, f"manage_group_repos {action} failed: {reply.snippet()}"


def _make_git_repo(repo: Path, name: str) -> None:
    repo.mkdir(parents=True)
    module = name.replace("-", "_")
    (repo / f"{module}_core.py").write_text(
        f'"""Core module of {name}."""\n\n\ndef {module}_entry() -> int:\n'
        "    return 1\n",
        encoding="utf-8",
    )
    identity = ["-c", "user.name=e2e", "-c", "user.email=e2e@example.com"]
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "."],
        [*identity, "commit", "-qm", "init"],
    ):
        subprocess.run(
            ["git", *args], cwd=repo, check=True, timeout=GIT_SUBPROCESS_TIMEOUT
        )


def _token(source: HeaderSource) -> str:
    return str(source.get_headers()["Authorization"]).split(" ", 1)[1]


def _wait_completed(client: httpx.Client, admin: HeaderSource, job_id: str) -> None:
    job = wait_for_job(
        client,
        job_id,
        _token(admin),
        timeout=JOB_TIMEOUT_SECONDS,
        poll_interval=JOB_POLL_SECONDS,
    )
    assert job.get("status") == "completed", f"job {job_id}: {job}"


def _register_repos(ledger: WorldLedger, work_dir: Path) -> None:
    client, admin = ledger.client, ledger.admin
    for name in WORLD_REPOS:
        repo = work_dir / name
        if not repo.exists():
            _make_git_repo(repo, name)
        reply = rest(
            client,
            "POST",
            "/api/admin/golden-repos",
            admin.get_headers(),
            json={"repo_url": str(repo), "alias": name},
        )
        assert reply.status in (200, 202), f"register {name}: {reply.snippet()}"
        ledger.repos.append(name)
        _wait_completed(client, admin, reply.body["job_id"])


def _is_internal(relative: Path) -> bool:
    return bool(relative.parts) and relative.parts[0] in _INTERNAL_DIRS


def snapshot_catalog(catalog_root: Path) -> Dict[str, bytes]:
    """Every catalog file outside the repository internals, by path."""
    return {
        str(path.relative_to(catalog_root)): path.read_bytes()
        for path in sorted(catalog_root.rglob("*"))
        if path.is_file() and not _is_internal(path.relative_to(catalog_root))
    }


def restore_catalog(catalog_root: Path, snapshot: Dict[str, bytes]) -> None:
    """Put the catalog clone back to *snapshot*: files added since are
    removed, changed or removed ones rewritten."""
    removed: List[str] = []
    for path in sorted(catalog_root.rglob("*"), reverse=True):
        relative = path.relative_to(catalog_root)
        if _is_internal(relative):
            continue
        if path.is_file() and str(relative) not in snapshot:
            path.unlink()
            removed.append(str(relative))
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for relative_name, content in snapshot.items():
        target = catalog_root / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_bytes() != content:
            target.write_bytes(content)
    _commit_tracked_removals(catalog_root, removed)


def _commit_tracked_removals(catalog_root: Path, removed: List[str]) -> None:
    """Commit the removal of the *removed* files the catalog repository
    tracks (the server commits a pattern stored through its front door), so
    its working tree matches HEAD again: an activation copies tracked
    changes and logs them.  Only those paths are committed."""
    if not removed or not (catalog_root / ".git").exists():
        return
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", *removed],
        cwd=catalog_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_SUBPROCESS_TIMEOUT,
    )
    tracked = [name for name in listed.stdout.split("\0") if name]
    if not tracked:
        return
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=e2e",
            "-c",
            "user.email=e2e@example.com",
            "commit",
            "-qm",
            "e2e: remove the catalog world's files",
            "--",
            *tracked,
        ],
        cwd=catalog_root,
        capture_output=True,
        check=True,
        timeout=GIT_SUBPROCESS_TIMEOUT,
    )


def seed_catalog(catalog_root: Path) -> None:
    """Write the neutral catalog content into the catalog's base clone."""
    assert catalog_root.is_dir(), f"catalog clone missing at {catalog_root}"
    write_catalog_content(catalog_root)
    write_audit_domain(catalog_root / "dependency-map")
    (catalog_root / f"{SHORT_ALIAS}.md").write_text(
        f"# {SHORT_ALIAS}\n\nAdapter repository used by {ALPHA}.\n", encoding="utf-8"
    )
    guidelines = [
        f"{k}. Keep each domain analysis guideline precise and cite evidence files."
        for k in range(1, GUIDELINE_LINES + 1)
    ]
    (catalog_root / "dependency-map" / "_analysis_guidelines.md").write_text(
        "# Domain analysis guidelines\n\n" + "\n".join(guidelines) + "\n",
        encoding="utf-8",
    )
    for name in (ALPHA, CANARY):
        (catalog_root / f"{name}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg"><title>{name}</title></svg>\n',
            encoding="utf-8",
        )


def refresh_catalog(client: httpx.Client, admin: HeaderSource) -> None:
    """Refresh the catalog through REST and wait for its job."""
    reply = rest(
        client,
        "POST",
        f"/api/admin/golden-repos/{CATALOG}/refresh",
        admin.get_headers(),
    )
    assert reply.status == 202, f"catalog refresh: {reply.snippet()}"
    job_id = reply.body.get("job_id") if isinstance(reply.body, dict) else None
    if job_id:
        _wait_completed(client, admin, job_id)


def refresh_catalog_until_served(client: httpx.Client, admin: HeaderSource) -> None:
    """Refresh the catalog, then wait until an admin full-text search serves
    the seeded content (the refresh publishes a new snapshot).  Registering
    repositories starts catalog refreshes of its own: never submit a second
    refresh while one runs."""
    wait_for_idle_catalog_refreshes(client, admin)
    refresh_catalog(client, admin)
    deadline = time.monotonic() + INDEX_READY_TIMEOUT_SECONDS
    last: Optional[Reply] = None
    while time.monotonic() < deadline:
        last = mcp(
            client,
            admin.get_headers(),
            "search_code",
            {
                "query_text": "Settlement",
                "repository_alias": CATALOG_GLOBAL,
                "search_mode": "fts",
                "limit": 5,
            },
        )
        if last.ok and "payments.md" in last.text:
            return
        time.sleep(INDEX_READY_POLL_SECONDS)
    raise TimeoutError(
        f"catalog index never served the seed: {last and last.snippet()}"
    )


def _create_restricted_user(ledger: WorldLedger) -> tuple[str, str]:
    client = ledger.client
    password = f"Vw{secrets.token_urlsafe(18)}9!"
    headers = ledger.admin.get_headers()
    user = mcp(
        client,
        headers,
        "create_user",
        {"username": RESTRICTED_USER, "password": password, "role": "power_user"},
    )
    assert user.ok, f"create_user: {user.snippet()}"
    ledger.user_created = True
    group = mcp(client, headers, "create_group", {"name": RESTRICTED_GROUP})
    assert group.ok, f"create_group: {group.snippet()}"
    group_id = str(group.body["group_id"])
    ledger.group_id = group_id
    member = mcp(
        client,
        headers,
        "manage_group_members",
        {"action": "add", "group_id": group_id, "user_id": RESTRICTED_USER},
    )
    assert member.ok, f"manage_group_members: {member.snippet()}"
    return password, group_id


def _login_provider(
    client: httpx.Client, username: str, password: str
) -> AdminTokenProvider:
    def _login() -> tuple[str, Optional[str]]:
        resp = client.post(
            "/auth/login", json={"username": username, "password": password}
        )
        assert resp.status_code == 200, f"login {username}: HTTP {resp.status_code}"
        body = resp.json()
        return str(body["access_token"]), body.get("refresh_token")

    access, refresh = _login()
    return AdminTokenProvider(
        login_fn=_login, initial_access_token=access, initial_refresh_token=refresh
    )


def provision_catalog_world(
    client: httpx.Client,
    admin: HeaderSource,
    catalog_root: Path,
    work_dir: Path,
    web_client_factory: Callable[[], httpx.Client],
) -> CatalogWorld:
    """Build the world through the front door on a server that does not
    hold it yet; ``teardown_catalog_world`` removes it again.  A failing
    step rolls back everything created before it."""
    ledger = WorldLedger(client, admin, catalog_root)
    try:
        ledger.snapshot = snapshot_catalog(catalog_root)
        _register_repos(ledger, work_dir)
        ledger.catalog_seeded = True
        seed_catalog(catalog_root)
        refresh_catalog_until_served(client, admin)
        password, group_id = _create_restricted_user(ledger)
        world = CatalogWorld(
            client=client,
            admin=admin,
            restricted=_login_provider(client, RESTRICTED_USER, password),
            restricted_password=password,
            group_id=group_id,
            web_client_factory=web_client_factory,
            catalog_root=catalog_root,
            ledger=ledger,
        )
        world.set_alpha_grant(True)
        return world
    except Exception as exc:
        failures = run_cleanup_steps(cleanup_steps(ledger))
        if failures:
            raise AssertionError(
                f"catalog world setup failed ({exc!r}); rollback failures: {failures}"
            ) from exc
        raise


def teardown_catalog_world(world: CatalogWorld) -> None:
    """Remove the world through the front doors, attempting every step and
    reporting every failure (see ``cleanup_steps``)."""
    failures = run_cleanup_steps(cleanup_steps(world.ledger))
    assert not failures, f"catalog world teardown failures: {failures}"


def _admin_delete(ledger: WorldLedger, path: str) -> Reply:
    reply = rest(ledger.client, "DELETE", path, ledger.admin.get_headers())
    # A resource already gone (a rolled-back registration) needs no removal.
    assert reply.status in (200, 202, 204, 404), f"DELETE {path}: {reply.snippet()}"
    return reply


def _delete_user(ledger: WorldLedger) -> None:
    _admin_delete(ledger, f"/api/admin/users/{RESTRICTED_USER}")


def _remove_repo(ledger: WorldLedger, name: str) -> None:
    removed = _admin_delete(ledger, f"/api/admin/golden-repos/{name}")
    job_id = removed.body.get("job_id") if isinstance(removed.body, dict) else None
    if job_id:
        _wait_completed(ledger.client, ledger.admin, job_id)


def _delete_group(ledger: WorldLedger, group_id: str) -> None:
    reply = mcp(
        ledger.client,
        ledger.admin.get_headers(),
        "delete_group",
        {"group_id": group_id},
    )
    assert reply.ok, f"delete_group: {reply.snippet()}"


def _restore_and_refresh(ledger: WorldLedger, snapshot: Dict[str, bytes]) -> None:
    # Removing a repository refreshes the catalog on its own, deferred by the
    # server's debounce: a refresh starting after the restore publishes it.
    # Only when none comes within the debounce window is one submitted, so a
    # second refresh is never submitted while one runs.
    client, admin = ledger.client, ledger.admin
    wait_for_idle_catalog_refreshes(client, admin)
    restored_at = datetime.now(timezone.utc)
    restore_catalog(ledger.catalog_root, snapshot)
    if not _catalog_refreshed_after(
        client, admin, restored_at, CATALOG_DEBOUNCE_GRACE_SECONDS
    ):
        wait_for_idle_catalog_refreshes(client, admin)
        refresh_catalog(client, admin)
    wait_for_idle_catalog_refreshes(client, admin)


def cleanup_steps(ledger: WorldLedger) -> List[CleanupStep]:
    """The removal of everything *ledger* recorded: the restricted user,
    its group, each registered repository, then the catalog clone put back
    to its pre-seed state and refreshed."""
    steps: List[CleanupStep] = []
    if ledger.user_created:
        steps.append(("delete user", functools.partial(_delete_user, ledger)))
    if ledger.group_id is not None:
        steps.append(
            (
                "delete group",
                functools.partial(_delete_group, ledger, ledger.group_id),
            )
        )
    for name in reversed(ledger.repos):
        steps.append((f"remove {name}", functools.partial(_remove_repo, ledger, name)))
    if ledger.snapshot is not None and (ledger.catalog_seeded or ledger.repos):
        steps.append(
            (
                "restore catalog",
                functools.partial(_restore_and_refresh, ledger, ledger.snapshot),
            )
        )
    return steps


def wait_for_idle_catalog_refreshes(client: httpx.Client, admin: HeaderSource) -> None:
    """Wait until no repository refresh job is pending or running."""
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        busy = []
        for status in ("pending", "running"):
            listing = rest(
                client,
                "GET",
                "/api/jobs",
                admin.get_headers(),
                params={"status": status, "limit": 100},
            )
            assert listing.status == 200, f"job listing: {listing.snippet()}"
            busy += [
                job
                for job in listing.body.get("jobs", [])
                if job.get("operation_type") == "global_repo_refresh"
            ]
        if not busy:
            return
        time.sleep(JOB_POLL_SECONDS)
    raise TimeoutError("catalog refresh jobs still running")


def _catalog_last_refresh(client: httpx.Client, admin: HeaderSource) -> datetime:
    reply = mcp(
        client, admin.get_headers(), "repository_status", {"alias": CATALOG_GLOBAL}
    )
    assert reply.ok, f"repository_status: {reply.snippet()}"
    raw = str(reply.body["status"]["last_refresh"]).replace("Z", "+00:00")
    return datetime.fromisoformat(raw)


def _catalog_refreshed_after(
    client: httpx.Client, admin: HeaderSource, since: datetime, within: float
) -> bool:
    """Whether the catalog's last refresh passes *since* within *within* s."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if _catalog_last_refresh(client, admin) > since:
            return True
        time.sleep(JOB_POLL_SECONDS)
    return False


def wait_for_catalog_refresh_after(
    client: httpx.Client, admin: HeaderSource, since: datetime
) -> None:
    """Wait until the catalog was refreshed after *since* (the refresh a
    catalog write starts, possibly deferred) and no refresh is running."""
    if not _catalog_refreshed_after(client, admin, since, JOB_TIMEOUT_SECONDS):
        raise TimeoutError(f"catalog not refreshed after {since.isoformat()}")
    wait_for_idle_catalog_refreshes(client, admin)


def web_session(world: CatalogWorld, username: str, password: str) -> httpx.Client:
    """A fresh client (own cookie jar) logged in to the Web UI."""
    client = world.web_client_factory()
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "login page must carry a CSRF token"
    resp = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": match.group(1)},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"web login {username}: HTTP {resp.status_code}"
    return client
