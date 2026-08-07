"""DB-backed golden-repo alias locking (Issue #1546 Phase 1).

Replaces the file-based ``WriteLockManager`` (Story #230) with a
session-held-transaction lock, implemented for both PostgreSQL and SQLite.
See the module docstring in ``base.py`` for the full design rationale.

Phase 1 scope: the ``AliasLockStore`` abstraction itself, standalone and
fully tested. Rewiring the ~8 real call sites onto this mechanism is later
phase work -- nothing in this package is imported by production code paths
yet.
"""

from __future__ import annotations

from .base import (
    AliasLockHandle,
    AliasLockOwnershipLostError,
    AliasLockStore,
)
from .postgres_store import PostgresAliasLockStore
from .sqlite_store import SqliteAliasLockStore

__all__ = [
    "AliasLockHandle",
    "AliasLockOwnershipLostError",
    "AliasLockStore",
    "PostgresAliasLockStore",
    "SqliteAliasLockStore",
]
