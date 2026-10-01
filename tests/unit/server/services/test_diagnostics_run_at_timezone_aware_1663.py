"""
Regression tests for Bug #1663: diagnostics_service.py wrote a NAIVE,
LOCAL-timezone ``run_at`` value into ``diagnostic_results.run_at`` -- a
``TIMESTAMPTZ`` column on PostgreSQL. If the PostgreSQL session's configured
timezone ever differs from the app process's local timezone, this skews
``get_status()``'s freshness/TTL comparison (``datetime.now() -
cached_timestamp``), causing a permanent cache miss or an over-long
freshness window.

Fix: write ``run_at`` as a timezone-AWARE UTC value
(``datetime.now(timezone.utc)``) so the value round-trips correctly
regardless of the PostgreSQL session's timezone setting.

Read-side check (this bug's own "verify, don't assume" instruction):
switching the write shape to an aware-UTC ISO string changes what
``_coerce_run_at`` sees on the SQLite (str) path too -- previously the
str branch never had to handle an offset-bearing string because
``datetime.now().isoformat()`` never produced one. ``datetime.fromisoformat()``
happily parses an offset-bearing string into a tz-AWARE datetime, and the
str branch returned it unchanged, unlike the ``isinstance(raw, datetime)``
branch which explicitly strips tzinfo. That aware value flows straight into
``_cache_timestamps``, and the very next ``get_status()`` call's
``now - cached_timestamps[category] < cache_ttl`` comparison (naive vs
aware) raises ``TypeError``. This file's
``TestCoerceRunAtAwareIsoString1663`` and
``TestFullRoundTripSecondGetStatusCall1663`` classes are the discriminating
RED tests for that specific hazard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticResult,
    DiagnosticStatus,
    DiagnosticsService,
)
from code_indexer.server.storage.database_manager import DatabaseSchema

_TIMESTAMP_TOLERANCE = timedelta(minutes=1)


def _sample_results() -> list:
    return [
        DiagnosticResult(
            name="Sample Tool",
            status=DiagnosticStatus.WORKING,
            message="ok",
            details={},
        )
    ]


def _service(tmp_path: Path) -> DiagnosticsService:
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path=db_path).initialize_database()
    return DiagnosticsService(db_path=db_path)


class TestSaveResultsToDbWritesAwareUtc1663:
    """The write side must persist a timezone-AWARE UTC value, not naive-local."""

    def test_raw_persisted_run_at_string_carries_utc_offset(
        self, tmp_path: Path
    ) -> None:
        """The raw TEXT value written to SQLite must itself carry a UTC
        offset marker (e.g. '+00:00') -- proof it came from an aware
        datetime, not ``datetime.now().isoformat()`` (which never emits an
        offset)."""
        service = _service(tmp_path)
        service._save_results_to_db(DiagnosticCategory.CLI_TOOLS, _sample_results())

        with service._conn_manager.guarded_connection() as conn:
            cursor = conn.execute(
                "SELECT run_at FROM diagnostic_results WHERE category = ?",
                (DiagnosticCategory.CLI_TOOLS.value,),
            )
            raw_run_at = cursor.fetchone()[0]

        assert isinstance(raw_run_at, str)
        parsed = datetime.fromisoformat(raw_run_at)
        assert parsed.tzinfo is not None, (
            f"persisted run_at {raw_run_at!r} must be timezone-aware -- "
            "naive-local values are the exact bug #1663 reports"
        )
        assert parsed.utcoffset() == timedelta(0), (
            "persisted run_at must be UTC specifically, not just any offset"
        )


class TestCoerceRunAtAwareIsoString1663:
    """RED: _coerce_run_at's str branch must strip tzinfo from an
    offset-bearing ISO string, mirroring what it already does for a real
    tz-aware datetime object (the PostgreSQL/psycopg shape)."""

    def test_aware_utc_iso_string_is_coerced_to_naive(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        aware_iso = datetime.now(timezone.utc).isoformat()

        coerced = service._coerce_run_at(aware_iso)

        assert coerced is not None
        assert coerced.tzinfo is None, (
            "an offset-bearing ISO string (the new SQLite write shape after "
            "the #1663 fix) must be normalized to a naive local datetime, "
            "exactly like the tz-aware-datetime branch already does -- "
            "otherwise get_status()'s naive `datetime.now()` comparison "
            "raises TypeError on the very next call"
        )
        assert abs(coerced - datetime.now()) < _TIMESTAMP_TOLERANCE

    def test_aware_non_utc_iso_string_is_coerced_to_naive_local(
        self, tmp_path: Path
    ) -> None:
        """A non-UTC offset must also be normalized (converted to local
        time), not merely have its tzinfo blindly discarded."""
        service = _service(tmp_path)
        # A fixed, known offset far from UTC so a wrong "just strip tzinfo"
        # implementation would produce a value far from "now".
        fixed_offset = timezone(timedelta(hours=5))
        aware_iso = datetime.now(fixed_offset).isoformat()

        coerced = service._coerce_run_at(aware_iso)

        assert coerced is not None
        assert coerced.tzinfo is None
        assert abs(coerced - datetime.now()) < _TIMESTAMP_TOLERANCE, (
            "a non-UTC offset string must be converted to local time before "
            "tzinfo is stripped, not merely truncated in place"
        )


class TestFullRoundTripSecondGetStatusCall1663:
    """The actual user-visible contract: writing via the fixed
    _save_results_to_db and reading back via the public get_status() API
    must never raise, across repeated calls (this is precisely where the
    TTL/freshness comparison lives)."""

    def test_second_get_status_call_does_not_raise_after_db_round_trip(
        self, tmp_path: Path
    ) -> None:
        service = _service(tmp_path)
        service._save_results_to_db(DiagnosticCategory.CLI_TOOLS, _sample_results())
        service.clear_cache(DiagnosticCategory.CLI_TOOLS)

        # First call loads from DB and publishes the coerced run_at into
        # _cache_timestamps.
        status_first = service.get_status()
        assert any(
            r.name == "Sample Tool" for r in status_first[DiagnosticCategory.CLI_TOOLS]
        )

        # Second call exercises the `now - cached_timestamps[category] <
        # cache_ttl` freshness comparison against the just-published
        # timestamp. This raises TypeError if that timestamp is tz-aware.
        status_second = service.get_status()
        assert any(
            r.name == "Sample Tool" for r in status_second[DiagnosticCategory.CLI_TOOLS]
        )

    def test_cache_timestamp_after_round_trip_is_naive(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service._save_results_to_db(DiagnosticCategory.CLI_TOOLS, _sample_results())
        service.clear_cache(DiagnosticCategory.CLI_TOOLS)

        service.get_status()

        cached_at = service._cache_timestamps[DiagnosticCategory.CLI_TOOLS]
        assert cached_at.tzinfo is None
        assert abs(cached_at - datetime.now()) < _TIMESTAMP_TOLERANCE
