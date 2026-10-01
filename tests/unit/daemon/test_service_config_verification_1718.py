"""Bug #1718: `daemon/service.py` has 6 unverified
`ConfigManager.create_with_backtrack()` call sites, carrying the SAME
root-cause pattern already fixed for other daemon/server call sites in
Bug #1690 (server read paths) and Bug #1713 (`daemon/watch_manager.py`).

`create_with_backtrack()` walks UP the directory tree from the target
directory looking for `.code-indexer/config.json`, and either:

(a) silently returns an unrelated ANCESTOR's config when the target
    directory has no config of its own, or
(b) defaults to a bare `Config()` (codebase_dir=".") when nothing is
    found anywhere.

Both outcomes leave the resolved config's `codebase_dir` pointing
somewhere OTHER than the caller's intended target directory. Four of the
six sites in this file are WRITE/indexing paths -- the SAME failure class
as Bug #1683's original severe incident (silently indexing the wrong
directory + real embedding-API cost):

- `exposed_index_blocking` temporal branch (~line 672)
- `exposed_index_blocking` semantic branch (~line 779)
- `_run_indexing_background` (~line 1025)
- `exposed_rebuild_fts_index` (~line 1980)

The remaining two are READ paths, closer to #1690's severity:

- `exposed_query_temporal` (~line 489)
- `_execute_semantic_search` (~line 1741)

Fix: route every site through `ConfigManager.load_verified_config(target_dir)`
(added in #1690), which raises `ConfigVerificationError` (a `ValueError`
subclass) unless the resolved config's `codebase_dir` strictly equals the
target directory -- failing loud instead of silently indexing/querying
against the wrong directory.

Each test below proves the SILENT WRONG-DIRECTORY hazard directly: it
sets up a real ancestor directory with its own genuine
`.code-indexer/config.json`, and a target project directory NESTED under
it with NO config of its own. Before the fix, `create_with_backtrack()`
silently walks up and returns the ANCESTOR's config, and the daemon
proceeds to construct real indexing/search machinery using it -- these
tests capture that machinery's construction (SmartIndexer, TemporalIndexer,
FileFinder, BackendFactory) and assert it was NEVER reached once the fix
is in place, proving the wrong-directory operation was refused rather
than merely relabeled with a different exception type.

NOTE on test isolation: mirrors `test_watch_manager_config_verification_1713.py`
-- an `isolated_tmp_root` fixture rooted under `~/.tmp` (never bare `/tmp`)
avoids any real `.code-indexer/config.json` that may exist as an ancestor
of the system `/tmp` on this dev machine.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# Mock rpyc before import if not available (matches sibling daemon test files).
try:
    import rpyc  # noqa: F401
except ImportError:
    sys.modules["rpyc"] = MagicMock()
    sys.modules["rpyc.utils.server"] = MagicMock()

from code_indexer.config import ConfigManager
from code_indexer.daemon.service import CIDXDaemonService


@pytest.fixture
def isolated_tmp_root():
    """A temp directory tree rooted under `~/.tmp` (never `/tmp` -- project
    convention), immune to any real `.code-indexer/config.json` that may
    exist as an ancestor of the system `/tmp`."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1718_"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _make_ancestor_and_project(isolated_tmp_root: Path) -> Path:
    """Create a real ancestor directory with its own genuine
    `.code-indexer/config.json`, and return a NESTED project directory
    that has no config of its own -- `find_config_path` will walk up and
    find the ancestor's."""
    ancestor_dir = isolated_tmp_root / "server-data"
    ancestor_dir.mkdir()
    ConfigManager(ancestor_dir / ".code-indexer" / "config.json").create_default_config(
        codebase_dir=ancestor_dir
    )
    project_path = ancestor_dir / "activated-repos" / "user1" / "myrepo"
    project_path.mkdir(parents=True)
    return project_path


def _fake_backend(vector_store: Optional[MagicMock] = None):
    backend = MagicMock()
    backend.get_vector_store_client.return_value = vector_store or MagicMock()
    return backend


class TestExposedQueryTemporalRefusesAncestorConfig:
    """Site 1 (READ path, ~line 489): `exposed_query_temporal`'s lazy
    `self.config_manager` init blind-trusted `create_with_backtrack`."""

    def test_raises_instead_of_querying_ancestor_directory(self, isolated_tmp_root):
        project_path = _make_ancestor_and_project(isolated_tmp_root)

        service = CIDXDaemonService()
        mock_cache_entry = MagicMock()
        mock_cache_entry.project_path = project_path
        service.cache_entry = mock_cache_entry

        with (
            patch.object(service, "_ensure_cache_loaded"),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.execute_temporal_query_with_fusion"
            ) as mock_fusion,
        ):
            with pytest.raises(Exception) as exc_info:
                service.exposed_query_temporal(
                    project_path=str(project_path),
                    query="test query",
                    time_range="all",
                    limit=10,
                )

        from code_indexer.config import ConfigVerificationError

        assert isinstance(exc_info.value, ConfigVerificationError)
        mock_fusion.assert_not_called()


