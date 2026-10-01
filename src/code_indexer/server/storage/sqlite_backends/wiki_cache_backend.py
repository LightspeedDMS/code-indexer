"""
SQLite backend for wiki cache storage (Story #523).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import sqlite3
from typing import Any, Dict, List, Optional

from ..database_manager import DatabaseConnectionManager


class WikiCacheSqliteBackend:
    """
    SQLite backend for wiki cache storage (Story #523).

    Satisfies the WikiCacheBackend Protocol.
    Uses the main cidx_server.db (wiki_cache, wiki_sidebar_cache,
    wiki_article_views tables).
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (cidx_server.db).
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create wiki tables if they do not already exist, with migration."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_cache (
                    repo_alias TEXT NOT NULL,
                    article_path TEXT NOT NULL,
                    rendered_html TEXT NOT NULL,
                    title TEXT NOT NULL,
                    file_mtime REAL NOT NULL,
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
                    max_mtime REAL NOT NULL,
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
                    first_viewed_at TIMESTAMP,
                    last_viewed_at TIMESTAMP,
                    PRIMARY KEY (repo_alias, article_path)
                )
                """
            )
            # Migration: add metadata_json column if it does not exist
            existing_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(wiki_cache)").fetchall()
            }
            if "metadata_json" not in existing_cols:
                conn.execute("ALTER TABLE wiki_cache ADD COLUMN metadata_json TEXT")

        self._conn_manager.execute_atomic(_op)

    def get_article(
        self, repo_alias: str, article_path: str
    ) -> Optional[Dict[str, Any]]:
        """Return dict with rendered_html, title, file_mtime, file_size, metadata_json or None."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT rendered_html, title, file_mtime, file_size, metadata_json "
            "FROM wiki_cache WHERE repo_alias = ? AND article_path = ?",
            (repo_alias, article_path),
        )
        row = cursor.fetchone()
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

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_cache "
                "(repo_alias, article_path, rendered_html, title, file_mtime, file_size, rendered_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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

        self._conn_manager.execute_atomic(_op)

    def get_sidebar(self, repo_alias: str) -> Optional[str]:
        """Return sidebar_json string for repo_alias, or None."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT sidebar_json FROM wiki_sidebar_cache WHERE repo_alias = ?",
            (repo_alias,),
        )
        row = cursor.fetchone()
        return row[0] if row else None  # type: ignore[no-any-return]

    def put_sidebar(
        self,
        repo_alias: str,
        sidebar_json: str,
        max_mtime: float,
        built_at: str,
    ) -> None:
        """Store (upsert) sidebar row."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_sidebar_cache "
                "(repo_alias, sidebar_json, max_mtime, built_at) VALUES (?, ?, ?, ?)",
                (repo_alias, sidebar_json, max_mtime, built_at),
            )

        self._conn_manager.execute_atomic(_op)

    def invalidate_repo(self, repo_alias: str) -> None:
        """Delete all wiki_cache and wiki_sidebar_cache rows for repo_alias."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM wiki_cache WHERE repo_alias = ?", (repo_alias,))
            conn.execute(
                "DELETE FROM wiki_sidebar_cache WHERE repo_alias = ?", (repo_alias,)
            )

        self._conn_manager.execute_atomic(_op)

    def increment_view(self, repo_alias: str, article_path: str, now: str) -> None:
        """Upsert wiki_article_views, incrementing real_views."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO wiki_article_views
                    (repo_alias, article_path, real_views, first_viewed_at, last_viewed_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(repo_alias, article_path) DO UPDATE SET
                    real_views = real_views + 1,
                    last_viewed_at = excluded.last_viewed_at
                """,
                (repo_alias, article_path, now, now),
            )

        self._conn_manager.execute_atomic(_op)

    def get_view_count(self, repo_alias: str, article_path: str) -> int:
        """Return real_views count for article, or 0."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT real_views FROM wiki_article_views WHERE repo_alias = ? AND article_path = ?",
            (repo_alias, article_path),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def get_all_view_counts(self, repo_alias: str) -> List[Dict[str, Any]]:
        """Return all view records for repo as list of dicts."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT article_path, real_views, first_viewed_at, last_viewed_at "
            "FROM wiki_article_views WHERE repo_alias = ? ORDER BY real_views DESC",
            (repo_alias,),
        )
        return [
            {
                "article_path": row[0],
                "real_views": row[1],
                "first_viewed_at": row[2],
                "last_viewed_at": row[3],
            }
            for row in cursor.fetchall()
        ]

    def delete_views_for_repo(self, repo_alias: str) -> None:
        """Delete all wiki_article_views rows for repo_alias."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM wiki_article_views WHERE repo_alias = ?", (repo_alias,)
            )

        self._conn_manager.execute_atomic(_op)

    def insert_initial_views(
        self, repo_alias: str, article_path: str, views: int, now: str
    ) -> None:
        """Insert initial view count (INSERT OR IGNORE)."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR IGNORE INTO wiki_article_views
                    (repo_alias, article_path, real_views, first_viewed_at, last_viewed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (repo_alias, article_path, views, now, now),
            )

        self._conn_manager.execute_atomic(_op)

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()
