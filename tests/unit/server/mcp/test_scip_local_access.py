"""Repository access control on the SCIP query path (EVO-64616).

SCIP query delegation was removed, so every SCIP query handler reads this
pod's ``index.scip.db`` files through SCIPQueryService. These tests pin, for
all seven handlers, that the access check still holds end to end: the handler
builds SCIPQueryService with the server's access filtering service and passes
the caller's username, and ``_scan_repository_scip_files`` (the one place
every query gets its databases) never yields a repository the caller cannot
access -- named directly, in ``-global`` form, or reached by an unscoped
query. Without the access service every query is refused before any lookup,
admins included, and the refusal is logged once.

The spy records what the real ``_scan_repository_scip_files`` found and then
hands the handler no files, so nothing opens the placeholder databases.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from cidx_scip.paths import SCIP_DB_FILENAME, scip_index_dir
from code_indexer.server.auth.user_manager import User, UserRole
from cidx_scip.server import _core as scip_core
from cidx_scip.server.mcp import _common as scip_common
from cidx_scip.server.mcp import scip as scip_handlers
from code_indexer.server.services.repo_access_guard import (
    AccessFilteringServiceUnavailableError,
)
from cidx_scip.server.query_service import SCIPQueryService


@contextmanager
def _server_state(golden: Any, access: Any) -> Iterator[None]:
    """The server state the SCIP handlers read (the cidx-scip feature since
    modular phase 4d, through ``cidx_scip.server._core``): the golden repos
    directory and the access service on app.state, no repository managers."""
    state = SimpleNamespace(
        golden_repos_dir=str(golden), access_filtering_service=access
    )
    with (
        patch.object(scip_core, "app_state", return_value=state),
        patch.object(scip_core, "lazy_app_attr", return_value=None),
    ):
        yield


_TOOLS = (
    "scip_callchain",
    "scip_context",
    "scip_definition",
    "scip_dependencies",
    "scip_dependents",
    "scip_impact",
    "scip_references",
)
_OMIT = object()


class _Access:
    """AccessFilteringService stand-in: each user sees their own set."""

    def __init__(self, accessible: Dict[str, Set[str]]) -> None:
        self._accessible = accessible

    def is_admin_user(self, user_id: str) -> bool:
        return False

    def get_accessible_repos(self, user_id: str) -> Set[str]:
        return set(self._accessible.get(user_id, set()))


_ACCESS = _Access({"alice": {"flask"}})


def _user(name: str) -> User:
    user = Mock(spec=User)
    user.username = name
    user.role = UserRole.NORMAL_USER
    return user


def _golden_repos(tmp_path: Path) -> Path:
    """Two golden repositories, each with an index and a ``-global`` alias.

    The aliases matter: a named query resolves its alias and returns that
    index directly, without the directory scan, so the access check on that
    path is only exercised when the alias resolves.
    """
    root = tmp_path / "golden-repos"
    aliases = AliasManager(root / "aliases")
    for repo in ("flask", "secret"):
        scip_dir = scip_index_dir(root / repo)
        scip_dir.mkdir(parents=True)
        (scip_dir / SCIP_DB_FILENAME).write_bytes(b"")
        aliases.create_alias(f"{repo}-global", str(root / repo), repo_name=repo)
    return root


def _params(tool: str, alias: Any) -> Dict[str, Any]:
    params: Dict[str, Any] = (
        {"from_symbol": "a", "to_symbol": "b"}
        if tool == "scip_callchain"
        else {"symbol": "A"}
    )
    if alias is not _OMIT:
        params["repository_alias"] = alias
    return params


Found = List[Tuple[Dict[str, Any], List[Path]]]


def _run(
    tool: str, golden: Path, alias: Any, access: Optional[_Access] = _ACCESS
) -> Tuple[Dict[str, Any], Found, List[Any]]:
    found: Found = []
    services: List[Any] = []
    real_scan = SCIPQueryService._scan_repository_scip_files

    def spy(
        self: SCIPQueryService,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[Tuple[str, Path]]:
        services.append(self)
        pairs = real_scan(self, repository_alias=repository_alias, username=username)
        found.append(
            (
                {"repository_alias": repository_alias, "username": username},
                [path for _, path in pairs],
            )
        )
        return []

    with (
        patch.object(
            SCIPQueryService,
            "_scan_repository_scip_files",
            autospec=True,
            side_effect=spy,
        ),
        _server_state(golden, access),
        patch.object(
            scip_handlers, "check_query_admission", return_value=Mock(allowed=True)
        ),
    ):
        response = getattr(scip_handlers, tool)(_params(tool, alias), _user("alice"))
    return json.loads(response["content"][0]["text"]), found, services


def _repos(found: Found, golden: Path) -> Set[str]:
    """The golden repositories whose databases the lookups found."""
    return {path.relative_to(golden).parts[0] for _, files in found for path in files}


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("alias", ["secret", "secret-global"])
def test_an_inaccessible_alias_finds_no_index(
    tool: str, alias: str, tmp_path: Path
) -> None:
    golden = _golden_repos(tmp_path)
    body, found, _ = _run(tool, golden, alias)
    assert found, f"{tool} never looked for SCIP files"
    assert _repos(found, golden) == set()
    assert body["success"] is True, body


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("alias", ["flask", "flask-global"])
def test_an_accessible_alias_finds_its_index(
    tool: str, alias: str, tmp_path: Path
) -> None:
    golden = _golden_repos(tmp_path)
    _, found, _ = _run(tool, golden, alias)
    assert _repos(found, golden) == {"flask"}


@pytest.mark.parametrize("tool", _TOOLS)
def test_an_unscoped_query_finds_only_accessible_indexes(
    tool: str, tmp_path: Path
) -> None:
    golden = _golden_repos(tmp_path)
    _, found, _ = _run(tool, golden, _OMIT)
    assert found, f"{tool} never looked for SCIP files"
    assert _repos(found, golden) == {"flask"}


@pytest.mark.parametrize("tool", _TOOLS)
def test_every_lookup_carries_the_caller_and_the_access_service(
    tool: str, tmp_path: Path
) -> None:
    _, found, services = _run(tool, _golden_repos(tmp_path), "flask")
    assert [call["username"] for call, _ in found] == ["alice"] * len(found)
    assert all(svc.access_filtering_service is _ACCESS for svc in services)


def test_a_caller_granted_both_repositories_sees_both(tmp_path: Path) -> None:
    # Control: the same tree yields both repositories to a caller granted both,
    # so the empty and {"flask"} results above come from the access check and
    # not from a fixture that only ever held one repository.
    golden = _golden_repos(tmp_path)
    both = _Access({"alice": {"flask", "secret"}})
    _, found, _ = _run("scip_definition", golden, _OMIT, access=both)
    assert _repos(found, golden) == {"flask", "secret"}


# -- no access service: fail closed ------------------------------------------

_EMPTY_KEY = {
    "scip_callchain": "chains",
    "scip_context": "files",
    "scip_impact": "affected_symbols",
}


@pytest.fixture
def refusal_not_yet_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts as a fresh process would: refusal not logged yet."""
    monkeypatch.setattr(scip_common, "_scip_access_refusal_logged", False)


