"""Published-points reference size for GoldenRepoMetadataSqliteBackend
(Story #2118 slice 1b).

``golden_repos_metadata.published_points`` is the number of points in a
golden repo's last verified published snapshot. The refresh volume alarm
compares a repo's 24 h refresh embedding volume against a fraction of it.
NULL means "never published (since this column exists)": the alarm's floor
applies. The column is added in place by the backend's
``ensure_table_exists`` (see ``PUBLISHED_POINTS_COLUMN``); PostgreSQL adds
it in migration 068. It lives on the repo's row, so removing the repo
removes it.

Split into its own module because the backend's main module and its other
mixin are already near the project's 1,000-line-per-file limit.
"""

import sqlite3
from typing import TYPE_CHECKING, Dict, Iterable, Optional, Tuple

if TYPE_CHECKING:
    from ..database_manager import DatabaseConnectionManager

#: (name, definition) for the in-place column migration.
PUBLISHED_POINTS_COLUMN: Tuple[str, str] = ("published_points", "INTEGER")

#: Aliases per IN-list: below the 999 host-parameter limit of older SQLite
#: builds, far above any realistic set of alarming repos.
_IN_LIST_CHUNK = 500


def _validate(alias: str, points: int) -> None:
    if not alias:
        raise ValueError("alias must be a non-empty string")
    if not isinstance(points, int) or isinstance(points, bool) or points < 0:
        raise ValueError(f"points must be a non-negative int, got {points!r}")


class _PublishedPointsSqliteMixin:
    """Published-points methods (see module docstring)."""

    # Supplied at runtime by GoldenRepoMetadataSqliteBackend.__init__.
    _conn_manager: "DatabaseConnectionManager"

    def set_published_points(self, alias: str, points: int) -> bool:
        """Record *points* for the golden repo *alias* (bare alias). False
        when no such repo is registered (nothing recorded)."""
        _validate(alias, points)

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE golden_repos_metadata SET published_points = ? WHERE alias = ?",
                (points, alias),
            )
            return cursor.rowcount == 1

        return bool(self._conn_manager.execute_atomic(operation))

    def get_published_points(self, alias: str) -> Optional[int]:
        """Points of *alias*'s last verified publish, or None when it was
        never recorded (or the repo is not registered). A store error
        propagates."""
        if not alias:
            raise ValueError("alias must be a non-empty string")
        row = (
            self._conn_manager.get_connection()
            .execute(
                "SELECT published_points FROM golden_repos_metadata WHERE alias = ?",
                (alias,),
            )
            .fetchone()
        )
        return None if row is None or row[0] is None else int(row[0])

    def get_published_points_many(
        self, aliases: Iterable[str]
    ) -> Dict[str, Optional[int]]:
        """``{alias: points or None}`` for every requested bare alias in one
        query (chunked only beyond _IN_LIST_CHUNK aliases, so older SQLite
        builds' host-parameter limit is never hit). A store error
        propagates."""
        wanted = sorted(set(aliases))
        found: Dict[str, Optional[int]] = {alias: None for alias in wanted}
        conn = self._conn_manager.get_connection()
        for start in range(0, len(wanted), _IN_LIST_CHUNK):
            chunk = wanted[start : start + _IN_LIST_CHUNK]
            marks = ", ".join("?" * len(chunk))
            for alias, points in conn.execute(
                "SELECT alias, published_points FROM golden_repos_metadata "
                f"WHERE alias IN ({marks})",
                chunk,
            ).fetchall():
                found[alias] = None if points is None else int(points)
        return found
