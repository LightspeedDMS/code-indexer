"""GitHub Issue #1482 (extension, site 4 -- LOW priority/future-proofing):
CIDXDaemonService.exposed_query_temporal() never constructs or forwards a
TemporalShardResolver into execute_temporal_query_with_fusion. For the
ONE genuine standalone case where the daemon happens to be running
directly inside a golden repo's own clone (bypassing the server -- see
temporal_sister_root_detection.py), temporal data relocated to the
golden-owned sister location (Story #1457 AC1) is entirely invisible to
the daemon query path.

Mocking pattern mirrors test_service_temporal_query.py's
test_exposed_query_temporal_forwards_query_params_to_fusion_dispatch
exactly (ConfigManager/BackendFactory/execute_temporal_query_with_fusion
patched). The golden-repos-shaped project_path + a real AliasManager
pointer to a real versioned snapshot dir constitute the "real infra"
sister-location layout.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

try:
    import rpyc  # noqa: F401
except ImportError:
    sys.modules["rpyc"] = MagicMock()
    sys.modules["rpyc.utils.server"] = MagicMock()

from src.code_indexer.daemon.service import CIDXDaemonService
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)

REPO_ALIAS = "myrepo"
POINTER_NAMESPACE = "myrepo-temporal-voyage_code_3-2024Q1"
FIXTURE_VERSION_TIMESTAMP = "1785164318"


class TestExposedQueryTemporalSisterResolverWiring(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.golden_repos_dir = Path(self.temp_dir) / "golden-repos"
        self.project_path = self.golden_repos_dir / REPO_ALIAS
        (self.project_path / ".code-indexer" / "index").mkdir(parents=True)

        version_dir = (
            self.golden_repos_dir
            / ".versioned"
            / POINTER_NAMESPACE
            / f"v_{FIXTURE_VERSION_TIMESTAMP}"
        )
        version_dir.mkdir(parents=True)
        (version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
        alias_manager = AliasManager(str(self.golden_repos_dir / "aliases"))
        alias_manager.create_alias(POINTER_NAMESPACE, str(version_dir))

    def tearDown(self):
        import shutil

        if Path(self.temp_dir).exists():
            shutil.rmtree(self.temp_dir)

    @patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch.execute_temporal_query_with_fusion"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    def test_sister_relocated_temporal_data_forwarded_via_resolver(
        self,
        mock_backend_factory,
        mock_config_manager,
        mock_execute_fusion,
    ):
        service = CIDXDaemonService()

        mock_config = MagicMock()
        mock_config.embedding_provider = "voyage-ai"
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config.codebase_dir = self.project_path
        mock_config.get_config.return_value = mock_config
        mock_config_manager.create_with_backtrack.return_value = mock_config

        mock_vector_store = MagicMock()
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        mock_execute_fusion.return_value = TemporalSearchResults(
            results=[],
            query="test",
            filter_type=None,
            filter_value=None,
            total_found=0,
        )

        mock_cache_entry = MagicMock()
        mock_cache_entry.project_path = self.project_path

        with patch.object(service, "cache_lock"):
            with patch.object(service, "_ensure_cache_loaded"):
                service.cache_entry = mock_cache_entry

                service.exposed_query_temporal(
                    project_path=str(self.project_path),
                    query="test query",
                    time_range="all",
                    limit=10,
                )

        mock_execute_fusion.assert_called_once()
        call_kwargs = mock_execute_fusion.call_args[1]
        resolver = call_kwargs.get("resolver")
        self.assertIsInstance(
            resolver,
            TemporalShardResolver,
            f"execute_temporal_query_with_fusion must receive a real "
            f"TemporalShardResolver via resolver= (Bug #1482 extension), "
            f"got: {resolver!r}",
        )
        self.assertIs(
            getattr(mock_vector_store, "_temporal_shard_resolver", None),
            resolver,
            "resolver must also be attached to the vector_store instance "
            "used for search (the 'disconnected reader' lesson)",
        )
