"""Unit tests for composites.py graceful handling of missing call_graph table.

Bug #238: SCIP composites query fails with 'no such table: call_graph' on test
fixture databases that have a minimal schema (no call_graph table).

These tests verify that:
- AC1: Missing call_graph table does NOT produce ERROR-level log entries
- AC2: Valid SCIP databases continue to work normally
- AC3: Missing table errors are logged at DEBUG level, not ERROR level
"""

import logging
from pathlib import Path

try:
    from pysqlite3 import dbapi2 as sqlite3
except ImportError:
    import sqlite3

import pytest

from code_indexer.scip.query.composites import (
    _find_target_definition,
    _bfs_traverse_dependents,
    trace_call_chain,
    get_smart_context,
)


def _create_minimal_scip_db(db_path: Path) -> None:
    """Create a minimal .scip.db without the call_graph table.

    Simulates a test fixture database that lacks the full schema.
    Tables: documents, symbols, occurrences (no call_graph, no symbol_references).
    """
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            relative_path TEXT NOT NULL,
            language TEXT
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            display_name TEXT,
            kind INTEGER,
            documentation TEXT
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS occurrences (
            id INTEGER PRIMARY KEY,
            symbol_id INTEGER REFERENCES symbols(id),
            document_id INTEGER REFERENCES documents(id),
            start_line INTEGER,
            start_character INTEGER,
            end_line INTEGER,
            end_character INTEGER,
            role INTEGER
        )
    """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def comprehensive_fixture_dir() -> Path:
    """Path to the directory containing the real comprehensive SCIP fixture DB.

    Uses the authoritative comprehensive_index.scip.db which has the correct
    full schema (call_graph, symbol_references, symbol_relationships, etc.).
    Skips if fixture is not available.
    """
    fixture_dir = Path(__file__).parent.parent.parent / "scip" / "fixtures"
    fixture_db = fixture_dir / "comprehensive_index.scip.db"
    if not fixture_db.exists():
        pytest.skip(f"Comprehensive SCIP fixture not found at {fixture_db}")
    return fixture_dir


class TestFindTargetDefinitionMissingCallGraph:
    """Tests for _find_target_definition with minimal schema databases."""

    def test_handles_missing_call_graph_table(self, tmp_path, caplog):
        """_find_target_definition returns None when call_graph table is missing.

        AC1: No ERROR-level log entries for missing table.
        AC3: Missing table logged at DEBUG level (if at all).
        """
        db_path = tmp_path / "test.scip.db"
        _create_minimal_scip_db(db_path)

        with caplog.at_level(logging.DEBUG, logger="code_indexer.scip.query.composites"):
            result = _find_target_definition("SomeSymbol", tmp_path)

        assert result is None

        error_messages = [
            r.message for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert not error_messages, (
            f"Expected no ERROR-level log messages, but got: {error_messages}"
        )

        debug_messages = [
            r.message
            for r in caplog.records
            if r.levelno == logging.DEBUG and "incomplete schema" in r.message
        ]
        assert len(debug_messages) >= 1, (
            "Expected at least one DEBUG-level 'incomplete schema' message"
        )

    def test_no_exception_raised_for_missing_call_graph(self, tmp_path):
        """_find_target_definition must not raise when call_graph is missing."""
        db_path = tmp_path / "test.scip.db"
        _create_minimal_scip_db(db_path)

        result = _find_target_definition("AnySymbol", tmp_path)
        assert result is None


class TestBfsTraverseDependentsMissingCallGraph:
    """Tests for _bfs_traverse_dependents with minimal schema databases."""

    def test_handles_missing_call_graph_table(self, tmp_path, caplog):
        """_bfs_traverse_dependents returns empty list when call_graph is missing.

        AC1: No ERROR-level log entries for missing table.
        AC3: Missing table logged at DEBUG level (if at all).
        """
        db_path = tmp_path / "test.scip.db"
        _create_minimal_scip_db(db_path)

        with caplog.at_level(logging.DEBUG, logger="code_indexer.scip.query.composites"):
            result = _bfs_traverse_dependents(
                symbol="SomeSymbol",
                scip_dir=tmp_path,
                depth=2,
                project=None,
                exclude=None,
                include=None,
                kind=None,
            )

        assert isinstance(result, list)
        assert result == []

        error_messages = [
            r.message for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert not error_messages, (
            f"Expected no ERROR-level log messages, but got: {error_messages}"
        )

        debug_messages = [
            r.message
            for r in caplog.records
            if r.levelno == logging.DEBUG and "incomplete schema" in r.message
        ]
        assert len(debug_messages) >= 1, (
            "Expected at least one DEBUG-level 'incomplete schema' message"
        )

    def test_no_exception_raised_for_missing_call_graph(self, tmp_path):
        """_bfs_traverse_dependents must not raise when call_graph is missing."""
        db_path = tmp_path / "test.scip.db"
        _create_minimal_scip_db(db_path)

        result = _bfs_traverse_dependents(
            symbol="AnySymbol",
            scip_dir=tmp_path,
            depth=2,
            project=None,
            exclude=None,
            include=None,
            kind=None,
        )
        assert result == []


class TestTraceCallChainMissingCallGraph:
    """Tests for trace_call_chain with minimal schema databases."""

    def test_handles_missing_call_graph_table(self, tmp_path, caplog):
        """trace_call_chain returns empty chains when call_graph is missing.

        AC1: No ERROR or WARNING-level log messages for missing table.
        AC3: Missing table logged at DEBUG level (if at all).
        """
        db_path = tmp_path / "test.scip.db"
        _create_minimal_scip_db(db_path)

        with caplog.at_level(logging.DEBUG, logger="code_indexer.scip.query.composites"):
            result = trace_call_chain(
                from_symbol="from_sym",
                to_symbol="to_sym",
                scip_dir=tmp_path,
            )

        assert result is not None
        assert result.chains == []

        noisy_messages = [
            r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and ("call_graph" in r.message or "no such table" in r.message.lower())
        ]
        assert not noisy_messages, (
            f"Expected no WARNING/ERROR log messages for missing table, "
            f"but got: {noisy_messages}"
        )

    def test_no_exception_raised_for_missing_call_graph(self, tmp_path):
        """trace_call_chain must not raise when call_graph is missing."""
        db_path = tmp_path / "test.scip.db"
        _create_minimal_scip_db(db_path)

        result = trace_call_chain(
            from_symbol="from_sym",
            to_symbol="to_sym",
            scip_dir=tmp_path,
        )
        assert result.chains == []
        assert result.total_chains_found == 0


class TestGetSmartContextMissingCallGraph:
    def test_handles_missing_call_graph_table(self, tmp_path, caplog):
        """get_smart_context handles .scip.db without call_graph table."""
        db_path = tmp_path / "test.scip.db"
        _create_minimal_scip_db(db_path)

        with caplog.at_level(logging.DEBUG, logger="code_indexer.scip.query.composites"):
            result = get_smart_context("SomeSymbol", tmp_path)

        # Should return a result (not raise), with empty data
        assert result is not None

        # No ERROR-level messages
        error_messages = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_messages


class TestValidScipDbStillWorks:
    """Regression tests ensuring valid SCIP databases still work normally.

    AC2: Valid SCIP databases with call_graph table continue to work normally.
    Uses the real comprehensive_index.scip.db fixture which has the authoritative
    full schema (call_graph, symbol_references, symbol_relationships, etc.).
    """

    def test_find_target_definition_returns_none_for_unknown_symbol(
        self, comprehensive_fixture_dir
    ):
        """_find_target_definition returns None for an unknown symbol (no error)."""
        result = _find_target_definition("__NonExistentSymbol__", comprehensive_fixture_dir)
        assert result is None

    def test_bfs_traverse_dependents_returns_empty_for_unknown_symbol(
        self, comprehensive_fixture_dir
    ):
        """_bfs_traverse_dependents returns empty list for an unknown symbol."""
        result = _bfs_traverse_dependents(
            symbol="__NonExistentSymbol__",
            scip_dir=comprehensive_fixture_dir,
            depth=2,
            project=None,
            exclude=None,
            include=None,
            kind=None,
        )
        assert result == []

    def test_trace_call_chain_returns_empty_for_unknown_symbols(
        self, comprehensive_fixture_dir
    ):
        """trace_call_chain returns empty result for unknown symbols."""
        result = trace_call_chain(
            from_symbol="__NonExistent_from__",
            to_symbol="__NonExistent_to__",
            scip_dir=comprehensive_fixture_dir,
        )
        assert result.chains == []
        assert result.total_chains_found == 0

    def test_no_errors_on_real_fixture(self, comprehensive_fixture_dir, caplog):
        """No ERROR-level logs when querying the real full-schema fixture database."""
        with caplog.at_level(logging.DEBUG, logger="code_indexer.scip.query.composites"):
            _find_target_definition("__NonExistentSymbol__", comprehensive_fixture_dir)
            _bfs_traverse_dependents(
                "__NonExistentSymbol__",
                comprehensive_fixture_dir,
                depth=1,
                project=None,
                exclude=None,
                include=None,
                kind=None,
            )
            trace_call_chain(
                "__NonExistent_from__",
                "__NonExistent_to__",
                comprehensive_fixture_dir,
            )

        error_messages = [
            r.message for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert not error_messages, (
            f"Expected no ERROR-level logs with real fixture, but got: {error_messages}"
        )