@pytest.mark.usefixtures("refusal_not_yet_logged")
@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("alias", ["flask", "flask-global", _OMIT])
def test_without_the_access_service_every_query_is_refused(
    tool: str, alias: Any, tmp_path: Path
) -> None:
    body, found, _ = _run(tool, _golden_repos(tmp_path), alias, access=None)
    assert body["success"] is False
    assert body["error"] == scip_common.SCIP_ACCESS_CONTROL_UNAVAILABLE
    assert body[_EMPTY_KEY.get(tool, "results")] == []
    assert found == [], "no SCIP index may be looked up without access control"


@pytest.mark.usefixtures("refusal_not_yet_logged")
@pytest.mark.parametrize("tool", _TOOLS)
def test_an_admin_is_refused_too(tool: str, tmp_path: Path) -> None:
    # Admin status cannot be established without the service either.
    admin = Mock(spec=User)
    admin.username = "root"
    admin.role = UserRole.ADMIN
    with (
        _server_state(tmp_path, None),
        patch.object(
            scip_handlers, "check_query_admission", return_value=Mock(allowed=True)
        ),
        patch.object(
            SCIPQueryService, "_scan_repository_scip_files", autospec=True
        ) as find,
    ):
        response = getattr(scip_handlers, tool)(_params(tool, _OMIT), admin)
    body = json.loads(response["content"][0]["text"])
    assert body["success"] is False
    assert body["error"] == scip_common.SCIP_ACCESS_CONTROL_UNAVAILABLE
    find.assert_not_called()


@pytest.mark.usefixtures("refusal_not_yet_logged")
def test_the_refusal_is_logged_once_per_process(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    golden = _golden_repos(tmp_path)
    with caplog.at_level(logging.DEBUG):
        for tool in _TOOLS:
            _run(tool, golden, "flask", access=None)
            _run(tool, golden, _OMIT, access=None)
    refusals = [r for r in caplog.records if "QUERY-MIGRATE-015" in r.getMessage()]
    assert len(refusals) == 1
    assert refusals[0].levelno == logging.ERROR
    # The handlers do not add a traceback per refused query.
    assert not [
        r for r in caplog.records if r.getMessage().startswith("Error in scip_")
    ]


def test_other_handler_errors_still_log_their_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch.object(
            scip_handlers,
            "_get_scip_query_service",
            side_effect=RuntimeError("golden_repos_dir not configured"),
        ),
        patch.object(scip_core, "lazy_app_attr", return_value=None),
        caplog.at_level(logging.ERROR),
    ):
        scip_handlers.scip_definition({"symbol": "A"}, _user("alice"))
    errors = [r for r in caplog.records if r.getMessage().startswith("Error in scip_")]
    assert len(errors) == 1
    assert errors[0].exc_info is not None


@pytest.mark.usefixtures("refusal_not_yet_logged")
def test_the_getter_refuses_before_reading_any_other_state() -> None:
    with (
        patch.object(
            scip_core,
            "app_state",
            return_value=SimpleNamespace(access_filtering_service=None),
        ),
        patch.object(scip_common, "_get_golden_repos_dir") as golden,
        pytest.raises(AccessFilteringServiceUnavailableError),
    ):
        scip_common._get_scip_query_service()
    golden.assert_not_called()


def test_require_scip_access_service_returns_the_service_it_was_given() -> None:
    assert scip_common.require_scip_access_service(_ACCESS) is _ACCESS


@pytest.mark.usefixtures("refusal_not_yet_logged")
def test_require_scip_access_service_refuses_none() -> None:
    with pytest.raises(
        AccessFilteringServiceUnavailableError,
        match="SCIP query refused: repository access control is unavailable",
    ):
        scip_common.require_scip_access_service(None)
