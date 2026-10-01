"""
Regression tests for Bug #1653: diagnostics_service.py aborts the
diagnostics-cache population loop on a PostgreSQL JSONB row.

Root cause: ``_load_results_from_db()`` and ``_read_category_from_db()``
called ``json.loads()`` unconditionally on the ``results_json`` column read
from the ``diagnostic_results`` table. That column is JSONB on PostgreSQL
(psycopg already deserializes it to a native Python ``list`` before the row
reaches application code) but TEXT on SQLite (a ``str`` needing
``json.loads()``). Calling ``json.loads()`` on an already-deserialized list
raises ``TypeError``. Because ``_load_results_from_db()``'s per-row loop had
no per-row exception boundary, that ``TypeError`` propagated straight out of
the entire ``for row in rows:`` loop into the method's outer
``except Exception`` handler -- aborting the WHOLE cache-population pass, so
every category iterated AFTER the first PostgreSQL-shaped row never made it
into the cache.

This is the same bug class as #1622/#1652/#1655, fixed here (as those were)
via the shared ``parse_json_column()`` helper
(``src/code_indexer/server/storage/json_column.py``).

Round 2 (code-review remediation): the ``diagnostic_results.run_at`` column
is ALSO PostgreSQL-native-typed (``TIMESTAMPTZ`` -- psycopg returns a real,
often tz-aware, ``datetime``) vs. SQLite's TEXT ``str``. Round 1 fixed only
``results_json`` and left ``datetime.fromisoformat(run_at)`` unguarded,
which raises on a real PostgreSQL row exactly like the original bug did --
just silently (caught by the per-row except and logged as a WARNING),
leaving ``_cache_timestamps`` empty and every category rendering as NOT_RUN
on a real cluster. Every "PG-shaped" row below now pairs a native ``list``
``results_json`` with a REAL tz-aware ``datetime`` ``run_at`` -- the true
psycopg row shape -- rather than the round-1 chimera (native list +
string timestamp) that does not occur in any real deployment.

Assertion note: the population-loop tests below assert directly against
``service._cache``/``service._cache_timestamps`` (populated by ``__init__``'s
call to ``_load_results_from_db()``), NOT via ``get_status()``. ``get_status()``
has its own independent per-category DB fallback (``_read_category_from_db``)
that would transparently reload a category missing from the cache -- which
would mask a broken population loop and make the test pass even against the
buggy code. Reading the cache dicts directly is the only way to prove
``_load_results_from_db()`` itself completed the whole pass and published a
consistent pair. The ``TestGetStatusFullyPgShapedContract1653`` class below is
the one exception: it deliberately DOES go through ``get_status()``, because
that public method's user-visible return value (real results vs. NOT_RUN
placeholders) is the actual contract Bug #1653 is about.

Test-double note: ``_FakeDiagnosticsRowsBackend`` implements only the two
``DiagnosticsBackend`` Protocol (``storage/protocols.py``) read methods
these tests actually exercise (``load_all_results``,
``load_category_results``) -- ``save_results``/``close`` are omitted
because ``DiagnosticsService`` never calls them on the code paths under
test here (``_load_results_from_db()``/``_read_category_from_db()``), and
the Protocol is structural (``@runtime_checkable`` with no ``isinstance``
enforcement in ``DiagnosticsService``), so a partial double is sufficient.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticResult,
    DiagnosticStatus,
    DiagnosticsService,
)

# A genuinely malformed results_json value: neither a list (the PostgreSQL
# JSONB shape) nor a str/bytes JSON payload (the SQLite TEXT shape) --
# parse_json_column() must fail soft on this rather than raise.
MALFORMED_RESULTS_JSON = 12345

# A genuinely malformed run_at value: neither a datetime (PostgreSQL
# TIMESTAMPTZ shape) nor a parseable ISO str (SQLite TEXT shape).
MALFORMED_RUN_AT = 12345

# How close a coerced run_at must land to "now" to count as "populated
# correctly" -- generous enough to absorb test execution time and the
# tz-aware -> local-naive conversion, tight enough to catch a wrong value.
_TIMESTAMP_TOLERANCE = timedelta(minutes=1)


class _FakeDiagnosticsRowsBackend:
    """Read-only DiagnosticsBackend test double: category -> (results_json, run_at).

    ``results_json``/``run_at`` are stored verbatim so a test can inject
    either the real PostgreSQL JSONB/TIMESTAMPTZ shapes (a native ``list``
    and a real ``datetime``, as psycopg returns them) or the real SQLite
    TEXT shapes (JSON ``str`` and ISO-format ``str``) per row, reproducing
    the exact mixed-shape scenario this bug depends on.
    """

    def __init__(self, rows: Dict[str, Tuple[object, object]]) -> None:
        self.rows = rows

    def load_all_results(self) -> List[Tuple[str, object, object]]:
        return [
            (category_str, results_json, run_at)
            for category_str, (results_json, run_at) in self.rows.items()
        ]

    def load_category_results(self, category: str) -> Optional[Tuple[object, object]]:
        return self.rows.get(category)


def _result_dict(name: str) -> Dict[str, Any]:
    result = DiagnosticResult(
        name=name,
        status=DiagnosticStatus.WORKING,
        message="ok",
        details={},
        timestamp=datetime.now(),
    )
    # mypy resolves DiagnosticResult.to_dict()'s return here as Any despite
    # its Dict[str, Any] annotation at definition -- same accepted
    # workaround already used for parse_json_column() call sites in
    # wiki_cache.py.
    return result.to_dict()  # type: ignore[no-any-return]


def _pg_run_at() -> datetime:
    """A real, tz-aware datetime -- exactly what psycopg returns for a
    TIMESTAMPTZ column. Never a string: that is the SQLite TEXT shape."""
    return datetime.now(timezone.utc)


def _service(
    tmp_path: Path, backend: _FakeDiagnosticsRowsBackend
) -> DiagnosticsService:
    return DiagnosticsService(
        db_path=str(tmp_path / "cidx_server.db"),
        storage_backend=backend,  # type: ignore[arg-type]
    )


class TestLoadResultsFromDbPgJsonb1653:
    """Bug #1653: _load_results_from_db() must not abort on a PG-shaped row."""

    def test_pg_shaped_row_before_sqlite_shaped_row_does_not_abort_the_loop(
        self, tmp_path: Path
    ) -> None:
        """RED: reproduces the exact reported bug.

        A fully PostgreSQL-shaped row (native list results_json + real
        tz-aware datetime run_at, first in iteration order) must not
        prevent a LATER, well-formed SQLite-shaped row (JSON str +
        ISO str) from being loaded by _load_results_from_db(), which
        __init__ calls directly.
        """
        pg_results = [_result_dict("PG Tool")]
        sqlite_results = [_result_dict("SQLite Tool")]

        rows: Dict[str, Tuple[object, object]] = {
            # PostgreSQL row: JSONB deserialized to a native list,
            # TIMESTAMPTZ deserialized to a real tz-aware datetime.
            DiagnosticCategory.CLI_TOOLS.value: (
                pg_results,
                _pg_run_at(),
            ),
            # SQLite row: TEXT columns, both genuine strings, iterated
            # SECOND (dict preserves insertion order in Python 3.7+).
            DiagnosticCategory.SDK_PREREQUISITES.value: (
                json.dumps(sqlite_results),
                datetime.now().isoformat(),
            ),
        }

        service = _service(tmp_path, _FakeDiagnosticsRowsBackend(rows))

        # Assert directly against the in-memory cache populated by
        # __init__'s call to _load_results_from_db() -- see module
        # docstring's "Assertion note" for why get_status() cannot be used
        # here (its per-category DB fallback would mask this exact bug).
        assert DiagnosticCategory.SDK_PREREQUISITES in service._cache, (
            "A category iterated AFTER a PostgreSQL-shaped row must still be "
            "populated into _cache by _load_results_from_db() itself -- the "
            "population loop must not abort on the first row's shape "
            "mismatch"
        )
        sdk_results = service._cache[DiagnosticCategory.SDK_PREREQUISITES]
        assert any(r.name == "SQLite Tool" for r in sdk_results)

    def test_pg_shaped_row_itself_parses_correctly(self, tmp_path: Path) -> None:
        """The fully PG-shaped row must ALSO parse correctly, not merely
        fail to crash the rest of the loop."""
        pg_results = [_result_dict("PG Tool")]
        rows: Dict[str, Tuple[object, object]] = {
            DiagnosticCategory.CLI_TOOLS.value: (
                pg_results,
                _pg_run_at(),
            ),
        }

        service = _service(tmp_path, _FakeDiagnosticsRowsBackend(rows))

        assert DiagnosticCategory.CLI_TOOLS in service._cache
        cli_results = service._cache[DiagnosticCategory.CLI_TOOLS]
        assert any(r.name == "PG Tool" for r in cli_results), (
            "A PostgreSQL-shaped row (native list results_json) should be "
            "accepted as-is by parse_json_column() and produce the correct "
            "cached results"
        )

    def test_pg_shaped_row_populates_cache_timestamps(self, tmp_path: Path) -> None:
        """RED (round 2): a fully PG-shaped row must populate
        _cache_timestamps, not just _cache.

        Finding 1/2 of the round-1 review: the round-1 fix left
        datetime.fromisoformat(run_at) unguarded, so a real (tz-aware)
        psycopg datetime raised TypeError there -- caught by the per-row
        except, leaving _cache_timestamps empty even though _cache had
        (before the round-2 atomic-publish fix) already been written.
        get_status() requires BOTH dicts to have the category before it
        will consider the cache fresh, so an empty _cache_timestamps means
        every category still renders as NOT_RUN on a real cluster.
        """
        pg_results = [_result_dict("PG Tool")]
        rows: Dict[str, Tuple[object, object]] = {
            DiagnosticCategory.CLI_TOOLS.value: (
                pg_results,
                _pg_run_at(),
            ),
        }

        service = _service(tmp_path, _FakeDiagnosticsRowsBackend(rows))

        assert DiagnosticCategory.CLI_TOOLS in service._cache_timestamps, (
            "A fully PG-shaped row (real tz-aware datetime run_at) must "
            "populate _cache_timestamps -- an unguarded "
            "datetime.fromisoformat(run_at) raises on a real psycopg "
            "datetime, silently leaving this dict empty"
        )
        cached_at = service._cache_timestamps[DiagnosticCategory.CLI_TOOLS]
        assert cached_at.tzinfo is None, (
            "get_status() compares cached timestamps against a naive "
            "datetime.now() -- a tz-aware value here would raise "
            "'can't subtract offset-naive and offset-aware datetimes' the "
            "first time this category's staleness is checked"
        )
        assert abs(cached_at - datetime.now()) < _TIMESTAMP_TOLERANCE

        # And the two dicts must have been published as a consistent pair
        # (round-1 review Finding 2): no _cache entry without a matching
        # _cache_timestamps entry, or vice versa.
        assert (DiagnosticCategory.CLI_TOOLS in service._cache) == (
            DiagnosticCategory.CLI_TOOLS in service._cache_timestamps
        )