class TestExposedIndexBlockingTemporalRefusesAncestorConfig:
    """Site 2 (WRITE path, ~line 672): `exposed_index_blocking`'s
    `index_commits=True` temporal branch blind-trusted
    `create_with_backtrack`."""

    def test_refuses_to_index_ancestor_directory(self, isolated_tmp_root):
        project_path = _make_ancestor_and_project(isolated_tmp_root)

        service = CIDXDaemonService()
        with patch(
            "code_indexer.services.temporal.temporal_indexer.TemporalIndexer"
        ) as mock_temporal_indexer:
            result = service.exposed_index_blocking(
                str(project_path), index_commits=True
            )

        assert result["status"] == "error", (
            f"Must fail loud instead of silently indexing the ancestor's "
            f"directory: {result}"
        )
        mock_temporal_indexer.assert_not_called()


class TestExposedIndexBlockingSemanticRefusesAncestorConfig:
    """Site 3 (WRITE path, ~line 779): `exposed_index_blocking`'s standard
    semantic-indexing branch blind-trusted `create_with_backtrack`."""

    def test_refuses_to_index_ancestor_directory(self, isolated_tmp_root):
        project_path = _make_ancestor_and_project(isolated_tmp_root)

        service = CIDXDaemonService()
        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
                return_value=MagicMock(),
            ),
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create",
                return_value=_fake_backend(),
            ),
            patch(
                "code_indexer.services.smart_indexer.SmartIndexer"
            ) as mock_smart_indexer,
        ):
            result = service.exposed_index_blocking(str(project_path))

        assert result["status"] == "error", (
            f"Must fail loud instead of silently indexing the ancestor's "
            f"directory: {result}"
        )
        mock_smart_indexer.assert_not_called()


class TestRunIndexingBackgroundRefusesAncestorConfig:
    """Site 4 (WRITE path, ~line 1025): `_run_indexing_background` blind-
    trusted `create_with_backtrack`. Called directly (not via the
    background thread spawned by `exposed_index`) so the test can assert
    synchronously."""

    def test_refuses_to_index_ancestor_directory(self, isolated_tmp_root):
        project_path = _make_ancestor_and_project(isolated_tmp_root)

        service = CIDXDaemonService()
        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
                return_value=MagicMock(),
            ),
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create",
                return_value=_fake_backend(),
            ),
            patch(
                "code_indexer.services.smart_indexer.SmartIndexer"
            ) as mock_smart_indexer,
        ):
            service._run_indexing_background(str(project_path), {})

        mock_smart_indexer.assert_not_called()
        assert service.indexing_stats is None, (
            "Must not report completed indexing stats for the ancestor's directory"
        )
        assert service.indexing_error is not None, (
            "Must record a loud error instead of silently indexing the "
            "ancestor's directory"
        )


class TestExecuteSemanticSearchRefusesAncestorConfig:
    """Site 5 (READ path, ~line 1741): `_execute_semantic_search` blind-
    trusted `create_with_backtrack`."""

    def test_refuses_to_search_ancestor_directory(self, isolated_tmp_root):
        project_path = _make_ancestor_and_project(isolated_tmp_root)

        service = CIDXDaemonService()
        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
                return_value=MagicMock(),
            ),
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create"
            ) as mock_backend_factory,
        ):
            results, timing_info = service._execute_semantic_search(
                str(project_path), "query text"
            )

        mock_backend_factory.assert_not_called()
        assert results == []
        assert "error" in timing_info


class TestExposedRebuildFtsIndexRefusesAncestorConfig:
    """Site 6 (WRITE path, ~line 1980): `exposed_rebuild_fts_index` blind-
    trusted `create_with_backtrack`."""

    def test_refuses_to_rebuild_ancestor_directory(self, isolated_tmp_root):
        project_path = _make_ancestor_and_project(isolated_tmp_root)

        # exposed_rebuild_fts_index requires an indexing_progress.json to
        # exist directly at project_path -- unrelated to the config-
        # verification hazard under test, but a precondition of reaching it.
        progress_file = project_path / ".code-indexer" / "indexing_progress.json"
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        progress_file.write_text("{}")

        service = CIDXDaemonService()
        with patch("code_indexer.indexing.file_finder.FileFinder") as mock_file_finder:
            result = service.exposed_rebuild_fts_index(str(project_path))

        assert result["status"] == "error", (
            f"Must fail loud instead of silently rebuilding the FTS index "
            f"for the ancestor's directory: {result}"
        )
        mock_file_finder.assert_not_called()
