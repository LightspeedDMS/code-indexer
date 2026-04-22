"""
Tests for Story #876 Phase B-2 Deliverable 3 — relocate the description-refresh
caller so it fires LifecycleBatchRunner unconditionally on every refresh event.

Contract under test (D3 behaviour of DescriptionRefreshScheduler):
  1. `_run_loop_single_pass` no longer consults the has_changes_since_last_run
     gate or the lifecycle_schema_version backfill gate; every stale repo is
     handed to LifecycleBatchRunner.run([alias], parent_job_id=<job>).
  2. LifecycleBatchRunner is constructed with the four wired collaborators
     (job_tracker, refresh_scheduler, debouncer, claude_cli_invoker) plus
     golden_repos_dir.
  3. When any wiring slot is missing (None), the scheduler logs a WARNING
     and does NOT instantiate a runner — Messi Rule #2 anti-fallback.
  4. When runner.run raises, the scheduler calls
     job_tracker.fail_job(job_id, error=<str(exc)>) and does NOT re-raise
     (sidecar discipline — keeps the scheduler alive for subsequent repos).

Mock boundary map:
  REAL   : DescriptionRefreshScheduler (with real SQLite backends seeded via
           the same _seed_* helpers used by test_description_refresh_scheduler_*
           suites), REAL DescriptionRefreshTrackingBackend + GoldenRepoMetadataSqliteBackend.
  MOCKED : LifecycleBatchRunner — patched at the scheduler module's USE-SITE
           (_PATCH_RUNNER) never at its import source; mirrors the D2/D4 pattern.
  STUBBED: lifecycle_invoker/lifecycle_debouncer/refresh_scheduler — Mock
           stand-ins passed into __init__; the scheduler forwards them to the
           runner ctor.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker


# Patch LifecycleBatchRunner at its USE-SITE inside the scheduler module,
# never at its import source. Mirrors D2/D4 proven pattern.
_PATCH_RUNNER = (
    "code_indexer.server.services.description_refresh_scheduler.LifecycleBatchRunner"
)

_KNOWN_COMMIT = "abc1234567890"
_STALE_NEXT_RUN = "2020-01-02T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Test-infra helpers — each under 20 lines, parallel to the D2/D4 patterns.
# ---------------------------------------------------------------------------


@pytest.fixture
def atomic_db_path(tmp_path):
    """Create SQLite DB with the schema+partial-unique-index background_jobs needs."""
    db = tmp_path / "test_d3.db"
    with closing(sqlite3.connect(str(db))) as conn:
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
            metadata TEXT
        )"""
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running')
              AND repo_alias IS NOT NULL
            """
        )
        conn.commit()
    return str(db)


@pytest.fixture
def real_job_tracker(atomic_db_path):
    return JobTracker(atomic_db_path)


def _seed_golden_repo(db_file: str, alias: str, clone_path: str) -> None:
    """Insert golden repo row pointing to a real on-disk clone path."""
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


def _seed_tracking_row(db_file: str, alias: str, lifecycle_version: int) -> None:
    """Insert pending tracking row with stale next_run + given lifecycle schema version."""
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
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "UPDATE description_refresh_tracking SET lifecycle_schema_version=? WHERE repo_alias=?",
            (lifecycle_version, alias),
        )


def _seed_clone_metadata(clone_dir: Path) -> None:
    """Create .code-indexer/metadata.json with matching commit (no changes detected)."""
    inner = clone_dir / ".code-indexer"
    inner.mkdir(parents=True, exist_ok=True)
    (inner / "metadata.json").write_text(json.dumps({"current_commit": _KNOWN_COMMIT}))


def _seed_meta_md(meta_dir: Path, alias: str) -> None:
    """Create cidx-meta/<alias>.md with valid frontmatter for RepoAnalyzer."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / f"{alias}.md").write_text(
        "---\nlast_analyzed: 2020-01-01T00:00:00+00:00\ndescription: Test\n---\nBody.\n"
    )