class TestLoadResultsFromDbMalformedRows1653:
    """Bug #1653: a single malformed row (either column) must not abort
    the rest of _load_results_from_db()'s population loop."""

    def test_malformed_results_json_does_not_abort_rows_after_it(
        self, tmp_path: Path
    ) -> None:
        """A genuinely malformed results_json value must be skipped, not
        abort the loop."""
        sqlite_results = [_result_dict("SQLite Tool")]
        rows: Dict[str, Tuple[object, object]] = {
            DiagnosticCategory.CLI_TOOLS.value: (
                MALFORMED_RESULTS_JSON,
                datetime.now().isoformat(),
            ),
            DiagnosticCategory.SDK_PREREQUISITES.value: (
                json.dumps(sqlite_results),
                datetime.now().isoformat(),
            ),
        }

        service = _service(tmp_path, _FakeDiagnosticsRowsBackend(rows))

        assert DiagnosticCategory.SDK_PREREQUISITES in service._cache, (
            "A malformed row must be skipped without aborting the rest of "
            "the population loop"
        )
        sdk_results = service._cache[DiagnosticCategory.SDK_PREREQUISITES]
        assert any(r.name == "SQLite Tool" for r in sdk_results)
        # The malformed row itself must never have been populated.
        assert DiagnosticCategory.CLI_TOOLS not in service._cache

    def test_malformed_run_at_does_not_abort_rows_after_it(
        self, tmp_path: Path
    ) -> None:
        """A genuinely malformed run_at value (valid results_json, but a
        run_at that is neither datetime nor a parseable str) must be
        skipped -- and skipped ATOMICALLY (no orphan _cache entry) --
        without aborting the rest of the population loop."""
        cli_results_data = [_result_dict("CLI Tool")]
        sqlite_results = [_result_dict("SQLite Tool")]
        rows: Dict[str, Tuple[object, object]] = {
            DiagnosticCategory.CLI_TOOLS.value: (
                cli_results_data,
                MALFORMED_RUN_AT,
            ),
            DiagnosticCategory.SDK_PREREQUISITES.value: (
                json.dumps(sqlite_results),
                datetime.now().isoformat(),
            ),
        }

        service = _service(tmp_path, _FakeDiagnosticsRowsBackend(rows))

        assert DiagnosticCategory.SDK_PREREQUISITES in service._cache, (
            "A row with a malformed run_at must be skipped without "
            "aborting the rest of the population loop"
        )
        sdk_results = service._cache[DiagnosticCategory.SDK_PREREQUISITES]
        assert any(r.name == "SQLite Tool" for r in sdk_results)

        # The malformed-run_at row must never have landed a partial/torn
        # entry in EITHER dict (atomic publish -- round-1 review Finding 2).
        assert DiagnosticCategory.CLI_TOOLS not in service._cache
        assert DiagnosticCategory.CLI_TOOLS not in service._cache_timestamps


