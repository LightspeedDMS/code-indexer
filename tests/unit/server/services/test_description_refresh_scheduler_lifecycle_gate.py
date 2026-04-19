"""
Unit tests for Bug #835 fix — lifecycle gate in _run_loop_single_pass.

Root cause: repos with lifecycle_schema_version < LIFECYCLE_SCHEMA_VERSION
but no code changes never transition from 'pending' to 'queued' because
has_changes_since_last_run() returns False and the scheduler skips them.

Fix: bypass the change gate when needs_lifecycle_backfill is True.

Test strategy:
- Real SQLite DB seeded via GoldenRepoMetadataSqliteBackend and
  DescriptionRefreshTrackingBackend APIs
- Real filesystem: clone_path/.code-indexer/metadata.json with matching
  current_commit so has_changes_since_last_run() returns False naturally
- Real cidx-meta/<alias>.md with valid frontmatter so _read_existing_description works
- Mocked test infrastructure: config_manager (no config file in tmp_path),
  claude_cli_manager (external Claude CLI process manager)
- Patched external collaborators only:
  - invoke_claude_cli (module-level subprocess boundary)
  - RepoAnalyzer.get_prompt (git tree analysis)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_KNOWN_COMMIT = "abc1234567890"
_STALE_NEXT_RUN = "2020-01-02T00:00:00+00:00"
_MODULE = "code_indexer.server.services.description_refresh_scheduler"
_REPO_ANALYZER = "code_indexer.global_repos.repo_analyzer.RepoAnalyzer.get_prompt"


# ---------------------------------------------------------------------------
# Five focused helpers — each under 20 lines
# ---------------------------------------------------------------------------


def _seed_golden_repo(db_file: str, alias: str) -> None:
    """Insert golden repo row pointing to clone_path on disk."""
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    GoldenRepoMetadataSqliteBackend(db_file).add_repo(
        alias=alias,
        repo_url=f"git@example.com:{alias}.git",
        default_branch="main",
        clone_path=str(Path(db_file).parent / "clone"),
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _seed_tracking_row(db_file: str, alias: str, lifecycle_version: int) -> None:
    """Insert pending tracking row and set lifecycle_schema_version."""
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


def _seed_clone_metadata(tmp_path: Path) -> None:
    """Create .code-indexer/metadata.json with matching commit (no changes detected)."""
    clone_dir = tmp_path / "clone" / ".code-indexer"
    clone_dir.mkdir(parents=True, exist_ok=True)
    (clone_dir / "metadata.json").write_text(
        json.dumps({"current_commit": _KNOWN_COMMIT})
    )


def _seed_meta_md(tmp_path: Path, alias: str) -> Path:
    """Create cidx-meta/<alias>.md with valid frontmatter; return meta_dir."""
    meta_dir = tmp_path / "cidx-meta"
    meta_dir.mkdir(exist_ok=True)
    (meta_dir / f"{alias}.md").write_text(
        "---\nlast_analyzed: 2020-01-01T00:00:00+00:00\ndescription: Test\n---\nBody.\n"
    )
    return meta_dir


def _make_scheduler(db_file: str, meta_dir: Path):
    """Construct scheduler with mocked test-infrastructure config and CLI manager."""
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
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLifecycleGateBypass:
    """Bug #835: lifecycle-stale repos must bypass the has_changes_since_last_run gate."""

    @pytest.mark.parametrize(
        "alias",
        [
            pytest.param(
                "stale-lifecycle-repo",
                id="lifecycle_stale_bypasses_change_gate",
            ),
            pytest.param(
                "backfill-pending-repo",
                id="pending_lifecycle_row_transitions_after_one_pass",
            ),
        ],
    )
    def test_lifecycle_stale_repo_transitions_to_queued(self, tmp_path, alias):
        """
        A repo with lifecycle_schema_version < LIFECYCLE_SCHEMA_VERSION and no code
        changes (matching commit in real metadata.json) must transition to 'queued'
        in a single pass.

        Case 'lifecycle_stale_bypasses_change_gate': verifies the bypass logic.
        Case 'pending_lifecycle_row_transitions_after_one_pass': regression for the
        production symptom — row stuck in 'pending' forever before the fix.

        has_changes_since_last_run() runs against the real metadata.json and returns
        False naturally; no scheduler decision logic is patched.
        """
        from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.storage.sqlite_backends import (
            DescriptionRefreshTrackingBackend,
        )

        db_file = str(tmp_path / "test.db")
        DatabaseSchema(db_file).initialize_database()
        stale_version = max(0, LIFECYCLE_SCHEMA_VERSION - 1)

        _seed_golden_repo(db_file, alias)
        _seed_tracking_row(db_file, alias, lifecycle_version=stale_version)
        _seed_clone_metadata(tmp_path)
        meta_dir = _seed_meta_md(tmp_path, alias)
        scheduler = _make_scheduler(db_file, meta_dir)

        with (
            patch(f"{_MODULE}.invoke_claude_cli", return_value=(False, "test-skipped")),
            patch(_REPO_ANALYZER, return_value="refresh prompt"),
        ):
            scheduler._run_loop_single_pass()

        record = DescriptionRefreshTrackingBackend(db_file).get_tracking_record(alias)
        assert record is not None
        assert record["status"] == "queued", (
            f"Bug #835: lifecycle-stale repo must transition to 'queued' in one pass, "
            f"got status='{record['status']}'"
        )

    def test_lifecycle_fresh_repo_with_no_changes_is_skipped(self, tmp_path):
        """
        A repo with lifecycle_schema_version == LIFECYCLE_SCHEMA_VERSION and no code
        changes must be skipped — status stays 'pending', next_run is rescheduled.

        No external collaborators patched: the change gate fires and reschedules
        without touching the CLI or analysis layer.
        """
        from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.storage.sqlite_backends import (
            DescriptionRefreshTrackingBackend,
        )

        alias = "fresh-lifecycle-repo"
        db_file = str(tmp_path / "test.db")
        DatabaseSchema(db_file).initialize_database()

        _seed_golden_repo(db_file, alias)
        _seed_tracking_row(db_file, alias, lifecycle_version=LIFECYCLE_SCHEMA_VERSION)
        _seed_clone_metadata(tmp_path)
        meta_dir = _seed_meta_md(tmp_path, alias)
        scheduler = _make_scheduler(db_file, meta_dir)

        scheduler._run_loop_single_pass()

        record = DescriptionRefreshTrackingBackend(db_file).get_tracking_record(alias)
        assert record is not None
        assert record["status"] == "pending", (
            f"Lifecycle-fresh repo with no changes must stay 'pending', "
            f"got status='{record['status']}'"
        )
        assert record["next_run"] > _STALE_NEXT_RUN, (
            f"Expected next_run rescheduled beyond {_STALE_NEXT_RUN}, "
            f"got: {record['next_run']}"
        )
