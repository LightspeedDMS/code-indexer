"""Story #1488 / Codex Finding D2: `--new-collection-layout` must reach EVERY
embedding provider's backend on the `cidx index` path, not just the primary.

The multi-provider index loop (Story #620) creates one FilesystemVectorStore
per configured provider via ``BackendFactory.create(...)``. The PRIMARY
provider's backend was correctly threaded the resolved
``use_chunks_db_for_new_collections`` layout, but every ADDITIONAL/secondary
provider's ``BackendFactory.create(...)`` omitted it, silently falling back to
the env/default layout. A server child invoked with
``--new-collection-layout=chunks_db`` therefore built the primary provider's
collection as CHUNKS_DB but the secondary providers' collections in the
default SHARDED_JSON layout -- violating AC1 (server enforces CHUNKS_DB for
ALL new collections it creates).

This test drives the REAL `cidx index` command through its real multi-provider
loop and observes the actual ``BackendFactory.create`` call arguments via a
recording spy (the collaborator, not the SUT). The SUT -- the cli index
command's argument-passing wiring -- is exercised for real; only the
surrounding harness (embedding provider, smart indexer, config manager) is
stood in, mirroring the accepted pattern in
``tests/unit/cli/test_cli_multi_provider_index.py``. The resolved value must
reach every provider uniformly across the whole value domain, so all three
layout choices (chunks_db, sharded_json, flag-absent) are exercised.
"""

import contextlib
from unittest.mock import MagicMock, patch

import pytest


def _make_config(codebase_dir: str) -> MagicMock:
    """Minimal Config mock with two embedding providers (voyage-ai + cohere)."""
    cfg = MagicMock()
    cfg.codebase_dir = codebase_dir
    cfg.embedding_provider = "voyage-ai"
    cfg.embedding_providers = ["voyage-ai", "cohere"]

    cfg.voyage_ai = MagicMock()
    cfg.voyage_ai.parallel_requests = 8

    cfg.cohere = MagicMock()
    cfg.cohere.parallel_requests = 4

    cfg.vector_store = None
    cfg.daemon = None  # standalone index path (runs the multi-provider loop)

    cfg.get_embedding_providers = lambda: ["voyage-ai", "cohere"]
    return cfg


def _provider_aware_create(config, console):
    """Provider-aware embedding factory: health checks always pass."""
    provider_name = getattr(config, "embedding_provider", "voyage-ai")
    model = "voyage-3" if provider_name == "voyage-ai" else "embed-v4.0"
    mock = MagicMock()
    mock.health_check.return_value = True  # honors both () and (test_api=True)
    mock.get_provider_name.return_value = provider_name
    mock.get_current_model.return_value = model
    mock.get_model_info.return_value = {}
    return mock


def _make_indexer() -> MagicMock:
    stats = MagicMock()
    stats.duration = 1.0
    stats.files_processed = 5
    stats.chunks_created = 20
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
def _index_test_env(tmp_path, cfg, backend_spy, mock_indexer):
    """Standard patches for a `cidx index` test, spying on BackendFactory.create."""
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "metadata.json").write_text("{}")

    with (
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.resolve_api_key",
            side_effect=lambda p: "key-123",
        ),
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.create",
            side_effect=_provider_aware_create,
        ),
        patch(
            "code_indexer.cli.BackendFactory.create",
            side_effect=backend_spy,
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
    ):
        mock_cm.create_with_backtrack.return_value.load.return_value = cfg
        mock_cm.create_with_backtrack.return_value.config_path = (
            config_dir / "config.json"
        )

        from click.testing import CliRunner

        yield CliRunner()


def _run_index_recording_layout(tmp_path, layout_arg):
    """Invoke `cidx index` (with `--new-collection-layout <layout_arg>` unless
    layout_arg is None) with two providers configured; return the list of
    recorded ``use_chunks_db_for_new_collections`` values, one per
    BackendFactory.create call on the index path, plus the indexer and result.

    This is the single owner of the recording spy -- every scenario shares it.
    """
    cfg = _make_config(codebase_dir=str(tmp_path))
    mock_indexer = _make_indexer()

    recorded = []

    def backend_spy(*args, **kwargs):
        recorded.append(kwargs.get("use_chunks_db_for_new_collections"))
        return MagicMock(
            health_check=lambda: True,
            get_vector_store_client=lambda: MagicMock(),
        )

    argv = ["index"]
    if layout_arg is not None:
        argv += ["--new-collection-layout", layout_arg]

    with _index_test_env(tmp_path, cfg, backend_spy, mock_indexer) as runner:
        from code_indexer.cli import cli

        result = runner.invoke(cli, argv)

    return recorded, mock_indexer, result


class TestNewCollectionLayoutReachesAllProviders:
    @pytest.mark.parametrize(
        "layout_arg, expected",
        [
            ("chunks_db", True),
            ("sharded_json", False),
            (None, None),  # flag absent -> uniform env fallback for all providers
        ],
    )
    def test_resolved_layout_reaches_every_provider_backend(
        self, tmp_path, layout_arg, expected
    ):
        recorded, mock_indexer, result = _run_index_recording_layout(
            tmp_path, layout_arg
        )

        # Sanity: the real multi-provider loop actually ran both providers.
        assert mock_indexer.smart_index.call_count >= 2, (
            f"Expected smart_index called at least twice (one per provider), got "
            f"{mock_indexer.smart_index.call_count}.\nexit={result.exit_code}\n{result.output}"
        )

        # Both the primary AND every secondary provider's backend must have been
        # constructed with the SAME resolved layout value.
        assert len(recorded) >= 2, (
            f"Expected at least two BackendFactory.create calls (primary + "
            f"secondary), got {len(recorded)}: {recorded}\n{result.output}"
        )
        assert all(v is expected for v in recorded), (
            "Every provider's BackendFactory.create must receive the same "
            f"resolved use_chunks_db_for_new_collections={expected!r}; got "
            f"{recorded}. A secondary-provider create call dropping the explicit "
            "layout is Codex Finding D2."
        )
