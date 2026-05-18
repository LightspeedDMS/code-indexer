"""
Alias Manager for managing alias pointer files.

Provides atomic creation and management of alias pointer JSON files
that map global alias names to index directory paths.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from code_indexer.utils.file_locking import nfs_safe_fsync

logger = logging.getLogger(__name__)


class AliasManager:
    """
    Manages alias pointer files for global repos.

    Each alias is a JSON file containing metadata that points to
    the actual index directory for a golden repository.

    Dual-mode write-mode redirection (Task #21):
      - Standalone: reads .write_mode/{alias}.json from the local filesystem.
      - Cluster: reads write_mode_markers table via WriteModePgBackend.
        The filesystem .write_mode/ directory is NOT shared across nodes, so
        it is never consulted when a PG backend is active.
    """

    def __init__(self, aliases_dir: str):
        """
        Initialize the alias manager.

        Args:
            aliases_dir: Directory where alias JSON files are stored
        """
        self.aliases_dir = Path(aliases_dir)
        self.aliases_dir.mkdir(parents=True, exist_ok=True)
        # Optional PG backend for cluster-visible write-mode markers (Task #21).
        # Type Optional[Any]: psycopg is an optional dependency — a concrete type
        # would force its import at module load time, breaking standalone (CLI) mode.
        self._write_mode_backend: Optional[Any] = None

    def set_write_mode_backend(self, backend: Any) -> None:
        """Wire a PostgreSQL write-mode backend for cluster mode.

        When set, read_alias() exclusively uses the PG backend to check
        write-mode markers — the local filesystem .write_mode/ directory
        is not consulted (it is not NFS-shared across cluster nodes).

        Args:
            backend: WriteModePgBackend instance (or compatible interface).
                     Type is Any — psycopg is an optional dependency, so a
                     concrete type annotation would force its import at module
                     load time, breaking standalone (CLI) mode.
        """
        self._write_mode_backend = backend
        logger.info("AliasManager: using PostgreSQL write-mode backend (cluster mode)")

    def _read_write_mode_source_path(self, alias_without_global: str) -> Optional[str]:
        """Return the live source path for alias if write-mode is active, else None.

        Dual-mode implementation:
          - Cluster mode (PG backend set): queries write_mode_markers table.
            The filesystem .write_mode/ directory is NOT consulted because it
            is not NFS-shared and would give stale/empty results on other nodes.
          - Standalone mode (no PG backend): reads the local filesystem marker.

        Args:
            alias_without_global: Repository alias without the -global suffix.

        Returns:
            source_path string if write-mode is active with a valid source,
            None otherwise (write-mode inactive, no marker, or missing source_path).
        """
        if self._write_mode_backend is not None:
            # Cluster mode: PG is the single source of truth for write-mode markers.
            # Filesystem .write_mode/ is intentionally NOT checked here because it
            # is local to each node and invisible to siblings in the cluster.
            try:
                marker = self._write_mode_backend.get_marker(alias_without_global)
                if marker is None:
                    return None
                # marker.get() returns object; cast to Optional[str] is safe because
                # source_path is always written as str by files.py (or None/absent).
                source_path: Optional[str] = marker.get("source_path")  # type: ignore[assignment]
                if source_path:
                    return source_path
                # Marker present but source_path absent — write session not yet
                # redirected; fall through to normal alias resolution.
                logger.warning(
                    "PG write-mode marker for %s has no source_path; "
                    "falling back to normal alias resolution",
                    alias_without_global,
                )
                return None
            except Exception as e:
                logger.warning(
                    "Failed to read PG write-mode marker for %s: %s; "
                    "falling back to normal alias resolution",
                    alias_without_global,
                    e,
                )
                return None
        else:
            # Standalone mode: read the local filesystem marker.
            golden_repos_dir = self.aliases_dir.parent
            write_mode_marker = (
                golden_repos_dir / ".write_mode" / f"{alias_without_global}.json"
            )
            if not write_mode_marker.exists():
                return None
            try:
                marker_data = json.loads(write_mode_marker.read_text())
                fs_source_path: Optional[str] = marker_data.get("source_path")
                if fs_source_path:
                    return fs_source_path
                logger.warning(
                    "Write-mode marker for %s has no source_path; "
                    "falling back to normal alias resolution",
                    alias_without_global,
                )
                return None
            except (json.JSONDecodeError, IOError, OSError) as e:
                logger.warning(
                    "Corrupt write-mode marker for %s: %s; "
                    "falling back to normal alias resolution",
                    alias_without_global,
                    e,
                )
                return None

    def create_alias(
        self, alias_name: str, target_path: str, repo_name: Optional[str] = None
    ) -> None:
        """
        Create an alias pointer file.

        Args:
            alias_name: Name of the alias (e.g., "my-repo-global")
            target_path: Path to the index directory
            repo_name: Optional repository name for metadata

        Raises:
            RuntimeError: If atomic write fails
        """
        alias_file = self.aliases_dir / f"{alias_name}.json"
        now = datetime.now(timezone.utc).isoformat()

        alias_data = {
            "target_path": target_path,
            "created_at": now,
            "last_refresh": now,
            "repo_name": repo_name or alias_name.removesuffix("-global"),
        }

        # Atomic write using temp file + rename
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self.aliases_dir), prefix=f".{alias_name}_", suffix=".tmp"
        )

        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(alias_data, f, indent=2)
                f.flush()
                nfs_safe_fsync(f.fileno())

            # Atomic rename
            os.replace(tmp_path, str(alias_file))
            logger.info(f"Created alias: {alias_name} -> {target_path}")

        except Exception as e:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise RuntimeError(f"Failed to create alias {alias_name}: {e}")

    def read_alias(self, alias_name: str) -> Optional[str]:
        """
        Read the target path from an alias.

        Write-mode redirection (Story #231, Task #21):
        If the alias ends with '-global', check for an active write session
        via _read_write_mode_source_path(). When found, the live source path
        is returned instead of the versioned snapshot path, allowing reads
        during an active write session to see the in-progress changes.

        Args:
            alias_name: Name of the alias

        Returns:
            Target path or None if alias doesn't exist
        """
        # Task #21 / Story #231: Check write-mode for -global aliases
        if alias_name.endswith("-global"):
            alias_without_global = alias_name[: -len("-global")]
            source_path = self._read_write_mode_source_path(alias_without_global)
            if source_path:
                logger.debug(
                    f"Write-mode active for {alias_name}: "
                    f"redirecting reads to source {source_path}"
                )
                return source_path
            # source_path is None — no active write session; fall through to
            # normal alias file resolution below.

        alias_file = self.aliases_dir / f"{alias_name}.json"

        if not alias_file.exists():
            return None

        try:
            with open(alias_file, "r") as f:
                alias_data = json.load(f)
                target_path: Optional[str] = alias_data.get("target_path")
                return target_path
        except (json.JSONDecodeError, IOError, KeyError) as e:
            logger.warning(f"Failed to read alias {alias_name}: {e}")
            return None

    def delete_alias(self, alias_name: str) -> None:
        """
        Delete an alias pointer file.

        Args:
            alias_name: Name of the alias to delete
        """
        alias_file = self.aliases_dir / f"{alias_name}.json"

        if alias_file.exists():
            try:
                alias_file.unlink()
                logger.info(f"Deleted alias: {alias_name}")
            except OSError as e:
                logger.warning(f"Failed to delete alias {alias_name}: {e}")

    def alias_exists(self, alias_name: str) -> bool:
        """
        Check if an alias exists.

        Args:
            alias_name: Name of the alias

        Returns:
            True if alias exists, False otherwise
        """
        alias_file = self.aliases_dir / f"{alias_name}.json"
        return alias_file.exists()

    def update_refresh_timestamp(self, alias_name: str) -> None:
        """
        Update the last refresh timestamp for an alias.

        Args:
            alias_name: Name of the alias

        Raises:
            RuntimeError: If alias doesn't exist or update fails
        """
        alias_file = self.aliases_dir / f"{alias_name}.json"

        if not alias_file.exists():
            raise RuntimeError(f"Alias {alias_name} does not exist")

        try:
            with open(alias_file, "r") as f:
                alias_data = json.load(f)

            alias_data["last_refresh"] = datetime.now(timezone.utc).isoformat()

            # Atomic write for update
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self.aliases_dir), prefix=f".{alias_name}_", suffix=".tmp"
            )

            with os.fdopen(tmp_fd, "w") as f:
                json.dump(alias_data, f, indent=2)
                f.flush()
                nfs_safe_fsync(f.fileno())

            os.replace(tmp_path, str(alias_file))
            logger.debug(f"Updated refresh timestamp for alias: {alias_name}")

        except Exception as e:
            try:
                os.unlink(tmp_path)
            except (OSError, NameError):
                pass
            raise RuntimeError(f"Failed to update alias {alias_name}: {e}")

    def swap_alias(self, alias_name: str, new_target: str, old_target: str) -> None:
        """
        Atomically swap alias to point to new target.

        Updates the alias pointer to the new index path while preserving
        the old path for cleanup tracking. Uses atomic write pattern.

        Args:
            alias_name: Name of the alias to swap
            new_target: New index path to point to
            old_target: Expected current target (for validation)

        Raises:
            RuntimeError: If alias doesn't exist or swap fails
            ValueError: If old_target doesn't match current target
        """
        alias_file = self.aliases_dir / f"{alias_name}.json"

        if not alias_file.exists():
            raise RuntimeError(f"Alias {alias_name} does not exist")

        try:
            # Read current alias data
            with open(alias_file, "r") as f:
                alias_data = json.load(f)

            # Validate old_target matches current target
            current_target = alias_data.get("target_path")
            if current_target != old_target:
                raise ValueError(
                    f"Current target '{current_target}' does not match "
                    f"expected old target '{old_target}'"
                )

            # Update alias data
            alias_data["target_path"] = new_target
            alias_data["previous_path"] = old_target
            alias_data["swapped_at"] = datetime.now(timezone.utc).isoformat()
            alias_data["last_refresh"] = datetime.now(timezone.utc).isoformat()

            # Atomic write using temp file + rename
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self.aliases_dir), prefix=f".{alias_name}_", suffix=".tmp"
            )

            with os.fdopen(tmp_fd, "w") as f:
                json.dump(alias_data, f, indent=2)
                f.flush()
                nfs_safe_fsync(f.fileno())

            # Atomic rename
            os.replace(tmp_path, str(alias_file))
            logger.info(f"Swapped alias {alias_name}: {old_target} -> {new_target}")

        except ValueError:
            raise
        except Exception as e:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except (OSError, NameError):
                pass
            raise RuntimeError(f"Failed to swap alias {alias_name}: {e}")

    def get_previous_path(self, alias_name: str) -> Optional[str]:
        """
        Get the previous target path from an alias.

        Used by cleanup manager to identify old indexes for deletion.

        Args:
            alias_name: Name of the alias

        Returns:
            Previous target path or None if no swap occurred or alias doesn't exist
        """
        alias_file = self.aliases_dir / f"{alias_name}.json"

        if not alias_file.exists():
            return None

        try:
            with open(alias_file, "r") as f:
                alias_data = json.load(f)
                previous_path: Optional[str] = alias_data.get("previous_path")
                return previous_path
        except (json.JSONDecodeError, IOError, KeyError) as e:
            logger.warning(f"Failed to read previous path for {alias_name}: {e}")
            return None
