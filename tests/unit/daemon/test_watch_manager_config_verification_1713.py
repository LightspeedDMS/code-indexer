"""Bug #1713: `DaemonWatchManager._create_watch_handler` blind-trusts
`ConfigManager.create_with_backtrack(project_path)`, the same root-cause
pattern fixed for server call sites in Bug #1690 (which itself mirrored the
Bug #1683 round 4 fix for `AutoWatchManager.start_watch`).

`create_with_backtrack()` walks UP the directory tree from `project_path`
and either (a) silently returns an unrelated ANCESTOR's config, or (b)
defaults to a bare `Config()` with `codebase_dir="."` when nothing is found
anywhere. Both `config` (embedder/provider settings) AND
`config_manager.config_path.parent` (used to derive `metadata_path` for the
constructed `SmartIndexer`) would then be wrong -- and unlike Bug #1690's
read-only server sites, this is a WRITE/indexing path: the constructed
`SmartIndexer` performs real indexing on file-watch events.

Fix: route through `ConfigManager.load_verified_config(Path(project_path))`
(added in #1690), which verifies the resolved config's `codebase_dir`
strictly equals the target directory and raises `ConfigVerificationError`
(a `ValueError` subclass) otherwise -- failing loud instead of silently
constructing a `SmartIndexer` against/into the wrong directory.

NOTE on test isolation: mirrors `test_config_manager_load_verified_config_1690.py`
-- an `isolated_tmp_root` fixture rooted under `~/.tmp` (never bare `/tmp`)
avoids any real `.code-indexer/config.json` that may exist as an ancestor of
the system `/tmp` on this dev machine.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import Config, ConfigManager, ConfigVerificationError
from code_indexer.daemon.watch_manager import DaemonWatchManager


@pytest.fixture
def isolated_tmp_root():
    """A temp directory tree rooted under `~/.tmp` (never `/tmp` -- project
    convention), immune to any real `.code-indexer/config.json` that may
    exist as an ancestor of the system `/tmp`."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1713_"))
    yield root
    shutil.rmtree(root)


def _create_watch_handler_with_mocked_dependencies(manager, project_path, config):
    """Invoke `_create_watch_handler` with every heavy construction
    dependency past the config-resolution step mocked out, isolating the
    test to ONLY the config verification behavior under scrutiny.

    Returns (handler, smart_indexer_mock) so callers can additionally
    inspect how SmartIndexer was constructed (e.g. the metadata_path it
    received).
    """
    targets = [
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
        "code_indexer.backends.backend_factory.BackendFactory.create",
        "code_indexer.services.smart_indexer.SmartIndexer",
        "code_indexer.services.git_topology_service.GitTopologyService",
        "code_indexer.services.watch_metadata.WatchMetadata.load_from_disk",
        "code_indexer.services.git_aware_watch_handler.GitAwareWatchHandler",
    ]
    with ExitStack() as stack:
        mocks = [
            stack.enter_context(patch(target, return_value=MagicMock()))
            for target in targets
        ]
        smart_indexer_mock = mocks[2]
        handler = manager._create_watch_handler(str(project_path), config=config)
        return handler, smart_indexer_mock


class TestCreateWatchHandlerRefusesAncestorOnlyConfig:
    """Discriminating scenario: project_path has NO `.code-indexer/config.json`
    of its own, but a real ANCESTOR directory does. `create_with_backtrack`
    walks up and silently returns the ancestor's config --
    `_create_watch_handler` must refuse it instead of silently constructing
    a `SmartIndexer` for the wrong directory.

    Parametrized over both call-site branches the bug report identifies:
    `config is None` (daemon/service.py's real caller) and `config`
    already supplied (AutoWatchManager.start_watch's caller) -- both
    branches independently called the unverified `create_with_backtrack()`.
    """

    @pytest.mark.parametrize("config_already_provided", [False, True])
    def test_raises_config_verification_error(
        self, isolated_tmp_root, config_already_provided
    ) -> None:
        ancestor_dir = isolated_tmp_root / "server-data"
        ancestor_dir.mkdir()
        ConfigManager(
            ancestor_dir / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=ancestor_dir)

        # Nested UNDER the ancestor but has no config of its own --
        # find_config_path will walk up and find the ancestor's.
        project_path = ancestor_dir / "activated-repos" / "user1" / "myrepo"
        project_path.mkdir(parents=True)

        config = Config(codebase_dir=project_path) if config_already_provided else None

        manager = DaemonWatchManager()
        with pytest.raises(ConfigVerificationError):
            _create_watch_handler_with_mocked_dependencies(
                manager, project_path, config
            )


class TestCreateWatchHandlerRefusesNoConfigFoundAnywhere:
    """No `.code-indexer/config.json` for project_path or any parent --
    must fail loud, never silently default to a bare Config()."""

    def test_raises_config_verification_error(
        self, isolated_tmp_root, monkeypatch
    ) -> None:
        project_path = isolated_tmp_root / "never-configured-repo"
        project_path.mkdir()
        # Force create_with_backtrack's CODEBASE_DIR override path so the
        # real ancestor .code-indexer dirs on this dev machine cannot be
        # accidentally found by directory-walk backtracking.
        monkeypatch.setenv("CODEBASE_DIR", str(project_path))

        manager = DaemonWatchManager()
        with pytest.raises(ConfigVerificationError):
            _create_watch_handler_with_mocked_dependencies(manager, project_path, None)


class TestCreateWatchHandlerSucceedsWithGenuineOwnConfig:
    """Positive control: project_path has its own real
    `.code-indexer/config.json` -- must succeed and derive metadata_path
    from project_path's OWN `.code-indexer` directory, never an ancestor's."""

    def test_uses_project_path_own_code_indexer_dir_for_metadata_path(
        self, isolated_tmp_root
    ) -> None:
        project_path = isolated_tmp_root / "real-repo"
        project_path.mkdir()
        ConfigManager(
            project_path / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=project_path)

        manager = DaemonWatchManager()
        handler, smart_indexer_mock = _create_watch_handler_with_mocked_dependencies(
            manager, project_path, None
        )

        assert handler is not None
        # SmartIndexer(config, embedding_provider, vector_store_client, metadata_path)
        called_metadata_path = smart_indexer_mock.call_args[0][3]
        expected_metadata_path = (
            project_path.resolve() / ".code-indexer" / "metadata.json"
        )
        assert Path(called_metadata_path) == expected_metadata_path
