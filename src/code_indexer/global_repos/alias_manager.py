"""
Alias Manager: golden-repo alias pointers.

An alias maps a global alias name (``my-repo-global``) to the index directory
queries are served from. The pointer is control-plane metadata (content seam
step 5a): it lives in the server's registry, with the old
``golden-repos/aliases/<name>.json`` file kept in step during the rollback
window. Storage, read source and dual-write are decided by
``ControlPlaneMetadata`` -- this class only shapes alias documents.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from code_indexer.global_repos.control_plane_metadata import (
    KIND_ALIAS,
    ControlPlaneMetadata,
    PathLike,
    metadata_for,
    pointer_from_document,
)
from code_indexer.global_repos.write_mode_markers import WriteModeMarkers
from code_indexer.utils.index_store import INDEX_STORE_ALIAS_FIELD, INDEX_VERSION_FIELD

logger = logging.getLogger(__name__)


def resolve_alias_or_index_path(
    alias_manager: "AliasManager",
    alias_name: str,
    repo_entry: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Resolve alias_name to a filesystem path, falling back to the registry's
    index_path when the alias pointer file is missing/unreadable (Bug #1315).

    Both direct (-global) and omni cross-repo search resolution call this
    shared helper (Messi Rule #4 anti-duplication) so the fallback semantics
    stay identical between the two paths -- see
    ``code_indexer.server.multi.multi_search_service.MultiSearchService._get_repository_path``
    and ``code_indexer.server.mcp.handlers.search._resolve_global_repo_target``.

    The alias pointer JSON (managed by ``AliasManager``) is normally created
    at activation time and is the authoritative, current target path (it
    tracks refresh/swap operations). The registry's own ``index_path`` field
    is written atomically at registration time and becomes a valid fallback
    when the alias pointer file was never created or was lost (e.g. a
    bulk-provisioning gap) -- it is an already-trusted field from the SAME
    registry row already used to confirm the repo exists, not a new/masking
    fallback (Messi Rule #2 anti-fallback).

    Args:
        alias_manager: AliasManager instance scoped to the aliases directory.
        alias_name: Alias to resolve (e.g. "my-repo-global").
        repo_entry: Registry row dict (must contain "index_path" for the
            fallback to be attempted), or None if the repo entry is unknown.

    Returns:
        Resolved filesystem path string, or None if neither the alias
        pointer nor a valid on-disk index_path resolves.
    """
    target_path = alias_manager.read_alias(alias_name)
    if target_path:
        alias_manager.ensure_served_here(alias_name, target_path)
        return target_path

    index_path = repo_entry.get("index_path") if repo_entry else None
    if index_path and Path(index_path).exists():
        logger.warning(
            "Alias pointer for '%s' missing/unreadable; falling back to "
            "registry index_path '%s' (Bug #1315)",
            alias_name,
            index_path,
        )
        return str(index_path)

    return None


