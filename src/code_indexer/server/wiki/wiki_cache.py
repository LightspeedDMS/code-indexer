"""SQLite render cache for wiki articles with stat-based coherence (Story #283)."""
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CREATE_WIKI_CACHE_TABLE = """
    CREATE TABLE IF NOT EXISTS wiki_cache (
        repo_alias TEXT NOT NULL,
        article_path TEXT NOT NULL,
        rendered_html TEXT NOT NULL,
        title TEXT NOT NULL,
        file_mtime REAL NOT NULL,
        file_size INTEGER NOT NULL,
        rendered_at TEXT NOT NULL,
        PRIMARY KEY (repo_alias, article_path)
    )
"""

_CREATE_WIKI_SIDEBAR_CACHE_TABLE = """
    CREATE TABLE IF NOT EXISTS wiki_sidebar_cache (
        repo_alias TEXT PRIMARY KEY,
        sidebar_json TEXT NOT NULL,
        max_mtime REAL NOT NULL,
        built_at TEXT NOT NULL
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

    def ensure_tables(self) -> None:
        """Create wiki_cache and wiki_sidebar_cache tables if they do not exist."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(_CREATE_WIKI_CACHE_TABLE)
            conn.execute(_CREATE_WIKI_SIDEBAR_CACHE_TABLE)
            conn.commit()
        finally:
            conn.close()

    def get_article(self, repo_alias: str, article_path: str, file_path: Path) -> Optional[Dict]:
        """Return cached article dict if stat values match, else None."""
        try:
            stat = file_path.stat()
        except OSError:
            return None
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT rendered_html, title, file_mtime, file_size "
                "FROM wiki_cache WHERE repo_alias=? AND article_path=?",
                (repo_alias, article_path),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        stored_html, stored_title, stored_mtime, stored_size = row
        if stored_mtime != stat.st_mtime or stored_size != stat.st_size:
            return None
        return {"html": stored_html, "title": stored_title}

    def put_article(self, repo_alias: str, article_path: str, html: str, title: str, file_path: Path) -> None:
        """Store rendered article with current stat values from os.stat(file_path)."""
        stat = file_path.stat()
        rendered_at = datetime.utcnow().isoformat()
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_cache "
                "(repo_alias, article_path, rendered_html, title, file_mtime, file_size, rendered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo_alias, article_path, html, title, stat.st_mtime, stat.st_size, rendered_at),
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
