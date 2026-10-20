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
  frontmatter lists), memories and X-Ray patterns, then refreshed so the
  semantic and full-text indexes serve it.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Protocol

import httpx

from tests.e2e.helpers import GIT_SUBPROCESS_TIMEOUT, _auth_headers, wait_for_job
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

JOB_TIMEOUT_SECONDS = 300.0
JOB_POLL_SECONDS = 1.0
INDEX_READY_TIMEOUT_SECONDS = 240.0
INDEX_READY_POLL_SECONDS = 2.0
REPLY_SNIPPET = 600
# Long enough that a search hit on it exceeds the 2000-character preview and
# is returned with a cache handle.
GUIDELINE_LINES = 80

_GAMMA_WORD = re.compile(rf"(?<![a-z0-9_-]){GAMMA}(?![a-z0-9_])", re.IGNORECASE)
_SHORT_VALUES = frozenset({SHORT_ALIAS, f"{SHORT_ALIAS}-global", f"{SHORT_ALIAS}.md"})


class HeaderSource(Protocol):
    def get_headers(self) -> dict: ...


@dataclass(frozen=True)
class Reply:
    """One front-door reply: HTTP status, parsed body (or None) and raw text."""

    status: int
    body: Any
    text: str

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


def mcp(client: httpx.Client, headers: dict, tool: str, args: Dict[str, Any]) -> Reply:
    """Call one MCP tool; the body is the decoded tool payload, or the
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
        error = envelope["error"]
        return Reply(resp.status_code, {"success": False, "error": error}, resp.text)
    result = envelope.get("result", {}) if isinstance(envelope, dict) else {}
    items = result.get("content", []) if isinstance(result, dict) else []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                return Reply(resp.status_code, json.loads(item["text"]), resp.text)
            except ValueError:
                return Reply(resp.status_code, {"text": item["text"]}, resp.text)
    return Reply(resp.status_code, result, resp.text)


def rest(
    client: httpx.Client, method: str, path: str, headers: dict, **kwargs: Any
) -> Reply:
    resp = client.request(method, path, headers=headers, **kwargs)
    try:
        body: Any = resp.json()
    except ValueError:
        body = None
    return Reply(resp.status_code, body, resp.text)


def _strip_keys(value: Any, ignore: FrozenSet[str]) -> Any:
    if isinstance(value, dict):
        return {k: _strip_keys(v, ignore) for k, v in value.items() if k not in ignore}
    if isinstance(value, list):
        return [_strip_keys(v, ignore) for v in value]
    return value


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


def leaks(reply: Reply, ignore_keys: Iterable[str] = ()) -> List[str]:
    """Every way *reply* names a repository the restricted user cannot see:
    the canary or ``gamma`` anywhere in the serialized reply, the short alias
    as a whole structured value or path segment (3-character aliases in
    prose are an accepted residual).  *ignore_keys* drops echoed request
    fields (e.g. the query text) before the scan."""
    ignore = frozenset(ignore_keys)
    found: List[str] = []
    if reply.body is None:
        text = reply.text
    else:
        stripped = _strip_keys(reply.body, ignore)
        text = json.dumps(stripped)
        _structured_short_alias(stripped, "$", found)
    if CANARY in text.lower():
        found.append(f"canary {CANARY!r} present")
    if _GAMMA_WORD.search(text):
        found.append(f"{GAMMA!r} present")
    return found


def mentions(reply: Reply, needle: str) -> bool:
    return needle.lower() in reply.text.lower()


@dataclass
class CatalogWorld:
    client: httpx.Client
    admin: HeaderSource
    restricted: HeaderSource
    restricted_password: str
    group_id: str
    web_client_factory: Callable[[], httpx.Client]

    def admin_mcp(self, tool: str, args: Dict[str, Any]) -> Reply:
        return mcp(self.client, self.admin.get_headers(), tool, args)

    def user_mcp(self, tool: str, args: Dict[str, Any]) -> Reply:
        return mcp(self.client, self.restricted.get_headers(), tool, args)

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


def _register_repos(client: httpx.Client, admin: HeaderSource, work_dir: Path) -> None:
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
        job = wait_for_job(
            client,
            reply.body["job_id"],
            _token(admin),
            timeout=JOB_TIMEOUT_SECONDS,
            poll_interval=JOB_POLL_SECONDS,
        )
        assert job.get("status") == "completed", f"register {name}: {job}"


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


def refresh_catalog_until_served(client: httpx.Client, admin: HeaderSource) -> None:
    """Refresh the catalog through REST, then wait until an admin full-text
    search serves the seeded content (the refresh publishes a new snapshot)."""
    reply = rest(
        client,
        "POST",
        f"/api/admin/golden-repos/{CATALOG}/refresh",
        admin.get_headers(),
    )
    assert reply.status == 202, f"catalog refresh: {reply.snippet()}"
    job_id = reply.body.get("job_id") if isinstance(reply.body, dict) else None
    if job_id:
        job = wait_for_job(
            client,
            job_id,
            _token(admin),
            timeout=JOB_TIMEOUT_SECONDS,
            poll_interval=JOB_POLL_SECONDS,
        )
        assert job.get("status") == "completed", f"catalog refresh: {job}"
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


def _create_restricted_user(
    client: httpx.Client, admin: HeaderSource
) -> tuple[str, str]:
    password = f"Vw{secrets.token_urlsafe(18)}9!"
    headers = admin.get_headers()
    user = mcp(
        client,
        headers,
        "create_user",
        {"username": RESTRICTED_USER, "password": password, "role": "power_user"},
    )
    assert user.ok, f"create_user: {user.snippet()}"
    group = mcp(client, headers, "create_group", {"name": RESTRICTED_GROUP})
    assert group.ok, f"create_group: {group.snippet()}"
    group_id = str(group.body["group_id"])
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
    """Build the world through the front door; idempotent per server only
    in the sense that it must run once per fresh server."""
    _register_repos(client, admin, work_dir)
    seed_catalog(catalog_root)
    refresh_catalog_until_served(client, admin)
    password, group_id = _create_restricted_user(client, admin)
    world = CatalogWorld(
        client=client,
        admin=admin,
        restricted=_login_provider(client, RESTRICTED_USER, password),
        restricted_password=password,
        group_id=group_id,
        web_client_factory=web_client_factory,
    )
    world.set_alpha_grant(True)
    return world


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


def bearer(token: str) -> dict:
    return _auth_headers(token)
