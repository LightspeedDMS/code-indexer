"""Bug #2022 Gap 4: classification of a fatal chunk-store failure into the
reserved ``cidx index`` exit codes.

Disk-full in particular cannot be produced by a real child inside a unit
test, so its classification is proven here on the same exception chain the
chunker raises (``ChunkStoreUnavailableError(...) from <sqlite/OS error>``).
ENVIRONMENT must never be reported as CORRUPTION: corruption triggers a
restore over the store.
"""

from __future__ import annotations

import errno
import sqlite3

import pytest

from code_indexer.services.index_failure_exit_codes import (
    EXIT_CODE_CHUNK_STORE_CORRUPTION,
    EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
    GENERIC_INDEX_FAILURE_EXIT_CODE,
    ChunkStoreFailureKind,
    FatalChunkStoreIndexError,
    chunk_store_failure_kind_for_exit_code,
    classify_fatal_chunk_store_failure,
    index_failure_exit_code,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError


def _typed_from(cause: BaseException) -> ChunkStoreUnavailableError:
    """The exact shape file_chunking_manager raises."""
    try:
        try:
            raise cause
        except BaseException as inner:
            raise ChunkStoreUnavailableError(
                f"Chunk store unavailable while writing f.py: {inner}"
            ) from inner
    except ChunkStoreUnavailableError as typed:
        return typed


@pytest.mark.parametrize(
    "cause",
    [
        sqlite3.OperationalError("database or disk is full"),
        OSError(errno.ENOSPC, "No space left on device"),
        sqlite3.OperationalError("attempt to write a readonly database"),
        sqlite3.OperationalError("unable to open database file"),
        PermissionError(errno.EACCES, "Permission denied"),
    ],
    ids=["sqlite-full", "enospc", "readonly", "cantopen", "permission"],
)
def test_environment_failures_are_never_corruption(cause: BaseException) -> None:
    typed = _typed_from(cause)
    assert (
        classify_fatal_chunk_store_failure(typed) is ChunkStoreFailureKind.ENVIRONMENT
    )
    assert index_failure_exit_code(typed) == EXIT_CODE_CHUNK_STORE_ENVIRONMENT


@pytest.mark.parametrize(
    "message", ["database disk image is malformed", "file is not a database"]
)
def test_damaged_store_is_corruption(message: str) -> None:
    typed = _typed_from(sqlite3.DatabaseError(message))
    assert classify_fatal_chunk_store_failure(typed) is ChunkStoreFailureKind.CORRUPTION
    assert index_failure_exit_code(typed) == EXIT_CODE_CHUNK_STORE_CORRUPTION


def test_typed_error_wrapped_in_a_generic_error_is_still_found() -> None:
    typed = _typed_from(sqlite3.DatabaseError("database disk image is malformed"))
    try:
        raise RuntimeError("Git-aware indexing failed") from typed
    except RuntimeError as wrapper:
        assert index_failure_exit_code(wrapper) == EXIT_CODE_CHUNK_STORE_CORRUPTION


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("chunks.db cannot be opened: database or disk is full", "environment"),
        ("chunks.db cannot be opened: database disk image is malformed", "corruption"),
    ],
)
def test_typed_error_without_recorded_cause_uses_its_message(
    message: str, expected: str
) -> None:
    kind = classify_fatal_chunk_store_failure(ChunkStoreUnavailableError(message))
    assert kind is not None and kind.value == expected


def test_non_chunk_store_failure_keeps_generic_exit_code() -> None:
    error = ValueError("VOYAGE_API_KEY environment variable is required")
    assert classify_fatal_chunk_store_failure(error) is None
    assert index_failure_exit_code(error) == GENERIC_INDEX_FAILURE_EXIT_CODE


def test_cause_cycle_terminates() -> None:
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert classify_fatal_chunk_store_failure(first) is None


@pytest.mark.parametrize("kind", list(ChunkStoreFailureKind))
def test_exit_code_round_trip(kind: ChunkStoreFailureKind) -> None:
    typed = _typed_from(
        sqlite3.DatabaseError("database disk image is malformed")
        if kind is ChunkStoreFailureKind.CORRUPTION
        else OSError(errno.ENOSPC, "No space left on device")
    )
    assert (
        chunk_store_failure_kind_for_exit_code(index_failure_exit_code(typed)) is kind
    )


@pytest.mark.parametrize("returncode", [None, 0, 1, 2, -15, 137])
def test_other_exit_codes_carry_no_kind(returncode) -> None:
    assert chunk_store_failure_kind_for_exit_code(returncode) is None


@pytest.mark.parametrize(
    "message",
    [
        "disk I/O error",
        "database is locked",
        "database table is locked",
        "no such table: chunks",
    ],
)
def test_io_lock_and_unknown_sqlite_errors_are_never_corruption(message: str) -> None:
    typed = _typed_from(sqlite3.OperationalError(message))
    assert index_failure_exit_code(typed) == EXIT_CODE_CHUNK_STORE_ENVIRONMENT


def _named(message: str, error_name: str) -> sqlite3.OperationalError:
    """An error carrying SQLite's result-code name (Python 3.11+ sets it)."""
    error = sqlite3.OperationalError(message)
    setattr(error, "sqlite_errorname", error_name)
    return error


@pytest.mark.parametrize(
    ("error_name", "expected"),
    [
        ("SQLITE_IOERR_SHORT_READ", ChunkStoreFailureKind.ENVIRONMENT),
        ("SQLITE_IOERR", ChunkStoreFailureKind.ENVIRONMENT),
        ("SQLITE_BUSY", ChunkStoreFailureKind.ENVIRONMENT),
        ("SQLITE_CORRUPT_INDEX", ChunkStoreFailureKind.CORRUPTION),
        ("SQLITE_NOTADB", ChunkStoreFailureKind.CORRUPTION),
    ],
)
def test_result_code_name_wins_over_message(
    error_name: str, expected: ChunkStoreFailureKind
) -> None:
    typed = _typed_from(_named("some driver text", error_name))
    assert classify_fatal_chunk_store_failure(typed) is expected


@pytest.mark.parametrize(
    "message",
    [
        "database disk image is malformed",
        "file is not a database",
        "malformed database schema (idx_chunks_path)",
    ],
)
def test_read_path_corruption_without_typed_error_exits_corruption(
    message: str,
) -> None:
    try:
        try:
            raise sqlite3.DatabaseError(message)
        except sqlite3.DatabaseError as inner:
            raise RuntimeError("HNSW finalize failed") from inner
    except RuntimeError as wrapper:
        assert index_failure_exit_code(wrapper) == EXIT_CODE_CHUNK_STORE_CORRUPTION


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.OperationalError("database is locked"),
        _named("busy", "SQLITE_BUSY"),
    ],
    ids=["ioerr", "locked", "busy"],
)
def test_read_path_io_lock_and_busy_keep_generic_exit_code(error: Exception) -> None:
    assert classify_fatal_chunk_store_failure(error) is None
    assert index_failure_exit_code(error) == GENERIC_INDEX_FAILURE_EXIT_CODE


def test_fatal_error_carries_kind_and_is_a_runtime_error() -> None:
    error = FatalChunkStoreIndexError("boom", ChunkStoreFailureKind.ENVIRONMENT)
    assert isinstance(error, RuntimeError)
    assert error.kind is ChunkStoreFailureKind.ENVIRONMENT
