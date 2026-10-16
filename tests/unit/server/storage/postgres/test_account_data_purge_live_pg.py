"""Deleting an account removes every row keyed to its name (PostgreSQL).

Live-PostgreSQL tests, gated by ``TEST_POSTGRES_DSN`` like the other live-PG
tests here (skipped cleanly when unset or unreachable).  The module creates a
throwaway database, applies the real migrations and builds the real
PostgreSQL backend registry, exactly as a cluster node does at start-up.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Iterator

import pytest

psycopg = pytest.importorskip("psycopg")

from code_indexer.server.auth.jwt_manager import JWTManager  # noqa: E402
from code_indexer.server.auth.refresh_token_manager import (  # noqa: E402
    RefreshTokenManager,
)
from code_indexer.server.auth.totp_service import TOTPService  # noqa: E402
from code_indexer.server.auth.user_manager import UserManager, UserRole  # noqa: E402
from code_indexer.server.services.account_data_purge import (  # noqa: E402
    ALL_KEYED_TABLES,
    build_account_data_purger,
    sweep_orphaned_account_data,
)
from code_indexer.server.storage.factory import StorageFactory  # noqa: E402
from code_indexer.server.storage.postgres.groups_backend import (  # noqa: E402
    GroupsPostgresBackend,
)
from code_indexer.server.storage.postgres.migrations.runner import (  # noqa: E402
    MigrationRunner,
)
from code_indexer.server.utils.jwt_secret_manager import (  # noqa: E402
    JWTSecretManager,
)
from tests.unit.server._account_rows import (  # noqa: E402
    OTHER_PASSWORD,
    PASSWORD,
    Stores,
    assert_account_has_no_inherited_rows,
    assert_account_keeps_its_rows,
    seed_account_rows,
)


@pytest.fixture(scope="module")
def purge_dsn() -> Iterator[str]:
    """A throwaway, fully migrated database on the TEST_POSTGRES_DSN server."""
    admin_dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not admin_dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - skip on any connection failure
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    name = f"cidx_account_purge_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    dsn = psycopg.conninfo.make_conninfo(admin_dsn, dbname=name)
    try:
        MigrationRunner(dsn).run()
        JWTSecretManager(pg_dsn=dsn).get_or_create_secret()  # cluster_secrets
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def pg_stores(purge_dsn: str, tmp_path: Path) -> Iterator[Stores]:
    with psycopg.connect(purge_dsn, autocommit=True) as conn:
        tables = ", ".join(table for table, _ in ALL_KEYED_TABLES)
        conn.execute(f"TRUNCATE users, {tables} CASCADE")
    registry = StorageFactory.create_backends(
        config={"storage_mode": "postgres", "postgres_dsn": purge_dsn},
        data_dir=str(tmp_path / "data"),
    )
    groups = registry.groups
    assert isinstance(groups, GroupsPostgresBackend)
    groups.bootstrap_default_groups()  # as GroupAccessManager does at start-up
    pool = registry.connection_pool
    assert pool is not None
    user_manager = UserManager(
        storage_backend=registry.users,
        account_data_purger=build_account_data_purger("postgres", tmp_path, pool),
    )
    totp = TOTPService(db_path=str(tmp_path / "mfa.db"))
    totp.set_connection_pool(pool)
    refresh_tokens = RefreshTokenManager(
        jwt_manager=JWTManager(secret_key="example-secret-key-for-tests"),
        storage_backend=registry.refresh_tokens,
    )
    try:
        yield Stores(tmp_path, registry, user_manager, refresh_tokens, totp)
    finally:
        pool.close()
        if registry.critical_connection_pool is not None:
            registry.critical_connection_pool.close()


def test_recreated_account_inherits_nothing_pg(pg_stores: Stores) -> None:
    """Deleting an account removes all rows keyed to its name in PostgreSQL."""
    pg_stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    seeded = seed_account_rows(pg_stores, "alice")

    assert pg_stores.user_manager.delete_user_audited("alice", actor="admin")
    pg_stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)

    assert_account_has_no_inherited_rows(pg_stores, "alice", seeded)


def test_deleting_one_account_keeps_other_accounts_rows_pg(pg_stores: Stores) -> None:
    pg_stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    pg_stores.user_manager.create_user("bob", PASSWORD, UserRole.ADMIN)
    seed_account_rows(pg_stores, "alice")
    bob = seed_account_rows(pg_stores, "bob")

    assert pg_stores.user_manager.delete_user_audited("alice", actor="admin")

    assert pg_stores.registry.groups.get_user_group("bob") is not None
    assert pg_stores.totp.is_mfa_enabled("bob") is True
    assert pg_stores.registry.oauth.get_oidc_identity(bob.sso_subject) is not None
    assert len(pg_stores.registry.git_credentials.list_credentials("bob")) == 1
    assert len(pg_stores.user_manager.get_api_keys("bob")) == 1


def test_account_row_delete_failure_leaves_every_keyed_row_pg(
    pg_stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the account row cannot be deleted, nothing keyed to it was
    removed: the live account keeps its MFA and its group."""
    pg_stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    seed_account_rows(pg_stores, "alice")

    def _refuse(username: str) -> bool:
        raise OSError("example accounts store failure")

    monkeypatch.setattr(pg_stores.registry.users, "delete_user", _refuse)

    with pytest.raises(OSError):
        pg_stores.user_manager.delete_user_audited("alice", actor="admin")
    assert pg_stores.user_manager.get_user("alice") is not None
    assert pg_stores.totp.is_mfa_enabled("alice") is True
    assert pg_stores.registry.groups.get_user_group("alice") is not None


