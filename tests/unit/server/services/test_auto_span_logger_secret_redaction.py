"""MCP tracing spans never carry a secret value.

Invariant: whatever AutoSpanLogger hands to the tracing service -- span
input (tool arguments), span output (the tool's MCP response, whose payload
is a JSON string) and error output -- has every value under a secret-named
key replaced, at any depth, and credentials embedded in URLs or
Authorization text masked. Non-secret values, including numeric counts
under token-like names, are kept. The caller still receives the real,
unredacted response.

The Langfuse SDK is the external boundary, so it is the only stand-in:
FakeLangfuseSdk, injected at LangfuseClient's real ``_langfuse`` seam,
records exactly what would be sent. LangfuseClient, TraceStateManager,
AutoSpanLogger, the MCP git handler, the git service and the shared token
store are real.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import git_write
from code_indexer.server.mcp.handlers._utils import _mcp_response
from code_indexer.server.services.auto_span_logger import AutoSpanLogger
from tests.unit.server.services._fake_langfuse_sdk import (
    FakeLangfuseSdk,
    traced_client,
)
from tests.unit.server.services._git_confirm_helpers import (
    REPO_A,
    make_repo,
    singleton_confirmation_store_fixture,  # noqa: F401 -- registers the fixture
)

_SESSION = "example-session"
_PASSWORD = "ExamplePasswordValue1"
_CLIENT_SECRET = "ExampleClientSecretValue2"
_API_KEY = "ExampleApiKeyValue3"
_NESTED_KEY = "ExampleNestedKeyValue4"
_BEARER = "ExampleBearerValue5"
_COOKIE = "ExampleCookieValue6"
_URL_PASSWORD = "ExampleUrlPassword7"


def _traced() -> Tuple[AutoSpanLogger, FakeLangfuseSdk]:
    client, traces, sdk = traced_client()
    assert traces.start_trace(session_id=_SESSION, name="example") is not None
    return AutoSpanLogger(traces, client), sdk


async def _call(
    span_logger: AutoSpanLogger,
    tool: str,
    args: Dict[str, Any],
    run: Callable[[Dict[str, Any]], Any],
) -> Any:
    async def handler() -> Any:
        return run(args)

    return await span_logger.intercept_tool_call(
        session_id=_SESSION, tool_name=tool, arguments=args, handler=handler
    )


def _payload(response: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


async def test_git_confirmation_spans_carry_no_token(
    tmp_path: Path, singleton_confirmation_store: Any
) -> None:
    repo = make_repo(tmp_path / "repos", REPO_A)
    admin = User(
        username="example-admin",
        role=UserRole.ADMIN,
        password_hash="unused",
        created_at=datetime.now(),
    )
    span_logger, tracing = _traced()

    def clean(args: Dict[str, Any]) -> Any:
        return git_write.git_clean(args, admin)

    with patch(
        "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
        return_value=(str(repo), None),
    ):
        first = await _call(
            span_logger, "git_clean", {"repository_alias": REPO_A}, clean
        )
        token = _payload(first)["confirmation_token_required"]["token"]
        confirmed = await _call(
            span_logger,
            "git_clean",
            {"repository_alias": REPO_A, "confirmation_token": token},
            clean,
        )

    assert _payload(confirmed)["success"] is True
    assert token not in tracing.sent_text()
    assert REPO_A in tracing.sent_text()


async def test_create_user_span_carries_no_password() -> None:
    span_logger, tracing = _traced()
    args = {"username": "example-user", "password": _PASSWORD, "role": "normal_user"}

    await _call(
        span_logger,
        "create_user",
        args,
        lambda a: _mcp_response({"success": True, "username": a["username"]}),
    )

    assert _PASSWORD not in tracing.sent_text()
    assert "example-user" in tracing.sent_text()
    assert args["password"] == _PASSWORD, "the caller's arguments are untouched"


async def test_credential_creating_output_spans_carry_no_secret() -> None:
    span_logger, tracing = _traced()

    response = await _call(
        span_logger,
        "manage_mcp_credential",
        {"action": "create", "description": "example"},
        lambda _a: _mcp_response(
            {
                "success": True,
                "client_id": "example-client",
                "credential_id": "example-credential-id",
                "client_secret": _CLIENT_SECRET,
                "credential": _CLIENT_SECRET,
            }
        ),
    )
    await _call(
        span_logger,
        "create_api_key",
        {"description": "example"},
        lambda _a: _mcp_response(
            {"success": True, "key_id": "example-key-id", "api_key": _API_KEY}
        ),
    )

    sent = tracing.sent_text()
    assert _CLIENT_SECRET not in sent
    assert _API_KEY not in sent
    for kept in ("example-client", "example-credential-id", "example-key-id"):
        assert kept in sent
    assert _payload(response)["client_secret"] == _CLIENT_SECRET


async def test_nested_and_list_inputs_are_redacted_and_others_kept() -> None:
    span_logger, tracing = _traced()
    args = {
        "config": {
            "providers": [{"name": "example-provider", "api_key": _NESTED_KEY}],
            "token_count": 4096,
        },
        "headers": {"Authorization": f"Bearer {_BEARER}", "Cookie": _COOKIE},
    }

    await _call(span_logger, "set_global_config", args, lambda _a: {"ok": True})

    sent = tracing.sent_text()
    for secret in (_NESTED_KEY, _BEARER, _COOKIE):
        assert secret not in sent
    assert "example-provider" in sent
    assert "4096" in sent


async def test_elevate_session_span_carries_no_mfa_code() -> None:
    from code_indexer.server.mcp.handlers.admin.elevate_session import (
        elevate_session,
    )

    span_logger, tracing = _traced()
    admin = User(
        username="example-admin",
        role=UserRole.ADMIN,
        password_hash="unused",
        created_at=datetime.now(),
    )
    args = {"totp_code": "864209", "recovery_code": "EXAMPLE-RECOVERY-CODE"}

    await _call(
        span_logger, "elevate_session", args, lambda a: elevate_session(a, admin)
    )

    sent = tracing.sent_text()
    assert "864209" not in sent
    assert "EXAMPLE-RECOVERY-CODE" not in sent
    assert "elevate_session" in sent


async def test_error_spans_carry_no_url_credentials() -> None:
    span_logger, tracing = _traced()

    def fail(_args: Dict[str, Any]) -> Any:
        raise RuntimeError(
            f"clone failed for https://example-user:{_URL_PASSWORD}@example.com/r.git"
        )

    with pytest.raises(RuntimeError):
        await _call(span_logger, "add_golden_repo", {"alias": "example"}, fail)

    assert _URL_PASSWORD not in tracing.sent_text()
    assert "example.com/r.git" in tracing.sent_text()
