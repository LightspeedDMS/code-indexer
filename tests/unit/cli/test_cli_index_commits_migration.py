"""Tests for Bug #642 Step 1: cli.py --index-commits calls migration before resolve.

TDD: Tests written BEFORE implementation to drive the design.

The bug: cli.py constructs TemporalIndexer with the new provider-aware name BEFORE
migration runs, so temporal_meta.json is not found -> last_commit = None -> full
git log with no limit.

Fix: call migrate_legacy_temporal_collection before resolve_temporal_collection_from_config
in the --index-commits code path.

Covers:
- test_index_commits_calls_migration_before_resolve
- test_index_commits_migration_called_with_index_dir
"""

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.services.temporal.temporal_migration import MigrationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Patch paths — migrate is module-level in cli; the rest are lazily imported.
_MIGRATE_PATH = "code_indexer.cli.migrate_legacy_temporal_collection"
_RESOLVE_PATH = (
    "code_indexer.services.temporal.temporal_collection_naming"
    ".resolve_temporal_collection_from_config"
)
_TEMPORAL_INDEXER_PATH = (
    "code_indexer.services.temporal.temporal_indexer.TemporalIndexer"
)
_VECTOR_STORE_PATH = (
    "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore"
)


def _stub_indexer_result() -> MagicMock:
    return MagicMock(
        total_commits=0,
        files_processed=0,
        approximate_vectors_created=0,
        skip_ratio=1.0,
        branches_indexed=[],
        commits_per_branch={},
    )


def _default_migrate(path, cfg):
    return MigrationResult.COMPLETED


def _default_resolve(cfg):
    return "code-indexer-temporal-voyage_code_3"


@contextlib.contextmanager
def _patch_index_commits_path(
    migrate_side_effect=None,
    resolve_side_effect=None,
):
    """Context manager that patches only the temporal parts of --index-commits.

    Allows the real ConfigManager to run (it uses backtracking from CWD).
    Only migrate_legacy_temporal_collection, resolve, TemporalIndexer, and
    FilesystemVectorStore are patched.

    Yields (runner,) to the caller.
    """
    runner = CliRunner()
    mock_vs_instance = MagicMock()
    mock_vs_instance.project_root = Path.cwd()
    mock_vs_instance.base_path = Path.cwd() / ".code-indexer" / "index"

    mock_ti_instance = MagicMock()
    mock_ti_instance.index_commits.return_value = _stub_indexer_result()

    with (
        patch(_MIGRATE_PATH, side_effect=migrate_side_effect or _default_migrate),
        patch(_RESOLVE_PATH, side_effect=resolve_side_effect or _default_resolve),
        patch(_TEMPORAL_INDEXER_PATH, return_value=mock_ti_instance),
        patch(_VECTOR_STORE_PATH, return_value=mock_vs_instance),
    ):
        yield (runner,)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCliIndexCommitsMigration:
    """Bug #642 Step 1: migration must run before resolve in --index-commits path."""

    def test_index_commits_calls_migration_before_resolve(self):
        """migrate_legacy_temporal_collection must be called before
        resolve_temporal_collection_from_config when --index-commits runs.
        """
        call_order = []

        def recording_migrate(index_path, cfg):
            call_order.append("migrate")
            return MigrationResult.COMPLETED

        def recording_resolve(cfg):
            call_order.append("resolve")
            return "code-indexer-temporal-voyage_code_3"

        with _patch_index_commits_path(
            migrate_side_effect=recording_migrate,
            resolve_side_effect=recording_resolve,
        ) as (runner,):
            runner.invoke(cli, ["index", "--index-commits"], catch_exceptions=False)

        assert "migrate" in call_order, (
            "migrate_legacy_temporal_collection must be called"
        )
        assert "resolve" in call_order, (
            "resolve_temporal_collection_from_config must be called"
        )
        assert call_order.index("migrate") < call_order.index("resolve"), (
            f"Bug #642: migration must happen BEFORE resolve. call_order={call_order}"
        )

    def test_index_commits_migration_called_with_index_dir(self):
        """migrate_legacy_temporal_collection must receive the project's
        .code-indexer/index directory (derived from codebase_dir via backtracking).
        """
        captured_paths = []

        def recording_migrate(index_path, cfg):
            captured_paths.append(Path(index_path))
            return MigrationResult.COMPLETED

        project_root = Path.cwd()

        with _patch_index_commits_path(migrate_side_effect=recording_migrate) as (
            runner,
        ):
            runner.invoke(cli, ["index", "--index-commits"], catch_exceptions=False)

        assert captured_paths, (
            "migrate_legacy_temporal_collection must have been called"
        )
        path_arg = captured_paths[0]
        assert path_arg.name == "index", (
            f"Bug #642: migration path must end in 'index', got {path_arg}"
        )
        assert path_arg.parent.name == ".code-indexer", (
            f"Bug #642: migration path parent must be '.code-indexer', got {path_arg}"
        )
        assert project_root in path_arg.parents, (
            f"Bug #642: migration path must be under project root {project_root}, got {path_arg}"
        )