class TestReadCategoryFromDbPgJsonb1653:
    """Bug #1653: _read_category_from_db() must handle the same PG shapes."""

    def test_pg_shaped_single_category_row_parses_without_raising(
        self, tmp_path: Path
    ) -> None:
        pg_results = [_result_dict("PG Tool")]
        backend = _FakeDiagnosticsRowsBackend({})
        # Bypass _load_results_from_db's initial pass entirely -- store the
        # row directly on the backend AFTER construction so this test
        # exercises _read_category_from_db() specifically (called from
        # get_status() when the in-memory cache is empty for a category).
        backend.rows[DiagnosticCategory.CLI_TOOLS.value] = (
            pg_results,
            _pg_run_at(),
        )

        service = _service(tmp_path, backend)

        loaded = service._read_category_from_db(DiagnosticCategory.CLI_TOOLS)

        assert loaded is not None, (
            "_read_category_from_db must successfully parse a fully "
            "PostgreSQL-shaped row (native list + real tz-aware datetime), "
            "not raise or return None"
        )
        results, run_at = loaded
        assert any(r.name == "PG Tool" for r in results)
        assert run_at.tzinfo is None, (
            "the returned run_at must be naive -- callers (e.g. "
            "get_status()) compare it against a naive datetime.now()"
        )
        assert abs(run_at - datetime.now()) < _TIMESTAMP_TOLERANCE

    def test_malformed_results_json_returns_none_gracefully(
        self, tmp_path: Path
    ) -> None:
        backend = _FakeDiagnosticsRowsBackend({})
        backend.rows[DiagnosticCategory.CLI_TOOLS.value] = (
            MALFORMED_RESULTS_JSON,
            datetime.now().isoformat(),
        )

        service = _service(tmp_path, backend)

        loaded = service._read_category_from_db(DiagnosticCategory.CLI_TOOLS)

        assert loaded is None, (
            "A malformed results_json value must fail soft (return None), never raise"
        )

    def test_malformed_run_at_returns_none_gracefully(self, tmp_path: Path) -> None:
        """A malformed run_at (valid results_json) must also fail soft."""
        backend = _FakeDiagnosticsRowsBackend({})
        backend.rows[DiagnosticCategory.CLI_TOOLS.value] = (
            [_result_dict("PG Tool")],
            MALFORMED_RUN_AT,
        )

        service = _service(tmp_path, backend)

        loaded = service._read_category_from_db(DiagnosticCategory.CLI_TOOLS)

        assert loaded is None, (
            "A malformed run_at value must fail soft (return None), never raise"
        )


