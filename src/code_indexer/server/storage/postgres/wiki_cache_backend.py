"""
PostgreSQL backend for wiki cache storage (Story #523).

Drop-in replacement for WikiCacheSqliteBackend using psycopg v3 sync
connections via ConnectionPool.  Satisfies the WikiCacheBackend Protocol
(protocols.py).

Tables created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class WikiCachePostgresBackend:
    """
    PostgreSQL backend for wiki cache storage.

    Satisfies the WikiCacheBackend Protocol (protocols.py).
    All mutations commit immediately after DML execution.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool and ensure tables exist.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create wiki tables and indexes if they do not already exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS wiki_cache (
                        repo_alias TEXT NOT NULL,
                        article_path TEXT NOT NULL,
                        rendered_html TEXT NOT NULL,
                        title TEXT NOT NULL,
                        file_mtime DOUBLE PRECISION NOT NULL,
                        file_size INTEGER NOT NULL,
                        rendered_at TEXT NOT NULL,
                        metadata_json TEXT,
                        PRIMARY KEY (repo_alias, article_path)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS wiki_sidebar_cache (
                        repo_alias TEXT PRIMARY KEY,
                        sidebar_json TEXT NOT NULL,
                        max_mtime DOUBLE PRECISION NOT NULL,
                        built_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS wiki_article_views (
                        repo_alias TEXT NOT NULL,
                        article_path TEXT NOT NULL,
                        real_views INTEGER DEFAULT 0,
                        first_viewed_at TEXT,
                        last_viewed_at TEXT,
                        PRIMARY KEY (repo_alias, article_path)
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.error("WikiCachePostgresBackend: schema setup failed: %s", exc)
            raise

    def get_article(
        self, repo_alias: str, article_path: str
    ) -> Optional[Dict[str, Any]]:
        """Return dict with rendered_html, title, file_mtime, file_size, metadata_json or None."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT rendered_html, title, file_mtime, file_size, metadata_json "
                "FROM wiki_cache WHERE repo_alias = %s AND article_path = %s",
                (repo_alias, article_path),
            ).fetchone()
        if row is None:
            return None
        return {
            "rendered_html": row[0],
            "title": row[1],
            "file_mtime": row[2],
            "file_size": row[3],
            "metadata_json": row[4],
        }

    def put_article(
        self,
        repo_alias: str,
        article_path: str,
        html: str,
        title: str,
        file_mtime: float,
        file_size: int,
        rendered_at: str,
        metadata_json: Optional[str],
    ) -> None:
        """Store (upsert) rendered article row."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO wiki_cache
                    (repo_alias, article_path, rendered_html, title, file_mtime, file_size, rendered_at, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (repo_alias, article_path) DO UPDATE SET
                    rendered_html = EXCLUDED.rendered_html,
                    title = EXCLUDED.title,
                    file_mtime = EXCLUDED.file_mtime,
                    file_size = EXCLUDED.file_size,
                    rendered_at = EXCLUDED.rendered_at,
                    metadata_json = EXCLUDED.metadata_json
                """,
                (
                    repo_alias,
                    article_path,
                    html,
                    title,
                    file_mtime,
                    file_size,
                    rendered_at,
                    metadata_json,
                ),
            )
            conn.commit()

    def get_sidebar(self, repo_alias: str) -> Optional[str]:
        """Return sidebar_json string for repo_alias, or None."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT sidebar_json FROM wiki_sidebar_cache WHERE repo_alias = %s",
                (repo_alias,),
            ).fetchone()
        return row[0] if row else None  # type: ignore[no-any-return]

    def put_sidebar(
        self,
        repo_alias: str,
        sidebar_json: str,
        max_mtime: float,
        built_at: str,
    ) -> None:
        """Store (upsert) sidebar row."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO wiki_sidebar_cache (repo_alias, sidebar_json, max_mtime, built_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (repo_alias) DO UPDATE SET
                    sidebar_json = EXCLUDED.sidebar_json,
                    max_mtime = EXCLUDED.max_mtime,
                    built_at = EXCLUDED.built_at
                """,
                (repo_alias, sidebar_json, max_mtime, built_at),
            )
            conn.commit()

    def invalidate_repo(self, repo_alias: str) -> None:
        """Delete all wiki_cache and wiki_sidebar_cache rows for repo_alias."""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM wiki_cache WHERE repo_alias = %s", (repo_alias,))
            conn.execute(
                "DELETE FROM wiki_sidebar_cache WHERE repo_alias = %s", (repo_alias,)
            )
            conn.commit()

    def increment_view(self, repo_alias: str, article_path: str, now: str) -> None:
        """Upsert wiki_article_views, incrementing real_views."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO wiki_article_views
                    (repo_alias, article_path, real_views, first_viewed_at, last_viewed_at)
                VALUES (%s, %s, 1, %s, %s)
                ON CONFLICT (repo_alias, article_path) DO UPDATE SET
                    real_views = wiki_article_views.real_views + 1,
                    last_viewed_at = EXCLUDED.last_viewed_at
                """,
                (repo_alias, article_path, now, now),
            )
            conn.commit()

    def get_view_count(self, repo_alias: str, article_path: str) -> int:
        """Return real_views count for article, or 0."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT real_views FROM wiki_article_views WHERE repo_alias = %s AND article_path = %s",
                (repo_alias, article_path),
            ).fetchone()
        return int(row[0]) if row else 0

    def get_all_view_counts(self, repo_alias: str) -> List[Dict[str, Any]]:
        """Return all view records for repo as list of dicts."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT article_path, real_views, first_viewed_at, last_viewed_at "
                "FROM wiki_article_views WHERE repo_alias = %s ORDER BY real_views DESC",
                (repo_alias,),
            ).fetchall()
        return [
            {
                "article_path": row[0],
                "real_views": row[1],
                "first_viewed_at": row[2],
                "last_viewed_at": row[3],
            }
            for row in rows
        ]

    def delete_views_for_repo(self, repo_alias: str) -> None:
        """Delete all wiki_article_views rows for repo_alias."""
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM wiki_article_views WHERE repo_alias = %s", (repo_alias,)
            )
            conn.commit()

    def insert_initial_views(
        self, repo_alias: str, article_path: str, views: int, now: str
    ) -> None:
        """Insert initial view count (INSERT ... ON CONFLICT DO NOTHING)."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO wiki_article_views
                    (repo_alias, article_path, real_views, first_viewed_at, last_viewed_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (repo_alias, article_path) DO NOTHING
                """,
                (repo_alias, article_path, views, now, now),
            )
            conn.commit()

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
