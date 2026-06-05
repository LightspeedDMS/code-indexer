"""
Tests for LifecycleBatchRunner concurrency wiring (bug fix).

Bug: LifecycleBatchRunner(concurrency=2) is hardcoded at all 3 call sites
in DescriptionRefreshScheduler, ignoring max_concurrent_claude_cli in config.

Contract under test:
  1. _get_lifecycle_concurrency() returns max_concurrent_claude_cli from config
     when config is available.
  2. _get_lifecycle_concurrency() returns ClaudeIntegrationConfig default when
     config_manager is None.
  3. LifecycleBatchRunner receives concurrency=_get_lifecycle_concurrency() at
     the single-alias refresh call site.
  4. LifecycleBatchRunner receives concurrency=_get_lifecycle_concurrency() at
     the startup description backfill call site.
  5. LifecycleBatchRunner receives concurrency=_get_lifecycle_concurrency() at
     the lifecycle backfill call site.

Mock boundary map:
  REAL  : DescriptionRefreshScheduler, real SQLite backends.
  MOCKED: LifecycleBatchRunner — patched at the scheduler module use-site.
  STUBBED: config_manager, lifecycle_invoker, lifecycle_debouncer,
           refresh_scheduler — minimal MagicMock stand-ins.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


_PATCH_RUNNER = (
    "code_indexer.server.services.description_refresh_scheduler.LifecycleBatchRunner"
)

_STALE_NEXT_RUN = "2020-01-02T00:00:00+00:00"
_KNOWN_COMMIT = "abc1234567890"


# ---------------------------------------------------------------------------
# Shared test-infra helpers
# ---------------------------------------------------------------------------


def _full_schema_init(db_file: str) -> None:
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(db_file).initialize_database()
    with closing(sqlite3.connect(db_file)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT,
            progress_info TEXT,
            metadata TEXT,
            actor_username TEXT
        )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running')
              AND repo_alias IS NOT NULL"""
        )
        conn.commit()


def _seed_golden_repo(db_file: str, alias: str, clone_path: str) -> None:
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    GoldenRepoMetadataSqliteBackend(db_file).add_repo(
        alias=alias,
        repo_url=f"git@example.com:{alias}.git",
        default_branch="main",
        clone_path=clone_path,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _seed_tracking_row(db_file: str, alias: str) -> None:
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
    )

    now = datetime.now(timezone.utc).isoformat()
    DescriptionRefreshTrackingBackend(db_file).upsert_tracking(
        repo_alias=alias,
        status="pending",
        last_run="2020-01-01T00:00:00+00:00",
        next_run=_STALE_NEXT_RUN,
        last_known_commit=_KNOWN_COMMIT,
        created_at=now,
        updated_at=now,
    )


def _seed_meta_md(meta_dir: Path, alias: str) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / f"{alias}.md").write_text(
        "---\nlast_analyzed: 2020-01-01T00:00:00+00:00\ndescription: Test\n---\nBody.\n"
    )


def _make_mock_config(concurrency: int):
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    config = ServerConfig(server_dir="/tmp")
    config.claude_integration_config = ClaudeIntegrationConfig()
    config.claude_integration_config.max_concurrent_claude_cli = concurrency
    config.claude_integration_config.description_refresh_enabled = True
    config.claude_integration_config.description_refresh_interval_hours = 24
    return config


def _make_scheduler_with_concurrency(db_file: str, meta_dir: Path, concurrency: int):
    """Construct a scheduler with config-driven concurrency and all wiring slots filled."""
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )
    from code_indexer.server.services.job_tracker import JobTracker

    config = _make_mock_config(concurrency)
    mock_cfg = MagicMock()
    mock_cfg.load_config.return_value = config

    return DescriptionRefreshScheduler(
        db_path=db_file,
        config_manager=mock_cfg,
        claude_cli_manager=MagicMock(),
        meta_dir=meta_dir,
        job_tracker=JobTracker(db_file),
        lifecycle_invoker=MagicMock(),
        golden_repos_dir=meta_dir.parent / "golden",
        lifecycle_debouncer=MagicMock(),
        refresh_scheduler=MagicMock(),
    )


def _seed_runner_test_scenario(tmp_path: Path, alias: str, concurrency: int):
    """
    Seed the full test scenario for a LifecycleBatchRunner call-site test.

    Creates: initialised DB, golden repo row, tracking row, meta .md file,
    and a scheduler with config-driven concurrency.

    Returns (db_file, scheduler) ready for use.
    """
    db_file = str(tmp_path / f"{alias}.db")
    _full_schema_init(db_file)
    clone_path = tmp_path / "clone"
    clone_path.mkdir(exist_ok=True)
    _seed_golden_repo(db_file, alias, str(clone_path))
    _seed_tracking_row(db_file, alias)
    meta_dir = tmp_path / "meta"
    _seed_meta_md(meta_dir, alias)
    scheduler = _make_scheduler_with_concurrency(db_file, meta_dir, concurrency)
    return db_file, scheduler


