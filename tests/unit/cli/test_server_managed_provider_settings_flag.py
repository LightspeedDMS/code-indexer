"""Server-spawned indexing uses server-managed provider settings, never a
repository-authored embedding-provider endpoint or daemon override.

`cidx index` gains a new internal `--server-managed-provider-settings` flag,
stamped on every server-spawned invocation via
`code_indexer.server.utils.index_command_layout.append_server_layout_args`
(mirroring the `--ignore-resume-state` CLI-level
mechanism). When present, the loaded `Config`'s `voyage_ai.api_endpoint`,
`cohere.api_endpoint`, and `daemon.enabled` are reset to fixed,
server-managed values via
`code_indexer.server.utils.server_managed_provider_settings.
enforce_server_managed_provider_settings` before any embedding-provider
client is constructed or daemon delegation is considered -- regardless of
what a repository-authored `config.json` set those fields to.

`config_manager.load()` re-parses config.json from disk and returns a NEW
`Config` object EVERY time it is called, and `cidx index`'s single Click
command body calls it more than once across its branches (once early to
decide daemon delegation, again inside the main indexing try-block before
constructing the embedding-provider client). The fixture below mirrors that
real behaviour -- returning a fresh, independently-overridden config object on
each call, exactly like a real re-parse of an unchanged file would -- so this
test exercises every reload the command performs, not just the first one.
The security-relevant object is whichever one ultimately reaches
`EmbeddingProviderFactory.create`, so that is what the test inspects.

This test drives the REAL `cidx index` command (via click's CliRunner)
through its real flag-parsing and config-mutation wiring, with
EmbeddingProviderFactory, BackendFactory, SmartIndexer, and daemon
delegation replaced by recording spies (mirroring the accepted pattern in
tests/unit/cli/test_ignore_resume_state_flag.py and
test_ignore_resume_state_forces_local_indexing.py) -- only those
collaborators are stood in; the SUT (cli.py's flag threading) runs for real.
"""

from __future__ import annotations

import contextlib
from typing import List
from unittest.mock import MagicMock, patch

from code_indexer.config import CohereConfig, VoyageAIConfig

_REPO_CONFIGURED_VOYAGE_ENDPOINT = "http://127.0.0.1:9/voyage-embeddings"
_REPO_CONFIGURED_COHERE_ENDPOINT = "http://127.0.0.1:9/cohere-embed"


def _make_config(codebase_dir: str) -> MagicMock:
    """A fresh, independently-overridden config -- a new object each call,
    exactly like a real ConfigManager.load() re-parse of the same
    (unchanged) repository-authored config.json."""
    cfg = MagicMock()
    cfg.codebase_dir = codebase_dir
    cfg.embedding_provider = "voyage-ai"
    cfg.embedding_providers = None

    cfg.voyage_ai = MagicMock()
    cfg.voyage_ai.parallel_requests = 8
    cfg.voyage_ai.api_endpoint = _REPO_CONFIGURED_VOYAGE_ENDPOINT

    cfg.cohere = MagicMock()
    cfg.cohere.api_endpoint = _REPO_CONFIGURED_COHERE_ENDPOINT

    cfg.vector_store = None
    cfg.daemon = MagicMock()
    cfg.daemon.enabled = True
    cfg.daemon.model_dump.return_value = {"enabled": True}

    cfg.temporal = MagicMock()
    cfg.temporal.active_embedder = "voyage-context-4"

    cfg.get_embedding_providers = lambda: ["voyage-ai"]
    return cfg


def _make_indexer() -> MagicMock:
    stats = MagicMock()
    stats.duration = 1.0
    stats.files_processed = 1
    stats.chunks_created = 1
    stats.failed_files = 0
    stats.cancelled = False

    indexer = MagicMock()
    indexer.smart_index.return_value = stats
    indexer.get_git_status.return_value = {
        "git_available": False,
        "project_id": "test-proj",
    }
    indexer.get_indexing_status.return_value = {
        "status": "completed",
        "can_resume": False,
        "files_processed": 0,
        "chunks_indexed": 0,
    }
    indexer.slot_tracker = None
    return indexer


