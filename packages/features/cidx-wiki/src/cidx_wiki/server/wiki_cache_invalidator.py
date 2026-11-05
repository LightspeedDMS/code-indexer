"""Event-driven wiki cache invalidation service (Story #304).

The core announces repository changes through the ``repository_events``
capability (file tools, git write tools, write-mode exit, golden repository
removal and rename); the feature's ``on_startup`` subscribes
:data:`wiki_cache_invalidator` to them with a live :class:`WikiCache` over the
shared store (Bug #1667: without that wiring every method below is a no-op,
and ``WikiCache.get_sidebar()`` has no staleness check of its own).
"""

import logging
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from cidx_wiki.server.wiki_cache import WikiCache

logger = logging.getLogger(__name__)


class WikiCacheInvalidator:
    """Receives mutation events and invalidates wiki cache accordingly."""

    def __init__(self) -> None:
        self.wiki_cache: Optional["WikiCache"] = None
        self._cache_factory: Optional[Callable[[], Optional["WikiCache"]]] = None

    def set_wiki_cache(self, cache: "WikiCache") -> None:
        """Wire the WikiCache instance this invalidator operates on."""
        self.wiki_cache = cache

    def set_wiki_cache_factory(
        self, factory: Optional[Callable[[], Optional["WikiCache"]]]
    ) -> None:
        """Build the WikiCache on the first event instead (the server's
        services may not exist yet when the feature starts)."""
        self._cache_factory = factory

    def _resolve(self) -> None:
        if self.wiki_cache is None and self._cache_factory is not None:
            self.wiki_cache = self._cache_factory()

    def invalidate_repo(self, repo_alias: str) -> None:
        """Invalidate all wiki cache entries for a repository."""
        self._resolve()
        if self.wiki_cache is None:
            return
        logger.debug("Invalidating wiki cache for repo: %s", repo_alias)
        self.wiki_cache.invalidate_repo(repo_alias)

    def invalidate_for_file_change(self, repo_alias: str, file_path: str) -> None:
        """Invalidate wiki cache if the changed file is a wiki-relevant file type.

        Only .md, .markdown, and .txt files trigger invalidation (AC1, AC5).
        """
        self._resolve()
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
        self._resolve()
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

    # ``repository_events`` listener (cidx_sdk RepositoryEventListener) -------

    def content_changed(self, alias: str, path: Optional[str]) -> None:
        """Files of *alias* changed: one *path*, or any file (None)."""
        if path is None:
            self.invalidate_for_git_operation(alias)
        else:
            self.invalidate_for_file_change(alias, path)

    def repository_removed(self, alias: str) -> None:
        """The golden repository *alias* was removed: its article view
        records go (Story #287, AC4)."""
        self._resolve()
        if self.wiki_cache is None:
            return
        self.wiki_cache.delete_views_for_repo(alias)

    def repository_renamed(self, old_alias: str, new_alias: str) -> None:
        """The old alias's rendered pages go (re-rendered under the new one);
        its view counts stay as history."""
        self.invalidate_repo(old_alias)


# Module-level singleton: process wiring (on_startup sets its cache), never
# request state -- every invalidation goes to the shared wiki cache store.
wiki_cache_invalidator = WikiCacheInvalidator()
