"""The MCP dispatcher's repository pre-check judges a caller's own activation
by its source repositories, the same rule the query access filter applies.

Driven through the real ``POST /mcp`` endpoint of a real app (``create_app``
via ``isolated_app``, never ~/.cidx-server). The services the tools reach
are the real ones (QueryAccessEnv): AccessFilteringService over a real
GroupAccessManager, real ActivatedRepoManager activations (real git clones),
GoldenRepoManager, global registry and alias files. Only the external
embedding + HNSW search boundary is replaced.

Repository aliases and usernames are neutral placeholders.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.mcp.session_registry import get_session_registry
from code_indexer.server.mcp.tool_access import ToolAccessMemo
from code_indexer.server.services.auto_watch_manager import auto_watch_manager
from code_indexer.server.services.file_service import file_service
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    ALL_ROWS_LIMIT,
    GRANTED_REPO,
    GRANTED_REPO_2,
    OWN_ACTIVATION,
    ROWS_PER_REPO,
    UNGRANTED_REPO,
    USER,
    QueryAccessEnv,
    build_server_db_template,
    global_alias,
)

PASSWORD = "Example-Activation-Passw0rd!"
# A second non-admin in USER's group: granted the same repositories.
PEER = "example_peer"
PEER_ACTIVATION = "peer-repo"
# A power user (activates and writes) in USER's group.
POWER = "example_power"
USER_EMAIL = "example.user@example.com"
COMPOSITE_ACTIVATION = "my-composite"
# An alias no activation or golden repository carries.
UNKNOWN_ALIAS = "never-activated-repo"
# Every example repository's README.md holds this text (_init_git_repo).
README = "README.md"
README_TEXT = "example"

# The module-scoped real app via isolated_app (~7 s alone) is paid by
# whichever test runs first, slower under parallel gate load.
pytestmark = pytest.mark.timeout(45)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    with isolated_app(tmp_path_factory.mktemp("activation-precheck-app")) as app:
        accounts: UserManager = app.state.user_manager
        accounts.create_user(USER, PASSWORD, UserRole.NORMAL_USER)
        # acting_users names its users by email.
        accounts.update_user(USER, new_email=USER_EMAIL)
        accounts.create_user(PEER, PASSWORD, UserRole.NORMAL_USER)
        accounts.create_user(POWER, PASSWORD, UserRole.POWER_USER)
        accounts.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        yield TestClient(app, follow_redirects=False)


@pytest.fixture(scope="module")
def server_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_server_db_template(tmp_path_factory.mktemp("server_db_template"))


@pytest.fixture
def env(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_db_template: Path,
) -> Iterator[QueryAccessEnv]:
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    e = QueryAccessEnv(tmp_path, server_db_template)
    for member in (PEER, POWER):
        e.group_manager.assign_user_to_group(member, e.group_id, assigned_by="test")
    # The process-wide file service resolves activations through this env's
    # real manager (its plain backing attribute: reading the property would
    # construct a manager under the real home).
    monkeypatch.setattr(
        file_service, "_activated_repo_manager_lazy", e.activated_repo_manager
    )
    try:
        with e.installed(e.access_service):
            yield e
    finally:
        e.close()


@pytest.fixture
def app_state(client: TestClient, env: QueryAccessEnv) -> Any:
    """The installed stand-in app's state, also exposing the real account
    store (acting_users resolution) and the env's activation manager (file
    writes). The stand-in is discarded when ``env`` uninstalls it."""
    state = vars(importlib.import_module("code_indexer.server.app"))["app"].state
    state.user_manager = client.app.state.user_manager  # type: ignore[attr-defined]
    state.activated_repo_manager = env.activated_repo_manager
    return state


def _mcp(
    client: TestClient,
    username: str,
    tool: str,
    arguments: Dict[str, Any],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One ``tools/call`` through ``POST /mcp``; the JSON-RPC body."""
    account = client.app.state.user_manager.get_user(username)  # type: ignore[attr-defined]
    assert account is not None
    jwt = dependencies.jwt_manager
    assert jwt is not None
    token = jwt.create_token({"username": username, "role": account.role.value})
    headers = {"Authorization": f"Bearer {token}"}
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    client.cookies.clear()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


