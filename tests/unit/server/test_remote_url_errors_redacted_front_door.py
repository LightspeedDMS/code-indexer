# ruff: noqa: F811
"""Error responses that name a repository's remote return it with its
userinfo redacted, and a remote's credential host never includes userinfo.

Driven through a real app (repo_url_userinfo_env): the user's activation
has a real ``origin`` remote whose URL carries userinfo, as activation
copies it from the golden clone. Every response body is also checked as a
whole for the secret (``mcp_text``).

Hosts, usernames and secrets are neutral placeholders.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from tests.unit.server.repo_url_userinfo_env import (  # noqa: F401 - fixtures
    REDACTED_URL,
    USER,
    USER_ACTIVATION,
    activate_for_user,
    app,
    assert_no_userinfo,
    client,
    mcp_text,
    store_userinfo_origin,
)

FORGE_HOST = "git.example.com:8443"
CREDENTIAL_FREE_URL = "https://git.example.com:8443/example/userinfo-repo.git"

# Pull-request tools open to a user with query_repos, with minimal arguments.
PR_TOOL_CALLS = [
    ("list_pull_requests", {}),
    ("get_pull_request", {"number": 1}),
    ("list_pull_request_comments", {"number": 1}),
    ("comment_on_pull_request", {"number": 1, "body": "example"}),
    ("update_pull_request", {"number": 1, "title": "example"}),
]
PR_IDS = [t for t, _ in PR_TOOL_CALLS]


@pytest.fixture
def activation(app: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    # An existing activation whose stored origin still carries userinfo.
    store_userinfo_origin(activate_for_user(app, monkeypatch))
    return USER_ACTIVATION


def _payload(
    client: TestClient, app: Any, tool: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    payload: Dict[str, Any] = json.loads(mcp_text(client, app, USER, tool, arguments))
    return payload


def test_switch_branch_error_names_the_remote_redacted(
    client: TestClient, app: Any, activation: str
) -> None:
    """The fetch from the example remote fails, so the error names the
    remote. The stored origin is rewritten to its credential-free URL
    before the fetch, so that is the URL named."""
    payload = _payload(
        client,
        app,
        "switch_branch",
        {"user_alias": activation, "branch_name": "no-such-branch"},
    )
    assert payload.get("success") is False, payload
    assert_no_userinfo(payload["error"])
    assert CREDENTIAL_FREE_URL in payload["error"], payload


@pytest.mark.parametrize("tool,arguments", PR_TOOL_CALLS, ids=PR_IDS)
def test_missing_credential_error_names_the_host_without_userinfo(
    client: TestClient,
    app: Any,
    activation: str,
    tool: str,
    arguments: Dict[str, Any],
) -> None:
    payload = _payload(client, app, tool, {"repository_alias": activation, **arguments})
    assert payload.get("success") is False, payload
    assert_no_userinfo(payload["error"])
    assert f"No git credential configured for {FORGE_HOST}." in payload["error"]


@pytest.fixture
def stored_credential(app: Any) -> Iterator[None]:
    """USER's PAT for the remote's host, stored by the credential manager's
    own backend and encryption (the forge API validation is skipped)."""
    from code_indexer.server.mcp.handlers.git_write import _get_credential_manager

    manager = _get_credential_manager()
    credential_id = str(uuid.uuid4())
    manager._backend.upsert_credential(
        credential_id=credential_id,
        username=USER,
        forge_type="gitlab",
        forge_host=FORGE_HOST,
        encrypted_token=manager._encrypt_token("example-pat-456"),
    )
    yield
    manager.delete_credential(USER, credential_id)


@pytest.mark.parametrize("tool,arguments", PR_TOOL_CALLS, ids=PR_IDS)
def test_forge_detection_error_returns_redacted_remote_url(
    client: TestClient,
    app: Any,
    activation: str,
    stored_credential: None,
    tool: str,
    arguments: Dict[str, Any],
) -> None:
    payload = _payload(client, app, tool, {"repository_alias": activation, **arguments})
    assert payload.get("success") is False, payload
    assert_no_userinfo(payload["error"])
    assert f"remote URL '{REDACTED_URL}'" in payload["error"], payload
