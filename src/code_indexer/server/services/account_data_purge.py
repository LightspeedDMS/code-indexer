"""Removal of every stored row keyed to an account name.

An account name owns rows in several stores besides the ``users`` row: group
membership, MFA enrollment and recovery codes, SSO identity links, OAuth and
refresh tokens, API keys, MCP credentials and git credentials.  Deleting an
account deletes the ``users`` row FIRST (nothing can authenticate as it any
more), then removes those rows -- each only while no account with the name
exists (``purge_deleted``), so an account re-created meanwhile keeps its own.
Before a name is created its rows are removed unconditionally (``purge``), and
the start-up sweep (``purge_orphans``) removes rows whose account is gone, so
an account created with an earlier name starts with nothing from it.

SQLite keeps these rows in four files, so no single transaction covers them.
Tables are visited in a fixed order (token stores first, group membership
last) and every delete is idempotent: an interrupted purge leaves at worst
rows that the next creation of the name, or the start-up sweep, removes.
PostgreSQL keeps every table in one database and purges in one transaction.

Table and column names below are fixed constants, never caller input.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol, Tuple

if TYPE_CHECKING:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

KeyedTable = Tuple[str, str]  # (table, column holding the account name)

# Relative to the server data directory, mirroring StorageFactory's SQLite
# layout and the MFA store (data/cidx_server.db).
ACCOUNTS_DB = "data/cidx_server.db"
SQLITE_STORES: Tuple[Tuple[str, Tuple[KeyedTable, ...]], ...] = (
    (
        "refresh_tokens.db",
        (("refresh_tokens", "username"), ("token_families", "username")),
    ),
    (
        "oauth.db",
        (
            ("oauth_tokens", "user_id"),
            ("oauth_codes", "user_id"),
            ("oidc_identity_links", "username"),
        ),
    ),
    (
        ACCOUNTS_DB,
        (
            ("user_api_keys", "username"),
            ("user_mcp_credentials", "username"),
            ("user_oidc_identities", "username"),
            ("user_git_credentials", "username"),
            ("user_recovery_codes", "user_id"),
            ("user_mfa", "user_id"),
        ),
    ),
    ("groups.db", (("user_group_membership", "user_id"),)),
)
ALL_KEYED_TABLES: Tuple[KeyedTable, ...] = tuple(
    table for _, tables in SQLITE_STORES for table in tables
)

# WHERE templates shared by both stores; {p} is the driver's placeholder and
# {users} the qualified users table.
ONE_ACCOUNT_WHERE = "{column} = {p}"
ORPHAN_WHERE = (
    "NOT EXISTS (SELECT 1 FROM {users} u WHERE u.username = {table}.{column})"
)
# After a deletion: the name's rows, only while no account with the name
# exists -- checked in the same DELETE statement, so a concurrently
# re-created account never loses its rows.
DELETED_ACCOUNT_WHERE = ONE_ACCOUNT_WHERE + " AND " + ORPHAN_WHERE


class AccountDataPurger(Protocol):
    def purge(self, username: str) -> int: ...

    def purge_deleted(self, username: str) -> int: ...

    def purge_orphans(self) -> int: ...


def _require_username(username: str) -> str:
    """An account name must be a non-blank string before any delete runs."""
    if not isinstance(username, str) or not username.strip():
        raise ValueError("username must be a non-empty account name")
    return username


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


_SQLITE_BUSY_TIMEOUT_SECONDS = 30.0


class SqliteAccountDataPurger:
    """Purges the SQLite stores under one server data directory."""

    def __init__(self, server_data_dir: Path) -> None:
        if server_data_dir is None:
            raise ValueError("server_data_dir is required")
        self._server_data_dir = Path(server_data_dir)

    def _delete_in_store(
        self,
        relative: str,
        tables: Tuple[KeyedTable, ...],
        where: str,
        params: Tuple[str, ...],
    ) -> int:
        path = self._server_data_dir / relative
        if not path.exists():
            return 0  # a store never created holds no rows
        # mode=rw: never create a store file as a side effect.
        conn = sqlite3.connect(
            f"file:{path}?mode=rw", uri=True, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS
        )
        removed = 0
        try:
            users = "main.users"
            # Only the orphan template reads the users table; a per-account
            # purge never depends on the accounts file.
            if relative != ACCOUNTS_DB and "{users}" in where:
                accounts = self._server_data_dir / ACCOUNTS_DB
                # Read-only URI: a missing accounts store must fail, never be
                # created empty (an empty users table would match every row).
                conn.execute(
                    "ATTACH DATABASE ? AS accounts", (f"file:{accounts}?mode=ro",)
                )
                users = "accounts.users"
            for table, column in tables:
                if not _table_exists(conn, table):
                    continue
                clause = where.format(column=column, table=table, users=users, p="?")
                removed += conn.execute(
                    f"DELETE FROM {table} WHERE {clause}", params
                ).rowcount
            conn.commit()
        finally:
            conn.close()
        return removed

    def purge(self, username: str) -> int:
        """Delete every row keyed to *username*; returns rows removed."""
        name = _require_username(username)
        return sum(
            self._delete_in_store(relative, tables, ONE_ACCOUNT_WHERE, (name,))
            for relative, tables in SQLITE_STORES
        )

    def purge_deleted(self, username: str) -> int:
        """Delete rows keyed to a deleted *username*, each store's rows only
        while no account with that name exists (checked in the DELETE)."""
        name = _require_username(username)
        return sum(
            self._delete_in_store(relative, tables, DELETED_ACCOUNT_WHERE, (name,))
            for relative, tables in SQLITE_STORES
        )

    def purge_orphans(self) -> int:
        """Delete rows whose account name has no ``users`` row."""
        if not (self._server_data_dir / ACCOUNTS_DB).exists():
            raise FileNotFoundError(f"accounts store missing: {ACCOUNTS_DB}")
        return sum(
            self._delete_in_store(relative, tables, ORPHAN_WHERE, ())
            for relative, tables in SQLITE_STORES
        )


class PostgresAccountDataPurger:
    """Purges the shared PostgreSQL tables in one transaction."""

    def __init__(self, pool: "ConnectionPool") -> None:
        if pool is None:
            raise ValueError("a PostgreSQL connection pool is required")
        self._pool = pool

    def _delete_all(self, where: str, params: Tuple[str, ...]) -> int:
        removed = 0
        with self._pool.connection() as conn:
            for table, column in ALL_KEYED_TABLES:
                clause = where.format(column=column, table=table, users="users", p="%s")
                removed += conn.execute(
                    f"DELETE FROM {table} WHERE {clause}", params
                ).rowcount
            conn.commit()
        return removed

    def purge(self, username: str) -> int:
        """Delete every row keyed to *username*; returns rows removed."""
        return self._delete_all(ONE_ACCOUNT_WHERE, (_require_username(username),))

    def purge_deleted(self, username: str) -> int:
        """Delete rows keyed to a deleted *username*, only while no account
        with that name exists (checked in each DELETE, one transaction)."""
        return self._delete_all(DELETED_ACCOUNT_WHERE, (_require_username(username),))

    def purge_orphans(self) -> int:
        """Delete rows whose account name has no ``users`` row."""
        return self._delete_all(ORPHAN_WHERE, ())


def build_account_data_purger(
    storage_mode: str,
    server_data_dir: Path,
    connection_pool: Optional["ConnectionPool"],
) -> AccountDataPurger:
    """The purger for the configured storage mode (no fallback between modes)."""
    if storage_mode == "postgres":
        if connection_pool is None:
            raise ValueError("postgres storage requires a connection pool")
        return PostgresAccountDataPurger(connection_pool)
    if storage_mode == "sqlite":
        return SqliteAccountDataPurger(server_data_dir)
    raise ValueError(f"unknown storage_mode {storage_mode!r}")


def sweep_orphaned_account_data(purger: AccountDataPurger) -> None:
    """Start-up sweep: remove rows whose account no longer exists.

    Idempotent.  Logs the removed count at INFO; a failure is logged at ERROR
    and never propagates, so start-up does not depend on it.
    """
    try:
        removed = purger.purge_orphans()
    except Exception as exc:  # noqa: BLE001 - start-up must not fail on this
        logger.error("Orphaned account data sweep failed: %s", exc, exc_info=True)
        return
    logger.info("Orphaned account data sweep removed %d row(s)", removed)


async def run_startup_orphan_sweep(purger: AccountDataPurger) -> None:
    """Run :func:`sweep_orphaned_account_data` on a worker thread, so the
    store I/O never blocks the event loop."""
    import anyio.to_thread

    await anyio.to_thread.run_sync(sweep_orphaned_account_data, purger)
