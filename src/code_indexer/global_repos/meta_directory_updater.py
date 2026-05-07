"""
Meta Directory Updater - update strategy for meta-directories.

Implements UpdateStrategy interface for meta-directories (repos with repo_url=None).
Meta-directories contain description files for registered golden repos; this updater
detects when repos are added/removed and syncs description files accordingly.

PATH MODEL (four path types in code-indexer golden repos):

1. BASE CLONE (golden_repos_dir/{alias}/): Mutable. Git pull, description
   updates, metadata sync happen here. MetaDirectoryUpdater operates here.
2. VERSIONED SNAPSHOT (.versioned/{alias}/v_{timestamp}/): IMMUTABLE after
   creation. Query/search reads go here. NEVER write to versioned paths.
3. ACTIVATED REPO (activated-repos/{user}/{alias}/): User workspace copy.
   Users may modify for individual development.
4. CIDX-META (golden_repos_dir/cidx-meta/): Special meta-directory.
   Writes only through locked writers. Commits through CidxMetaBackupSync.
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

# Safety gate: block mass-deletion when >= MIN_FILES_FOR_THRESHOLD managed files
# exist and the deletion ratio would exceed MAX_DELETE_RATIO.
MAX_DELETE_RATIO = 0.5
MIN_FILES_FOR_THRESHOLD = 3


class MetaDirectoryMassDeleteBlocked(RuntimeError):
    """Raised when update() would delete more than the safety threshold allows."""

    def __init__(self, to_delete_count: int, existing_count: int, aliases: set) -> None:
        self.to_delete_count = to_delete_count
        self.existing_count = existing_count
        self.aliases = aliases
        ratio_str = (
            f"{to_delete_count / existing_count:.0%}" if existing_count > 0 else "N/A"
        )
        super().__init__(
            f"Blocked mass-delete of {to_delete_count}/{existing_count} managed files "
            f"(ratio {ratio_str} exceeds 50% threshold). "
            f"Aliases: {sorted(aliases)[:10]}"
        )


class MetaDirectoryUpdater(UpdateStrategy):
    """
    Update strategy for meta-directories.

    Meta-directories (repo_url=None) contain Markdown description files for
    each registered golden repo.  This updater detects divergence between the
    registry contents and the files present on disk, and syncs them on update().

    Only aliases that pass filename-safety validation are considered for sync.
    Unsafe aliases are logged and excluded, so both has_changes() and update()
    operate on the same validated alias set and always converge.

    Managed files are identified by the *-global.md glob pattern only.
    Non-managed files (README.md, CHANGELOG.md, etc.) are never touched.
    """

    def __init__(
        self,
        meta_dir: str,
        registry: "GlobalRegistry",
        refresh_scheduler=None,
    ) -> None:
        """
        Initialize meta-directory updater.

        Args:
            meta_dir: Path to the meta-directory (e.g., golden_repos/cidx-meta)
            registry: GlobalRegistry instance to query registered repos
            refresh_scheduler: Optional RefreshScheduler for write-lock acquisition.
                When provided, update() acquires the cidx-meta write lock before
                making any filesystem changes and releases it in a finally block.
        """
        self.meta_dir = Path(meta_dir)
        self.registry = registry
        self._refresh_scheduler = refresh_scheduler

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

        Creates missing description files for new repos (stub only -- never
        overwrites existing content) and removes orphaned description files
        for deleted repos.  Only operates on validated aliases.

        A safety gate blocks deletions when the ratio of files-to-delete
        exceeds MAX_DELETE_RATIO and at least MIN_FILES_FOR_THRESHOLD
        managed files exist.  In that case MetaDirectoryMassDeleteBlocked
        is raised and no files are deleted.

        When refresh_scheduler is set, the cidx-meta write lock is acquired
        before any filesystem changes and released in a finally block.

        Args:
            force_reset: Accepted for UpdateStrategy interface compatibility;
                ignored for meta-directories which have no git state to reset.
        """
        self.meta_dir.mkdir(parents=True, exist_ok=True)

        lock_acquired = False
        if self._refresh_scheduler is not None:
            try:
                lock_acquired = self._refresh_scheduler.acquire_write_lock(
                    "cidx-meta", owner_name="meta_directory_updater"
                )
            except Exception as lock_err:
                logger.warning(
                    "MetaDirectoryUpdater: could not acquire write lock: %s", lock_err
                )
            if not lock_acquired:
                logger.warning(
                    "MetaDirectoryUpdater: write lock not acquired, skipping update"
                )
                return

        try:
            registered = self._get_safe_registered_aliases()
            existing_files = self._get_existing_description_aliases()

            # Create missing description files (stub only -- never overwrite existing)
            for alias in registered - existing_files:
                desc_file = self.meta_dir / f"{alias}.md"
                if not desc_file.exists():
                    desc_file.write_text(f"# {alias}\n\nGolden repository: {alias}\n")
                    logger.info(
                        "MetaDirectoryUpdater: created description file %s", desc_file
                    )

            # Safety threshold: block mass-deletion
            to_delete = existing_files - registered
            if to_delete and len(existing_files) >= MIN_FILES_FOR_THRESHOLD:
                ratio = len(to_delete) / len(existing_files)
                if ratio > MAX_DELETE_RATIO:
                    logger.error(
                        "MetaDirectoryUpdater: BLOCKED mass-delete of %d/%d files (%.0f%%). "
                        "Aliases: %s",
                        len(to_delete),
                        len(existing_files),
                        ratio * 100,
                        sorted(to_delete)[:10],
                    )
                    raise MetaDirectoryMassDeleteBlocked(
                        len(to_delete), len(existing_files), to_delete
                    )

            for alias in to_delete:
                desc_file = self.meta_dir / f"{alias}.md"
                desc_file.unlink(missing_ok=True)
                logger.info("MetaDirectoryUpdater: removed orphaned file %s", desc_file)
        finally:
            if lock_acquired and self._refresh_scheduler is not None:
                self._refresh_scheduler.release_write_lock(
                    "cidx-meta", owner_name="meta_directory_updater"
                )

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
        """Return set of alias names inferred from existing *-global.md files in meta_dir.

        Only files matching the *-global.md pattern are considered managed.
        Non-managed files (README.md, CHANGELOG.md, notes.md, etc.) are ignored
        and never modified by this updater.
        """
        if not self.meta_dir.exists():
            return set()
        return {f.stem for f in self.meta_dir.glob("*-global.md")}