def _payload(body: Dict[str, Any]) -> Dict[str, Any]:
    assert "result" in body, body
    payload: Dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return payload


def _refusal(body: Dict[str, Any], alias: str) -> Dict[str, Any]:
    """The pre-check's JSON-RPC refusal, with the requested alias masked."""
    assert "error" in body, body
    error: Dict[str, Any] = body["error"]
    assert "Access denied" in error["message"], error
    return {**error, "message": error["message"].replace(f"'{alias}'", "'<alias>'")}


def _search(alias: Union[str, List[str]]) -> Dict[str, Any]:
    return {"query_text": "find", "repository_alias": alias, "limit": ALL_ROWS_LIMIT}


def _regex(alias: str) -> Dict[str, Any]:
    return {"pattern": README_TEXT, "repository_alias": alias}


def _read(alias: str) -> Dict[str, Any]:
    return {"repository_alias": alias, "file_path": README}


def _rows_of(payload: Dict[str, Any], alias: str) -> list:
    return [r for r in payload["results"]["results"] if r["repository_alias"] == alias]


def _file_text(payload: Dict[str, Any]) -> str:
    assert payload["success"] is True, payload
    return "".join(str(block.get("text", "")) for block in payload["file_content"])


def _impersonating(client: TestClient, target: str) -> str:
    """An ADMIN MCP session that impersonates *target*; its session id."""
    accounts = client.app.state.user_manager  # type: ignore[attr-defined]
    session_id = f"example-session-{uuid.uuid4()}"
    session = get_session_registry().get_or_create_session(
        session_id, accounts.get_user(ADMIN)
    )
    session.set_impersonation(accounts.get_user(target))
    return session_id


