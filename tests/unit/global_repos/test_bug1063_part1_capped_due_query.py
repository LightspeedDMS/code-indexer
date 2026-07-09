"""
Tests for Bug #1063 Part 1: Capped oldest-first due-query for RefreshScheduler.

Covers:
- GlobalRegistry.list_due_repos() SQLite backend: returns only due repos,
  ordered by CAST(next_refresh AS REAL) ASC (numeric ordering), capped at limit.
- GlobalRegistry.list_due_repos() JSON fallback: same semantics.
- BackgroundJobsConfig.max_concurrent_refresh_jobs new field with correct default.
- BackgroundJobManager.count_active_refresh_jobs() counts pending+running refresh jobs.
- _scheduler_loop submits at most N repos per cycle (N = budget - active).
"""

import time

from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sqlite_registry(tmp_path):
    """Create a SQLite-backed GlobalRegistry with fully initialized schema."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    golden_dir = tmp_path / "golden"
    golden_dir.mkdir(parents=True, exist_ok=True)
    # db_path is sibling of golden_repos_dir (matches production layout)
    db_path = str(tmp_path / "cidx_server.db")

    # Initialize full schema (creates global_repos table + all migrations)
    DatabaseSchema(db_path).initialize_database()

    return GlobalRegistry(
        golden_repos_dir=str(golden_dir),
        use_sqlite=True,
        db_path=db_path,
    )


def _make_json_registry(tmp_path):
    return GlobalRegistry(
        golden_repos_dir=str(tmp_path / "golden_json"),
    )


def _register_repo(registry, alias, next_refresh=None):
    """Register a test repo, then optionally set its next_refresh."""
    registry.register_global_repo(
        repo_name=alias.removesuffix("-global"),
        alias_name=alias,
        repo_url=f"https://example.com/{alias}.git",
        index_path=f"/tmp/{alias}",
    )
    if next_refresh is not None:
        registry.update_next_refresh(alias, next_refresh)


# ===========================================================================
# Part 1A: list_due_repos() — SQLite backend
# ===========================================================================


class TestListDueReposSQLite:
    """GlobalRegistry.list_due_repos() with SQLite backend."""

    def test_returns_empty_when_no_repos(self, tmp_path):
        reg = _make_sqlite_registry(tmp_path)
        result = reg.list_due_repos(limit=10, now=time.time())
        assert result == []

    def test_returns_only_due_repos(self, tmp_path):
        reg = _make_sqlite_registry(tmp_path)
        now = time.time()
        _register_repo(reg, "due-repo-global", next_refresh=now - 60)  # overdue
        _register_repo(reg, "future-repo-global", next_refresh=now + 3600)  # not due
        _register_repo(
            reg, "null-repo-global", next_refresh=None
        )  # unscheduled = not due

        result = reg.list_due_repos(limit=10, now=now)

        aliases = [r["alias_name"] for r in result]
        assert "due-repo-global" in aliases
        assert "future-repo-global" not in aliases
        assert "null-repo-global" not in aliases

    def test_returns_oldest_first_numeric_ordering(self, tmp_path):
        """Ordering must be numeric (CAST AS REAL), not lexicographic string ordering."""
        reg = _make_sqlite_registry(tmp_path)
        now = time.time()
        # These timestamps would sort differently as strings vs numbers
        # e.g. "1700000010" < "999999999" lexicographically but 1700000010 > 999999999 numerically
        old = now - 300  # oldest (due)
        mid = now - 200  # middle (due)
        new = now - 100  # newest due (still due but most recent)

        _register_repo(reg, "mid-repo-global", next_refresh=mid)
        _register_repo(reg, "new-repo-global", next_refresh=new)
        _register_repo(reg, "old-repo-global", next_refresh=old)

        result = reg.list_due_repos(limit=10, now=now)
        aliases = [r["alias_name"] for r in result]

        # oldest first
        assert aliases.index("old-repo-global") < aliases.index("mid-repo-global")
        assert aliases.index("mid-repo-global") < aliases.index("new-repo-global")

    def test_cap_limits_results_to_n(self, tmp_path):
        """list_due_repos(limit=N) returns at most N repos even with more overdue."""
        reg = _make_sqlite_registry(tmp_path)
        now = time.time()
        for i in range(10):
            _register_repo(reg, f"repo-{i}-global", next_refresh=now - (i + 1) * 60)

        result = reg.list_due_repos(limit=3, now=now)
        assert len(result) == 3

    def test_cap_of_zero_returns_empty(self, tmp_path):
        reg = _make_sqlite_registry(tmp_path)
        now = time.time()
        _register_repo(reg, "due-repo-global", next_refresh=now - 60)

        result = reg.list_due_repos(limit=0, now=now)
        assert result == []

    def test_numeric_ordering_not_lexicographic(self, tmp_path):
        """Verify CAST ordering: timestamps that differ in leading digit sort correctly."""
        reg = _make_sqlite_registry(tmp_path)
        # Use timestamps where string sort and numeric sort would disagree
        # e.g. "9" < "10" is False as strings but 9 < 10 is True as numbers
        # Use float timestamps: 1000000000.5 vs 999999999.9
        # As strings: "1000000000.5" < "999999999.9"  (False — "1" < "9" as chars)
        # As numbers:  1000000000.5  >  999999999.9   (correct numeric comparison)
        t_smaller = 999999999.9  # truly smaller number
        t_larger = 1000000000.5  # truly larger number
        now = t_larger + 60  # both are in the past

        _register_repo(reg, "larger-ts-global", next_refresh=t_larger)
        _register_repo(reg, "smaller-ts-global", next_refresh=t_smaller)

        result = reg.list_due_repos(limit=10, now=now)
        aliases = [r["alias_name"] for r in result]

        # smaller_ts is older (smaller number) → should come FIRST
        assert aliases.index("smaller-ts-global") < aliases.index("larger-ts-global")

    def test_returns_full_repo_dicts(self, tmp_path):
        """Result dicts must contain alias_name and other standard fields."""
        reg = _make_sqlite_registry(tmp_path)
        now = time.time()
        _register_repo(reg, "test-repo-global", next_refresh=now - 60)

        result = reg.list_due_repos(limit=10, now=now)
        assert len(result) == 1
        repo = result[0]
        assert repo["alias_name"] == "test-repo-global"
        assert "repo_url" in repo
        assert "next_refresh" in repo


# ===========================================================================
# Part 1B: list_due_repos() — JSON fallback
# ===========================================================================


class TestListDueReposJSON:
    """GlobalRegistry.list_due_repos() with JSON file backend."""

    def test_returns_only_due_repos(self, tmp_path):
        reg = _make_json_registry(tmp_path)
        now = time.time()
        _register_repo(reg, "due-json-global", next_refresh=now - 60)
        _register_repo(reg, "future-json-global", next_refresh=now + 3600)
        _register_repo(reg, "null-json-global", next_refresh=None)

        result = reg.list_due_repos(limit=10, now=now)
        aliases = [r["alias_name"] for r in result]

        assert "due-json-global" in aliases
        assert "future-json-global" not in aliases
        assert "null-json-global" not in aliases

    def test_returns_oldest_first(self, tmp_path):
        reg = _make_json_registry(tmp_path)
        now = time.time()
        _register_repo(reg, "old-json-global", next_refresh=now - 300)
        _register_repo(reg, "new-json-global", next_refresh=now - 60)

        result = reg.list_due_repos(limit=10, now=now)
        aliases = [r["alias_name"] for r in result]

        assert aliases.index("old-json-global") < aliases.index("new-json-global")

    def test_cap_limits_results_to_n(self, tmp_path):
        reg = _make_json_registry(tmp_path)
        now = time.time()
        for i in range(8):
            _register_repo(
                reg, f"json-repo-{i}-global", next_refresh=now - (i + 1) * 30
            )

        result = reg.list_due_repos(limit=3, now=now)
        assert len(result) == 3

    def test_cap_of_zero_returns_empty(self, tmp_path):
        reg = _make_json_registry(tmp_path)
        now = time.time()
        _register_repo(reg, "due-json-global", next_refresh=now - 60)

        result = reg.list_due_repos(limit=0, now=now)
        assert result == []


# ===========================================================================
# Part 1C: max_concurrent_refresh_jobs config setting
# ===========================================================================


class TestMaxConcurrentRefreshJobsConfig:
    """BackgroundJobsConfig.max_concurrent_refresh_jobs has correct default."""

    def test_field_exists_with_correct_default(self):
        cfg = BackgroundJobsConfig()
        # Default: max(1, max_concurrent_background_jobs // 2) = max(1, 5//2) = max(1, 2) = 2
        assert hasattr(cfg, "max_concurrent_refresh_jobs")
        default_bg = cfg.max_concurrent_background_jobs  # 5
        expected_default = max(1, default_bg // 2)
        assert cfg.max_concurrent_refresh_jobs == expected_default

    def test_field_is_independently_configurable(self):
        cfg = BackgroundJobsConfig(max_concurrent_refresh_jobs=4)
        assert cfg.max_concurrent_refresh_jobs == 4

    def test_default_is_at_least_one(self):
        # Even if max_concurrent_background_jobs=1, refresh default >= 1
        cfg = BackgroundJobsConfig(max_concurrent_background_jobs=1)
        assert cfg.max_concurrent_refresh_jobs >= 1


# ===========================================================================
# Part 1D: BackgroundJobManager.count_active_refresh_jobs()
# ===========================================================================


class TestCountActiveRefreshJobs:
    """BackgroundJobManager.count_active_refresh_jobs() counts pending+running refresh jobs."""

    def _make_manager(self, tmp_path):
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )
        from code_indexer.server.utils.config_manager import BackgroundJobsConfig

        db_path = str(tmp_path / "jobs.db")
        return BackgroundJobManager(
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=5
            ),
            db_path=db_path,
        )

    def test_zero_when_no_jobs(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        assert mgr.count_active_refresh_jobs() == 0

    def test_counts_pending_refresh_in_memory(self, tmp_path):
        """A PENDING refresh job in memory is counted."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )
        from datetime import datetime, timezone
        import uuid

        mgr = self._make_manager(tmp_path)

        job_id = str(uuid.uuid4())
        job = BackgroundJob(
            job_id=job_id,
            operation_type="global_repo_refresh",
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="system",
            is_admin=True,
            repo_alias="test-repo-global",
        )
        with mgr._lock:
            mgr.jobs[job_id] = job

        assert mgr.count_active_refresh_jobs() == 1

    def test_does_not_count_non_refresh_jobs(self, tmp_path):
        """Jobs with a different operation_type are not counted."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )
        from datetime import datetime, timezone
        import uuid

        mgr = self._make_manager(tmp_path)

        job_id = str(uuid.uuid4())
        job = BackgroundJob(
            job_id=job_id,
            operation_type="add_golden_repo",  # not a refresh
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=20,
            username="admin",
            is_admin=True,
            repo_alias="other-repo-global",
        )
        with mgr._lock:
            mgr.jobs[job_id] = job

        assert mgr.count_active_refresh_jobs() == 0

    def test_does_not_count_completed_refresh_jobs_in_memory(self, tmp_path):
        """Completed refresh jobs in memory are not counted."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )
        from datetime import datetime, timezone
        import uuid

        mgr = self._make_manager(tmp_path)

        job_id = str(uuid.uuid4())
        job = BackgroundJob(
            job_id=job_id,
            operation_type="global_repo_refresh",
            status=JobStatus.COMPLETED,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            result={"success": True},
            error=None,
            progress=100,
            username="system",
            is_admin=True,
            repo_alias="done-repo-global",
        )
        with mgr._lock:
            mgr.jobs[job_id] = job

        assert mgr.count_active_refresh_jobs() == 0
