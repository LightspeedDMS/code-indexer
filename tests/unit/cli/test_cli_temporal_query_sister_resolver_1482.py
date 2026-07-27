"""GitHub Issue #1482 (extension, site 4 -- LOW priority/future-proofing):
the standalone CLI temporal query path (`cidx query ... --time-range...`)
only ever scans the local `.code-indexer/index/` directory for a temporal
collection, and never forwards a TemporalShardResolver into
execute_temporal_query_with_fusion. For the ONE genuine standalone case
where an operator runs `cidx query` directly inside a golden repo's own
clone (bypassing the server -- see temporal_sister_root_detection.py),
temporal data relocated to the golden-owned sister location (Story #1457
AC1) is entirely invisible: the existence gate reports "not available"
and the query never even reaches fusion dispatch.

Test 1 (`test_sister_relocated_temporal_data_is_queried_via_resolver`)
asserts the TARGET/fixed behavior and is expected to FAIL (RED) against
the current code, which never constructs/forwards a resolver. Test 2
(`test_ordinary_standalone_repo_stays_byte_identical`) is a companion
regression guard for the overwhelmingly common case (an ordinary
standalone repo, no golden-repos ancestor) -- it already passes today and
must keep passing after the fix.

Patching strategy mirrors test_cli_temporal_filter_bug_1210.py exactly
(FilesystemVectorStore patched at its defining module, ConfigManager
patched, execute_temporal_query_with_fusion patched at its source
module, BackendFactory.create patched) -- except `collection_exists` is
explicitly configured to return False (no local temporal index) and NO
stub `code-indexer-temporal` directory is created locally, so the ONLY
way `_has_temporal` can become True is via genuine sister-location
resolution. The golden-repos-shaped project directory + a real
AliasManager pointer to a real versioned snapshot dir constitute the
"real infra" sister-location layout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)

_VOYAGE_KEY = "test-voyage-key-1482"
_TEST_FILESYSTEM_PORT = 6333
_TEST_FILESYSTEM_GRPC_PORT = 6334
REPO_ALIAS = "myrepo"
POINTER_NAMESPACE = "myrepo-temporal-voyage_code_3-2024Q1"
FIXTURE_VERSION_TIMESTAMP = "1785164318"


def _write_config_json(config_dir: Path, project: Path) -> None:
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "codebase_dir": str(project),
                "filesystem": {
                    "port": _TEST_FILESYSTEM_PORT,
                    "grpc_port": _TEST_FILESYSTEM_GRPC_PORT,
                },
                "voyage_api": {"api_key": _VOYAGE_KEY},
                "embedding_provider": "voyage",
            }
        ),
        encoding="utf-8",
    )


def _build_sister_only_golden_clone(tmp_path: Path) -> Path:
    """Golden-repo-shaped project dir (flat layout) with NO local temporal
    collection, but real sister-location temporal data (alias pointer +
    versioned dir)."""
    golden_repos_dir = tmp_path / "golden-repos"
    project = golden_repos_dir / REPO_ALIAS
    config_dir = project / ".code-indexer"
    index_dir = config_dir / "index"
    index_dir.mkdir(parents=True)
    _write_config_json(config_dir, project)

    version_dir = (
        golden_repos_dir
        / ".versioned"
        / POINTER_NAMESPACE
        / f"v_{FIXTURE_VERSION_TIMESTAMP}"
    )
    version_dir.mkdir(parents=True)
    (version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    alias_manager = AliasManager(str(golden_repos_dir / "aliases"))
    alias_manager.create_alias(POINTER_NAMESPACE, str(version_dir))

    return project


def _make_mock_config(project: Path) -> MagicMock:
    mock_config = MagicMock()
    mock_config.codebase_dir = project
    mock_config.embedding_provider = "voyage-ai"
    mock_config.voyage_api = MagicMock(api_key=_VOYAGE_KEY)
    mock_config.filesystem = MagicMock(port=_TEST_FILESYSTEM_PORT)
    mock_config.daemon = MagicMock(enabled=False)
    mock_config.vector_store = None
    return mock_config


def _make_mock_cm(project: Path) -> MagicMock:
    mock_config = _make_mock_config(project)
    mock_cm = MagicMock()
    mock_cm.get_config.return_value = mock_config
    mock_cm.load.return_value = mock_config
    mock_cm.get_daemon_config.return_value = {"enabled": False}
    return mock_cm


def _make_mock_fsvs(project: Path) -> MagicMock:
    mock_vs = MagicMock()
    mock_vs.health_check.return_value = True
    mock_vs.collection_exists.return_value = False
    mock_vs.base_path = project / ".code-indexer" / "index"
    mock_vs.project_root = project
    return mock_vs


def _make_mock_backend(project: Path) -> MagicMock:
    mock_vs = _make_mock_fsvs(project)
    mock_backend = MagicMock()
    mock_backend.get_vector_store_client.return_value = mock_vs
    return mock_backend


def _empty_results() -> TemporalSearchResults:
    return TemporalSearchResults(
        results=[],
        query="test query",
        filter_type="time_range",
        filter_value=("1970-01-01", "2100-12-31"),
        total_found=0,
    )


def _invoke(project: Path, extra_args: List[str], fusion_mock: MagicMock) -> Any:
    mock_cm = _make_mock_cm(project)
    mock_backend = _make_mock_backend(project)
    mock_vs = mock_backend.get_vector_store_client()
    mock_fsvs_class = MagicMock(return_value=mock_vs)

    runner = CliRunner()
    old_cwd = os.getcwd()
    try:
        os.chdir(str(project))
        with (
            patch(
                "code_indexer.cli.ConfigManager.create_with_backtrack",
                return_value=mock_cm,
            ),
            patch(
                "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore",
                mock_fsvs_class,
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch"
                ".execute_temporal_query_with_fusion",
                side_effect=fusion_mock,
            ),
            patch(
                "code_indexer.cli.BackendFactory.create",
                return_value=mock_backend,
            ),
        ):
            return runner.invoke(
                cli,
                ["query", "my search", "--time-range-all", "--quiet"] + extra_args,
                catch_exceptions=False,
            )
    finally:
        os.chdir(old_cwd)


class TestCliTemporalQuerySisterResolverWiring:
    def test_sister_relocated_temporal_data_is_queried_via_resolver(
        self, tmp_path: Path
    ) -> None:
        project = _build_sister_only_golden_clone(tmp_path)
        fusion_mock = MagicMock(return_value=_empty_results())

        result = _invoke(project, [], fusion_mock)

        assert result.exit_code == 0, result.output
        assert fusion_mock.called, (
            "execute_temporal_query_with_fusion was never called -- the CLI "
            "temporal existence gate must recognize sister-relocated "
            "temporal data (Bug #1482 extension) instead of reporting "
            f"'not available'. CLI output: {result.output!r}"
        )
        _, kwargs = fusion_mock.call_args
        resolver = kwargs.get("resolver")
        assert isinstance(resolver, TemporalShardResolver), (
            "execute_temporal_query_with_fusion must receive a real "
            f"TemporalShardResolver via resolver=, got: {resolver!r}"
        )

    def test_ordinary_standalone_repo_stays_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """An ordinary standalone repo (no golden-repos ancestor) with no
        local temporal index must still correctly report 'not available'
        -- never inventing a sister root."""
        project = tmp_path / "my-project"
        config_dir = project / ".code-indexer"
        (config_dir / "index").mkdir(parents=True)
        _write_config_json(config_dir, project)
        fusion_mock = MagicMock(return_value=_empty_results())

        result = _invoke(project, [], fusion_mock)

        assert result.exit_code == 0, result.output
        assert not fusion_mock.called
        assert "not available" in result.output.lower()