# ---------------------------------------------------------------------------
# Tests for _get_lifecycle_concurrency
# ---------------------------------------------------------------------------


class TestGetLifecycleConcurrency:
    """_get_lifecycle_concurrency reads max_concurrent_claude_cli from config."""

    def test_get_lifecycle_concurrency_reads_from_config(self, tmp_path):
        """Should return max_concurrent_claude_cli from config (not the hardcoded 2)."""
        db_file = str(tmp_path / "test.db")
        _full_schema_init(db_file)
        scheduler = _make_scheduler_with_concurrency(
            db_file, tmp_path / "meta", concurrency=7
        )

        result = scheduler._get_lifecycle_concurrency()

        assert result == 7, f"Expected 7 (from config), got {result}"

    def test_get_lifecycle_concurrency_returns_default_when_config_none(self, tmp_path):
        """Should return ClaudeIntegrationConfig default when config_manager is None."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )
        from code_indexer.server.storage.sqlite_backends import (
            DescriptionRefreshTrackingBackend,
            GoldenRepoMetadataSqliteBackend,
        )
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        db_file = str(tmp_path / "test.db")
        _full_schema_init(db_file)

        scheduler = DescriptionRefreshScheduler(
            config_manager=None,
            tracking_backend=DescriptionRefreshTrackingBackend(db_file),
            golden_backend=GoldenRepoMetadataSqliteBackend(db_file),
        )

        result = scheduler._get_lifecycle_concurrency()

        expected = ClaudeIntegrationConfig().max_concurrent_claude_cli
        assert result == expected, f"Expected default {expected}, got {result}"


# ---------------------------------------------------------------------------
# Tests for concurrency kwarg at all 3 LifecycleBatchRunner call sites
# ---------------------------------------------------------------------------


class TestLifecycleBatchRunnerConcurrencyPropagation:
    """LifecycleBatchRunner must receive concurrency= from _get_lifecycle_concurrency."""

    @patch(_PATCH_RUNNER)
    def test_lifecycle_batch_runner_receives_config_driven_concurrency_at_single_alias_site(
        self, runner_cls, tmp_path
    ):
        """Single-alias refresh site must pass concurrency=config value to runner."""
        alias = "concurrency-test-repo"
        _, scheduler = _seed_runner_test_scenario(tmp_path, alias, concurrency=5)
        runner_cls.return_value.run = MagicMock()

        scheduler._run_lifecycle_via_batch_runner(alias, "test-job-id-001")

        runner_cls.assert_called_once()
        _, ctor_kwargs = runner_cls.call_args
        assert "concurrency" in ctor_kwargs, (
            "LifecycleBatchRunner must receive concurrency= kwarg at single-alias site"
        )
        assert ctor_kwargs["concurrency"] == 5, (
            f"Expected concurrency=5 from config, got {ctor_kwargs['concurrency']}"
        )

    @patch(_PATCH_RUNNER)
    def test_lifecycle_batch_runner_receives_config_driven_concurrency_at_description_backfill_site(
        self, runner_cls, tmp_path
    ):
        """Startup description backfill site must pass concurrency=config value to runner."""
        alias = "backfill-concurrency-repo"
        _, scheduler = _seed_runner_test_scenario(tmp_path, alias, concurrency=4)
        runner_cls.return_value.run = MagicMock()

        scheduler._run_description_backfill_async([alias])

        runner_cls.assert_called_once()
        _, ctor_kwargs = runner_cls.call_args
        assert "concurrency" in ctor_kwargs, (
            "Description backfill LifecycleBatchRunner must receive concurrency= kwarg"
        )
        assert ctor_kwargs["concurrency"] == 4, (
            f"Expected concurrency=4 from config, got {ctor_kwargs['concurrency']}"
        )

    @patch(_PATCH_RUNNER)
    def test_lifecycle_batch_runner_receives_config_driven_concurrency_at_lifecycle_backfill_site(
        self, runner_cls, tmp_path
    ):
        """Lifecycle backfill site must pass concurrency=config value to runner."""
        alias = "lc-backfill-concurrency-repo"
        _, scheduler = _seed_runner_test_scenario(tmp_path, alias, concurrency=6)
        runner_cls.return_value.run = MagicMock()

        scheduler._run_lifecycle_backfill_async([alias])

        runner_cls.assert_called_once()
        _, ctor_kwargs = runner_cls.call_args
        assert "concurrency" in ctor_kwargs, (
            "Lifecycle backfill LifecycleBatchRunner must receive concurrency= kwarg"
        )
        assert ctor_kwargs["concurrency"] == 6, (
            f"Expected concurrency=6 from config, got {ctor_kwargs['concurrency']}"
        )