class AliasManager:
    """
    Manages alias pointers for global repos.

    Each alias is a JSON document (target_path, previous_path, created_at,
    last_refresh, swapped_at, repo_name) stored as control-plane metadata.
    ``aliases_dir`` names the golden-repos directory it belongs to (its
    parent); it is created on disk only when this process has no registry,
    i.e. when the files ARE the store.

    Write-mode redirection (Story #231): ``read_alias`` of a ``-global`` alias
    whose repo has an open write session returns the live source path, read
    from the write-mode marker (``WriteModeMarkers``).
    """

    def __init__(
        self,
        aliases_dir: PathLike,
        *,
        metadata: Optional[ControlPlaneMetadata] = None,
    ):
        """
        Args:
            aliases_dir: The golden-repos ``aliases`` directory.
            metadata: The control-plane metadata component; defaults to the
                one installed for ``aliases_dir``'s golden-repos directory.
        """
        self.aliases_dir = Path(aliases_dir)
        golden_repos_dir = self.aliases_dir.parent
        self._metadata = (
            metadata if metadata is not None else metadata_for(golden_repos_dir)
        )
        self._write_mode = WriteModeMarkers(golden_repos_dir, metadata=self._metadata)
        if not self._metadata.is_registry_backed:
            self.aliases_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def existing(cls, aliases_dir: PathLike) -> Optional["AliasManager"]:
        """An AliasManager only if aliases can exist for this directory: a
        registry is installed for it, or the directory is already on disk.
        Never creates the directory (callers that probe must not)."""
        path = Path(aliases_dir)
        if metadata_for(path.parent).is_registry_backed or path.is_dir():
            return cls(path)
        return None

    def _read_write_mode_source_path(self, alias_without_global: str) -> Optional[str]:
        """Live source path if a write session is open for the alias, else None."""
        try:
            return self._write_mode.source_path(alias_without_global)
        except Exception as e:
            logger.warning(
                "Failed to read write-mode marker for %s: %s; "
                "falling back to normal alias resolution",
                alias_without_global,
                e,
            )
            return None

    def read_record(self, alias_name: str) -> Optional[Dict[str, Any]]:
        """The whole alias document, or None. No write-mode redirection."""
        return self._metadata.get(KIND_ALIAS, alias_name)

    def read_pointer(self, alias_name: str) -> Optional[Tuple[str, Optional[str]]]:
        """``(target_path, previous_path)`` from ONE read of the document, with
        no write-mode redirection (Bug #1567 Gap 1 holes A/B)."""
        return pointer_from_document(self.read_record(alias_name))

    def list_records(self, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        """Every alias document whose name starts with *prefix*, whole (no
        write-mode redirection). Raises when the store cannot be read."""
        return dict(self._metadata.list_all(KIND_ALIAS, prefix))

    def list_pointers(self, prefix: str = "") -> Dict[str, Tuple[str, Optional[str]]]:
        """Every alias whose name starts with *prefix*, as ``read_pointer``
        would return it; aliases with no usable target_path are absent."""
        pointers: Dict[str, Tuple[str, Optional[str]]] = {}
        for name, document in self._metadata.list_all(KIND_ALIAS, prefix).items():
            pointer = pointer_from_document(document)
            if pointer is not None:
                pointers[name] = pointer
        return pointers

    def create_alias(
        self,
        alias_name: str,
        target_path: str,
        repo_name: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Create (or replace) an alias pointer.

        Args:
            alias_name: Name of the alias (e.g., "my-repo-global")
            target_path: Path to the index directory
            repo_name: Optional repository name for metadata
            extra: Optional fields published WITH the pointer (one write)

        Raises:
            RuntimeError: If the write fails
        """
        now = datetime.now(timezone.utc).isoformat()
        alias_data = {
            "target_path": target_path,
            "created_at": now,
            "last_refresh": now,
            "repo_name": repo_name or alias_name.removesuffix("-global"),
        }
        if extra:
            alias_data.update(extra)
        try:
            self._metadata.put(KIND_ALIAS, alias_name, alias_data)
        except Exception as e:
            raise RuntimeError(f"Failed to create alias {alias_name}: {e}") from e
        logger.info(f"Created alias: {alias_name} -> {target_path}")

    def read_alias(self, alias_name: str) -> Optional[str]:
        """
        Read the target path from an alias.

        Write-mode redirection (Story #231, Task #21): for a '-global' alias
        with an open write session, the live source path is returned instead
        of the versioned snapshot path, so reads during the session see the
        in-progress changes.

        Args:
            alias_name: Name of the alias

        Returns:
            Target path or None if alias doesn't exist
        """
        if alias_name.endswith("-global"):
            alias_without_global = alias_name[: -len("-global")]
            source_path = self._read_write_mode_source_path(alias_without_global)
            if source_path:
                logger.debug(
                    f"Write-mode active for {alias_name}: "
                    f"redirecting reads to source {source_path}"
                )
                return source_path

        pointer = self.read_pointer(alias_name)
        return pointer[0] if pointer is not None else None

    def ensure_served_here(self, alias_name: str, target_path: str) -> None:
        """Make the index at ``target_path`` (just read from ``alias_name``)
        readable on this pod before a caller READS it.

        Under a delegated IndexStore the target may have been published by
        another pod: its recorded version is checked out here first
        (``server/services/index_materializer.py``), and a failure raises
        ``PublishedIndexUnavailableError`` rather than reading as "not found".
        A write-mode redirect (a live source path) is left untouched. Under
        the local store this does nothing (no metadata read).
        """
        from code_indexer.server.startup.index_store_wiring import (
            server_index_store,
        )

        store = server_index_store()
        if store.is_local:
            return
        record = self.read_record(alias_name)
        pointer = pointer_from_document(record)
        if pointer is None or pointer[0] != target_path:
            return
        from code_indexer.server.services.index_materializer import (
            ensure_published_index,
        )

        ensure_published_index(target_path, record, store=store)

    def record_republished_index(
        self,
        alias_name: str,
        target_path: str,
        expected_version: str,
        fields: Dict[str, Any],
    ) -> None:
        """Record that the index at ``target_path`` was re-published in place
        (an HNSW repair under a delegated IndexStore), replacing
        ``expected_version``.

        Refuses (RuntimeError) when the alias no longer points at
        ``target_path`` or records another version -- a swap or another
        republish happened meanwhile, and the newer pointer wins.

        **Race window**: the read-check-write here is NOT atomic -- another
        writer that slips between ``read_record`` and ``put`` will have its
        update silently overwritten. The IndexStore's own CAS on the version
        field (``base_version`` in ``publish_index``) is the primary
        concurrency guard; this method is a secondary bookkeeping step whose
        staleness window is small (the competing writer is a refresh or
        ``swap_alias`` on any pod, not only this sweep tick).
        """
        from code_indexer.utils.index_store import INDEX_VERSION_FIELD

        record = self.read_record(alias_name)
        pointer = pointer_from_document(record)
        if record is None or pointer is None or pointer[0] != target_path:
            raise RuntimeError(
                f"Alias {alias_name} no longer points at {target_path}; "
                "not recording the repaired index"
            )
        if record.get(INDEX_VERSION_FIELD) != expected_version:
            raise RuntimeError(
                f"Alias {alias_name} records version "
                f"{record.get(INDEX_VERSION_FIELD)!r}, not {expected_version!r}; "
                "not recording the repaired index"
            )
        record.update(fields)
        try:
            self._metadata.put(KIND_ALIAS, alias_name, record)
        except Exception as e:
            raise RuntimeError(f"Failed to update alias {alias_name}: {e}") from e
        logger.info(
            "Alias %s: index at %s re-published as %s",
            alias_name,
            target_path,
            fields.get(INDEX_VERSION_FIELD),
        )

    def delete_alias(self, alias_name: str) -> None:
        """
        Delete an alias pointer. A failure is logged, not raised.

        Args:
            alias_name: Name of the alias to delete
        """
        try:
            self._metadata.delete(KIND_ALIAS, alias_name)
            logger.info(f"Deleted alias: {alias_name}")
        except Exception as e:
            logger.warning(f"Failed to delete alias {alias_name}: {e}")

    def alias_exists(self, alias_name: str) -> bool:
        """
        Check if an alias exists.

        Args:
            alias_name: Name of the alias

        Returns:
            True if alias exists, False otherwise
        """
        return self.read_record(alias_name) is not None

    def update_refresh_timestamp(self, alias_name: str) -> None:
        """
        Update the last refresh timestamp for an alias.

        Args:
            alias_name: Name of the alias

        Raises:
            RuntimeError: If alias doesn't exist or update fails
        """
        alias_data = self.read_record(alias_name)
        if alias_data is None:
            raise RuntimeError(f"Alias {alias_name} does not exist")
        alias_data["last_refresh"] = datetime.now(timezone.utc).isoformat()
        try:
            self._metadata.put(KIND_ALIAS, alias_name, alias_data)
        except Exception as e:
            raise RuntimeError(f"Failed to update alias {alias_name}: {e}") from e
        logger.debug(f"Updated refresh timestamp for alias: {alias_name}")

    def swap_alias(
        self,
        alias_name: str,
        new_target: str,
        old_target: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Swap alias to point to new target.

        Updates the alias pointer to the new index path while preserving
        the old path for cleanup tracking.

        Args:
            alias_name: Name of the alias to swap
            new_target: New index path to point to
            old_target: Expected current target (for validation)
            extra: Optional fields describing what is published, written in
                the SAME document write as the new pointer (so a reader never
                sees one without the other). Fields not named are kept, except
                the published-index fields (``index_store_alias``,
                ``index_version``): they describe the OLD target, so a swap
                that does not name them drops them and a pod that does not
                hold the new target fails loud instead of checking the old
                version out into it.

        Raises:
            RuntimeError: If the swap fails
            ValueError: If old_target doesn't match current target
        """
        alias_data = self.read_record(alias_name)
        if alias_data is None:
            # Self-heal rather than abort. The pointer is DERIVED state: the record
            # that a repo is globally active lives in the global registry, and the
            # caller has already built and validated the snapshot being published.
            # Raising here fails the whole refresh AND leaves queries served from the
            # previous snapshot, so a missing pointer degrades into permanently stale
            # answers that look healthy -- observed on the fleet as every
            # global_repo_refresh failing with "Alias <x>-global does not exist" while
            # a freshly built snapshot sat unpublished beside the one being served.
            #
            # There is no old_target to validate against when no pointer exists, so
            # the conflict check this method otherwise performs has nothing to protect.
            logger.warning(
                "Alias %s missing on publish; recreating it at %s "
                "(pointer is derived state, rebuilt from the publish in progress)",
                alias_name,
                new_target,
            )
            self.create_alias(
                alias_name=alias_name, target_path=new_target, extra=extra
            )
            return

        current_target = alias_data.get("target_path")
        if current_target != old_target:
            raise ValueError(
                f"Current target '{current_target}' does not match "
                f"expected old target '{old_target}'"
            )

        now = datetime.now(timezone.utc).isoformat()
        alias_data["target_path"] = new_target
        alias_data["previous_path"] = old_target
        alias_data["swapped_at"] = now
        alias_data["last_refresh"] = now
        alias_data.pop(INDEX_STORE_ALIAS_FIELD, None)
        alias_data.pop(INDEX_VERSION_FIELD, None)
        if extra:
            alias_data.update(extra)
        try:
            self._metadata.put(KIND_ALIAS, alias_name, alias_data)
        except Exception as e:
            raise RuntimeError(f"Failed to swap alias {alias_name}: {e}") from e
        logger.info(f"Swapped alias {alias_name}: {old_target} -> {new_target}")

    def get_previous_path(self, alias_name: str) -> Optional[str]:
        """
        Get the previous target path from an alias.

        Used by cleanup manager to identify old indexes for deletion.

        Args:
            alias_name: Name of the alias

        Returns:
            Previous target path or None if no swap occurred or alias doesn't exist
        """
        alias_data = self.read_record(alias_name)
        if alias_data is None:
            return None
        previous_path = alias_data.get("previous_path")
        return previous_path if isinstance(previous_path, str) else None
