"""Test helper: a running server whose wired snapshot manager has a mount.

The server's lifespan stores its ``VersionedSnapshotManager`` on
``app.state.snapshot_manager``. This helper places a stand-in running app
in the ``code_indexer.server.app`` module namespace (the same plain dict
lookup the server's own side-effect-free probe reads; the real app is never
built) whose state carries a REAL snapshot manager over a REAL
``OntapCloneBackend`` mounted at ``mount``. The ONTAP REST client is a
spec'd stand-in for the external service; nothing here calls it.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import code_indexer.server.app  # noqa: F401 -- module must be in sys.modules
from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend
from code_indexer.server.storage.shared.ontap_flexclone_client import (
    OntapFlexCloneClient,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)


def wire_running_server_snapshot_manager(
    monkeypatch: pytest.MonkeyPatch, mount: str
) -> VersionedSnapshotManager:
    """Make the running server's ``app.state.snapshot_manager`` a real
    manager whose clone backend is mounted at ``mount``; undone at teardown."""
    backend = OntapCloneBackend(
        flexclone_client=MagicMock(spec=OntapFlexCloneClient),
        mount_point=mount,
    )
    manager = VersionedSnapshotManager(clone_backend=backend)
    running_app = SimpleNamespace(state=SimpleNamespace(snapshot_manager=manager))
    app_module = sys.modules["code_indexer.server.app"]
    monkeypatch.setitem(vars(app_module), "app", running_app)
    return manager
