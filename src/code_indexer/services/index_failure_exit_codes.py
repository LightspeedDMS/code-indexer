"""Typed fatal chunk-store failure across the ``cidx index`` child boundary
(Bug #2022 Gap 4).

The server runs ``cidx index`` as a child process. A fatal chunk-store
failure inside the child (``ChunkStoreUnavailableError``, Bug #1746) used to
exit 1 like every other failure, so the parent could only tell "the store is
corrupt" apart from "anything else" by parsing free-form stderr -- which it
never did, so the integrity-gate self-heal never ran and the refresh failed
identically forever.

The child now exits with a reserved code per failure KIND, and the parent
maps the code back to the kind. Two kinds, because they demand opposite
reactions:

- ``CORRUPTION``: SQLite reports the store itself as damaged
  (SQLITE_CORRUPT / SQLITE_NOTADB). The parent may restore it from the
  published snapshot, but only after its own integrity check completes and
  confirms the damage.
- ``ENVIRONMENT``: every other fatal store failure -- I/O error, lock or
  busy, disk full, read-only, permission, any OS error. The store is not
  known to be damaged; restoring over it could destroy good data, so the
  parent only backs off.

Deliberately dependency-light: imported by the CLI's failure path and by the
refresh scheduler.
"""

from __future__ import annotations

import sqlite3
from enum import Enum
from typing import Iterator, Optional

from code_indexer.storage.sqlite_chunk_store import (
    ChunkStoreUnavailableError,
    message_reports_sqlite_corruption,
    sqlite_error_reports_corruption,
)


class ChunkStoreFailureKind(str, Enum):
    CORRUPTION = "corruption"
    ENVIRONMENT = "environment"


#: Exit code of a ``cidx index`` run that failed for any other reason.
GENERIC_INDEX_FAILURE_EXIT_CODE = 1

#: Reserved exit codes, one per kind. Outside the 0-2 range click uses and
#: below 128 (signal-terminated children report a negative returncode).
EXIT_CODE_CHUNK_STORE_CORRUPTION = 86
EXIT_CODE_CHUNK_STORE_ENVIRONMENT = 87

_EXIT_CODE_BY_KIND = {
    ChunkStoreFailureKind.CORRUPTION: EXIT_CODE_CHUNK_STORE_CORRUPTION,
    ChunkStoreFailureKind.ENVIRONMENT: EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
}
_KIND_BY_EXIT_CODE = {code: kind for kind, code in _EXIT_CODE_BY_KIND.items()}

#: Bound on how far an exception's cause/context chain is followed.
_MAX_CHAIN_DEPTH = 32


def _iter_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        if len(seen) >= _MAX_CHAIN_DEPTH:
            return
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def classify_fatal_chunk_store_failure(
    exc: BaseException,
) -> Optional[ChunkStoreFailureKind]:
    """Return the kind of fatal chunk-store failure behind ``exc``, or None
    when ``exc`` is not one.

    CORRUPTION only by allow-list: SQLite itself reported the store damaged
    (``sqlite_error_reports_corruption``). A typed failure for any other
    reason (I/O, locked, busy, full, read-only, permission, unknown) is
    ENVIRONMENT. Without a typed error in the chain, a bare SQLite
    corruption report raised on a read path is still CORRUPTION; nothing
    else is classified."""
    chain = list(_iter_chain(exc))
    typed_at = next(
        (i for i, e in enumerate(chain) if isinstance(e, ChunkStoreUnavailableError)),
        None,
    )
    if typed_at is None:
        if any(sqlite_error_reports_corruption(e) for e in chain):
            return ChunkStoreFailureKind.CORRUPTION
        return None
    for cause in chain[typed_at + 1 :]:
        if isinstance(cause, (OSError, sqlite3.Error)):
            return _kind(sqlite_error_reports_corruption(cause))
    # No underlying cause recorded: the typed error's own message embeds the
    # underlying SQLite/OS text.
    return _kind(message_reports_sqlite_corruption(str(chain[typed_at])))


def _kind(reports_corruption: bool) -> ChunkStoreFailureKind:
    if reports_corruption:
        return ChunkStoreFailureKind.CORRUPTION
    return ChunkStoreFailureKind.ENVIRONMENT


def index_failure_exit_code(exc: BaseException) -> int:
    """Exit code for a ``cidx index`` run that failed with ``exc``."""
    kind = classify_fatal_chunk_store_failure(exc)
    if kind is None:
        return GENERIC_INDEX_FAILURE_EXIT_CODE
    return _EXIT_CODE_BY_KIND[kind]


def chunk_store_failure_kind_for_exit_code(
    returncode: Optional[int],
) -> Optional[ChunkStoreFailureKind]:
    """Map a ``cidx index`` child's exit code back to its failure kind."""
    if returncode is None:
        return None
    return _KIND_BY_EXIT_CODE.get(returncode)


class FatalChunkStoreIndexError(RuntimeError):
    """Raised in the parent when a ``cidx index`` child failed with a fatal
    chunk-store error of a known ``kind``."""

    def __init__(self, message: str, kind: ChunkStoreFailureKind) -> None:
        super().__init__(message)
        self.kind = kind
