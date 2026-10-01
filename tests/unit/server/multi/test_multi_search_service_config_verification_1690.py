"""Bug #1690: MultiSearchService._search_temporal_sync blind-trusted
ConfigManager.create_with_backtrack(repo_path).get_config() without
verifying the resolved config genuinely describes repo_path itself --
mirroring the exact root-cause pattern Bug #1683 round 4 fixed for
AutoWatchManager.start_watch.

repo_path comes from `_get_repository_path(repo_id)`, a real golden-repo
resolution that should always carry its own `.code-indexer/config.json`.
When that invariant is violated (a dangling registry entry, corrupted
state), `create_with_backtrack` silently backtracks onto an unrelated
ANCESTOR's config instead of failing loud, and the temporal fusion dispatch
would then silently use the WRONG embedder/config settings for `repo_path`.

Fix: `_search_temporal_sync` now routes through
`ConfigManager.load_verified_config` (Bug #1690), which raises
`ConfigVerificationError` (a `ValueError` subclass) instead of returning
the ancestor's config -- this must happen BEFORE any of the downstream
FilesystemVectorStore / temporal-fusion-dispatch machinery is touched.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub missing optional dependencies not installed in the unit-test
# environment (same technique used by test_temporal_cache_injection_1170.py
# in this same directory, required for the code_indexer.server.multi.*
# import chain to complete).
# ---------------------------------------------------------------------------
_STUB_MODULES = [
    "google",
    "google.protobuf",
    "google.protobuf.descriptor",
    "google.protobuf.descriptor_pb2",
    "google.protobuf.descriptor_pool",
    "google.protobuf.internal",
    "google.protobuf.internal.builder",
    "google.protobuf.message",
    "google.protobuf.reflection",
    "google.protobuf.symbol_database",
    "google.protobuf.runtime_version",
    "rich",
    "rich.console",
    "rich.markup",
    "rich.table",
    "rich.panel",
    "rich.progress",
    "rich.text",
    "rich.syntax",
    "rich.traceback",
    "rich.logging",
    "pathspec",
    "code_indexer.scip.protobuf.scip_pb2",
    "code_indexer.scip.protobuf",
    "numpy",
    "msgpack",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = MagicMock()


@pytest.fixture
def isolated_tmp_root():
    """Rooted under `~/.tmp` (never bare `/tmp` -- project convention),
    immune to any real `.code-indexer/config.json` ancestor of `/tmp` on
    this dev machine."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1690_mss_"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


class TestSearchTemporalSyncRefusesAncestorOnlyConfig:
    def test_raises_before_touching_vector_store(self, isolated_tmp_root) -> None:
        from code_indexer.config import ConfigManager, ConfigVerificationError
        from code_indexer.server.multi.multi_search_service import (
            MultiSearchService,
        )
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig
        from code_indexer.server.multi.models import MultiSearchRequest
        import code_indexer.services.temporal.temporal_fusion_dispatch as _tfd
        import code_indexer.storage.filesystem_vector_store as _fvs_mod
        import code_indexer.services.temporal.temporal_server_paths as _tsp

        ancestor_dir = isolated_tmp_root / "server-data"
        ancestor_dir.mkdir()
        ConfigManager(
            ancestor_dir / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=ancestor_dir)

        # Dangling/orphaned repo path (no config of its own) nested under
        # the ancestor's real config.
        repo_path = ancestor_dir / "golden-repos" / "myrepo"
        repo_path.mkdir(parents=True)

        svc = MultiSearchService(MultiSearchConfig())
        request = MultiSearchRequest(
            query="test query",
            search_type="temporal",
            repositories=["myrepo-global"],
            limit=5,
        )

        # Downstream dependencies are mocked so that, absent the fix, the
        # OLD code path sails all the way through and returns [] silently
        # instead of raising -- proving the config-verification gap.
        with (
            patch.object(svc, "_get_repository_path", return_value=str(repo_path)),
            patch.object(
                _tsp,
                "resolve_golden_repo_coordinates",
                return_value=(ancestor_dir / "golden-repos", "myrepo"),
            ),
            patch.object(_fvs_mod, "FilesystemVectorStore", return_value=MagicMock()),
            patch.object(
                _tfd,
                "execute_temporal_query_with_fusion",
                return_value=MagicMock(warning=None, results=[]),
            ),
        ):
            with pytest.raises(ConfigVerificationError):
                svc._search_temporal_sync("myrepo-global", request)

        svc.thread_executor.shutdown(wait=False)