class TestOwnCustomAliasActivation:
    def test_search_code_own_custom_alias_is_allowed(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        payload = _payload(_mcp(client, USER, "search_code", _search(OWN_ACTIVATION)))

        assert payload["success"] is True, payload
        assert len(_rows_of(payload, OWN_ACTIVATION)) == ROWS_PER_REPO

    def test_regex_search_own_custom_alias_is_allowed(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        payload = _payload(_mcp(client, USER, "regex_search", _regex(OWN_ACTIVATION)))

        assert payload["success"] is True, payload
        assert [m["file_path"] for m in payload["matches"]] == [README]

    def test_get_file_content_own_custom_alias_is_allowed(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        body = _mcp(client, USER, "get_file_content", _read(OWN_ACTIVATION))

        assert README_TEXT in _file_text(_payload(body))


class TestRefusals:
    @pytest.mark.parametrize(
        "tool,arguments",
        [("search_code", _search), ("get_file_content", _read)],
        ids=["search_code", "get_file_content"],
    )
    def test_another_users_alias_is_refused_as_unknown(
        self, client, env, tool, arguments
    ):
        env.activate_for(PEER, GRANTED_REPO, PEER_ACTIVATION)

        theirs = _mcp(client, USER, tool, arguments(PEER_ACTIVATION))
        unknown = _mcp(client, USER, tool, arguments(UNKNOWN_ALIAS))

        assert _refusal(theirs, PEER_ACTIVATION) == _refusal(unknown, UNKNOWN_ALIAS)

    def test_ungranted_golden_alias_is_refused(self, client, env):
        body = _mcp(client, USER, "search_code", _search(global_alias(UNGRANTED_REPO)))

        _refusal(body, global_alias(UNGRANTED_REPO))

    def test_own_activation_of_ungranted_repo_is_refused(self, client, env):
        env.activate_for(USER, UNGRANTED_REPO, OWN_ACTIVATION)

        body = _mcp(client, USER, "get_file_content", _read(OWN_ACTIVATION))

        _refusal(body, OWN_ACTIVATION)

    def test_activation_whose_source_grant_was_lost_is_refused(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        env.group_manager.revoke_repo_access(GRANTED_REPO, env.group_id)

        for tool, arguments in (("search_code", _search), ("get_file_content", _read)):
            _refusal(
                _mcp(client, USER, tool, arguments(OWN_ACTIVATION)), OWN_ACTIVATION
            )

    def test_composite_with_one_ungranted_source_is_refused(self, client, env):
        """Every source of a composite activation must be granted."""
        job_id = env.activated_repo_manager.activate_repository(
            username=USER,
            golden_repo_aliases=[GRANTED_REPO, UNGRANTED_REPO],
            user_alias=COMPOSITE_ACTIVATION,
        )
        assert env.wait_for_job(job_id, USER)["status"] == "completed"

        body = _mcp(client, USER, "search_code", _search(COMPOSITE_ACTIVATION))

        _refusal(body, COMPOSITE_ACTIVATION)

    def test_omni_list_mixing_own_alias_and_ungranted_alias_is_refused(
        self, client, env
    ):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        ungranted = global_alias(UNGRANTED_REPO)

        body = _mcp(client, USER, "search_code", _search([OWN_ACTIVATION, ungranted]))

        _refusal(body, ungranted)

    def test_alias_naming_a_granted_repo_but_sourced_from_ungranted_is_refused(
        self, client, env
    ):
        """The alias is a granted golden name, but the caller's activation
        under it holds an ungranted repository: both must be granted."""
        env.activate_for(USER, UNGRANTED_REPO, GRANTED_REPO_2)

        body = _mcp(client, USER, "get_file_content", _read(GRANTED_REPO_2))

        _refusal(body, GRANTED_REPO_2)


class TestLookupFailure:
    # A server path a failing lookup may carry; it must never reach a client.
    SERVER_PATH = "/srv/example-data/activated-repos/example_user"

    @pytest.mark.parametrize(
        "failure",
        [OSError(5, "Input/output error", SERVER_PATH), AttributeError(SERVER_PATH)],
        ids=["oserror", "attributeerror"],
    )
    def test_failing_activation_lookup_is_refused_with_a_fixed_message(
        self, client, env, monkeypatch, failure
    ):
        def _failing_lookup(user_id: str) -> Any:
            raise failure

        monkeypatch.setattr(
            env.access_service, "caller_activation_sources", _failing_lookup
        )

        body = _mcp(client, USER, "search_code", _search(OWN_ACTIVATION))

        assert "error" in body, body
        message = body["error"]["message"]
        assert "Access denied: access check failed" in message, body
        assert "unavailable" not in message, body
        assert self.SERVER_PATH not in json.dumps(body), body


class TestActingUsersResolutionFailure:
    SERVER_PATH = TestLookupFailure.SERVER_PATH

    @pytest.mark.parametrize(
        "owner,method", [("accounts", "get_user_by_email"), ("access", "is_admin_user")]
    )
    def test_failing_acting_users_resolution_is_refused_with_a_fixed_message(
        self, client, env, app_state, monkeypatch, owner, method
    ):
        def _failing(*_args: Any) -> Any:
            raise OSError(5, "Input/output error", self.SERVER_PATH)

        target = app_state.user_manager if owner == "accounts" else env.access_service
        monkeypatch.setattr(target, method, _failing)

        body = _mcp(
            client,
            ADMIN,
            "get_file_content",
            {**_read(global_alias(GRANTED_REPO)), "acting_users": [USER_EMAIL]},
        )

        assert "error" in body, body
        assert "Access denied: access check failed" in body["error"]["message"], body
        assert self.SERVER_PATH not in json.dumps(body), body


class TestMalformedRepositoryParameter:
    @pytest.mark.parametrize(
        "value",
        [42, {"alias": GRANTED_REPO}, [[GRANTED_REPO]], [GRANTED_REPO, 7]],
        ids=["int", "dict", "nested-list", "list-with-int"],
    )
    def test_malformed_repository_parameter_is_rejected(self, client, env, value):
        """A repository parameter is a string or a list of strings; anything
        else is refused before any handler can trip over it."""
        body = _mcp(
            client,
            USER,
            "search_code",
            {"query_text": "find", "repository_alias": value},
        )

        assert "error" in body, body
        assert body["error"]["code"] == -32602, body
        assert "repository_alias" in body["error"]["message"], body
        assert "string" in body["error"]["message"], body


class TestGoldenOnlyParameters:
    def test_custom_activation_alias_as_golden_repo_alias_is_refused(self, client, env):
        """golden_repo_alias can only name a golden repository: the caller's
        own activation alias is not one, whatever its sources."""
        env.activate_for(POWER, GRANTED_REPO, OWN_ACTIVATION)

        body = _mcp(
            client,
            POWER,
            "activate_repository",
            {"golden_repo_alias": OWN_ACTIVATION, "user_alias": "second-copy"},
        )

        _refusal(body, OWN_ACTIVATION)


class TestWikiAnalyticsSearchFilter:
    """wiki_article_analytics names a golden wiki; its search filter reads
    that golden repository, never the caller's activation of the name."""

    def _wiki_views(self, env: QueryAccessEnv) -> List[str]:
        """Enable the wiki on GRANTED_REPO_2 and record a view per article
        the search boundary returns for it; the article paths."""
        from code_indexer.server.wiki.wiki_cache import WikiCache

        env.golden_repo_manager.set_wiki_enabled(GRANTED_REPO_2, True)
        cache = WikiCache(env.golden_repo_manager.db_path)
        paths = [f"src/{GRANTED_REPO_2}_{i}.py" for i in range(ROWS_PER_REPO)]
        for path in paths:
            cache.increment_view(GRANTED_REPO_2, path)
        return paths

    def _analytics(self, client: TestClient, alias: str) -> Dict[str, Any]:
        return _payload(
            _mcp(
                client,
                USER,
                "wiki_article_analytics",
                {"repo_alias": alias, "search_query": "example query"},
            )
        )

    @pytest.mark.parametrize(
        "alias", [GRANTED_REPO_2, global_alias(GRANTED_REPO_2)], ids=["bare", "global"]
    )
    def test_search_filter_reads_the_golden_wiki(self, client, env, alias):
        self._wiki_views(env)

        payload = self._analytics(client, alias)

        assert payload["success"] is True, payload
        assert str(env.repo_paths[GRANTED_REPO_2]) in env.searched_paths, (
            env.searched_paths
        )

    def test_colliding_activation_never_feeds_the_search_filter(self, client, env):
        """The caller's activation named like the wiki holds an ungranted
        repository: it is never searched, so it cannot shape the result."""
        self._wiki_views(env)
        env.activate_for(USER, UNGRANTED_REPO, GRANTED_REPO_2)
        activation = env.activated_repo_manager.get_activated_repo_path(
            username=USER, user_alias=GRANTED_REPO_2
        )

        payload = self._analytics(client, GRANTED_REPO_2)

        assert payload["success"] is True, payload
        assert str(activation) not in env.searched_paths, env.searched_paths
        assert str(env.repo_paths[GRANTED_REPO_2]) in env.searched_paths, (
            env.searched_paths
        )


class TestWriteTool:
    NEW_FILE = "notes/example.txt"

    def _create(self, client: TestClient, alias: str) -> Dict[str, Any]:
        return _mcp(
            client,
            POWER,
            "create_file",
            {"repository_alias": alias, "file_path": self.NEW_FILE, "content": "x\n"},
        )

    def test_create_file_on_own_granted_activation_is_allowed(
        self, client, env, app_state, monkeypatch
    ):
        # Auto-watch is a separate feature; switch it off with its real switch.
        monkeypatch.setattr(auto_watch_manager, "auto_watch_enabled", False)
        env.activate_for(POWER, GRANTED_REPO, OWN_ACTIVATION)

        payload = _payload(self._create(client, OWN_ACTIVATION))

        assert payload["success"] is True, payload
        repo = env.activated_repo_manager.get_activated_repo_path(
            username=POWER, user_alias=OWN_ACTIVATION
        )
        assert (Path(repo) / self.NEW_FILE).read_text() == "x\n"

    def test_create_file_on_peers_alias_is_refused(self, client, env):
        env.activate_for(PEER, GRANTED_REPO, PEER_ACTIVATION)

        _refusal(self._create(client, PEER_ACTIVATION), PEER_ACTIVATION)


class TestImpersonation:
    def test_impersonated_users_own_activation_is_allowed(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        session_id = _impersonating(client, USER)

        body = _mcp(
            client, ADMIN, "get_file_content", _read(OWN_ACTIVATION), session_id
        )

        assert README_TEXT in _file_text(_payload(body))

    def test_impersonated_peer_cannot_use_anothers_activation(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        session_id = _impersonating(client, PEER)

        body = _mcp(client, ADMIN, "search_code", _search(OWN_ACTIVATION), session_id)

        _refusal(body, OWN_ACTIVATION)


class TestActingUsersScope:
    def test_acting_users_cannot_reach_admins_ungranted_activation_by_granted_name(
        self, client, env, app_state
    ):
        """Story #568: acting_users only narrows. The admin's own activation
        of a repository the acting user is NOT granted, under an alias equal
        to one they ARE granted, stays out of reach."""
        env.activate_for(ADMIN, UNGRANTED_REPO, GRANTED_REPO_2)

        body = _mcp(
            client,
            ADMIN,
            "get_file_content",
            {**_read(GRANTED_REPO_2), "acting_users": [USER_EMAIL]},
        )

        assert "acting users" in _refusal(body, GRANTED_REPO_2)["message"], body


class TestGoldenAliasesUnchanged:
    def test_global_alias_of_granted_repo_is_allowed(self, client, env):
        alias = global_alias(GRANTED_REPO)

        payload = _payload(_mcp(client, USER, "search_code", _search(alias)))

        assert payload["success"] is True, payload
        assert len(_rows_of(payload, alias)) == ROWS_PER_REPO

    def test_bare_alias_falls_back_to_global(self, client, env):
        """Story #1039: a bare granted golden alias the caller has not
        activated is read through its -global form."""
        body = _mcp(client, USER, "get_file_content", _read(GRANTED_REPO))

        assert README_TEXT in _file_text(_payload(body))


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class TestPrecheckRunsOffTheEventLoop:
    def test_precheck_reads_activations_off_the_event_loop(
        self, client, env, monkeypatch
    ):
        """The activation lookup is filesystem/database I/O: the dispatcher
        runs the pre-check on a worker thread, never on the event loop."""
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        real_lookup = env.access_service.caller_activation_sources
        on_loop: List[bool] = []

        def _recording_lookup(user_id: str) -> Any:
            on_loop.append(_on_event_loop())
            return real_lookup(user_id)

        monkeypatch.setattr(
            env.access_service, "caller_activation_sources", _recording_lookup
        )

        _mcp(client, USER, "regex_search", _regex(OWN_ACTIVATION))

        # The first lookup is the dispatcher's pre-check.
        assert on_loop and on_loop[0] is False, on_loop

    def test_tool_grant_check_runs_off_the_event_loop(self, client, env, monkeypatch):
        """The group tool-grant check queries the group store."""
        real_is_allowed = ToolAccessMemo.is_allowed
        on_loop: List[bool] = []

        def _recording_is_allowed(memo: Any, tool_name: str, user: Any) -> Any:
            on_loop.append(_on_event_loop())
            return real_is_allowed(memo, tool_name, user)

        monkeypatch.setattr(ToolAccessMemo, "is_allowed", _recording_is_allowed)

        _mcp(client, USER, "search_code", _search(global_alias(GRANTED_REPO)))

        # The first grant check is the dispatcher's.
        assert on_loop and on_loop[0] is False, on_loop

    def test_acting_users_resolution_runs_off_the_event_loop(
        self, client, env, app_state, monkeypatch
    ):
        """Resolving acting_users reads admin status and grants."""
        service = env.access_service
        on_loop: List[bool] = []
        for name in ("is_admin_user", "get_accessible_repos"):
            real = getattr(service, name)

            def _recording(user_id: str, _real: Any = real) -> Any:
                on_loop.append(_on_event_loop())
                return _real(user_id)

            monkeypatch.setattr(service, name, _recording)

        _mcp(
            client,
            ADMIN,
            "get_file_content",
            {**_read(global_alias(GRANTED_REPO)), "acting_users": [USER_EMAIL]},
        )

        assert on_loop and not any(on_loop), on_loop
