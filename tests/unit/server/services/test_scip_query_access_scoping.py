"""SCIP context and impact queries search only repositories the caller may access.

Two golden repositories carry real SCIP indexes: ``repo-a`` (granted to the
caller's group) and ``repo-b`` (granted to another group only). Impact and
context queries, with and without a repository alias, must return repo-a's
data and nothing from repo-b -- through the service and through both front
doors (MCP handlers and REST routes). An admin (admins group) sees both.

Each repository's file and symbol names carry a unique tag (``repoa`` /
``repob``), so any unexpected row is visible in the serialized response.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.services.scip_query_service import SCIPQueryService

SYMBOL = "ExampleService"
GRANTED_TAG = "repoa"
UNGRANTED_TAG = "repob"
SOURCE = "class ExampleService:\n    pass\ndef run():\n    ExampleService()\n"


def _build_repo(repo: Path, tag: str) -> None:
    """A repository whose index defines ExampleService and a run() caller."""
    from code_indexer.scip.database.builder import ROLE_DEFINITION, SCIPDatabaseBuilder
    from code_indexer.scip.protobuf import scip_pb2

    source = f"{tag}_svc.py"
    repo.mkdir(parents=True)
    (repo / source).write_text(SOURCE)
    # scip_pb2 is protoc-generated: its classes are built at runtime, so mypy
    # cannot see them.
    index = scip_pb2.Index()  # type: ignore[attr-defined]
    target = f"python {tag} 1.0 `{tag}`/ExampleService#"
    caller = f"python {tag} 1.0 `{tag}`/run()."
    kinds = scip_pb2.SymbolInformation  # type: ignore[attr-defined]
    for symbol, kind in ((target, kinds.Class), (caller, kinds.Function)):
        info = index.external_symbols.add()
        info.symbol, info.kind = symbol, kind
    doc = index.documents.add()
    doc.relative_path, doc.language = source, "python"
    occ = doc.occurrences.add()
    occ.symbol, occ.symbol_roles = target, ROLE_DEFINITION
    occ.range.extend([0, 6, 0, 20])
    occ = doc.occurrences.add()
    occ.symbol, occ.symbol_roles = caller, ROLE_DEFINITION
    occ.range.extend([2, 4, 2, 7])
    occ.enclosing_range.extend([2, 0, 3, 20])
    occ = doc.occurrences.add()
    occ.symbol, occ.symbol_roles = target, 0
    occ.range.extend([3, 4, 3, 18])
    scip_dir = repo / ".code-indexer" / "scip"
    scip_dir.mkdir(parents=True)
    (scip_dir / "index.scip").write_bytes(index.SerializeToString())
    SCIPDatabaseBuilder().build(scip_dir / "index.scip", scip_dir / "index.scip.db")
    (scip_dir / "index.scip").unlink()
    # One open through the real backend runs the production migration (query
    # indexes + version marker), so every later open is the fast-path check.
    from code_indexer.scip.query.backends import DatabaseBackend

    DatabaseBackend(scip_dir / "index.scip.db").conn.close()


def _user(name: str, role: UserRole) -> User:
    return User(
        username=name,
        email=f"{name}@example.com",
        role=role,
        password_hash="unused",
        created_at=datetime.now(timezone.utc),
    )


ALICE = _user("alice", UserRole.NORMAL_USER)
ROOT = _user("root", UserRole.ADMIN)


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    """Built once per module: every test only reads the repositories, the
    SCIP databases and the group grants."""
    tmp_path = tmp_path_factory.mktemp("scip-access")
    golden = tmp_path / "golden-repos"
    _build_repo(golden / "repo-a", GRANTED_TAG)
    _build_repo(golden / "repo-b", UNGRANTED_TAG)
    groups = GroupAccessManager(tmp_path / "groups.db")
    team_a = groups.create_group("team-a", "granted repo-a")
    team_b = groups.create_group("team-b", "granted repo-b")
    groups.grant_repo_access("repo-a", team_a.id, "root")
    groups.grant_repo_access("repo-b", team_b.id, "root")
    groups.assign_user_to_group(ALICE.username, team_a.id, "root")
    admins = groups.get_group_by_name("admins")
    assert admins is not None
    groups.assign_user_to_group(ROOT.username, admins.id, "root")
    return {"golden": golden, "access": AccessFilteringService(groups)}


@pytest.fixture
def app_state(env: Dict[str, Any]) -> Iterator[Any]:
    """Wire the real app's state for the MCP and REST front doors."""
    from code_indexer.server.app import app

    missing = object()
    saved = {
        name: getattr(app.state, name, missing)
        for name in ("golden_repos_dir", "access_filtering_service")
    }
    app.state.golden_repos_dir = str(env["golden"])
    app.state.access_filtering_service = env["access"]
    try:
        yield app
    finally:
        for name, value in saved.items():
            if value is missing:
                delattr(app.state, name)
            else:
                setattr(app.state, name, value)


