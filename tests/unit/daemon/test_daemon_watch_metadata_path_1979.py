"""Bug #1979 (round 5 -- SUPERSEDED by the round-4 revert): daemon watch
mode must read/write the SAME bare `metadata.json` daemon-mode `--clear`/
index writes.

An earlier round (this file's original version) proposed matching
`daemon/service.py`'s then-new per-provider filename
(`metadata-<provider>.json`) here too. That per-provider daemon-write
change was itself reverted (see
`tests/unit/daemon/test_daemon_clear_metadata_path_1979.py`) because
propagating the new filename to every reader (`cidx status`, foreground
`cidx watch`, config_fixer) regressed a fresh daemon-only project's `cidx
status` to "Not Found". With the daemon back to writing bare
`metadata.json`, `DaemonWatchManager._create_watch_handler` (this file's
subject) must read that SAME bare file so a daemon `--clear` and a
subsequent daemon watch session stay consistent with each other.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.config import ConfigManager
from code_indexer.daemon.watch_manager import DaemonWatchManager


def test_daemon_watch_handler_uses_bare_metadata_path(tmp_path):
    # `_create_watch_handler` unconditionally calls
    # `ConfigManager.load_verified_config(project_path)` (Bug #1713) even
    # when a config is already supplied, so project_path needs a genuine
    # `.code-indexer/config.json` of its own.
    ConfigManager(tmp_path / ".code-indexer" / "config.json").create_default_config(
        codebase_dir=tmp_path
    )
    config = ConfigManager(tmp_path / ".code-indexer" / "config.json").load()
    config.embedding_provider = "voyage-ai"

    manager = DaemonWatchManager()

    with (
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
            return_value=MagicMock(),
        ),
        patch(
            "code_indexer.backends.backend_factory.BackendFactory.create",
            return_value=MagicMock(),
        ),
        patch("code_indexer.services.smart_indexer.SmartIndexer") as mock_smart_indexer,
        patch("code_indexer.services.git_topology_service.GitTopologyService"),
        patch(
            "code_indexer.services.watch_metadata.WatchMetadata.load_from_disk",
            return_value=MagicMock(),
        ),
        patch("code_indexer.services.git_aware_watch_handler.GitAwareWatchHandler"),
    ):
        manager._create_watch_handler(project_path=str(tmp_path), config=config)

    assert mock_smart_indexer.called, "SmartIndexer must be constructed"
    constructed_metadata_path = mock_smart_indexer.call_args[0][3]

    assert (
        constructed_metadata_path == Path(tmp_path) / ".code-indexer" / "metadata.json"
    ), (
        "daemon watch mode must read/write the SAME bare metadata file "
        f"the daemon's own --clear/index path uses, got: {constructed_metadata_path}"
    )
