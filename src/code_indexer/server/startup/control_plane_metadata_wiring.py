"""Startup wiring for control-plane metadata in the registry (content seam 5a).

Installs the registry-backed :class:`ControlPlaneMetadata` for the server's
golden-repos directory -- so every ``AliasManager`` / ``WriteModeMarkers``
built for it reads and writes the registry -- and starts the one-time import
of the existing alias and write-mode files. Runs in ``service_init`` right
after the storage backends exist and before anything resolves an alias.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from code_indexer.global_repos.control_plane_metadata import (
    ControlPlaneMetadata,
    MetadataSettings,
    install_control_plane_metadata,
)

logger = logging.getLogger(__name__)

STORAGE_MODE_POSTGRES = "postgres"


def settings_from_config_service(
    get_config: Optional[Callable[[], Any]] = None,
) -> Callable[[], MetadataSettings]:
    """A settings provider reading ``control_plane_metadata_config`` from the
    runtime (DB-backed) config on every call."""

    def _get_config() -> Any:
        if get_config is not None:
            return get_config()
        from code_indexer.server.services.config_service import get_config_service

        return get_config_service().get_config()

    def _provider() -> MetadataSettings:
        cpm = _get_config().control_plane_metadata_config
        return MetadataSettings(
            read_source=cpm.metadata_read_source,
            file_dual_write=bool(cpm.metadata_file_dual_write),
        )

    return _provider


def install_registry_metadata(
    backend_registry: Any,
    golden_repos_dir: Path,
    *,
    storage_mode: str,
    settings: Optional[Callable[[], MetadataSettings]] = None,
    start_import: bool = True,
) -> Optional[ControlPlaneMetadata]:
    """Install the registry-backed component and start the file import.

    Returns None (files stay the store) when the registry has no
    control-plane metadata backend.
    """
    backend = getattr(backend_registry, "control_plane_metadata", None)
    if backend is None:
        logger.warning(
            "Control-plane metadata: no registry backend; aliases and write-mode "
            "markers stay in files under %s",
            golden_repos_dir,
        )
        return None
    metadata = ControlPlaneMetadata(
        golden_repos_dir,
        backend=backend,
        settings=settings if settings is not None else settings_from_config_service(),
        cluster=storage_mode == STORAGE_MODE_POSTGRES,
    )
    install_control_plane_metadata(metadata)
    logger.info(
        "Control-plane metadata: aliases and write-mode markers in the %s "
        "registry for %s",
        storage_mode,
        golden_repos_dir,
    )
    if start_import:
        metadata.start_import()
    return metadata
