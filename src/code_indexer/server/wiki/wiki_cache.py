"""SQLite render cache for wiki articles with stat-based coherence (Story #283)."""
import json
import logging
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _json_default(obj: object) -> str:
    """JSON serializer for objects not serializable by default json module."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


_CREATE_WIKI_CACHE_TABLE = """
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

_ALTER_WIKI_CACHE_ADD_METADATA_JSON = """
    ALTER TABLE wiki_cache ADD COLUMN metadata_json TEXT
"""

_CREATE_WIKI_SIDEBAR_CACHE_TABLE = """
    CREATE TABLE IF NOT EXISTS wiki_sidebar_cache (
        repo_alias TEXT PRIMARY KEY,
        sidebar_json TEXT NOT NULL,
        max_mtime REAL NOT NULL,
        built_at TEXT NOT NULL
    )
"""

_CREATE_WIKI_ARTICLE_VIEWS_TABLE = """
    CREATE TABLE IF NOT EXISTS wiki_article_views (
        repo_alias TEXT NOT NULL,
        article_path TEXT NOT NULL,
        real_views INTEGER DEFAULT 0,
        first_viewed_at TIMESTAMP,
        last_viewed_at TIMESTAMP,
        PRIMARY KEY (repo_alias, article_path)
    )
"""


def _max_mtime_of_md_files(repo_dir: Path) -> float:
    """Compute max mtime of all .md files under repo_dir, excluding hidden directories."""
    max_mtime = 0.0
    for md_file in repo_dir.rglob("*.md"):
        rel = md_file.relative_to(repo_dir)
        if any(part.startswith(".") for part in rel.parts[:-1]):
            continue
        try:
            mtime = md_file.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    return max_mtime