@contextlib.contextmanager
def _index_test_env(
    tmp_path,
    loaded_configs: List[MagicMock],
    mock_indexer,
    mock_index_via_daemon,
):
    """Yields a CliRunner. `loaded_configs` accumulates EVERY config object
    `config_manager.load()` hands back across the whole command invocation,
    in call order -- the last entry is whichever reload the command actually
    used to construct the embedding-provider client (or none, if a branch
    exits before reaching that point, e.g. daemon delegation)."""
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "metadata.json").write_text("{}")

    def _load_side_effect() -> MagicMock:
        cfg = _make_config(str(tmp_path))
        loaded_configs.append(cfg)
        return cfg

    with (
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.resolve_api_key",
            return_value="key-123",
        ),
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.create",
            return_value=MagicMock(
                health_check=lambda test_api=False: True,
                get_provider_name=lambda: "voyage-ai",
                get_current_model=lambda: "voyage-3",
                get_model_info=lambda: {},
            ),
        ),
        patch(
            "code_indexer.cli.BackendFactory.create",
            return_value=MagicMock(
                health_check=lambda: True,
                get_vector_store_client=lambda: MagicMock(),
            ),
        ),
        patch(
            "code_indexer.services.smart_indexer.SmartIndexer",
            return_value=mock_indexer,
        ),
        patch("code_indexer.cli.ConfigManager") as mock_cm,
        patch("code_indexer.progress.progress_display.RichLiveProgressManager"),
        patch(
            "code_indexer.progress.multi_threaded_display.MultiThreadedProgressManager"
        ),
        patch(
            "code_indexer.cli_daemon_delegation._index_via_daemon",
            mock_index_via_daemon,
        ),
    ):
        mock_cm.create_with_backtrack.return_value.load.side_effect = _load_side_effect
        mock_cm.create_with_backtrack.return_value.config_path = (
            config_dir / "config.json"
        )

        from click.testing import CliRunner

        yield CliRunner()


class TestServerManagedProviderSettingsFlag:
    """--server-managed-provider-settings resets repo-controllable provider
    transport fields to server-managed values before indexing proceeds."""

    def test_flag_resets_provider_endpoints_and_disables_daemon(self, tmp_path) -> None:
        loaded_configs: List[MagicMock] = []
        mock_indexer = _make_indexer()
        mock_index_via_daemon = MagicMock(return_value=0)

        with _index_test_env(
            tmp_path, loaded_configs, mock_indexer, mock_index_via_daemon
        ) as runner:
            from code_indexer.cli import cli

            result = runner.invoke(cli, ["index", "--server-managed-provider-settings"])

        assert result.exit_code == 0, result.output
        assert loaded_configs, "config_manager.load() was never called"
        final_config = loaded_configs[-1]
        assert final_config.voyage_ai.api_endpoint == VoyageAIConfig().api_endpoint, (
            "SECURITY: --server-managed-provider-settings must reset "
            "voyage_ai.api_endpoint to the server-managed default on every "
            "config reload the command performs, never leave a repo-chosen "
            f"value in place. Got {final_config.voyage_ai.api_endpoint!r}"
        )
        assert final_config.cohere.api_endpoint == CohereConfig().api_endpoint, (
            "SECURITY: --server-managed-provider-settings must reset "
            "cohere.api_endpoint to the server-managed default. Got "
            f"{final_config.cohere.api_endpoint!r}"
        )
        assert final_config.daemon.enabled is False, (
            "SECURITY: --server-managed-provider-settings must disable "
            "daemon mode regardless of the repo config's own setting."
        )
        mock_index_via_daemon.assert_not_called()
        assert mock_indexer.smart_index.call_count == 1, result.output

    def test_default_index_leaves_provider_endpoints_and_daemon_untouched(
        self, tmp_path
    ) -> None:
        """Positive control: standalone CLI behaviour (flag absent) is
        byte-for-byte unaffected -- a developer's own repo config is trusted
        exactly as before."""
        loaded_configs: List[MagicMock] = []
        mock_indexer = _make_indexer()
        mock_index_via_daemon = MagicMock(return_value=0)

        with _index_test_env(
            tmp_path, loaded_configs, mock_indexer, mock_index_via_daemon
        ) as runner:
            from code_indexer.cli import cli

            runner.invoke(cli, ["index"])

        assert loaded_configs, "config_manager.load() was never called"
        final_config = loaded_configs[-1]
        assert final_config.voyage_ai.api_endpoint == _REPO_CONFIGURED_VOYAGE_ENDPOINT
        assert final_config.cohere.api_endpoint == _REPO_CONFIGURED_COHERE_ENDPOINT
        assert final_config.daemon.enabled is True
        mock_index_via_daemon.assert_called_once()
        mock_indexer.smart_index.assert_not_called()


# ---------------------------------------------------------------------------
# --index-commits (temporal) branch: a SEPARATE config reload from the
# semantic branch above (cli.py's `if index_commits:` block calls
# `_load_config_for_index()` on its own, then always exits via sys.exit
# before reaching the semantic try-block) -- so the semantic test above
# proves nothing about this reload. Mirrors the accepted temporal-branch
# mocking pattern in tests/unit/cli/test_cli_index_commits_migration.py.
# ---------------------------------------------------------------------------

_MIGRATE_TEMPORAL_PATH = "code_indexer.cli.migrate_legacy_temporal_collection"
_RESOLVE_TEMPORAL_FROM_CONFIG_PATH = (
    "code_indexer.services.temporal.temporal_collection_naming"
    ".resolve_temporal_collection_from_config"
)
_TEMPORAL_INDEXER_PATH = (
    "code_indexer.services.temporal.temporal_indexer.TemporalIndexer"
)
_TEMPORAL_VECTOR_STORE_PATH = (
    "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore"
)
_CONSOLIDATE_LEGACY_TEMPORAL_SHARDS_PATH = (
    "code_indexer.services.chunk_migration_cli.consolidate_legacy_temporal_shards"
)
_ACQUIRE_INDEX_MUTATION_LOCK_PATH = (
    "code_indexer.services.chunk_migration_cli.acquire_index_mutation_lock"
)


