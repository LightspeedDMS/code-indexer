"""
Meta Directory Updater - update strategy for meta-directories.

Implements UpdateStrategy interface for meta-directories (repos with repo_url=None).
Meta-directories contain description files for registered golden repos; this updater
detects when repos are added/removed and syncs description files accordingly.
"""

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .update_strategy import UpdateStrategy

if TYPE_CHECKING:
    from .global_registry import GlobalRegistry

logger = logging.getLogger(__name__)

# Only allow safe filename characters: letters, digits, hyphens, underscores, dots.
# This rejects path separators, traversal sequences, and shell-special characters.
_SAFE_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class MetaDirectoryUpdater(UpdateStrategy):
    """
    Update strategy for meta-directories.

    Meta-directories (repo_url=None) contain Markdown description files for
    each registered golden repo.  This updater detects divergence between the
    registry contents and the files present on disk, and syncs them on update().

    Only aliases that pass filename-safety validation are considered for sync.
    Unsafe aliases are logged and excluded, so both has_changes() and update()
    operate on the same validated alias set and always converge.
    """

    def __init__(self, meta_dir: str, registry: "GlobalRegistry") -> None:
        """
        Initialize meta-directory updater.

        Args:
            meta_dir: Path to the meta-directory (e.g., golden_repos/cidx-meta)
            registry: GlobalRegistry instance to query registered repos
        """
        self.meta_dir = Path(meta_dir)
        self.registry = registry

    def has_changes(self) -> bool:
        """
        Check if meta-directory is out of sync with registry.

        Returns True when:
        - A registered (safe) repo has no description file (new repo)
        - A description file exists for a repo no longer in registry (deleted repo)

        Returns:
            True if changes detected, False if in sync
        """
        registered = self._get_safe_registered_aliases()
        existing_files = self._get_existing_description_aliases()

        if registered - existing_files:
            return True
        if existing_files - registered:
            return True
        return False

    def update(self, force_reset: bool = False) -> None:
        """
        Sync meta-directory with registry.

        Creates missing description files for new repos and removes orphaned
        description files for deleted repos.  Only operates on validated aliases.

        Args:
            force_reset: Accepted for UpdateStrategy interface compatibility;
                ignored for meta-directories which have no git state to reset.
        """
        self.meta_dir.mkdir(parents=True, exist_ok=True)

        registered = self._get_safe_registered_aliases()
        existing_files = self._get_existing_description_aliases()

        for alias in registered - existing_files:
            desc_file = self.meta_dir / f"{alias}.md"
            desc_file.write_text(f"# {alias}\n\nGolden repository: {alias}\n")
            logger.info("MetaDirectoryUpdater: created description file %s", desc_file)

        for alias in existing_files - registered:
            desc_file = self.meta_dir / f"{alias}.md"
            desc_file.unlink(missing_ok=True)
            logger.info("MetaDirectoryUpdater: removed orphaned file %s", desc_file)

    def get_source_path(self) -> str:
        """
        Get the path to the meta-directory.

        Returns:
            Absolute path to meta-directory
        """
        return str(self.meta_dir)

    def _get_safe_registered_aliases(self) -> set:
        """
        Return set of alias names from registry that pass filename-safety validation.

        Uses _SAFE_ALIAS_PATTERN to reject aliases containing path separators,
        traversal sequences, or other unsafe characters. Rejected aliases are
        logged as warnings and excluded from sync so the updater always converges.
        """
        repos = self.registry.list_global_repos()
        safe = set()
        for repo in repos:
            alias = repo["alias_name"]
            if _SAFE_ALIAS_PATTERN.match(alias):
                safe.add(alias)
            else:
                logger.warning(
                    "MetaDirectoryUpdater: excluded unsafe alias from sync: %r", alias
                )
        return safe

    def _get_existing_description_aliases(self) -> set:
        """Return set of alias names inferred from existing .md files in meta_dir."""
        if not self.meta_dir.exists():
            return set()
        return {f.stem for f in self.meta_dir.glob("*.md")}
