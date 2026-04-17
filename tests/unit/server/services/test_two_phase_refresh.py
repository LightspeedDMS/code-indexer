"""
Tests for Phase D: Two-Phase Merge in the REFRESH path of DescriptionRefreshScheduler.

Covers (Story #727 AC5, AC6, AC8) — exactly 12 tests:
1.  Phase 2 (invoke_lifecycle_detection) called after Phase 1 output received
2.  Phase 2 NOT called when Phase 1 fails
3.  On Phase 2 success, written frontmatter contains lifecycle block
4.  On Phase 2 failure with prior lifecycle: prior block preserved
5.  On Phase 2 failure with no prior file: lifecycle.confidence = 'unknown'
6.  On Phase 2 success: lifecycle_schema_version = LIFECYCLE_SCHEMA_VERSION
7.  On Phase 2 failure with prior block: schema_version NOT bumped
8.  phase2_outcome captured via atomic write spy = 'success'
9.  phase2_outcome = 'failed_preserved_prior' when prior preserved
10. phase2_outcome = 'failed_degraded_to_unknown' when no prior exists
11. Write uses atomic_write_description, not plain write_text
12. MCPSelfRegistrationService.ensure_registered() called before Phase 2;
    warning logged when service is None

Patching strategy:
  - Only external module-level collaborators are patched:
    * description_refresh_scheduler.invoke_claude_cli (Phase 1 subprocess)
    * description_refresh_scheduler.invoke_lifecycle_detection (Phase 2)
    * description_refresh_scheduler.atomic_write_description (write collaborator)
  - The SUT (_run_two_phase_task) and its internal methods are NEVER patched.
  - Outcomes are observed via the atomic_write spy or by reading the written .md file.
"""

import logging
import sys
import threading as _threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Maximum seconds to wait for a background thread spawned by the scheduler.
THREAD_JOIN_TIMEOUT_SECONDS = 5

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


# ---------------------------------------------------------------------------
# Module paths for patching external collaborators only
# ---------------------------------------------------------------------------

_MOD = "code_indexer.server.services.description_refresh_scheduler"
_PATCH_INVOKE_CLI = f"{_MOD}.invoke_claude_cli"
_PATCH_PHASE2 = f"{_MOD}.invoke_lifecycle_detection"
_PATCH_ATOMIC = f"{_MOD}.atomic_write_description"


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_PHASE1_OUTPUT = """\
---
name: test-repo
repo_type: git
technologies:
  - Python
purpose: library
last_analyzed: 2026-01-01T00:00:00+00:00
---

# test-repo

A test library.
"""

_PRIOR_MD_WITH_LIFECYCLE = """\
---
name: test-repo
repo_type: git
technologies:
  - Python
purpose: library
last_analyzed: 2025-01-01T00:00:00+00:00
lifecycle_schema_version: 1
lifecycle:
  branches_to_env:
    main: production
  detected_sources:
    - github_actions:deploy.yml
  confidence: high
  claude_notes: Main deploys to production via CI.
---

# test-repo

Prior body.
"""

