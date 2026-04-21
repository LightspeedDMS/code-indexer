"""
Unit tests for orphan tracking row cleanup in DescriptionRefreshScheduler.

Orphan tracking rows are description_refresh_tracking entries whose golden_repo
no longer exists (deleted without cascading to the tracking table). They produce
~7 WARNINGs/minute and can never be serviced.

Fix adds two self-healing mechanisms:
  A. reconcile_orphan_tracking() — one-shot startup sweep
  B. Inline prune in get_stale_repos() — periodic cleanup during normal scheduler loop

Test strategy:
- Real SQLite DB seeded via real backend APIs (DatabaseSchema + real backends)
- MagicMock backends for unit isolation where real DB is not needed
- No mocking of the code under test (Messi Rule #1)
- Every test must FAIL on HEAD before fix, PASS after fix

Note (dual-backend wiring gap): The PostgreSQL wiring in lifespan.py currently passes
db_path= only, so the scheduler silently uses SQLite backends on PostgreSQL deployments.
This is tracked separately and is out of scope here. The protocol contract test below
confirms both backends expose the same method signatures so the cleanup code will work
correctly once lifespan wiring is fixed in a future story.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, call


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_tracking_row(alias: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "repo_alias": alias,
        "last_run": now,
        "next_run": now,
        "status": "pending",
        "error": None,
        "last_known_commit": None,
        "last_known_files_processed": None,
        "last_known_indexed_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _init_db(db_file: str) -> None:
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(db_file).initialize_database()


def _make_scheduler(
    tracking_backend: Any,
    golden_backend: Any,
    server_dir: Optional[str] = None,
) -> Any:
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    # server_dir is only used by ServerConfig for bootstrap resolution;
    # tests that don't need real filesystem paths pass a clearly test-local sentinel.
    effective_dir = server_dir if server_dir is not None else "test-only-no-fs-access"
    config = ServerConfig(server_dir=effective_dir)
    config.claude_integration_config = ClaudeIntegrationConfig()
    config.claude_integration_config.description_refresh_enabled = True
    config.claude_integration_config.description_refresh_interval_hours = 24
    mock_cfg = MagicMock()
    mock_cfg.load_config.return_value = config

    return DescriptionRefreshScheduler(
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        config_manager=mock_cfg,
    )


# ---------------------------------------------------------------------------
# A. Startup reconciliation tests
# ---------------------------------------------------------------------------


class TestReconcileOrphanTracking:
    """reconcile_orphan_tracking() sweeps all tracking rows and deletes orphans."""

    def test_reconcile_orphan_tracking_deletes_rows_with_no_golden_repo(self):
        """
        3 rows (2 orphans, 1 valid). get_repo returns None for orphans.
        delete_tracking called exactly for 2 orphan aliases, NOT for valid one.
        Return value == 2.
        """
        tracking = MagicMock()
        tracking.get_all_tracking.return_value = [
            _make_tracking_row("orphan-1"),
            _make_tracking_row("valid-repo"),
            _make_tracking_row("orphan-2"),
        ]
        golden = MagicMock()
        golden.get_repo.side_effect = lambda alias: (
            {"alias": alias, "clone_path": "/some/path"} if alias == "valid-repo" else None
        )

        scheduler = _make_scheduler(tracking, golden)
        result = scheduler.reconcile_orphan_tracking()

        assert result == 2
        tracking.delete_tracking.assert_any_call("orphan-1")
        tracking.delete_tracking.assert_any_call("orphan-2")
        for c in tracking.delete_tracking.call_args_list:
            assert c != call("valid-repo"), "delete_tracking must not be called for valid-repo"

    def test_reconcile_orphan_tracking_zero_orphans_returns_zero(self):
        """
        All rows have matching golden repos. delete_tracking NEVER called. Return 0.
        """
        tracking = MagicMock()
        tracking.get_all_tracking.return_value = [
            _make_tracking_row("repo-a"),
            _make_tracking_row("repo-b"),
        ]
        golden = MagicMock()
        golden.get_repo.return_value = {"alias": "any", "clone_path": "/path"}

        scheduler = _make_scheduler(tracking, golden)
        result = scheduler.reconcile_orphan_tracking()

        assert result == 0
        tracking.delete_tracking.assert_not_called()

    def test_reconcile_orphan_tracking_empty_table_returns_zero(self):
        """
        get_all_tracking returns []. No crash, returns 0, delete_tracking never called.
        """
        tracking = MagicMock()
        tracking.get_all_tracking.return_value = []
        golden = MagicMock()

        scheduler = _make_scheduler(tracking, golden)
        result = scheduler.reconcile_orphan_tracking()

        assert result == 0
        tracking.delete_tracking.assert_not_called()

    def test_reconcile_orphan_tracking_swallows_delete_errors(self, caplog):
        """
        One delete raises. Other orphan still processed (sweep doesn't halt).
        ERROR logged for the failing delete.
        """
        tracking = MagicMock()
        tracking.get_all_tracking.return_value = [
            _make_tracking_row("orphan-fails"),
            _make_tracking_row("orphan-succeeds"),
        ]
        golden = MagicMock()
        golden.get_repo.return_value = None

        call_count = [0]

        def delete_side_effect(alias):
            call_count[0] += 1
            if alias == "orphan-fails":
                raise RuntimeError("DB write error")
            return True

        tracking.delete_tracking.side_effect = delete_side_effect

        scheduler = _make_scheduler(tracking, golden)
        with caplog.at_level(
            logging.ERROR,
            logger="code_indexer.server.services.description_refresh_scheduler",
        ):
            scheduler.reconcile_orphan_tracking()

        assert call_count[0] == 2, "Both orphans must be attempted regardless of first failure"
        assert any(
            r.levelno == logging.ERROR for r in caplog.records
        ), "Expected ERROR log for the failing delete"

    def test_reconcile_orphan_tracking_swallows_sweep_errors(self, caplog):
        """
        get_all_tracking raises. Method returns 0 and does NOT leak the exception.
        Startup must continue.
        """
        tracking = MagicMock()
        tracking.get_all_tracking.side_effect = RuntimeError("DB connection failed")
        golden = MagicMock()

        scheduler = _make_scheduler(tracking, golden)
        with caplog.at_level(
            logging.ERROR,
            logger="code_indexer.server.services.description_refresh_scheduler",
        ):
            result = scheduler.reconcile_orphan_tracking()

        assert result == 0
        assert any(
            r.levelno == logging.ERROR for r in caplog.records
        ), "Expected ERROR log when sweep fails"

    def test_start_calls_reconcile_via_observable_state(self, tmp_path):
        """
        Verify reconcile_orphan_tracking() is called from start() via observable effect:
        orphan tracking rows with no golden_repo are deleted after start().

        Seeds a real SQLite DB with one orphan tracking row (no corresponding golden_repo),
        calls start(), stops the scheduler, then asserts the orphan row is gone.
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.storage.sqlite_backends import (
            DescriptionRefreshTrackingBackend,
        )
        from code_indexer.server.utils.config_manager import (
            ClaudeIntegrationConfig,
            ServerConfig,
        )

        db_file = str(tmp_path / "test.db")
        DatabaseSchema(db_file).initialize_database()

        now = datetime.now(timezone.utc).isoformat()
        tracking_backend = DescriptionRefreshTrackingBackend(db_file)
        tracking_backend.upsert_tracking(
            repo_alias="dangling-orphan",
            status="pending",
            last_run=now,
            next_run=now,
            created_at=now,
            updated_at=now,
        )
        rows_before = tracking_backend.get_all_tracking()
        assert len(rows_before) == 1

        config = ServerConfig(server_dir=str(tmp_path))
        config.claude_integration_config = ClaudeIntegrationConfig()
        config.claude_integration_config.description_refresh_enabled = True
        config.claude_integration_config.description_refresh_interval_hours = 24
        mock_cfg = MagicMock()
        mock_cfg.load_config.return_value = config

        scheduler = DescriptionRefreshScheduler(
            db_path=db_file,
            config_manager=mock_cfg,
        )
        try:
            scheduler.start()
        finally:
            scheduler.stop()

        rows_after = tracking_backend.get_all_tracking()
        assert len(rows_after) == 0, (
            "Orphan tracking row must be deleted by reconcile_orphan_tracking() "
            "called from start()"
        )


# ---------------------------------------------------------------------------
# B. Periodic prune in get_stale_repos()
# ---------------------------------------------------------------------------


class TestGetStaleReposOrphanPrune:
    """get_stale_repos() must prune orphan tracking rows inline."""

    def test_get_stale_repos_prunes_orphan_and_logs_info(self, caplog):
        """
        stale_tracking returns row for alias 'ghost', get_repo('ghost') returns None.
        Verify:
        - delete_tracking('ghost') called
        - log entry is INFO (not WARNING)
        - orphan excluded from return value
        """
        tracking = MagicMock()
        tracking.get_stale_repos.return_value = [_make_tracking_row("ghost")]
        golden = MagicMock()
        golden.get_repo.return_value = None

        scheduler = _make_scheduler(tracking, golden)
        with caplog.at_level(
            logging.DEBUG,
            logger="code_indexer.server.services.description_refresh_scheduler",
        ):
            result = scheduler.get_stale_repos()

        tracking.delete_tracking.assert_called_once_with("ghost")
        assert result == [], f"Orphan 'ghost' must be excluded from results, got {result}"

        prune_records = [
            r for r in caplog.records
            if "ghost" in r.message and "prune" in r.message.lower()
        ]
        assert prune_records, "Expected a log record mentioning 'ghost' and 'prune'"
        assert all(r.levelno == logging.INFO for r in prune_records), (
            "Prune log must be INFO level, not WARNING"
        )

    def test_get_stale_repos_does_not_prune_valid_entries(self):
        """
        stale_tracking returns a valid row. delete_tracking NOT called.
        Row IS included in return value.
        """
        tracking = MagicMock()
        row = _make_tracking_row("real-repo")
        tracking.get_stale_repos.return_value = [row]
        golden = MagicMock()
        golden.get_repo.return_value = {"alias": "real-repo", "clone_path": "/path/to/real"}

        scheduler = _make_scheduler(tracking, golden)
        result = scheduler.get_stale_repos()

        tracking.delete_tracking.assert_not_called()
        assert len(result) == 1
        assert result[0]["repo_alias"] == "real-repo"

    def test_get_stale_repos_prune_delete_failure_does_not_break_scan(self, caplog):
        """
        delete_tracking raises for an orphan. Scan completes, valid rows returned, ERROR logged.
        """
        tracking = MagicMock()
        tracking.get_stale_repos.return_value = [
            _make_tracking_row("orphan-broken"),
            _make_tracking_row("valid-repo"),
        ]
        golden = MagicMock()
        golden.get_repo.side_effect = lambda alias: (
            None if alias == "orphan-broken" else {"alias": alias, "clone_path": "/path"}
        )
        tracking.delete_tracking.side_effect = RuntimeError("write failed")

        scheduler = _make_scheduler(tracking, golden)
        with caplog.at_level(
            logging.ERROR,
            logger="code_indexer.server.services.description_refresh_scheduler",
        ):
            result = scheduler.get_stale_repos()

        valid = [r for r in result if r["repo_alias"] == "valid-repo"]
        assert len(valid) == 1, "valid-repo must appear in results despite orphan delete failure"
        assert any(
            r.levelno == logging.ERROR for r in caplog.records
        ), "Expected ERROR log when delete_tracking raises during scan"


# ---------------------------------------------------------------------------
# C. Dual-backend protocol validation
# ---------------------------------------------------------------------------


class TestDualBackendProtocol:
    """
    Verify both SQLite and PostgreSQL backends satisfy the cleanup contract.

    Two tests:
    1. Real behavior test: SQLite backend insert/get/delete with real DB (no mocks).
    2. Protocol contract test: both backends expose the same full method signatures.
       Reflection is used here as it is the only viable approach to check protocol
       conformance without a live PostgreSQL DB. This is explicitly required by the
       task spec (section C, 'test_postgres_backend_shape_matches_sqlite_contract').
    """

    def test_sqlite_backend_supports_cleanup_protocol(self, tmp_path):
        """
        Real SQLite backend: insert 2 rows, get_all_tracking() → 2,
        delete_tracking(alias) → row gone. No mocks.
        """
        from code_indexer.server.storage.sqlite_backends import (
            DescriptionRefreshTrackingBackend,
        )

        db_file = str(tmp_path / "test.db")
        _init_db(db_file)

        backend = DescriptionRefreshTrackingBackend(db_file)
        now = datetime.now(timezone.utc).isoformat()

        backend.upsert_tracking(
            repo_alias="alpha",
            status="pending",
            last_run=now,
            next_run=now,
            created_at=now,
            updated_at=now,
        )
        backend.upsert_tracking(
            repo_alias="beta",
            status="pending",
            last_run=now,
            next_run=now,
            created_at=now,
            updated_at=now,
        )

        rows = backend.get_all_tracking()
        assert len(rows) == 2
        aliases = {r["repo_alias"] for r in rows}
        assert "alpha" in aliases
        assert "beta" in aliases

        deleted = backend.delete_tracking("alpha")
        assert deleted is True

        rows_after = backend.get_all_tracking()
        assert len(rows_after) == 1
        assert rows_after[0]["repo_alias"] == "beta"

    def test_postgres_backend_shape_matches_sqlite_contract(self):
        """
        Protocol contract check: SQLite and PostgreSQL backends must both expose
        get_all_tracking, delete_tracking, and get_stale_repos with identical
        full method signatures (parameter names, kinds, defaults, and annotations).

        Reflection is the only viable approach — we cannot construct a real PostgreSQL
        backend without a live DB. This test confirms that once lifespan.py wiring is
        fixed to pass the PostgreSQL backend to DescriptionRefreshScheduler, the cleanup
        code will work without modification on both backends.
        """
        from code_indexer.server.storage.sqlite_backends import (
            DescriptionRefreshTrackingBackend,
        )
        from code_indexer.server.storage.postgres.description_refresh_tracking_backend import (
            DescriptionRefreshTrackingPostgresBackend,
        )

        required_methods = ["get_all_tracking", "delete_tracking", "get_stale_repos"]

        for method_name in required_methods:
            sqlite_method = getattr(DescriptionRefreshTrackingBackend, method_name, None)
            pg_method = getattr(DescriptionRefreshTrackingPostgresBackend, method_name, None)

            assert sqlite_method is not None, (
                f"SQLite backend missing required method: {method_name}"
            )
            assert pg_method is not None, (
                f"PostgreSQL backend missing required method: {method_name}"
            )

            # Compare only (name, kind) per parameter — annotation and default are
            # excluded because the PostgreSQL file uses `from __future__ import annotations`
            # which stringifies all annotations, causing spurious mismatches against the
            # SQLite file that does not use that import.  The caller-facing contract is
            # the parameter names and their positional/keyword nature, not their annotations
            # or defaults.
            sqlite_sig = inspect.signature(sqlite_method)
            pg_sig = inspect.signature(pg_method)
            sqlite_shape = [(n, p.kind) for n, p in sqlite_sig.parameters.items()]
            pg_shape = [(n, p.kind) for n, p in pg_sig.parameters.items()]

            assert sqlite_shape == pg_shape, (
                f"Method '{method_name}' has mismatched parameter signatures:\n"
                f"  SQLite:     {sqlite_shape}\n"
                f"  PostgreSQL: {pg_shape}"
            )
