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

Assertion note: the population-loop tests below assert directly against
``service._cache`` (populated by ``__init__``'s call to
``_load_results_from_db()``), NOT via ``get_status()``. ``get_status()`` has
its own independent per-category DB fallback (``_read_category_from_db``)
that would transparently reload a category missing from the cache -- which
would mask a broken population loop and make the test pass even against the
buggy code. Reading ``_cache`` directly is the only way to prove
``_load_results_from_db()`` itself completed the whole pass.

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
from datetime import datetime
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


class _FakeDiagnosticsRowsBackend:
    """Read-only DiagnosticsBackend test double: category -> (results_json, run_at).

    ``results_json`` is stored verbatim so a test can inject either a native
    ``list`` (PostgreSQL JSONB, as psycopg returns it) or a JSON ``str``
    (SQLite TEXT) per row, reproducing the exact mixed-shape scenario this
    bug depends on.
    """

    def __init__(self, rows: Dict[str, Tuple[object, str]]) -> None:
        self.rows = rows

    def load_all_results(self) -> List[Tuple[str, object, str]]:
        return [
            (category_str, results_json, run_at)
            for category_str, (results_json, run_at) in self.rows.items()
        ]

    def load_category_results(self, category: str) -> Optional[Tuple[object, str]]:
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


def _service(
    tmp_path: Path, backend: _FakeDiagnosticsRowsBackend
) -> DiagnosticsService:
    return DiagnosticsService(
        db_path=str(tmp_path / "cidx_server.db"),
        storage_backend=backend,  # type: ignore[arg-type]
    )


class TestLoadResultsFromDbPgJsonb1653:
    """Bug #1653: _load_results_from_db() must not abort on a PG JSONB row."""

    def test_pg_shaped_row_before_sqlite_shaped_row_does_not_abort_the_loop(
        self, tmp_path: Path
    ) -> None:
        """RED: reproduces the exact reported bug.

        A PostgreSQL-shaped row (native list value, first in iteration
        order) must not prevent a LATER, well-formed SQLite-shaped row (a
        JSON str value) from being loaded by _load_results_from_db(),
        which __init__ calls directly. Before the fix, the TypeError from
        json.loads() on the native list propagates out of the entire
        population loop and the second category is never cached.
        """
        pg_results = [_result_dict("PG Tool")]
        sqlite_results = [_result_dict("SQLite Tool")]

        rows: Dict[str, Tuple[object, str]] = {
            # PostgreSQL JSONB column: psycopg already deserialized this to
            # a native list, NOT a JSON string.
            DiagnosticCategory.CLI_TOOLS.value: (
                pg_results,
                datetime.now().isoformat(),
            ),
            # SQLite TEXT column: a genuine JSON string, iterated SECOND
            # (dict preserves insertion order in Python 3.7+).
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
            "A category iterated AFTER a PostgreSQL JSONB row must still be "
            "populated into _cache by _load_results_from_db() itself -- the "
            "population loop must not abort on the first row's shape "
            "mismatch"
        )
        sdk_results = service._cache[DiagnosticCategory.SDK_PREREQUISITES]
        assert any(r.name == "SQLite Tool" for r in sdk_results)

    def test_pg_shaped_row_itself_parses_correctly(self, tmp_path: Path) -> None:
        """The PG-shaped (native list) row must ALSO parse correctly, not
        merely fail to crash the rest of the loop."""
        pg_results = [_result_dict("PG Tool")]
        rows: Dict[str, Tuple[object, str]] = {
            DiagnosticCategory.CLI_TOOLS.value: (
                pg_results,
                datetime.now().isoformat(),
            ),
        }

        service = _service(tmp_path, _FakeDiagnosticsRowsBackend(rows))

        assert DiagnosticCategory.CLI_TOOLS in service._cache
        cli_results = service._cache[DiagnosticCategory.CLI_TOOLS]
        assert any(r.name == "PG Tool" for r in cli_results), (
            "PostgreSQL JSONB row (native list) should be accepted as-is by "
            "parse_json_column() and produce the correct cached results"
        )

    def test_malformed_row_does_not_abort_rows_after_it(self, tmp_path: Path) -> None:
        """A genuinely malformed results_json value must be skipped, not
        abort the loop."""
        sqlite_results = [_result_dict("SQLite Tool")]
        rows: Dict[str, Tuple[object, str]] = {
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


class TestReadCategoryFromDbPgJsonb1653:
    """Bug #1653: _read_category_from_db() must handle the same JSONB shape."""

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
            datetime.now().isoformat(),
        )

        service = _service(tmp_path, backend)

        loaded = service._read_category_from_db(DiagnosticCategory.CLI_TOOLS)

        assert loaded is not None, (
            "_read_category_from_db must successfully parse a native-list "
            "(PostgreSQL JSONB) results_json value, not raise or return None"
        )
        results, _run_at = loaded
        assert any(r.name == "PG Tool" for r in results)

    def test_malformed_single_category_row_returns_none_gracefully(
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
