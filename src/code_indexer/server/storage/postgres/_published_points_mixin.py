"""Published-points reference size for GoldenRepoMetadataPostgresBackend
(Story #2118 slice 1b) -- the cluster-mode mirror of
``sqlite_backends/_published_points_mixin.py``. Column added by migration
068 on ``golden_repos_metadata``; NULL means "never published": the refresh
volume alarm's floor applies.

Split into its own module because golden_repo_metadata_backend.py is already
over the project's 1,000-line-per-file limit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Iterable, Optional

if TYPE_CHECKING:
    from .connection_pool import ConnectionPool


class _PublishedPointsPostgresMixin:
    """Published-points methods (see module docstring)."""

    # Supplied at runtime by GoldenRepoMetadataPostgresBackend.__init__.
    _pool: "ConnectionPool"

    def set_published_points(self, alias: str, points: int) -> bool:
        """Record *points* for the golden repo *alias* (bare alias). False
        when no such repo is registered (nothing recorded)."""
        if not alias:
            raise ValueError("alias must be a non-empty string")
        if not isinstance(points, int) or isinstance(points, bool) or points < 0:
            raise ValueError(f"points must be a non-negative int, got {points!r}")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE golden_repos_metadata SET published_points = %s "
                    "WHERE alias = %s",
                    (points, alias),
                )
                updated: bool = cur.rowcount == 1
            conn.commit()
        return updated

    def get_published_points(self, alias: str) -> Optional[int]:
        """Points of *alias*'s last verified publish, or None when it was
        never recorded (or the repo is not registered). A store error
        propagates."""
        if not alias:
            raise ValueError("alias must be a non-empty string")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT published_points FROM golden_repos_metadata "
                    "WHERE alias = %s",
                    (alias,),
                )
                row = cur.fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def get_published_points_many(
        self, aliases: Iterable[str]
    ) -> Dict[str, Optional[int]]:
        """``{alias: points or None}`` for every requested bare alias, in one
        query. A store error propagates."""
        wanted = sorted(set(aliases))
        found: Dict[str, Optional[int]] = {alias: None for alias in wanted}
        if not wanted:
            return found
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT alias, published_points FROM golden_repos_metadata "
                    "WHERE alias = ANY(%s)",
                    (wanted,),
                )
                rows = cur.fetchall()
        for alias, points in rows:
            found[alias] = None if points is None else int(points)
        return found