def _via_service(env: Dict[str, Any], kind: str, user: User, alias: Optional[str]):
    service = SCIPQueryService(
        golden_repos_dir=env["golden"], access_filtering_service=env["access"]
    )
    if kind == "impact":
        return service.analyze_impact(
            SYMBOL, depth=1, repository_alias=alias, username=user.username
        )
    return service.get_context(
        SYMBOL, repository_alias=alias, username=user.username, timeout_seconds=0
    )


def _via_mcp(app: Any, kind: str, user: User, alias: Optional[str]):
    from code_indexer.server.mcp.handlers.scip import scip_context, scip_impact

    params: Dict[str, Any] = {"symbol": SYMBOL}
    if alias is not None:
        params["repository_alias"] = alias
    if kind == "impact":
        params["depth"] = 1
        response = scip_impact(params, user)
    else:
        response = scip_context(params, user)
    payload = json.loads(response["content"][0]["text"])
    assert payload["success"] is True, payload
    return payload


def _via_rest(app: Any, kind: str, user: User, alias: Optional[str]):
    from fastapi.testclient import TestClient

    from code_indexer.server.auth.dependencies import get_current_user

    query: Dict[str, Any] = {"symbol": SYMBOL}
    if alias is not None:
        query["project"] = alias
    if kind == "impact":
        query["depth"] = 1
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(f"/scip/{kind}", params=query)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200, response.text
    return response.json()


def _run(
    door: str, env: Dict[str, Any], request: Any
) -> Callable[[str, User, Optional[str]], Any]:
    if door == "service":
        return lambda kind, user, alias: _via_service(env, kind, user, alias)
    app = request.getfixturevalue("app_state")
    caller = _via_mcp if door == "mcp" else _via_rest
    return lambda kind, user, alias: caller(app, kind, user, alias)


DOORS = ["service", "mcp", "rest"]
KINDS = ["impact", "context"]


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("alias", [None, "repo-a"])
def test_caller_sees_granted_repository_only(env, request, door, kind, alias):
    result = _run(door, env, request)(kind, ALICE, alias)

    serialized = json.dumps(result)
    assert GRANTED_TAG in serialized
    assert UNGRANTED_TAG not in serialized


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("kind", KINDS)
def test_admin_sees_every_granted_repository(env, request, door, kind):
    result = _run(door, env, request)(kind, ROOT, None)

    serialized = json.dumps(result)
    assert GRANTED_TAG in serialized
    assert UNGRANTED_TAG in serialized


# -- Fail closed: no access service or no caller identity ---------------------

SERVICE_OPERATIONS: Dict[str, Callable[[SCIPQueryService, Optional[str]], Any]] = {
    "find_scip_files": lambda s, u: s.find_scip_files(username=u),
    "definition": lambda s, u: s.find_definition(SYMBOL, username=u),
    "references": lambda s, u: s.find_references(SYMBOL, username=u),
    "dependencies": lambda s, u: s.get_dependencies(SYMBOL, username=u),
    "dependents": lambda s, u: s.get_dependents(SYMBOL, username=u),
    "impact": lambda s, u: s.analyze_impact(SYMBOL, depth=1, username=u),
    "callchain": lambda s, u: s.trace_callchain(SYMBOL, "run", username=u),
    "context": lambda s, u: s.get_context(SYMBOL, username=u, timeout_seconds=0),
}