class WikiCache:
    """SQLite-backed render cache for wiki articles and sidebars."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._increment_lock = threading.Lock()

    def ensure_tables(self) -> None:
        """Create wiki_cache, wiki_sidebar_cache, and wiki_article_views tables if they do not exist.

        Also runs idempotent schema migrations for existing databases (e.g. adding
        metadata_json column to wiki_cache for Story #289).
        """
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_WIKI_CACHE_TABLE)
            conn.execute(_CREATE_WIKI_SIDEBAR_CACHE_TABLE)
            conn.execute(_CREATE_WIKI_ARTICLE_VIEWS_TABLE)
            # Migration: add metadata_json column if it does not exist (Story #289)
            existing_cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(wiki_cache)").fetchall()
            }
            if "metadata_json" not in existing_cols:
                conn.execute(_ALTER_WIKI_CACHE_ADD_METADATA_JSON)
            conn.commit()
        finally:
            conn.close()

    def increment_view(self, repo_alias: str, article_path: str) -> None:
        """Increment view count for an article. Inserts a new row on first view (real_views=1).

        Uses upsert: on conflict increments real_views and updates last_viewed_at.
        first_viewed_at is only set on initial insert and preserved on updates.
        Serialized via _increment_lock to prevent concurrent SQLite write contention.
        Failures are logged as warnings so the page still serves.
        """
        with self._increment_lock:
            now = datetime.utcnow().isoformat()
            conn = None
            try:
                conn = sqlite3.connect(self._db_path, timeout=5)
                conn.execute("PRAGMA journal_mode=WAL")
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
                conn.commit()
            except Exception as exc:
                logger.warning("Failed to increment view for %s/%s: %s", repo_alias, article_path, exc)
            finally:
                if conn:
                    conn.close()

    def get_view_count(self, repo_alias: str, article_path: str) -> int:
        """Return current real_views count for an article, or 0 if no record exists."""
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT real_views FROM wiki_article_views WHERE repo_alias=? AND article_path=?",
                (repo_alias, article_path),
            ).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row is not None else 0

    def get_all_view_counts(self, repo_alias: str) -> List[Dict]:
        """Return all view records for a repo as a list of dicts with article_path and real_views."""
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT article_path, real_views, first_viewed_at, last_viewed_at "
                "FROM wiki_article_views WHERE repo_alias=? ORDER BY real_views DESC",
                (repo_alias,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "article_path": r[0],
                "real_views": r[1],
                "first_viewed_at": r[2],
                "last_viewed_at": r[3],
            }
            for r in rows
        ]

    def delete_views_for_repo(self, repo_alias: str) -> None:
        """Delete all wiki_article_views records for a repo (called on repo removal, AC4)."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "DELETE FROM wiki_article_views WHERE repo_alias=?",
                (repo_alias,),
            )
            conn.commit()
        finally:
            conn.close()

    def insert_initial_views(self, repo_alias: str, article_path: str, views: int) -> None:
        """Insert an initial view count row (from front matter population). Uses INSERT OR IGNORE."""
        now = datetime.utcnow().isoformat()
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                INSERT OR IGNORE INTO wiki_article_views
                    (repo_alias, article_path, real_views, first_viewed_at, last_viewed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (repo_alias, article_path, views, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def get_article(self, repo_alias: str, article_path: str, file_path: Path) -> Optional[Dict]:
        """Return cached article dict if stat values match, else None.

        Result dict contains 'html', 'title', and 'metadata' (dict or None).
        """
        try:
            stat = file_path.stat()
        except OSError:
            return None
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT rendered_html, title, file_mtime, file_size, metadata_json "
                "FROM wiki_cache WHERE repo_alias=? AND article_path=?",
                (repo_alias, article_path),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        stored_html, stored_title, stored_mtime, stored_size, stored_metadata_json = row
        if stored_mtime != stat.st_mtime or stored_size != stat.st_size:
            return None
        metadata: Optional[Dict] = None
        if stored_metadata_json:
            try:
                metadata = json.loads(stored_metadata_json)
            except (ValueError, TypeError):
                logger.warning("Failed to parse cached metadata_json for %s/%s", repo_alias, article_path)
        return {"html": stored_html, "title": stored_title, "metadata": metadata}

    def put_article(
        self,
        repo_alias: str,
        article_path: str,
        html: str,
        title: str,
        file_path: Path,
        *,
        metadata: Optional[Dict] = None,
    ) -> None:
        """Store rendered article with current stat values from os.stat(file_path).

        Optional metadata kwarg stores front-matter dict as JSON (Story #289).
        """
        stat = file_path.stat()
        rendered_at = datetime.utcnow().isoformat()
        metadata_json_str: Optional[str] = json.dumps(metadata, default=_json_default) if metadata is not None else None
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_cache "
                "(repo_alias, article_path, rendered_html, title, file_mtime, file_size, rendered_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (repo_alias, article_path, html, title, stat.st_mtime, stat.st_size, rendered_at, metadata_json_str),
            )
            conn.commit()
        finally:
            conn.close()

    def get_sidebar(self, repo_alias: str, repo_dir: Path) -> Optional[List]:
        """Return cached sidebar list if max_mtime of .md files hasn't changed, else None."""
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT sidebar_json, max_mtime FROM wiki_sidebar_cache WHERE repo_alias=?",
                (repo_alias,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        stored_json, stored_max_mtime = row
        current_max_mtime = _max_mtime_of_md_files(repo_dir)
        if current_max_mtime != stored_max_mtime:
            return None
        return json.loads(stored_json)

    def put_sidebar(self, repo_alias: str, sidebar_data: List, repo_dir: Path) -> None:
        """Store sidebar JSON with current max_mtime of .md files."""
        max_mtime = _max_mtime_of_md_files(repo_dir)
        built_at = datetime.utcnow().isoformat()
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_sidebar_cache "
                "(repo_alias, sidebar_json, max_mtime, built_at) VALUES (?, ?, ?, ?)",
                (repo_alias, json.dumps(sidebar_data), max_mtime, built_at),
            )
            conn.commit()
        finally:
            conn.close()

    def invalidate_repo(self, repo_alias: str) -> None:
        """Delete all wiki_cache and wiki_sidebar_cache entries for this repo."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM wiki_cache WHERE repo_alias=?", (repo_alias,))
            conn.execute("DELETE FROM wiki_sidebar_cache WHERE repo_alias=?", (repo_alias,))
            conn.commit()
        finally:
            conn.close()

    def invalidate_user_wiki(self, username: str, alias: str) -> None:
        """Delete all cache entries for a user wiki (Story #291, AC5).

        Cache entries for user wikis use the key ``u:{username}:{alias}`` to
        isolate them from golden repo cache entries which use just the alias.
        """
        cache_key = f"u:{username}:{alias}"
        self.invalidate_repo(cache_key)