@contextlib.contextmanager
def _index_commits_test_env(tmp_path, loaded_configs: List[MagicMock]):
    """Yields (CliRunner, migrate_mock) for the `--index-commits` temporal
    branch, with every temporal collaborator stood in (mirroring
    test_cli_index_commits_migration.py) except config loading, which runs
    through the same fresh-object-per-call pattern as the semantic tests
    above. `daemon.enabled` is forced False here (independent of
    `_make_config`'s shared default) so the LOCAL temporal path is always
    taken -- daemon-mode forcing is already covered by the semantic tests;
    this suite is about the provider-endpoint reset specifically."""
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(exist_ok=True)

    def _load_side_effect() -> MagicMock:
        cfg = _make_config(str(tmp_path))
        cfg.daemon.enabled = False
        loaded_configs.append(cfg)
        return cfg

    from code_indexer.services.temporal.temporal_migration import MigrationResult

    mock_ti_instance = MagicMock()
    mock_ti_instance.index_commits.return_value = MagicMock(
        total_commits=0,
        files_processed=0,
        approximate_vectors_created=0,
        skip_ratio=1.0,
        branches_indexed=[],
        commits_per_branch={},
    )
    mock_vs_instance = MagicMock()
    mock_vs_instance.project_root = tmp_path
    mock_vs_instance.base_path = tmp_path / ".code-indexer" / "index"

    with (
        patch("code_indexer.cli.ConfigManager") as mock_cm,
        patch(
            _MIGRATE_TEMPORAL_PATH, return_value=MigrationResult.COMPLETED
        ) as mock_migrate,
        patch(_RESOLVE_TEMPORAL_FROM_CONFIG_PATH, return_value="temporal-coll"),
        patch(_TEMPORAL_INDEXER_PATH, return_value=mock_ti_instance),
        patch(_TEMPORAL_VECTOR_STORE_PATH, return_value=mock_vs_instance),
        patch(_CONSOLIDATE_LEGACY_TEMPORAL_SHARDS_PATH, return_value=(0, 0)),
        patch(
            _ACQUIRE_INDEX_MUTATION_LOCK_PATH,
            side_effect=lambda config_dir: contextlib.nullcontext(),
        ),
    ):
        mock_cm.create_with_backtrack.return_value.load.side_effect = _load_side_effect
        mock_cm.create_with_backtrack.return_value.config_path = (
            config_dir / "config.json"
        )

        from click.testing import CliRunner

        yield CliRunner(), mock_migrate


class TestServerManagedProviderSettingsIndexCommitsFlag:
    """--server-managed-provider-settings resets the TEMPORAL branch's
    (--index-commits) provider transport fields too, not just the
    semantic (--fts) branch's."""

    def test_index_commits_flag_resets_provider_endpoints(self, tmp_path) -> None:
        loaded_configs: List[MagicMock] = []

        with _index_commits_test_env(tmp_path, loaded_configs) as (
            runner,
            mock_migrate,
        ):
            from code_indexer.cli import cli

            result = runner.invoke(
                cli,
                ["index", "--index-commits", "--server-managed-provider-settings"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        mock_migrate.assert_called_once()
        # The config handed to migrate_legacy_temporal_collection is the
        # SAME object the temporal indexer/embedder construction downstream
        # uses -- capturing it here is a direct, unambiguous proof that the
        # temporal branch's own config reload was enforced.
        _, temporal_config = mock_migrate.call_args.args
        assert (
            temporal_config.voyage_ai.api_endpoint == VoyageAIConfig().api_endpoint
        ), (
            "SECURITY: --index-commits --server-managed-provider-settings must "
            "reset voyage_ai.api_endpoint on the TEMPORAL branch's own config "
            f"reload too. Got {temporal_config.voyage_ai.api_endpoint!r}"
        )
        assert temporal_config.cohere.api_endpoint == CohereConfig().api_endpoint, (
            "SECURITY: --index-commits --server-managed-provider-settings must "
            "reset cohere.api_endpoint on the TEMPORAL branch's own config "
            f"reload too. Got {temporal_config.cohere.api_endpoint!r}"
        )

    def test_index_commits_without_flag_leaves_provider_endpoints_untouched(
        self, tmp_path
    ) -> None:
        """Positive control: standalone `--index-commits` (flag absent) is
        unaffected."""
        loaded_configs: List[MagicMock] = []

        with _index_commits_test_env(tmp_path, loaded_configs) as (
            runner,
            mock_migrate,
        ):
            from code_indexer.cli import cli

            result = runner.invoke(
                cli, ["index", "--index-commits"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        mock_migrate.assert_called_once()
        _, temporal_config = mock_migrate.call_args.args
        assert (
            temporal_config.voyage_ai.api_endpoint == _REPO_CONFIGURED_VOYAGE_ENDPOINT
        )
        assert temporal_config.cohere.api_endpoint == _REPO_CONFIGURED_COHERE_ENDPOINT
