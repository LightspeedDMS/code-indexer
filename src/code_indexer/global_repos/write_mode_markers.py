"""Write-mode markers (Story #231), stored as control-plane metadata.

A marker says "a write session is open on this write-exception repo; reads of
``<alias>-global`` go to the live source, and CRUD is allowed". It used to be
``golden-repos/.write_mode/<alias>.json``; it is now a registry row (content
seam step 5a), reached through :func:`metadata_for` like the alias pointers.

Every reader and writer -- the MCP enter/exit handlers, FileCRUDService's gate,
AliasManager's read redirection and RefreshScheduler's stale-marker cleanup --
goes through :class:`WriteModeMarkers`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from code_indexer.global_repos.control_plane_metadata import (
    KIND_WRITE_MODE,
    ControlPlaneMetadata,
    PathLike,
    metadata_for,
)

_GLOBAL_SUFFIX = "-global"


def marker_alias(repo_alias: str) -> str:
    """The key a marker is stored under: the alias without ``-global``."""
    return repo_alias.removesuffix(_GLOBAL_SUFFIX)


class WriteModeMarkers:
    """Kind-specific view of control-plane metadata for write-mode markers."""

    def __init__(
        self,
        golden_repos_dir: PathLike,
        *,
        metadata: Optional[ControlPlaneMetadata] = None,
    ) -> None:
        self._metadata = (
            metadata if metadata is not None else metadata_for(golden_repos_dir)
        )

    @property
    def is_shared_across_nodes(self) -> bool:
        """True when markers live in a registry every cluster node reads."""
        return self._metadata.is_registry_backed and self._metadata.cluster

    def get(self, repo_alias: str) -> Optional[Dict[str, Any]]:
        return self._metadata.get(KIND_WRITE_MODE, marker_alias(repo_alias))

    def exists(self, repo_alias: str) -> bool:
        return self.get(repo_alias) is not None

    def source_path(self, repo_alias: str) -> Optional[str]:
        """The live source path an active session redirects reads to."""
        marker = self.get(repo_alias)
        if marker is None:
            return None
        source = marker.get("source_path")
        return source if isinstance(source, str) and source else None

    def enter(self, repo_alias: str, source_path: str) -> None:
        alias = marker_alias(repo_alias)
        self._metadata.put(
            KIND_WRITE_MODE,
            alias,
            {
                "alias": alias,
                "source_path": source_path,
                "entered_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def remove(self, repo_alias: str) -> None:
        self._metadata.delete(KIND_WRITE_MODE, marker_alias(repo_alias))

    def list_all(self) -> Dict[str, Dict[str, Any]]:
        return self._metadata.list_all(KIND_WRITE_MODE)

    def names(self) -> List[str]:
        """Every marker key, including unreadable marker files."""
        return self._metadata.names(KIND_WRITE_MODE)
