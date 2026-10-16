"""A refresh token is redeemable only by the client it was issued to
(RFC 6749 section 6), in every OAuth store.

Real stores: the SQLite OAuth store, the OAuth manager's own SQLite path, and
the PostgreSQL OAuth store over its own migrated database (enabled by
``TEST_POSTGRES_DSN``).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.oauth.oauth_manager import AuthorizationGrantRefused
from tests.unit.server.auth.test_oauth_code_exchange_account_check import (
    REDIRECT_URI,
    STORE_KINDS,
    _open_store,
)


@pytest.fixture(params=STORE_KINDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    yield from _open_store(request.param, tmp_path)


def _tokens_for_new_client(store: Any, name: str) -> Tuple[Dict[str, Any], str]:
    """Register a client and run its PKCE code exchange; return (tokens,
    client_id)."""
    client_id: str = store.register_client(name, [REDIRECT_URI])["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    code = store.generate_authorization_code(
        client_id=client_id,
        user_id="alice",
        code_challenge=challenge,
        redirect_uri=REDIRECT_URI,
        state=None,
    )
    tokens: Dict[str, Any] = store.exchange_code_for_token(
        code=code, code_verifier=verifier, client_id=client_id
    )
    return tokens, client_id


def test_refresh_token_is_refused_for_another_client(store: Any) -> None:
    issued, issued_client = _tokens_for_new_client(store, "example-client-x")
    _, other_client = _tokens_for_new_client(store, "example-client-y")

    with pytest.raises(AuthorizationGrantRefused):
        store.refresh_access_token(
            refresh_token=issued["refresh_token"], client_id=other_client
        )

    # The refused attempt neither rotated nor consumed it: its own client
    # still redeems it.
    renewed = store.refresh_access_token(
        refresh_token=issued["refresh_token"], client_id=issued_client
    )
    assert renewed["refresh_token"] != issued["refresh_token"]
    assert store.validate_token(renewed["access_token"])["client_id"] == issued_client


def _race_refresh(
    store: Any, refresh_token: str, client_id: str
) -> Tuple[List[Dict[str, Any]], List[BaseException]]:
    """Two threads, released together, redeem the same refresh token;
    return (successful responses, raised exceptions)."""
    import threading

    barrier = threading.Barrier(2)
    successes: List[Dict[str, Any]] = []
    failures: List[BaseException] = []

    def redeem() -> None:
        barrier.wait(timeout=30)
        try:
            successes.append(
                store.refresh_access_token(
                    refresh_token=refresh_token, client_id=client_id
                )
            )
        except BaseException as exc:  # noqa: BLE001 - collected and asserted
            failures.append(exc)

    threads = [threading.Thread(target=redeem) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    return successes, failures


def test_concurrent_refreshes_of_one_token_have_exactly_one_winner(
    store: Any,
) -> None:
    tokens, client_id = _tokens_for_new_client(store, "example-client")
    for _ in range(25):
        successes, failures = _race_refresh(store, tokens["refresh_token"], client_id)

        assert len(successes) == 1, (successes, failures)
        assert len(failures) == 1 and isinstance(
            failures[0], AuthorizationGrantRefused
        ), failures
        # What the winner received is what was stored.
        winner = successes[0]
        assert store.validate_token(winner["access_token"]) is not None
        tokens = winner


# ``store`` is typed Any throughout: the SQLite store, the OAuth manager and
# the PostgreSQL store share only a duck-typed protocol (no common base).


@pytest.fixture
def token_client(store: Any, tmp_path: Path) -> TestClient:
    """The real OAuth router over *store*, with a live account 'alice'."""
    from fastapi import FastAPI

    from code_indexer.server.auth.oauth import routes
    from code_indexer.server.auth.oauth.oauth_manager import OAuthManager
    from code_indexer.server.auth.user_manager import UserManager, UserRole

    manager = store if isinstance(store, OAuthManager) else None
    manager = manager or OAuthManager(storage_backend=store)
    users = UserManager(users_file_path=str(tmp_path / "users.json"))
    users.create_user("alice", "Example-Refresh-Passw0rd!", UserRole.NORMAL_USER)
    app = FastAPI()
    app.include_router(routes.router)
    # Wired as production does: the OAuth routes read both from app.state.
    app.state.oauth_manager = manager
    app.state.user_manager = users
    return TestClient(app)


def _set_hard_expiry(store: Any, refresh_token: str, value: str) -> None:
    """Write the stored hard_expires_at text of *refresh_token*'s row."""
    sql = "UPDATE oauth_tokens SET hard_expires_at = {p} WHERE refresh_token = {p}"
    pool = getattr(store, "_pool", None)
    if pool is not None:
        with pool.connection() as conn:
            conn.execute(sql.format(p="%s"), (value, refresh_token))
            conn.commit()
        return
    with closing(sqlite3.connect(store._conn_manager.db_path)) as conn:
        assert conn.execute(sql.format(p="?"), (value, refresh_token)).rowcount == 1
        conn.commit()


@pytest.mark.parametrize(
    "offset, zone, refused",
    [
        (timedelta(days=-1), None, True),
        # Naive is UTC: read in a session zone west of UTC it would look
        # hours in the future.
        (timedelta(hours=-1), None, True),
        (timedelta(days=-1), timezone.utc, True),
        (timedelta(hours=-1), timezone(timedelta(hours=5)), True),
        (timedelta(minutes=2), None, False),
        (timedelta(minutes=2), timezone.utc, False),
    ],
    ids=[
        "past-naive",
        "past-hour-naive",
        "past-utc",
        "past-plus5",
        "inside-naive",
        "inside-utc",
    ],
)
def test_refresh_past_hard_expiry_is_invalid_grant(
    store: Any,
    token_client: TestClient,
    offset: timedelta,
    zone: Optional[timezone],
    refused: bool,
) -> None:
    tokens, client_id = _tokens_for_new_client(store, "example-client")
    when = datetime.now(timezone.utc) + offset
    stored = when.replace(tzinfo=None) if zone is None else when.astimezone(zone)
    _set_hard_expiry(store, tokens["refresh_token"], stored.isoformat())

    response = token_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
    )

    if refused:
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_grant"
        # Not rotated: the stored row still holds the old credentials.
        assert store.validate_token(tokens["access_token"]) is not None
    else:
        assert response.status_code == 200, response.text
        assert "access_token" in response.json()
