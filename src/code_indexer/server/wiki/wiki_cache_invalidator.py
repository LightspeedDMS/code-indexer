"""Event-driven wiki cache invalidation service (Story #304)."""

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .wiki_cache import WikiCache

logger = logging.getLogger(__name__)


class WikiCacheInvalidator:
    """Receives mutation events and invalidates wiki cache accordingly."""

    def __init__(self) -> None:
        self.wiki_cache: Optional["WikiCache"] = None

    def set_wiki_cache(self, cache: "WikiCache") -> None:
        """Wire the WikiCache instance this invalidator operates on."""
        self.wiki_cache = cache

    def invalidate_repo(self, repo_alias: str) -> None:
        """Invalidate all wiki cache entries for a repository."""
        if self.wiki_cache is None:
            return
        logger.debug("Invalidating wiki cache for repo: %s", repo_alias)
        self.wiki_cache.invalidate_repo(repo_alias)

    def invalidate_for_file_change(self, repo_alias: str, file_path: str) -> None:
        """Invalidate wiki cache if the changed file is a wiki-relevant file type.

        Only .md, .markdown, and .txt files trigger invalidation (AC1, AC5).
        """
        if self.wiki_cache is None:
            return
        normalized = file_path.lower()
        if normalized.endswith((".md", ".markdown", ".txt")):
            logger.debug(
                "Markdown file changed, invalidating wiki cache for %s", repo_alias
            )
            self.wiki_cache.invalidate_repo(repo_alias)

    def invalidate_for_git_operation(self, repo_alias: str) -> None:
        """Invalidate wiki cache after a git operation that may have changed content."""
        if self.wiki_cache is None:
            return
        logger.debug(
            "Git operation completed, invalidating wiki cache for %s", repo_alias
        )
        self.wiki_cache.invalidate_repo(repo_alias)

    def on_refresh_complete(self, alias_name: str) -> None:
        """Called by RefreshScheduler after a successful refresh.

        Strips the -global suffix from the alias_name before invalidating,
        since wiki cache keys use the plain alias (not the global alias).
        """
        repo_alias = (
            alias_name[: -len("-global")]
            if alias_name.endswith("-global")
            else alias_name
        )
        self.invalidate_repo(repo_alias)


# Module-level singleton
wiki_cache_invalidator = WikiCacheInvalidator()