class TestGetStatusFullyPgShapedContract1653:
    """The actual user-visible contract of Bug #1653.

    get_status() must return REAL cached results for a category backed by
    a fully PostgreSQL-shaped row (native list results_json + real
    tz-aware datetime run_at) -- not fall back to NOT_RUN placeholders.
    Round 1's fix stopped the loud crash but left this exact scenario
    silently broken (empty _cache_timestamps -> every category treated as
    stale/uncached -> get_status() renders NOT_RUN). This test intentionally
    goes through the public get_status() API (unlike the population-loop
    tests above) because that is the actual behavior users observe.
    """

    def test_get_status_returns_cached_results_not_not_run(
        self, tmp_path: Path
    ) -> None:
        pg_results = [_result_dict("PG Tool")]
        rows: Dict[str, Tuple[object, object]] = {
            DiagnosticCategory.CLI_TOOLS.value: (
                pg_results,
                _pg_run_at(),
            ),
        }

        service = _service(tmp_path, _FakeDiagnosticsRowsBackend(rows))

        status = service.get_status()

        cli_results = status[DiagnosticCategory.CLI_TOOLS]
        assert not any(r.status == DiagnosticStatus.NOT_RUN for r in cli_results), (
            "get_status() must return the REAL cached results from a fully "
            "PostgreSQL-shaped row, not fall back to NOT_RUN placeholders "
            "-- this is the actual user-visible contract Bug #1653 is about"
        )
        assert any(r.name == "PG Tool" for r in cli_results)