def _full_schema_init(db_file: str) -> None:
    """Initialise every table the scheduler touches (golden_repos_metadata + description_refresh_tracking + background_jobs schema + index)."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(db_file).initialize_database()
    # DatabaseSchema covers golden_repos_metadata + description_refresh_tracking;
    # overlay the background_jobs unique index for atomicity-dependent flows.
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
            metadata TEXT
        )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running')
              AND repo_alias IS NOT NULL"""
        )
        conn.commit()


def _make_scheduler(
    db_file: str,
    meta_dir: Path,
    *,
    job_tracker,
    lifecycle_invoker=None,
    golden_repos_dir=None,
    lifecycle_debouncer=None,
    refresh_scheduler=None,
):
    """Construct the scheduler with D3 wiring kwargs exposed."""
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    config = ServerConfig(server_dir=str(Path(db_file).parent))
    config.claude_integration_config = ClaudeIntegrationConfig()
    config.claude_integration_config.description_refresh_enabled = True
    config.claude_integration_config.description_refresh_interval_hours = 24
    mock_cfg = MagicMock()
    mock_cfg.load_config.return_value = config
    return DescriptionRefreshScheduler(
        db_path=db_file,
        config_manager=mock_cfg,
        claude_cli_manager=MagicMock(),
        meta_dir=meta_dir,
        job_tracker=job_tracker,
        lifecycle_invoker=lifecycle_invoker,
        golden_repos_dir=golden_repos_dir,
        lifecycle_debouncer=lifecycle_debouncer,
        refresh_scheduler=refresh_scheduler,
    )


