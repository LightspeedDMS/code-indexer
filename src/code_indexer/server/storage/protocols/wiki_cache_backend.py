"""WikiCacheBackend Protocol (Story #523).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class WikiCacheBackend(Protocol):
    """Protocol for wiki cache storage (Story #523).

    Provides data-level access to wiki_cache, wiki_sidebar_cache, and
    wiki_article_views tables.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a WikiCacheBackend without inheritance.
    """

    def get_article(
        self, repo_alias: str, article_path: str
    ) -> "Optional[Dict[str, Any]]":
        """Return dict with rendered_html, title, file_mtime, file_size, metadata_json or None."""
        ...

    def put_article(
        self,
        repo_alias: str,
        article_path: str,
        html: str,
        title: str,
        file_mtime: float,
        file_size: int,
        rendered_at: str,
        metadata_json: "Optional[str]",
    ) -> None:
        """Store (upsert) rendered article row."""
        ...

    def get_sidebar(self, repo_alias: str) -> "Optional[str]":
        """Return sidebar_json string for repo_alias, or None."""
        ...

    def put_sidebar(
        self,
        repo_alias: str,
        sidebar_json: str,
        max_mtime: float,
        built_at: str,
    ) -> None:
        """Store (upsert) sidebar row."""
        ...

    def invalidate_repo(self, repo_alias: str) -> None:
        """Delete all wiki_cache and wiki_sidebar_cache rows for repo_alias."""
        ...

    def increment_view(self, repo_alias: str, article_path: str, now: str) -> None:
        """Upsert wiki_article_views, incrementing real_views."""
        ...

    def get_view_count(self, repo_alias: str, article_path: str) -> int:
        """Return real_views count for article, or 0."""
        ...

    def get_all_view_counts(self, repo_alias: str) -> "List[Dict[str, Any]]":
        """Return all view records for repo as list of dicts."""
        ...

    def delete_views_for_repo(self, repo_alias: str) -> None:
        """Delete all wiki_article_views rows for repo_alias."""
        ...

    def insert_initial_views(
        self, repo_alias: str, article_path: str, views: int, now: str
    ) -> None:
        """Insert initial view count (INSERT OR IGNORE)."""
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