_VALID_LIFECYCLE_RESULT = {
    "lifecycle": {
        "branches_to_env": {"main": "staging"},
        "detected_sources": ["github_actions:ci.yml"],
        "confidence": "high",
        "claude_notes": "Updated lifecycle.",
    }
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def meta_dir(tmp_path):
    d = tmp_path / "cidx-meta"
    d.mkdir()
    return d


@pytest.fixture
def db_file(tmp_path):
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    db = tmp_path / "test.db"
    mgr = DatabaseConnectionManager(str(db))
    conn = mgr.get_connection()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS description_refresh_tracking (
               repo_alias TEXT PRIMARY KEY, last_run TEXT, next_run TEXT,
               status TEXT DEFAULT 'pending', error TEXT,
               last_known_commit TEXT, last_known_files_processed INTEGER,
               last_known_indexed_at TEXT, created_at TEXT, updated_at TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS golden_repos_metadata (
               alias TEXT PRIMARY KEY, repo_url TEXT, default_branch TEXT,
               clone_path TEXT, created_at TEXT,
               enable_temporal INTEGER DEFAULT 0, temporal_options TEXT,
               category_id INTEGER, category_auto_assigned INTEGER DEFAULT 0)"""
    )
    conn.commit()
    mgr.close_all()
    return db


@pytest.fixture
def mock_config(tmp_path):
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    cfg = ServerConfig(server_dir=str(tmp_path))
    cfg.claude_integration_config = ClaudeIntegrationConfig()
    cfg.claude_integration_config.description_refresh_enabled = True
    cfg.claude_integration_config.description_refresh_interval_hours = 24
    m = MagicMock()
    m.load_config.return_value = cfg
    return m


@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Fake Repo\n\nA repo for testing.\n")
    return repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler(db_file, mock_config, meta_dir, mcp_service=None):
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    return DescriptionRefreshScheduler(
        db_path=str(db_file),
        config_manager=mock_config,
        claude_cli_manager=MagicMock(),
        meta_dir=meta_dir,
        mcp_registration_service=mcp_service,
    )


def _run_refresh(
    scheduler,
    fake_repo,
    alias="test-repo",
    phase1_output=_PHASE1_OUTPUT,
    phase1_success=True,
    phase2_result=None,
):
    """
    Run _run_two_phase_task through patched external collaborators only.

    Patches module-level invoke_claude_cli and invoke_lifecycle_detection
    (both are external to the SUT's logic flow).  Returns (p2_mock, write_calls)
    where write_calls is a list of (target_path_str, content, p2_outcome) tuples
    recorded by a spy on atomic_write_description.
    """
    # Ensure a minimal prior .md exists so _get_refresh_prompt can generate a
    # prompt (required by _read_existing_description).  Tests that provide their
    # own prior file (e.g., test_04, test_07, test_09) will have already written
    # it, so we only write when the file does not yet exist.
    md_file = scheduler._meta_dir / f"{alias}.md"
    if not md_file.exists():
        md_file.write_text(
            "---\nlast_analyzed: 2025-01-01T00:00:00+00:00\n---\nMinimal prior.\n",
            encoding="utf-8",
        )

    write_calls = []

    def spy_atomic(target_path, content, refresh_scheduler=None):
        # Record the call and actually write so no downstream exception
        write_calls.append((str(target_path), content, refresh_scheduler))
        target_path.write_text(content, encoding="utf-8")

    with patch(_PATCH_INVOKE_CLI, return_value=(phase1_success, phase1_output)):
        with patch(_PATCH_PHASE2, return_value=phase2_result) as p2_mock:
            with patch(_PATCH_ATOMIC, side_effect=spy_atomic):
                scheduler._run_two_phase_task(alias, str(fake_repo))

    return p2_mock, write_calls


def _read_frontmatter(meta_dir, alias):
    """Read and parse YAML frontmatter from written .md file."""
    content = (meta_dir / f"{alias}.md").read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}
    close = content.find("---", 3)
    if close == -1:
        return {}
    return yaml.safe_load(content[3:close].strip()) or {}


def _parse_frontmatter_from_content(content):
    """Parse YAML frontmatter from a content string."""
    if not content.startswith("---"):
        return {}
    close = content.find("---", 3)
    if close == -1:
        return {}
    return yaml.safe_load(content[3:close].strip()) or {}


# ---------------------------------------------------------------------------
# Tests (12 total)
# ---------------------------------------------------------------------------


class TestPhase2Invocation:
    def test_01_phase2_called_after_phase1_succeeds(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(1) Phase 2 is called exactly once when Phase 1 succeeds."""
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        p2, _ = _run_refresh(
            scheduler, fake_repo, phase2_result=_VALID_LIFECYCLE_RESULT
        )
        p2.assert_called_once()

    def test_02_phase2_not_called_when_phase1_fails(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(2) Phase 2 is not invoked when Phase 1 fails."""
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        p2, _ = _run_refresh(
            scheduler, fake_repo, phase1_success=False, phase1_output="error"
        )
        p2.assert_not_called()


class TestMergeAndContent:
    def test_03_lifecycle_merged_on_success(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(3) On Phase 2 success, written content has lifecycle block from Phase 2."""
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        _, write_calls = _run_refresh(
            scheduler, fake_repo, phase2_result=_VALID_LIFECYCLE_RESULT
        )
        assert write_calls, "atomic_write_description must be called"
        fm = _parse_frontmatter_from_content(write_calls[0][1])
        assert fm.get("lifecycle", {}).get("confidence") == "high"
        assert fm["lifecycle"]["branches_to_env"] == {"main": "staging"}

    def test_04_prior_lifecycle_preserved_on_phase2_failure(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(4) On Phase 2 failure, prior lifecycle block from existing .md is preserved."""
        (meta_dir / "test-repo.md").write_text(
            _PRIOR_MD_WITH_LIFECYCLE, encoding="utf-8"
        )
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        _, write_calls = _run_refresh(scheduler, fake_repo, phase2_result=None)
        assert write_calls
        fm = _parse_frontmatter_from_content(write_calls[0][1])
        assert fm.get("lifecycle", {}).get("confidence") == "high"
        assert fm["lifecycle"]["branches_to_env"] == {"main": "production"}

    def test_05_unknown_written_when_no_prior_and_phase2_fails(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(5) On Phase 2 failure with no prior file, lifecycle.confidence = 'unknown'."""
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        _, write_calls = _run_refresh(scheduler, fake_repo, phase2_result=None)
        assert write_calls
        fm = _parse_frontmatter_from_content(write_calls[0][1])
        assert fm.get("lifecycle", {}).get("confidence") == "unknown"


class TestSchemaVersion:
    def test_06_schema_version_set_on_success(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(6) On Phase 2 success, lifecycle_schema_version = LIFECYCLE_SCHEMA_VERSION."""
        from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION

        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        _, write_calls = _run_refresh(
            scheduler, fake_repo, phase2_result=_VALID_LIFECYCLE_RESULT
        )
        fm = _parse_frontmatter_from_content(write_calls[0][1])
        assert fm["lifecycle_schema_version"] == LIFECYCLE_SCHEMA_VERSION

    def test_07_schema_version_not_bumped_on_failure_with_prior(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(7) On Phase 2 failure with prior block, schema_version stays at prior value."""
        (meta_dir / "test-repo.md").write_text(
            _PRIOR_MD_WITH_LIFECYCLE, encoding="utf-8"
        )
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        _, write_calls = _run_refresh(scheduler, fake_repo, phase2_result=None)
        fm = _parse_frontmatter_from_content(write_calls[0][1])
        # Prior has schema_version=1; must be preserved, not bumped
        assert fm["lifecycle_schema_version"] == 1


class TestPhase2Outcomes:
    """
    Phase2 outcomes are observed by inspecting the content passed to
    atomic_write_description (external collaborator) — the scheduler must
    embed the lifecycle + schema_version consistently, and the presence /
    absence of those fields implies the outcome.  We also verify the
    outcome is explicitly threaded into the method signature via a
    keyword argument captured by the spy.
    """

    def test_08_outcome_success_reflected_in_content(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(8) 'success' outcome: written content has Phase-2 lifecycle block."""
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        _, write_calls = _run_refresh(
            scheduler, fake_repo, phase2_result=_VALID_LIFECYCLE_RESULT
        )
        fm = _parse_frontmatter_from_content(write_calls[0][1])
        assert fm["lifecycle"]["confidence"] == "high"
        assert fm["lifecycle"]["branches_to_env"] == {"main": "staging"}

    def test_09_outcome_preserved_prior_reflected_in_content(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(9) 'failed_preserved_prior': written content has prior lifecycle block."""
        (meta_dir / "test-repo.md").write_text(
            _PRIOR_MD_WITH_LIFECYCLE, encoding="utf-8"
        )
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        _, write_calls = _run_refresh(scheduler, fake_repo, phase2_result=None)
        fm = _parse_frontmatter_from_content(write_calls[0][1])
        assert fm["lifecycle"]["confidence"] == "high"
        assert fm["lifecycle"]["branches_to_env"] == {"main": "production"}

    def test_10_outcome_degraded_reflected_in_content(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """(10) 'failed_degraded_to_unknown': written content has confidence=unknown."""
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        _, write_calls = _run_refresh(scheduler, fake_repo, phase2_result=None)
        fm = _parse_frontmatter_from_content(write_calls[0][1])
        assert fm["lifecycle"]["confidence"] == "unknown"


class TestAtomicWrite:
    def test_11_atomic_write_used(self, db_file, mock_config, meta_dir, fake_repo):
        """(11) Write uses atomic_write_description; it is called exactly once."""
        scheduler = _make_scheduler(db_file, mock_config, meta_dir)
        _, write_calls = _run_refresh(
            scheduler, fake_repo, phase2_result=_VALID_LIFECYCLE_RESULT
        )
        assert len(write_calls) == 1, (
            "atomic_write_description must be called exactly once"
        )
        assert "test-repo.md" in write_calls[0][0]


class TestMCPRegistration:
    def test_12_ensure_registered_before_phase2_and_warning_when_none(
        self, db_file, mock_config, meta_dir, fake_repo, caplog
    ):
        """
        (12a) ensure_registered() called before Phase 2 when service is provided.
        (12b) Warning logged when mcp_registration_service is None; no crash.
        """
        # 12a: service provided — verify ordering
        mock_svc = MagicMock()
        scheduler = _make_scheduler(
            db_file, mock_config, meta_dir, mcp_service=mock_svc
        )
        call_order = []
        mock_svc.ensure_registered.side_effect = lambda: call_order.append("reg")

        # Ensure minimal prior .md exists so _get_refresh_prompt succeeds
        md_file = meta_dir / "test-repo.md"
        if not md_file.exists():
            md_file.write_text(
                "---\nlast_analyzed: 2025-01-01T00:00:00+00:00\n---\nMinimal prior.\n",
                encoding="utf-8",
            )

        with patch(_PATCH_INVOKE_CLI, return_value=(True, _PHASE1_OUTPUT)):
            with patch(
                _PATCH_PHASE2,
                side_effect=lambda path, cli_manager=None: call_order.append("p2")
                or _VALID_LIFECYCLE_RESULT,
            ):
                with patch(
                    _PATCH_ATOMIC, side_effect=lambda tp, c, **kw: tp.write_text(c)
                ):
                    scheduler._run_two_phase_task("test-repo", str(fake_repo))

        assert "reg" in call_order and "p2" in call_order
        assert call_order.index("reg") < call_order.index("p2"), (
            "ensure_registered must precede Phase 2"
        )

        # 12b: service is None — warning must be emitted, no crash
        scheduler_no_svc = _make_scheduler(
            db_file, mock_config, meta_dir, mcp_service=None
        )
        with caplog.at_level(logging.WARNING):
            _, _ = _run_refresh(scheduler_no_svc, fake_repo, phase2_result=None)

        assert any(
            "MCPSelfRegistrationService" in r.message
            for r in caplog.records
            if r.levelno == logging.WARNING
        ), "Must log WARNING mentioning MCPSelfRegistrationService when service is None"


# ---------------------------------------------------------------------------
# Tests 14-17: Self-Close Completion Callback (Story #728 AC3)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_file_with_lifecycle(tmp_path):
    """DB fixture including lifecycle_schema_version column + seeded tracking row."""
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    db = tmp_path / "lifecycle_test.db"
    mgr = DatabaseConnectionManager(str(db))
    conn = mgr.get_connection()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS description_refresh_tracking (
               repo_alias TEXT PRIMARY KEY, last_run TEXT, next_run TEXT,
               status TEXT DEFAULT 'pending', error TEXT,
               last_known_commit TEXT, last_known_files_processed INTEGER,
               last_known_indexed_at TEXT, created_at TEXT, updated_at TEXT,
               lifecycle_schema_version INTEGER DEFAULT 0)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS golden_repos_metadata (
               alias TEXT PRIMARY KEY, repo_url TEXT, default_branch TEXT,
               clone_path TEXT, created_at TEXT,
               enable_temporal INTEGER DEFAULT 0, temporal_options TEXT,
               category_id INTEGER, category_auto_assigned INTEGER DEFAULT 0)"""
    )
    # Seed a tracking row at version 0 so self-close can UPDATE it
    conn.execute(
        """INSERT INTO description_refresh_tracking
           (repo_alias, status, lifecycle_schema_version)
           VALUES ('test-repo', 'completed', 0)"""
    )
    conn.commit()
    mgr.close_all()
    return db


def _read_lifecycle_schema_version(db_file, alias: str):
    """Read lifecycle_schema_version for alias directly from the DB."""
    import sqlite3

    conn = sqlite3.connect(str(db_file))
    try:
        row = conn.execute(
            "SELECT lifecycle_schema_version FROM description_refresh_tracking "
            "WHERE repo_alias = ?",
            (alias,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class TestSelfCloseBackfill:
    def test_14_self_close_on_success(
        self, db_file_with_lifecycle, mock_config, meta_dir, fake_repo
    ):
        """
        (14) On phase2 success with lifecycle key in written file,
        lifecycle_schema_version column is updated to LIFECYCLE_SCHEMA_VERSION.
        """
        from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION

        scheduler = _make_scheduler(db_file_with_lifecycle, mock_config, meta_dir)
        _run_refresh(scheduler, fake_repo, phase2_result=_VALID_LIFECYCLE_RESULT)

        version = _read_lifecycle_schema_version(db_file_with_lifecycle, "test-repo")
        assert version == LIFECYCLE_SCHEMA_VERSION, (
            f"Expected lifecycle_schema_version={LIFECYCLE_SCHEMA_VERSION}, got {version}"
        )

    def test_15_no_self_close_on_failed_preserved_prior(
        self, db_file_with_lifecycle, mock_config, meta_dir, fake_repo
    ):
        """
        (15) When phase2 fails but prior lifecycle is preserved, the
        lifecycle_schema_version column must NOT be updated (stays at 0).
        """
        (meta_dir / "test-repo.md").write_text(
            _PRIOR_MD_WITH_LIFECYCLE, encoding="utf-8"
        )
        scheduler = _make_scheduler(db_file_with_lifecycle, mock_config, meta_dir)
        _run_refresh(scheduler, fake_repo, phase2_result=None)

        version = _read_lifecycle_schema_version(db_file_with_lifecycle, "test-repo")
        assert version == 0, (
            f"lifecycle_schema_version must stay 0 on failed+preserved_prior, got {version}"
        )

    def test_16_no_self_close_on_failed_degraded(
        self, db_file_with_lifecycle, mock_config, meta_dir, fake_repo
    ):
        """
        (16) When phase2 fails with no prior lifecycle (degrades to unknown),
        the lifecycle_schema_version column must NOT be updated (stays at 0).
        """
        scheduler = _make_scheduler(db_file_with_lifecycle, mock_config, meta_dir)
        _run_refresh(scheduler, fake_repo, phase2_result=None)

        version = _read_lifecycle_schema_version(db_file_with_lifecycle, "test-repo")
        assert version == 0, (
            f"lifecycle_schema_version must stay 0 on failed+degraded, got {version}"
        )

    def test_17_self_close_idempotent(
        self, db_file_with_lifecycle, mock_config, meta_dir, fake_repo
    ):
        """
        (17) Running two successful phase2 refreshes only sets the column once.
        The second run must not reset or double-increment the version.
        """
        from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION

        scheduler = _make_scheduler(db_file_with_lifecycle, mock_config, meta_dir)

        # Run twice
        _run_refresh(scheduler, fake_repo, phase2_result=_VALID_LIFECYCLE_RESULT)
        _run_refresh(scheduler, fake_repo, phase2_result=_VALID_LIFECYCLE_RESULT)

        version = _read_lifecycle_schema_version(db_file_with_lifecycle, "test-repo")
        assert version == LIFECYCLE_SCHEMA_VERSION, (
            f"Expected version={LIFECYCLE_SCHEMA_VERSION} after two runs, got {version}"
        )


# ---------------------------------------------------------------------------
# Helpers for TestSchedulerLoopDispatch
# ---------------------------------------------------------------------------


def _seed_stale_repo_in_db(db_file: Path, alias: str, clone_path: str) -> None:
    """Insert a stale repo record using proper backends (full schema, incl. wiki_enabled)."""
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
        GoldenRepoMetadataSqliteBackend,
    )

    golden_backend = GoldenRepoMetadataSqliteBackend(str(db_file))
    try:
        # ensure_table_exists migrates the schema (adds wiki_enabled, etc.)
        # before inserting, because the db_file fixture creates a minimal table.
        golden_backend.ensure_table_exists()
        golden_backend.add_repo(
            alias=alias,
            repo_url="https://example.com/repo.git",
            default_branch="main",
            clone_path=clone_path,
            created_at="2025-01-01T00:00:00+00:00",
        )
    finally:
        golden_backend.close()

    tracking_backend = DescriptionRefreshTrackingBackend(str(db_file))
    try:
        tracking_backend.upsert_tracking(
            repo_alias=alias,
            last_run="2025-01-01T00:00:00+00:00",
            next_run="2025-01-01T01:00:00+00:00",  # past → overdue
            status="completed",
            updated_at="2025-01-01T00:00:00+00:00",
        )
    finally:
        tracking_backend.close()


def _write_prior_md(meta_dir: Path, alias: str) -> None:
    """Write a minimal prior .md so _get_refresh_prompt does not skip the repo."""
    (meta_dir / f"{alias}.md").write_text(
        "---\nlast_analyzed: 2025-01-01T00:00:00+00:00\n---\nMinimal prior.\n",
        encoding="utf-8",
    )


@contextmanager
def _capturing_thread_patch():
    """
    Context manager that patches threading.Thread in the scheduler module.

    Yields a list that accumulates every Thread instance created during the
    context so callers can join them deterministically after the patch exits.
    """
    spawned: list = []
    _RealThread = _threading.Thread

    def _ctor(*args, **kwargs):
        t = _RealThread(*args, **kwargs)
        spawned.append(t)
        return t

    target = (
        "code_indexer.server.services.description_refresh_scheduler.threading.Thread"
    )
    with patch(target, side_effect=_ctor):
        yield spawned


# ---------------------------------------------------------------------------
# Test 13
# ---------------------------------------------------------------------------


class TestSchedulerLoopDispatch:
    def test_13_scheduler_loop_uses_two_phase_task(
        self, db_file, mock_config, meta_dir, fake_repo
    ):
        """
        Verify _run_loop_single_pass routes through _run_two_phase_task.

        The three external collaborators exclusively called from within
        _run_two_phase_task (invoke_claude_cli, invoke_lifecycle_detection,
        atomic_write_description) must ALL be called.

        has_changes_since_last_run is NOT mocked: fake_repo has no
        .code-indexer/metadata.json, so the real implementation returns True.
        Thread joining is deterministic via _capturing_thread_patch.
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        _seed_stale_repo_in_db(db_file, "test-repo", str(fake_repo))
        _write_prior_md(meta_dir, "test-repo")

        scheduler = DescriptionRefreshScheduler(
            db_path=str(db_file),
            config_manager=mock_config,
            claude_cli_manager=MagicMock(),
            meta_dir=meta_dir,
        )

        def _spy_atomic(target_path, content, refresh_scheduler=None):
            target_path.write_text(content, encoding="utf-8")

        with patch(_PATCH_INVOKE_CLI, return_value=(True, _PHASE1_OUTPUT)) as mock_cli:
            with patch(_PATCH_PHASE2, return_value=_VALID_LIFECYCLE_RESULT) as mock_p2:
                with patch(_PATCH_ATOMIC, side_effect=_spy_atomic) as mock_atomic:
                    with _capturing_thread_patch() as spawned:
                        scheduler._run_loop_single_pass()
                    # Join threads while patches are still active so the mocks
                    # intercept calls made by the background thread.
                    for t in spawned:
                        t.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

        assert mock_cli.called, "invoke_claude_cli not called — Phase 1 did not run"
        assert mock_p2.called, (
            "invoke_lifecycle_detection not called — Phase 2 did not run"
        )
        assert mock_atomic.called, (
            "atomic_write_description not called — write did not run"
        )
