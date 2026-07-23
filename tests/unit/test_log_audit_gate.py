"""Unit tests for the E2E log-audit gate (Story #1122).

Tests the core logic in tests/e2e/log_audit_gate.py:
  - Allowlist filtering
  - Watermark-based diffing (new entries only)
  - Poll-until-stable count detection (bounded, deterministic)
  - Phase-fail reporting
  - Pagination completeness: query_logs_via_mcp must return ALL entries
    regardless of how many pages the server needs to serve them across.

These tests use real in-process logic -- no mocks of the gate itself.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# The module under test does NOT exist yet -- these tests are RED.
# ---------------------------------------------------------------------------
from tests.e2e.log_audit_gate import (
    LOG_AUDIT_ALLOWLIST,
    AuditGateResult,
    filter_new_entries,
    is_allowlisted,
    build_audit_failure_message,
    poll_until_stable_count,
    query_logs_via_mcp,
)


# ---------------------------------------------------------------------------
# Fixtures: sample log entries matching admin_logs_query response shape
# ---------------------------------------------------------------------------


def _make_entry(
    level: str = "ERROR",
    message: str = "Something went wrong",
    entry_id: int = 1,
    source: str = "server",
) -> dict:
    """Return a minimal log entry dict matching LogAggregatorService shape."""
    return {
        "id": entry_id,
        "level": level,
        "message": message,
        "source": source,
        "timestamp": "2026-06-15T10:00:00.000Z",
        "correlation_id": None,
    }


# ==========================================================================
# AC2: Allowlist tests
# ==========================================================================


class TestIsAllowlisted:
    """Tests for is_allowlisted() -- the per-entry allowlist check."""

    def test_allowlist_is_a_non_empty_list_of_strings(self):
        """LOG_AUDIT_ALLOWLIST must be a non-empty list of strings."""
        assert isinstance(LOG_AUDIT_ALLOWLIST, list)
        assert len(LOG_AUDIT_ALLOWLIST) > 0
        for item in LOG_AUDIT_ALLOWLIST:
            assert isinstance(item, str), (
                f"Expected str, got {type(item).__name__}: {item!r}"
            )

    def test_allowlisted_message_returns_true(self):
        """An entry whose message contains an allowlisted substring passes."""
        first_pattern = LOG_AUDIT_ALLOWLIST[0]
        entry = _make_entry(message=f"prefix {first_pattern} suffix")
        assert is_allowlisted(entry) is True

    def test_unknown_message_returns_false(self):
        """An entry whose message is not on the allowlist fails the check."""
        entry = _make_entry(
            level="ERROR",
            message="CRITICAL: database connection pool exhausted -- this is brand new",
        )
        assert is_allowlisted(entry) is False

    def test_unknown_warning_returns_false(self):
        """A WARNING whose message is not on the allowlist fails the check."""
        entry = _make_entry(
            level="WARNING",
            message="New unrecognised warning that should fail the gate",
        )
        assert is_allowlisted(entry) is False

    def test_case_insensitive_matching(self):
        """Allowlist matching is case-insensitive so UPPER and mixed case pass."""
        first_pattern = LOG_AUDIT_ALLOWLIST[0]
        entry = _make_entry(message=first_pattern.upper())
        assert is_allowlisted(entry) is True

    def test_bug1421_temporal_snapshot_reassembly_retry_warning_is_allowlisted(self):
        """Issue #1445: the Bug #1421 concurrent-checkpoint-rewrite retry WARNING
        (temporal_snapshot_store.py's read_temporal_snapshot()) is the intentional,
        already-shipped self-healing retry path -- not an error condition -- and
        must be allowlisted so the zero-tolerance log-audit gate does not fail
        Phase 3 when this rare race actually triggers during a real e2e run.

        This is the exact message shape rendered by the logger.warning(...) call
        at temporal_snapshot_store.py:211-219, with a real job UUID and
        attempt/page numbers substituted in place of the %s/%d format fields.
        """
        message = (
            "temporal snapshot job 3f9c1a2e-8b7d-4e11-9a2c-5d6e7f8a9b0c: "
            "concurrent checkpoint rewrite detected during reassembly "
            "(attempt 1/5) -- retrying from page 0 against the latest write: "
            "Temporal snapshot for job "
            "'3f9c1a2e-8b7d-4e11-9a2c-5d6e7f8a9b0c' total_pages changed "
            "mid-reassembly (25 -> 30) while reading page 15."
        )
        entry = _make_entry(
            level="WARNING",
            message=message,
            source="code_indexer.server.services.temporal_snapshot_store",
        )
        assert is_allowlisted(entry) is True


# ==========================================================================
# AC2 + Watermark: filter_new_entries tests
# ==========================================================================


class TestFilterNewEntries:
    """Tests for filter_new_entries(entries, watermark_id) -- watermark diffing."""

    def test_empty_entries_returns_empty(self):
        """No entries -> no new entries."""
        assert filter_new_entries([], watermark_id=0) == []

    def test_entries_below_watermark_excluded(self):
        """Entries with id <= watermark_id are old (already audited)."""
        entries = [_make_entry(entry_id=5), _make_entry(entry_id=3)]
        assert filter_new_entries(entries, watermark_id=10) == []

    def test_entries_above_watermark_included(self):
        """Entries with id > watermark_id are new."""
        entries = [
            _make_entry(entry_id=11),
            _make_entry(entry_id=15),
        ]
        result = filter_new_entries(entries, watermark_id=10)
        assert len(result) == 2

    def test_mixed_returns_only_new(self):
        """Only entries newer than the watermark are returned."""
        entries = [
            _make_entry(entry_id=5),  # old
            _make_entry(entry_id=10),  # equal to watermark -- old
            _make_entry(entry_id=11),  # new
            _make_entry(entry_id=20),  # new
        ]
        result = filter_new_entries(entries, watermark_id=10)
        assert [e["id"] for e in result] == [11, 20]

    def test_watermark_zero_returns_all(self):
        """Watermark of 0 (no prior state) returns all entries."""
        entries = [_make_entry(entry_id=1), _make_entry(entry_id=2)]
        result = filter_new_entries(entries, watermark_id=0)
        assert len(result) == 2


# ==========================================================================
# Poll-until-stable count tests
# ==========================================================================


class TestPollUntilStableCount:
    """Tests for poll_until_stable_count() -- bounded polling for live phases.

    The function polls a count callable until two consecutive counts match
    (stable) or the max_attempts ceiling is reached.  This prevents
    under-reporting from the async log writer (Bug #1078).
    """

    def test_immediately_stable_returns_first_count(self):
        """If count is stable from the first poll, returns after two calls."""
        calls = []
        stable_count = 42

        def count_fn() -> int:
            calls.append(stable_count)
            return stable_count

        result = poll_until_stable_count(count_fn, max_attempts=5, sleep_seconds=0.0)
        assert result == stable_count
        # Must call at least twice to detect stability
        assert len(calls) >= 2

    def test_growing_count_stabilizes_eventually(self):
        """If count grows initially then stabilizes, returns the stable value."""
        sequence = iter([10, 12, 15, 15])

        def count_fn() -> int:
            return next(sequence)

        result = poll_until_stable_count(count_fn, max_attempts=10, sleep_seconds=0.0)
        assert result == 15

    def test_max_attempts_reached_returns_last_count(self):
        """If count never stabilizes within max_attempts, returns the last seen count."""
        call_count = [0]

        def count_fn() -> int:
            call_count[0] += 1
            return call_count[0]  # Always growing -- never stable

        result = poll_until_stable_count(count_fn, max_attempts=5, sleep_seconds=0.0)
        # Should have returned after max_attempts, returning the last count
        assert result == 5
        assert call_count[0] == 5

    def test_single_attempt_returns_count(self):
        """max_attempts=1 returns after a single count call."""

        def count_fn() -> int:
            return 7

        result = poll_until_stable_count(count_fn, max_attempts=1, sleep_seconds=0.0)
        assert result == 7


# ==========================================================================
# AC2: build_audit_failure_message tests
# ==========================================================================


class TestBuildAuditFailureMessage:
    """Tests for build_audit_failure_message() -- reporting."""

    def test_returns_string(self):
        """build_audit_failure_message returns a non-empty string."""
        entry = _make_entry(level="ERROR", message="DB pool exhausted")
        result = build_audit_failure_message([entry], phase_name="Phase 3")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_includes_phase_name(self):
        """Failure message includes the phase name."""
        entry = _make_entry(level="ERROR", message="DB pool exhausted")
        result = build_audit_failure_message([entry], phase_name="Phase 3")
        assert "Phase 3" in result

    def test_includes_entry_level(self):
        """Failure message includes each entry's level."""
        entry = _make_entry(level="WARNING", message="Some warning")
        result = build_audit_failure_message([entry], phase_name="Phase 3")
        assert "WARNING" in result

    def test_includes_entry_message(self):
        """Failure message includes each entry's message snippet."""
        entry = _make_entry(level="ERROR", message="unique_error_text_12345")
        result = build_audit_failure_message([entry], phase_name="Phase 3")
        assert "unique_error_text_12345" in result

    def test_includes_count(self):
        """Failure message includes the count of offending entries."""
        entries = [_make_entry(entry_id=i) for i in range(1, 4)]
        result = build_audit_failure_message(entries, phase_name="Phase 3")
        assert "3" in result


# ==========================================================================
# AuditGateResult tests
# ==========================================================================


class TestAuditGateResult:
    """Tests for AuditGateResult dataclass."""

    def test_passed_true_when_no_violations(self):
        """A result with no violations has passed=True."""
        result = AuditGateResult(passed=True, violations=[], phase_name="Phase 3")
        assert result.passed is True
        assert result.violations == []

    def test_passed_false_when_violations_present(self):
        """A result with violations has passed=False."""
        entry = _make_entry(level="ERROR")
        result = AuditGateResult(passed=False, violations=[entry], phase_name="Phase 3")
        assert result.passed is False
        assert len(result.violations) == 1

    def test_failure_message_present_when_not_passed(self):
        """AuditGateResult.failure_message returns a string when passed=False."""
        entry = _make_entry(level="ERROR", message="unique_marker_xyz")
        result = AuditGateResult(passed=False, violations=[entry], phase_name="Phase 3")
        msg = result.failure_message()
        assert isinstance(msg, str)
        assert "unique_marker_xyz" in msg

    def test_failure_message_empty_when_passed(self):
        """AuditGateResult.failure_message returns empty string when passed=True."""
        result = AuditGateResult(passed=True, violations=[], phase_name="Phase 3")
        assert result.failure_message() == ""


# ==========================================================================
# Mutation check: gate catches new non-allowlisted WARNING
# ==========================================================================


class TestMutationCheck:
    """Mandatory mutation/negative check (AC from Story #1122).

    Proves that a new non-allowlisted WARNING causes gate failure,
    and that its absence causes gate passage.
    """

    def test_gate_fails_on_new_non_allowlisted_warning(self):
        """
        Emitting a WARNING that is NOT on the allowlist must cause gate failure.

        This is the mutation check: when a new non-allowlisted WARNING appears
        in the log entries returned by admin_logs_query, the gate must FAIL.
        """
        entries = [
            _make_entry(
                level="WARNING",
                message="[MUTATION_TEST] deliberate non-allowlisted warning",
                entry_id=99,
            )
        ]
        # After watermark=0, entry 99 is new
        new_entries = filter_new_entries(entries, watermark_id=0)
        violations = [e for e in new_entries if not is_allowlisted(e)]
        # Gate FAILS: violations must be non-empty
        assert len(violations) == 1, "Gate must detect the non-allowlisted WARNING"

    def test_gate_passes_when_no_new_warning(self):
        """
        When no non-allowlisted entries are present, the gate must PASS.

        This is the complement to the mutation check: removing the offending
        WARNING makes violations empty -> gate passes.
        """
        entries: list[dict] = []
        new_entries = filter_new_entries(entries, watermark_id=0)
        violations = [e for e in new_entries if not is_allowlisted(e)]
        assert len(violations) == 0, "Gate must pass with no non-allowlisted entries"

    def test_gate_passes_when_only_allowlisted_entries(self):
        """
        An entry that IS on the allowlist must NOT cause gate failure.
        """
        first_pattern = LOG_AUDIT_ALLOWLIST[0]
        entries = [
            _make_entry(
                level="WARNING",
                message=f"Allowlisted message: {first_pattern}",
                entry_id=5,
            )
        ]
        new_entries = filter_new_entries(entries, watermark_id=0)
        violations = [e for e in new_entries if not is_allowlisted(e)]
        assert len(violations) == 0, "Allowlisted entry must not trigger gate failure"


# ==========================================================================
# Pagination completeness: query_logs_via_mcp must walk ALL pages
# ==========================================================================
#
# These helpers and tests cover Story #1122 regression: a single-page fetch
# silently dropped entries beyond page 1.  The helpers build a real SQLite
# logs.db and a fake MCP client that returns the real MCP wire shape (Shape A),
# letting query_logs_via_mcp be exercised end-to-end without a live server.
# ==========================================================================

import builtins as _builtins_module  # noqa: E402  (module-level import at end is fine)

_json = _builtins_module.__import__("json")


def _create_logs_db(db_path: Path) -> None:
    """Create a minimal logs.db using the same DDL as SqliteLogHandler."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                correlation_id TEXT,
                user_id TEXT,
                request_path TEXT,
                extra_data TEXT,
                alias TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
        conn.commit()
    finally:
        conn.close()


def _seed_logs(db_path: Path, rows: list[dict[str, Any]]) -> None:
    """Insert rows directly into logs.db for testing."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            """
            INSERT INTO logs (timestamp, level, source, message)
            VALUES (:timestamp, :level, :source, :message)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class _FakeResponse:
    """Minimal response object matching the .status_code / .json() interface."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body

    @property
    def text(self) -> str:
        return str(_json.dumps(self._body))


class _FakeMcpClient:
    """
    Fake MCP client backed by a real LogAggregatorService + real SQLite logs.db.

    Implements the same .post() interface as FastAPI TestClient and httpx.Client,
    returning the MCP wire Shape A: {"content": [{"type": "text", "text": "<json>"}]}.
    This lets query_logs_via_mcp be exercised with real pagination logic without
    a live server.
    """

    def __init__(self, db_path: Path) -> None:
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )

        self._service = LogAggregatorService(db_path)

    def post(
        self,
        endpoint: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
    ) -> _FakeResponse:
        """Simulate an MCP POST by calling LogAggregatorService directly."""
        args = json.get("params", {}).get("arguments", {})
        level_str = args.get("level", "")
        levels = [lv.strip() for lv in level_str.split(",") if lv.strip()]
        page = args.get("page", 1)
        page_size = args.get("page_size", 50)
        sort_order = args.get("sort_order", "asc")

        result = self._service.query(
            page=page,
            page_size=page_size,
            sort_order=sort_order,
            levels=levels if levels else None,
        )

        data = {
            "success": True,
            "logs": result["logs"],
            "pagination": result["pagination"],
        }
        body = {"result": {"content": [{"type": "text", "text": _json.dumps(data)}]}}
        return _FakeResponse(status_code=200, body=body)


class TestQueryLogsViaMcpPagination:
    """Pagination completeness: query_logs_via_mcp must return ALL entries.

    Regression test for Story #1122 silent-failure defect: a single-page
    fetch silently drops entries beyond page 1.  The fix introduces a
    bounded page-walk (_AUDIT_MAX_PAGES = 1000) so every entry is returned
    regardless of how many pages the server needs.

    The two tests here cover:
      1. The fix: all 7 seeded rows are returned with page_size=3 (3 pages).
      2. The regression: page 1 alone with page_size=3 returns only 3 rows,
         documenting exactly why the page-walk is necessary.
    """

    @pytest.fixture
    def db_path(self, tmp_path: Path) -> Path:
        """Provide a fresh logs.db path for each test."""
        path = tmp_path / "logs.db"
        _create_logs_db(path)
        return path

    def test_returns_all_entries_across_multiple_pages(self, db_path: Path) -> None:
        """query_logs_via_mcp with page_size=3 returns all 7 seeded rows.

        Seeding 7 ERROR/WARNING rows and querying with page_size=3 forces
        3 pages (3+3+1).  The pre-fix single-page implementation would have
        returned only 3 rows; the bounded page-walk must return all 7.
        """
        total_rows = 7
        rows = [
            {
                "timestamp": f"2026-06-15T10:00:0{i}Z",
                "level": "ERROR" if i % 2 == 0 else "WARNING",
                "source": "test_pagination",
                "message": f"pagination-test-entry-{i}",
            }
            for i in range(total_rows)
        ]
        _seed_logs(db_path, rows)

        client = _FakeMcpClient(db_path)
        result = query_logs_via_mcp(client, token="fake-token", page_size=3)

        assert len(result) == total_rows, (
            f"Expected {total_rows} entries across all pages, "
            f"got {len(result)} — entries beyond page 1 were silently dropped"
        )
        messages = {e["message"] for e in result}
        for i in range(total_rows):
            assert f"pagination-test-entry-{i}" in messages, (
                f"Entry {i} missing from result — page-walk did not reach it"
            )

    def test_single_page_would_miss_entries(self, db_path: Path) -> None:
        """Documents the pre-fix regression: page 1 alone returns fewer rows.

        Asserts that querying only page 1 with page_size=3 yields 3 rows (not 7),
        proving that the pre-fix single-page implementation silently discarded
        entries and that the page-walk fix is necessary for completeness.
        """
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )

        total_rows = 7
        page_size = 3
        rows = [
            {
                "timestamp": f"2026-06-15T10:00:0{i}Z",
                "level": "ERROR",
                "source": "test_pagination_regression",
                "message": f"regression-entry-{i}",
            }
            for i in range(total_rows)
        ]
        _seed_logs(db_path, rows)

        service = LogAggregatorService(db_path)
        page1_result = service.query(
            page=1,
            page_size=page_size,
            levels=["ERROR", "WARNING"],
        )
        page1_count = len(page1_result["logs"])

        assert page1_count < total_rows, (
            f"Expected page 1 (size={page_size}) to return fewer than "
            f"{total_rows} rows (got {page1_count}), confirming the regression "
            f"that the page-walk fix addresses"
        )
