"""Both OAuth stores check an authorization code's account against the
instant the code was issued, before any token is minted.

Real stores: the SQLite OAuth store, and the PostgreSQL OAuth store over its
own migrated database (enabled by ``TEST_POSTGRES_DSN``).
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, List, Tuple

import pytest

from code_indexer.server.auth.oauth.oauth_manager import (
    AuthorizationGrantRefused,
    OAuthError,
)

PG_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
REDIRECT_URI = "https://example.com/callback"


def _pg_dsn_for(dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(PG_DSN)
    params["dbname"] = dbname
    return make_conninfo(**params)  # type: ignore[arg-type]


def _pg_store(max_size: int = 2, timeout: float = 30.0) -> Iterator[Any]:
    import psycopg

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )
    from code_indexer.server.storage.postgres.oauth_backend import (
        OAuthPostgresBackend,
    )

    name = f"oauth_code_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    pool: Any = None
    try:
        with MigrationRunner(_pg_dsn_for(name)) as runner:
            runner.run()
        pool = ConnectionPool(
            _pg_dsn_for(name), min_size=1, max_size=max_size, timeout=timeout
        )
        yield OAuthPostgresBackend(pool)
    finally:
        if pool is not None:
            pool.close()
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


STORE_KINDS = ["sqlite", "manager-inline", "postgres"]


def _open_store(
    kind: str, tmp_path: Path, max_size: int = 2, timeout: float = 30.0
) -> Iterator[Any]:
    """The SQLite store, the OAuth manager's own SQLite path (no storage
    backend passed), or the PostgreSQL store (pool of *max_size*)."""
    if kind == "sqlite":
        from code_indexer.server.storage.sqlite_backends.oauth_backend import (
            OAuthSqliteBackend,
        )

        yield OAuthSqliteBackend(str(tmp_path / "oauth.db"))
    elif kind == "manager-inline":
        from code_indexer.server.auth.oauth.oauth_manager import OAuthManager

        yield OAuthManager(db_path=str(tmp_path / "oauth.db"))
    else:
        if not PG_DSN:
            pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
        yield from _pg_store(max_size=max_size, timeout=timeout)


@pytest.fixture(params=STORE_KINDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    yield from _open_store(request.param, tmp_path)


@pytest.fixture(params=STORE_KINDS)
def lone_connection_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[Any]:
    """A store whose PostgreSQL pool holds ONE connection (short acquire
    timeout), so an account check needing the database while the exchange
    still holds that connection cannot get one."""
    yield from _open_store(request.param, tmp_path, max_size=1, timeout=2.0)


def _check_using_the_store_database(store: Any) -> Callable[[str, datetime], bool]:
    """An account check that, like the server's account lookup, needs the
    store's database: a pooled connection (PostgreSQL) or a write lock
    (SQLite, through its own connection with a short busy timeout)."""

    def check(user_id: str, issued_at: datetime) -> bool:
        pool = getattr(store, "_pool", None)
        if pool is not None:
            with pool.connection() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        with closing(sqlite3.connect(store._conn_manager.db_path, timeout=0.5)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        return True

    return check


def _issue_code(store: Any) -> Tuple[str, str, str, datetime, datetime]:
    """(client_id, code, verifier, instant before issue, instant after)."""
    client_id = store.register_client("example-client", [REDIRECT_URI])["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    before = datetime.now(timezone.utc)
    code = store.generate_authorization_code(
        client_id=client_id,
        user_id="alice",
        code_challenge=challenge,
        redirect_uri=REDIRECT_URI,
        state=None,
    )
    return client_id, code, verifier, before, datetime.now(timezone.utc)


def test_refused_code_mints_nothing_and_stays_unused(store: Any) -> None:
    client_id, code, verifier, before, after = _issue_code(store)
    seen: List[Tuple[str, datetime]] = []

    def refuse(user_id: str, issued_at: datetime) -> bool:
        seen.append((user_id, issued_at))
        return False

    with pytest.raises(AuthorizationGrantRefused):
        store.exchange_code_for_token(
            code=code, code_verifier=verifier, client_id=client_id, account_check=refuse
        )

    assert len(seen) == 1 and seen[0][0] == "alice"
    assert before <= seen[0][1] <= after  # the instant the code was issued
    tokens = store.exchange_code_for_token(
        code=code,
        code_verifier=verifier,
        client_id=client_id,
        account_check=lambda user_id, issued_at: True,
    )
    assert store.validate_token(tokens["access_token"])["user_id"] == "alice"


def test_account_check_runs_while_the_exchange_holds_no_connection(
    lone_connection_store: Any,
) -> None:
    store = lone_connection_store
    client_id, code, verifier, _, _ = _issue_code(store)

    tokens = store.exchange_code_for_token(
        code=code,
        code_verifier=verifier,
        client_id=client_id,
        account_check=_check_using_the_store_database(store),
    )

    assert store.validate_token(tokens["access_token"])["user_id"] == "alice"


def test_overlapping_exchanges_redeem_a_code_only_once(store: Any) -> None:
    """A second exchange of the same code completes while the first is
    between its read and its consume (inside its account check): the first
    must then find the code used and mint nothing."""
    client_id, code, verifier, _, _ = _issue_code(store)
    overlapping: List[Any] = []

    def redeem_meanwhile(user_id: str, issued_at: datetime) -> bool:
        if not overlapping:
            overlapping.append(None)
            overlapping[0] = store.exchange_code_for_token(
                code=code,
                code_verifier=verifier,
                client_id=client_id,
                account_check=redeem_meanwhile,
            )
        return True

    with pytest.raises(OAuthError, match="already used"):
        store.exchange_code_for_token(
            code=code,
            code_verifier=verifier,
            client_id=client_id,
            account_check=redeem_meanwhile,
        )

    assert store.validate_token(overlapping[0]["access_token"])["user_id"] == "alice"
    with pytest.raises(OAuthError, match="already used"):
        store.exchange_code_for_token(
            code=code, code_verifier=verifier, client_id=client_id
        )


def test_code_exchanges_without_a_check(store: Any) -> None:
    client_id, code, verifier, _, _ = _issue_code(store)

    tokens = store.exchange_code_for_token(
        code=code, code_verifier=verifier, client_id=client_id
    )

    assert store.validate_token(tokens["access_token"]) is not None