def test_late_purge_never_removes_a_recreated_accounts_rows_pg(
    pg_stores: Stores,
) -> None:
    """The cleanup after a deletion removes rows only while no account with
    the name exists, checked inside each DELETE."""
    pg_stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    seed_account_rows(pg_stores, "alice")
    assert pg_stores.user_manager.delete_user("alice")  # cleanup still pending
    pg_stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.ADMIN)
    new_alice = seed_account_rows(pg_stores, "alice")

    pg_stores.user_manager._clean_up_after_delete("alice", "admin")

    assert_account_keeps_its_rows(pg_stores, "alice", new_alice)


def test_create_removes_leftovers_pg(pg_stores: Stores) -> None:
    pg_stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    alice = seed_account_rows(pg_stores, "alice")
    assert pg_stores.user_manager.delete_user("alice")  # leaves rows behind

    pg_stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)

    assert_account_has_no_inherited_rows(pg_stores, "alice", alice)


def test_token_of_earlier_account_rejected_pg(
    pg_stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from code_indexer.server.auth import dependencies

    jwt_manager = JWTManager(secret_key="example-secret-key-for-tests")
    monkeypatch.setattr(dependencies, "user_manager", pg_stores.user_manager)
    monkeypatch.setattr(dependencies, "jwt_manager", jwt_manager)
    pg_stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    old = jwt_manager.create_token({"username": "alice", "role": "admin"})
    assert pg_stores.user_manager.delete_user_audited("alice", actor="admin")
    pg_stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)
    new = jwt_manager.create_token({"username": "alice", "role": "normal_user"})

    recreated = pg_stores.user_manager.get_user("alice")
    assert recreated is not None and recreated.account_created_at is not None
    with pytest.raises(HTTPException) as refused:
        dependencies._validate_jwt_and_get_user(old)
    assert refused.value.status_code == 401
    assert dependencies._validate_jwt_and_get_user(new).username == "alice"


def test_orphan_sweep_removes_leftovers_pg(pg_stores: Stores) -> None:
    """Rows keyed to a name with no account are removed by the sweep; a second
    sweep finds nothing; existing accounts keep their rows."""
    pg_stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    pg_stores.user_manager.create_user("bob", PASSWORD, UserRole.ADMIN)
    alice = seed_account_rows(pg_stores, "alice")
    seed_account_rows(pg_stores, "bob")
    assert pg_stores.user_manager.delete_user("alice")  # leaves rows behind
    purger = pg_stores.user_manager.account_data_purger
    assert purger is not None

    sweep_orphaned_account_data(purger)

    assert purger.purge_orphans() == 0
    assert pg_stores.registry.groups.get_user_group("bob") is not None
    pg_stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)
    assert_account_has_no_inherited_rows(pg_stores, "alice", alice)