def _run_single_pass_synchronously(scheduler) -> None:
    """
    Execute a single scheduler pass with the executor shimmed to run inline so
    refresh_task fires in-process. Waits for all submissions before returning.
    """
    import concurrent.futures

    class _InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fut: concurrent.futures.Future = concurrent.futures.Future()
            try:
                fut.set_result(fn(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001 — test shim mirrors thread pool semantics
                fut.set_exception(exc)
            return fut

        def shutdown(self, wait=True):
            return None

    scheduler._executor = _InlineExecutor()
    scheduler._run_loop_single_pass()


# ---------------------------------------------------------------------------
# Tests — D3 contract
# ---------------------------------------------------------------------------


class TestLifecycleRunnerInvocation:
    """Every stale repo routed by the scheduler must reach LifecycleBatchRunner."""

    @patch(_PATCH_RUNNER)
    def test_refresh_calls_lifecycle_batch_runner_with_single_alias_and_parent_job_id(
        self, runner_cls, tmp_path, atomic_db_path, real_job_tracker
    ):
        """Happy path: runner ctor receives wired deps; run() gets [alias] and parent_job_id."""
        _full_schema_init(atomic_db_path)
        alias = "happy-repo"
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        _seed_golden_repo(atomic_db_path, alias, str(clone_path))
        _seed_tracking_row(atomic_db_path, alias, lifecycle_version=1)
        _seed_clone_metadata(clone_path)
        meta_dir = tmp_path / "cidx-meta"
        _seed_meta_md(meta_dir, alias)

        invoker = MagicMock()
        debouncer = MagicMock()
        refresh_scheduler = MagicMock()
        golden_repos_dir = tmp_path / "golden"
        scheduler = _make_scheduler(
            atomic_db_path,
            meta_dir,
            job_tracker=real_job_tracker,
            lifecycle_invoker=invoker,
            golden_repos_dir=golden_repos_dir,
            lifecycle_debouncer=debouncer,
            refresh_scheduler=refresh_scheduler,
        )
        runner_cls.return_value.run = MagicMock()

        _run_single_pass_synchronously(scheduler)

        runner_cls.assert_called_once()
        _args, ctor_kwargs = runner_cls.call_args
        assert ctor_kwargs["job_tracker"] is real_job_tracker
        assert ctor_kwargs["refresh_scheduler"] is refresh_scheduler
        assert ctor_kwargs["debouncer"] is debouncer
        assert ctor_kwargs["claude_cli_invoker"] is invoker
        assert Path(ctor_kwargs["golden_repos_dir"]) == golden_repos_dir

        runner_cls.return_value.run.assert_called_once()
        run_args, run_kwargs = runner_cls.return_value.run.call_args
        aliases = run_args[0] if run_args else run_kwargs["repo_aliases"]
        assert aliases == [alias]
        assert run_kwargs["parent_job_id"].startswith(f"desc-refresh-{alias}-")


class TestUnconditionalFiring:
    """The has_changes and lifecycle_backfill gates must no longer suppress refresh."""

    @patch(_PATCH_RUNNER)
    def test_refresh_fires_unconditionally_when_no_changes_detected(
        self, runner_cls, tmp_path, atomic_db_path, real_job_tracker
    ):
        """
        Repo metadata.json has matching current_commit (no code changes) AND
        lifecycle_schema_version is current. Old code path would skip —
        post-D3, runner MUST fire.
        """
        from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION

        _full_schema_init(atomic_db_path)
        alias = "no-changes-repo"
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        _seed_golden_repo(atomic_db_path, alias, str(clone_path))
        _seed_tracking_row(
            atomic_db_path, alias, lifecycle_version=LIFECYCLE_SCHEMA_VERSION
        )
        _seed_clone_metadata(clone_path)  # matching commit => no changes
        meta_dir = tmp_path / "cidx-meta"
        _seed_meta_md(meta_dir, alias)

        scheduler = _make_scheduler(
            atomic_db_path,
            meta_dir,
            job_tracker=real_job_tracker,
            lifecycle_invoker=MagicMock(),
            golden_repos_dir=tmp_path / "golden",
            lifecycle_debouncer=MagicMock(),
            refresh_scheduler=MagicMock(),
        )
        runner_cls.return_value.run = MagicMock()

        _run_single_pass_synchronously(scheduler)

        runner_cls.assert_called_once()
        runner_cls.return_value.run.assert_called_once()

    @patch(_PATCH_RUNNER)
    def test_refresh_fires_unconditionally_when_lifecycle_schema_current(
        self, runner_cls, tmp_path, atomic_db_path, real_job_tracker
    ):
        """
        lifecycle_schema_version == LIFECYCLE_SCHEMA_VERSION (no backfill
        needed) AND code changes exist. Old backfill-only path would have
        been taken. Post-D3, runner fires regardless of backfill need.
        """
        from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION

        _full_schema_init(atomic_db_path)
        alias = "schema-current-repo"
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        _seed_golden_repo(atomic_db_path, alias, str(clone_path))
        _seed_tracking_row(
            atomic_db_path, alias, lifecycle_version=LIFECYCLE_SCHEMA_VERSION
        )
        meta_dir = tmp_path / "cidx-meta"
        _seed_meta_md(meta_dir, alias)
        # No clone metadata.json => has_changes returns True, orthogonal to the gate removal.

        scheduler = _make_scheduler(
            atomic_db_path,
            meta_dir,
            job_tracker=real_job_tracker,
            lifecycle_invoker=MagicMock(),
            golden_repos_dir=tmp_path / "golden",
            lifecycle_debouncer=MagicMock(),
            refresh_scheduler=MagicMock(),
        )
        runner_cls.return_value.run = MagicMock()

        _run_single_pass_synchronously(scheduler)

        runner_cls.assert_called_once()
        runner_cls.return_value.run.assert_called_once()


class TestAntiFallbackWiringGuards:
    """Missing wiring MUST surface as WARNING log + skip — never silent no-op."""

    @patch(_PATCH_RUNNER)
    def test_refresh_skips_runner_when_lifecycle_invoker_not_wired(
        self, runner_cls, tmp_path, atomic_db_path, real_job_tracker, caplog
    ):
        _full_schema_init(atomic_db_path)
        alias = "unwired-invoker-repo"
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        _seed_golden_repo(atomic_db_path, alias, str(clone_path))
        _seed_tracking_row(atomic_db_path, alias, lifecycle_version=1)
        meta_dir = tmp_path / "cidx-meta"
        _seed_meta_md(meta_dir, alias)

        scheduler = _make_scheduler(
            atomic_db_path,
            meta_dir,
            job_tracker=real_job_tracker,
            lifecycle_invoker=None,  # <-- wiring hole
            golden_repos_dir=tmp_path / "golden",
            lifecycle_debouncer=MagicMock(),
            refresh_scheduler=MagicMock(),
        )

        with caplog.at_level(logging.WARNING):
            _run_single_pass_synchronously(scheduler)

        runner_cls.assert_not_called()
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("lifecycle" in m.lower() for m in warning_messages), (
            f"expected a WARNING about missing lifecycle wiring, got: {warning_messages}"
        )

    @patch(_PATCH_RUNNER)
    def test_refresh_skips_runner_when_golden_repos_dir_not_wired(
        self, runner_cls, tmp_path, atomic_db_path, real_job_tracker, caplog
    ):
        _full_schema_init(atomic_db_path)
        alias = "unwired-dir-repo"
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        _seed_golden_repo(atomic_db_path, alias, str(clone_path))
        _seed_tracking_row(atomic_db_path, alias, lifecycle_version=1)
        meta_dir = tmp_path / "cidx-meta"
        _seed_meta_md(meta_dir, alias)

        scheduler = _make_scheduler(
            atomic_db_path,
            meta_dir,
            job_tracker=real_job_tracker,
            lifecycle_invoker=MagicMock(),
            golden_repos_dir=None,  # <-- wiring hole
            lifecycle_debouncer=MagicMock(),
            refresh_scheduler=MagicMock(),
        )

        with caplog.at_level(logging.WARNING):
            _run_single_pass_synchronously(scheduler)

        runner_cls.assert_not_called()
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("lifecycle" in m.lower() for m in warning_messages), (
            f"expected a WARNING about missing lifecycle wiring, got: {warning_messages}"
        )


class TestRunnerExceptionForwarding:
    """runner.run raises -> job_tracker.fail_job is called; no re-raise."""

    @patch(_PATCH_RUNNER)
    def test_refresh_forwards_runner_exception_to_job_tracker_fail_job(
        self, runner_cls, tmp_path, atomic_db_path, real_job_tracker
    ):
        """
        Sidecar discipline: the scheduler thread-pool shim observes no unhandled
        exception, and the tracked job row reflects status='failed' with the
        runner's error string captured.
        """
        _full_schema_init(atomic_db_path)
        alias = "boom-repo"
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        _seed_golden_repo(atomic_db_path, alias, str(clone_path))
        _seed_tracking_row(atomic_db_path, alias, lifecycle_version=1)
        meta_dir = tmp_path / "cidx-meta"
        _seed_meta_md(meta_dir, alias)

        synthetic_error = RuntimeError("fleet scan boom")
        runner_cls.return_value.run = MagicMock(side_effect=synthetic_error)

        scheduler = _make_scheduler(
            atomic_db_path,
            meta_dir,
            job_tracker=real_job_tracker,
            lifecycle_invoker=MagicMock(),
            golden_repos_dir=tmp_path / "golden",
            lifecycle_debouncer=MagicMock(),
            refresh_scheduler=MagicMock(),
        )

        _run_single_pass_synchronously(scheduler)

        # Locate the tracked job by repo_alias and verify it landed as 'failed'
        # with the error captured.  No exception propagated to the test — the
        # scheduler swallowed it per sidecar discipline.
        with closing(sqlite3.connect(atomic_db_path)) as conn:
            rows = conn.execute(
                "SELECT status, error FROM background_jobs WHERE repo_alias = ?",
                (alias,),
            ).fetchall()
        assert len(rows) == 1, f"expected one job row for {alias}, got {rows}"
        status, error = rows[0]
        assert status == "failed", f"expected status=failed, got {status}"
        assert error and "fleet scan boom" in error, (
            f"expected synthetic error captured in job row, got {error!r}"
        )