@pytest.mark.parametrize("operation", sorted(SERVICE_OPERATIONS))
def test_service_without_access_service_fails_closed(env, operation):
    from code_indexer.server.services.repo_access_guard import (
        AccessFilteringServiceUnavailableError,
    )

    service = SCIPQueryService(
        golden_repos_dir=env["golden"], access_filtering_service=None
    )

    with pytest.raises(AccessFilteringServiceUnavailableError):
        SERVICE_OPERATIONS[operation](service, ALICE.username)


@pytest.mark.parametrize("username", [None, ""])
@pytest.mark.parametrize("operation", sorted(SERVICE_OPERATIONS))
def test_service_without_caller_identity_fails_closed(
    env, monkeypatch, operation, username
):
    """Refused before any index access: the golden-repos directory, where
    every discovery path starts, is never consulted."""
    from code_indexer.server.services.repo_access_guard import (
        AccessFilteringServiceUnavailableError,
    )

    service = SCIPQueryService(
        golden_repos_dir=env["golden"], access_filtering_service=env["access"]
    )
    consulted: List[str] = []

    def golden_repos_dir_sentinel() -> Path:
        consulted.append("golden-repos dir")
        return Path(env["golden"])

    monkeypatch.setattr(service, "get_golden_repos_dir", golden_repos_dir_sentinel)

    with pytest.raises(AccessFilteringServiceUnavailableError):
        SERVICE_OPERATIONS[operation](service, username)

    assert consulted == []


@pytest.fixture
def app_state_without_access(app_state: Any) -> Iterator[Any]:
    """The real app with golden repos wired but no access filtering service."""
    service = app_state.state.access_filtering_service
    del app_state.state.access_filtering_service
    try:
        yield app_state
    finally:
        app_state.state.access_filtering_service = service


def _assert_refused(payload: Dict[str, Any]) -> None:
    assert payload["success"] is False, payload
    assert "access control unavailable" in payload["error"].lower()
    serialized = json.dumps(payload)
    assert GRANTED_TAG not in serialized and UNGRANTED_TAG not in serialized


@pytest.mark.parametrize("kind", KINDS)
def test_mcp_handler_without_access_service_fails_closed(
    env, app_state_without_access, kind
):
    from code_indexer.server.mcp.handlers.scip import scip_context, scip_impact

    handler = scip_impact if kind == "impact" else scip_context
    response = handler({"symbol": SYMBOL}, ALICE)

    _assert_refused(json.loads(response["content"][0]["text"]))


def _dispatch(tool: str, user: User) -> Dict[str, Any]:
    """Call a tool through the real MCP dispatcher (tools/call)."""
    import asyncio

    from code_indexer.server.mcp.protocol import handle_tools_call

    response = asyncio.run(
        handle_tools_call(
            params={"name": tool, "arguments": {"symbol": SYMBOL}}, user=user
        )
    )
    payload: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return payload


@pytest.mark.parametrize("tool", ["scip_context", "scip_impact"])
def test_mcp_dispatcher_returns_granted_repository_only(env, app_state, tool):
    payload = _dispatch(tool, ALICE)

    assert payload["success"] is True, payload
    serialized = json.dumps(payload)
    assert GRANTED_TAG in serialized
    assert UNGRANTED_TAG not in serialized


@pytest.mark.parametrize("tool", ["scip_context", "scip_impact"])
def test_mcp_dispatcher_without_access_service_fails_closed(
    env, app_state_without_access, tool
):
    _assert_refused(_dispatch(tool, ALICE))


@pytest.mark.parametrize("kind", KINDS)
def test_rest_without_access_service_fails_closed(env, app_state_without_access, kind):
    from fastapi.testclient import TestClient

    from code_indexer.server.auth.dependencies import get_current_user

    app = app_state_without_access
    app.dependency_overrides[get_current_user] = lambda: ALICE
    try:
        response = TestClient(app).get(f"/scip/{kind}", params={"symbol": SYMBOL})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code >= 500, response.text
    assert "access_control_unavailable" in response.text
    assert GRANTED_TAG not in response.text and UNGRANTED_TAG not in response.text
