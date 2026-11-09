"""Shared helpers for control-plane metadata tests (content seam step 5a).

``scoped_install`` restores whatever component was installed before (the
running app's, when an earlier test in the same worker built it) instead of
uninstalling it, so a test never breaks another test's wiring.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from code_indexer.global_repos.control_plane_metadata import (
    ControlPlaneMetadata,
    install_control_plane_metadata,
    installed_control_plane_metadata,
    uninstall_control_plane_metadata,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    ControlPlaneMetadataSqliteBackend,
)


def build_schema_template(directory: Path) -> Path:
    """The real server SQLite schema, built once for a module."""
    db_path = directory / "cidx_server.db"
    DatabaseSchema(str(db_path)).initialize_database()
    return db_path


def sqlite_backend_from_template(
    template: Path, directory: Path
) -> ControlPlaneMetadataSqliteBackend:
    """A fresh registry backend over a private copy of the template schema."""
    db_path = directory / "cidx_server.db"
    shutil.copyfile(template, db_path)
    return ControlPlaneMetadataSqliteBackend(str(db_path))


@contextmanager
def scoped_install(component: ControlPlaneMetadata) -> Iterator[ControlPlaneMetadata]:
    """Install *component* for the duration; then restore the previous one."""
    previous = installed_control_plane_metadata()
    install_control_plane_metadata(component)
    try:
        yield component
    finally:
        if previous is None:
            uninstall_control_plane_metadata()
        else:
            install_control_plane_metadata(previous)
