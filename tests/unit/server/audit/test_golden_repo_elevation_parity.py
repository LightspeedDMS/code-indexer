"""Golden-repository add, remove and refresh require TOTP elevation on every door.

REST, MCP and Web apply the same rule: with elevation enforcement on, an
admin must hold an active elevation window of their own; without one the
request is refused with the standard error code and nothing is submitted.
An admin with no TOTP set up is told to set it up first.

Harness: ``_audit_front_doors`` (real auth components, real managers, real
bound audit store) with elevation enforcement switched ON for these tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pytest

from _audit_front_doors import ACTING_ADMIN, DoorsEnv, front_door_env
from _audit_repos_support import EXAMPLE_ALIAS
from code_indexer.server.middleware.audit_request_context import (
    bind_audit_request_context,
    build_request_context,
    reset_audit_request_context,
)
from tests.unit.server.self_service_elevation_harness import enforcement


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[DoorsEnv]:
    for doors in front_door_env(tmp_path, monkeypatch):
        doors.stack.enroll_mfa(ACTING_ADMIN)
        with enforcement(True):
            yield doors


def _jti(env: DoorsEnv) -> str:
    return str(env.stack.jwt_manager.validate_token(env.bearer)["jti"])


def _mcp(
    env: DoorsEnv, tool: str, arguments: Dict[str, Any], elevation_key: Optional[str]
) -> Dict[str, Any]:
    from code_indexer.server.mcp.protocol import process_jsonrpc_request

    token = bind_audit_request_context(build_request_context("/mcp", "127.0.0.1"))
    try:
        response = asyncio.run(
            process_jsonrpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
                env.admin,
                elevation_key=elevation_key,
            )
        )
    finally:
        reset_audit_request_context(token)
    assert "result" in response, response
    payload: Dict[str, Any] = json.loads(response["result"]["content"][0]["text"])
    return payload


def _rest_calls(env: DoorsEnv):
    return {
        "add": lambda: env.rest(
            "POST",
            "/api/admin/golden-repos",
            json={"repo_url": env.git_url, "alias": "example-new"},
        ),
        "remove": lambda: env.rest(
            "DELETE", f"/api/admin/golden-repos/{EXAMPLE_ALIAS}"
        ),
        "refresh": lambda: env.rest(
            "POST", f"/api/admin/golden-repos/{EXAMPLE_ALIAS}/refresh"
        ),
    }


_MCP_CALLS = {
    "add": ("add_golden_repo", {"alias": "example-new"}),
    "remove": ("remove_golden_repo", {"alias": EXAMPLE_ALIAS}),
    "refresh": ("refresh_golden_repo", {"alias": EXAMPLE_ALIAS}),
}
_WEB_CALLS = {
    "add": ("/admin/golden-repos/add", {"alias": "example-new"}),
    "remove": (f"/admin/golden-repos/{EXAMPLE_ALIAS}/delete", {}),
    "refresh": (f"/admin/golden-repos/{EXAMPLE_ALIAS}/refresh", {}),
}
_ACTIONS = ("add", "remove", "refresh")


@pytest.mark.parametrize("action", _ACTIONS)
class TestRest:
    def test_refused_without_an_elevation_window(self, env, action) -> None:
        resp = _rest_calls(env)[action]()
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "elevation_required"
        assert env.jobs.submissions == []

    def test_allowed_with_the_callers_window(self, env, action) -> None:
        env.stack.elevate(_jti(env), ACTING_ADMIN)
        resp = _rest_calls(env)[action]()
        assert resp.status_code in (202, 204), resp.text
        assert len(env.jobs.submissions) == 1


@pytest.mark.parametrize("action", _ACTIONS)
class TestMcp:
    def _arguments(self, env: DoorsEnv, action: str) -> Dict[str, Any]:
        tool, arguments = _MCP_CALLS[action]
        return (
            {"url": env.git_url, **arguments}
            if tool == "add_golden_repo"
            else arguments
        )

    def test_refused_without_an_elevation_window(self, env, action) -> None:
        tool = _MCP_CALLS[action][0]
        result = _mcp(env, tool, self._arguments(env, action), elevation_key=_jti(env))
        assert result["error"] == "elevation_required", result
        assert env.jobs.submissions == []

    def test_allowed_with_the_callers_window(self, env, action) -> None:
        env.stack.elevate(_jti(env), ACTING_ADMIN)
        tool = _MCP_CALLS[action][0]
        result = _mcp(env, tool, self._arguments(env, action), elevation_key=_jti(env))
        assert result.get("success") is True, result
        assert len(env.jobs.submissions) == 1


@pytest.mark.parametrize("action", _ACTIONS)
class TestWebParity:
    def _post(self, env: DoorsEnv, action: str):
        path, data = _WEB_CALLS[action]
        if action == "add":
            data = {**data, "repo_url": env.git_url}
        return env.web("POST", path, data=data)

    def test_refused_without_an_elevation_window(self, env, action) -> None:
        resp = self._post(env, action)
        assert resp.status_code == 403, resp.text
        assert env.jobs.submissions == []

    def test_allowed_with_the_callers_window(self, env, action) -> None:
        env.stack.elevate(env.cookie, ACTING_ADMIN)
        self._post(env, action)
        assert len(env.jobs.submissions) == 1


class TestRestAddIndex:
    def _post(self, env: DoorsEnv):
        return env.rest(
            "POST",
            f"/api/admin/golden-repos/{EXAMPLE_ALIAS}/indexes",
            json={"index_type": "fts"},
        )

    def test_refused_without_an_elevation_window(self, env) -> None:
        resp = self._post(env)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "elevation_required"
        assert env.jobs.submissions == []

    def test_allowed_with_the_callers_window(self, env) -> None:
        env.stack.elevate(_jti(env), ACTING_ADMIN)
        assert self._post(env).status_code == 202
        assert len(env.jobs.submissions) == 1


_MCP_INDEX_AND_BRANCH = {
    "add_index": (
        "add_golden_repo_index",
        {"alias": EXAMPLE_ALIAS, "index_type": "fts"},
    ),
    "change_branch": (
        "change_golden_repo_branch",
        {"alias": EXAMPLE_ALIAS, "branch": "dev"},
    ),
}


@pytest.mark.parametrize("action", sorted(_MCP_INDEX_AND_BRANCH))
class TestMcpIndexAndBranch:
    def test_refused_without_an_elevation_window(self, env, action) -> None:
        tool, arguments = _MCP_INDEX_AND_BRANCH[action]
        result = _mcp(env, tool, arguments, elevation_key=_jti(env))
        assert result["error"] == "elevation_required", result
        assert env.jobs.submissions == []

    def test_allowed_with_the_callers_window(self, env, action) -> None:
        env.stack.elevate(_jti(env), ACTING_ADMIN)
        tool, arguments = _MCP_INDEX_AND_BRANCH[action]
        result = _mcp(env, tool, arguments, elevation_key=_jti(env))
        assert result.get("success") is True, result
        assert len(env.jobs.submissions) == 1


class TestWebChangeBranchParity:
    def _post(self, env: DoorsEnv):
        return env.web(
            "POST",
            f"/admin/golden-repos/{EXAMPLE_ALIAS}/change-branch",
            json={"branch": "dev"},
        )

    def test_refused_without_an_elevation_window(self, env) -> None:
        assert self._post(env).status_code == 403
        assert env.jobs.submissions == []

    def test_allowed_with_the_callers_window(self, env) -> None:
        env.stack.elevate(env.cookie, ACTING_ADMIN)
        assert self._post(env).status_code == 202
        assert len(env.jobs.submissions) == 1


def test_rest_admin_without_totp_is_told_to_set_it_up(env) -> None:
    env.stack.totp.disable_mfa(ACTING_ADMIN, actor=ACTING_ADMIN)
    resp = _rest_calls(env)["remove"]()
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "totp_setup_required"
    assert env.jobs.submissions == []


def test_mcp_admin_without_totp_is_told_to_set_it_up(env) -> None:
    env.stack.totp.disable_mfa(ACTING_ADMIN, actor=ACTING_ADMIN)
    result = _mcp(env, "remove_golden_repo", {"alias": EXAMPLE_ALIAS}, _jti(env))
    assert result["error"] == "totp_setup_required", result
    assert env.jobs.submissions == []
