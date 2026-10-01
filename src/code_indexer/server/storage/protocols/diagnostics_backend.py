"""DiagnosticsBackend Protocol (Story #525).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Optional, Protocol, Sequence, Tuple, runtime_checkable


@runtime_checkable
class DiagnosticsBackend(Protocol):
    """Protocol for diagnostics results storage (Story #525).

    Provides data-level access to the diagnostic_results table.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a DiagnosticsBackend without inheritance.
    """

    def save_results(self, category: str, results_json: str, run_at: str) -> None:
        """Persist (upsert) diagnostic results for a category."""
        ...

    def load_all_results(self) -> "Sequence[Tuple[str, object, object]]":
        """Return all rows as list of (category, results_json, run_at) tuples.

        Bug #1662: `results_json`/`run_at` are honestly typed `object`, not
        `str`. The real PostgreSQL schema (`001_initial_schema.sql`)
        declares `results_json JSONB` and `run_at TIMESTAMPTZ` -- psycopg
        deserializes both to native Python objects (dict/list, and a
        real, often tz-aware, datetime) before the row reaches
        application code. SQLite's TEXT columns return genuine `str`
        values instead. A caller MUST normalize either shape (e.g. via
        `parse_json_column()` for the JSON field and a datetime-coercion
        helper for the timestamp field) rather than assuming `str` --
        that false assumption is exactly the bug class that slipped
        through review for Bug #1653. The return type is `Sequence`
        rather than `List` so a backend may return any list-like
        container without narrowing.
        """
        ...

    def load_category_results(self, category: str) -> "Optional[Tuple[object, object]]":
        """Return (results_json, run_at) for a category, or None if absent.

        Same dual-shape (JSONB dict/list, TIMESTAMPTZ datetime vs. SQLite
        TEXT str) contract as `load_all_results()` -- see its docstring.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
