"""
Wiki Cache PostgreSQL Service.

Story #429: Moves rendered wiki page cache from local filesystem to PostgreSQL
so that all cluster nodes share the same rendered HTML.

Provides render-once / read-always semantics with source_hash-based staleness
detection so a page is only re-rendered when the underlying source changes.

Uses psycopg v3 synchronous connection pool (same pattern as all other PG
backends in this project).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class WikiCachePGService:
    """PostgreSQL-backed wiki page cache (render once, read always)."""

    def __init__(self, pool: Any) -> None:
        """
        Initialize the wiki cache service.

        Args:
            pool: A psycopg v3 ConnectionPool instance (or compatible mock).
                  Must support the context-manager protocol via pool.connection().
        """
        self._pool = pool

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_cached_page(self, repo_alias: str, page_path: str) -> Optional[dict]:
        """
        Get a cached rendered page.

        Args:
            repo_alias: Repository alias (e.g. "my-repo-global").
            page_path: Path of the wiki page within the repo.

        Returns:
            dict with keys rendered_html, source_hash, cached_at (ISO timestamp),
            or None if not cached.
        """
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT rendered_html, source_hash, cached_at
                   FROM wiki_cache
                   WHERE repo_alias = %s AND page_path = %s""",
                (repo_alias, page_path),
            )
            row = cursor.fetchone()

        if row is None:
            return None

        return {
            "rendered_html": row[0],
            "source_hash": row[1],
            "cached_at": row[2],
        }

    def cache_page(
        self,
        repo_alias: str,
        page_path: str,
        rendered_html: str,
        source_hash: str,
    ) -> None:
        """
        Store a rendered page in the cache (UPSERT).

        If a row for (repo_alias, page_path) already exists it is replaced
        with the new rendered_html, source_hash, and cached_at timestamp.

        Args:
            repo_alias: Repository alias.
            page_path: Path of the wiki page.
            rendered_html: Fully rendered HTML string.
            source_hash: Hash of the source markdown (used for staleness checks).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO wiki_cache
                       (repo_alias, page_path, rendered_html, source_hash, cached_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (repo_alias, page_path)
                   DO UPDATE SET
                       rendered_html = EXCLUDED.rendered_html,
                       source_hash   = EXCLUDED.source_hash,
                       cached_at     = EXCLUDED.cached_at""",
                (repo_alias, page_path, rendered_html, source_hash, now),
            )
        logger.debug(f"Wiki page cached: {repo_alias}/{page_path} hash={source_hash}")

    def invalidate_repo(self, repo_alias: str) -> int:
        """
        Invalidate (delete) all cached pages for a repository.

        Args:
            repo_alias: Repository alias whose cached pages should be removed.

        Returns:
            Number of cache entries removed.
        """
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM wiki_cache WHERE repo_alias = %s",
                (repo_alias,),
            )
            count: int = cursor.rowcount
        logger.info(f"Invalidated {count} wiki cache entries for repo: {repo_alias}")
        return count

    def is_stale(
        self, repo_alias: str, page_path: str, current_source_hash: str
    ) -> bool:
        """
        Check whether the cached version of a page is stale.

        A page is stale when:
          - It is not cached at all (no row found).
          - The stored source_hash differs from current_source_hash.

        Args:
            repo_alias: Repository alias.
            page_path: Path of the wiki page.
            current_source_hash: Hash of the current source markdown.

        Returns:
            True if the cache should be refreshed, False if it is still fresh.
        """
        cached = self.get_cached_page(repo_alias, page_path)
        if cached is None:
            return True
        return bool(cached["source_hash"] != current_source_hash)
