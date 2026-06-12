"""
Unit tests for Bug #1093 fixes in DescriptionRefreshScheduler.

Bug A (#1094 revert): has_changes_since_last_run — None last_known_commit must
       return True (refresh fires to establish the marker).  The #1093 Fix A
       suppression-on-existing-.md was reverted; the refresh now refines the
       existing description rather than skipping it.

Bug B: on_refresh_complete — only reads metadata.json; golden repos use
       metadata-{provider}.json, so last_known_commit never written => Bug A
       fires every cycle permanently.

Bug C: _run_loop_single_pass — uses the lightweight _has_existing_description()
       gate instead of an expensive prompt build to detect a missing .md.

Test strategy:
- Use object.__new__(DescriptionRefreshScheduler) + manual attribute injection
  (same pattern as test_description_refresh_scheduler_lifecycle_backfill.py).
- No mocking of the code under test (Messi Rule #1 anti-mock).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock


SCHEDULER_MODULE = "code_indexer.server.services.description_refresh_scheduler"


def _make_scheduler_bare() -> Any:
    """
    Construct a DescriptionRefreshScheduler without calling __init__.

    Injects the minimal attributes needed by the methods under test.
    """
    import threading

    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    sched = object.__new__(DescriptionRefreshScheduler)

    # Core lifecycle collaborators (None = not wired yet)
    sched._lifecycle_invoker = None
    sched._golden_repos_dir = None
    sched._lifecycle_debouncer = None
    sched._refresh_scheduler = None
    sched._job_tracker = None
    sched._tracking_backend = MagicMock()
    sched._lifecycle_backfill_running = threading.Event()
    sched._description_backfill_running = threading.Event()
    sched._golden_backend = MagicMock()

    # Attributes needed by the methods under test
    sched._meta_dir = None
    sched._prompt_failure_counts = defaultdict(int)
    sched._claude_cli_manager = None

    return sched


# ---------------------------------------------------------------------------
# Bug A — has_changes_since_last_run: None last_known_commit handling
# ---------------------------------------------------------------------------


class TestBugANullLastKnownCommitWithExistingMd:
    """
    #1094 reverts #1093 Fix A: last_known_commit=None must ALWAYS return True
    (refresh fires) — it is the signal that the commit marker still needs
    establishing.  The presence of an existing .md no longer suppresses the
    refresh; instead the refresh now REFINES that existing description and
    stamps last_analyzed so the next cycle has an accurate anchor.
    """

    def test_returns_true_when_last_known_commit_none_and_md_exists(
        self, tmp_path: Path
    ) -> None:
        """
        #1094: NULL last_known_commit + existing .md => True (refresh fires to
        refine the description and establish the commit marker).
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        # Build a fake repo with provider metadata
        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        meta_file = code_indexer_dir / "metadata-voyage-code-3.json"
        meta_file.write_text(
            json.dumps({"current_commit": "abc123", "files_processed": 42})
        )

        # Build meta_dir with a non-empty .md file
        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        alias = "my-repo"
        (meta_dir / f"{alias}.md").write_text(
            "---\nlast_analyzed: 2024-01-01\n---\n# My Repo\n\nSome description.\n"
        )

        sched = _make_scheduler_bare()
        sched._meta_dir = meta_dir

        result = DescriptionRefreshScheduler.has_changes_since_last_run(
            sched, str(tmp_path), {"alias": alias, "last_known_commit": None}
        )

        assert result is True, (
            "Expected True (#1094): NULL last_known_commit must always fire a "
            "refresh to establish the commit marker, even when an existing .md "
            "is present — the refresh refines it rather than skipping."
        )

    def test_returns_true_when_last_known_commit_none_and_no_md_file(
        self, tmp_path: Path
    ) -> None:
        """
        No .md file + None last_known_commit: must return True (first analysis needed).
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        meta_file = code_indexer_dir / "metadata-voyage-code-3.json"
        meta_file.write_text(json.dumps({"current_commit": "abc123"}))

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        # No .md file created

        sched = _make_scheduler_bare()
        sched._meta_dir = meta_dir

        result = DescriptionRefreshScheduler.has_changes_since_last_run(
            sched, str(tmp_path), {"alias": "my-repo", "last_known_commit": None}
        )

        assert result is True, (
            "Expected True: no .md file exists, so analysis is needed."
        )

    def test_returns_true_normally_when_commit_differs(self, tmp_path: Path) -> None:
        """Normal path: commits differ => True (no regression)."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        meta_file = code_indexer_dir / "metadata-voyage-code-3.json"
        meta_file.write_text(json.dumps({"current_commit": "new-commit"}))

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        (meta_dir / "my-repo.md").write_text("# Existing description\n")

        sched = _make_scheduler_bare()
        sched._meta_dir = meta_dir

        result = DescriptionRefreshScheduler.has_changes_since_last_run(
            sched,
            str(tmp_path),
            {"alias": "my-repo", "last_known_commit": "old-commit"},
        )

        assert result is True

    def test_returns_false_normally_when_commit_matches(self, tmp_path: Path) -> None:
        """Normal path: commits match => False (no regression)."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        meta_file = code_indexer_dir / "metadata-voyage-code-3.json"
        meta_file.write_text(json.dumps({"current_commit": "abc"}))

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        sched = _make_scheduler_bare()
        sched._meta_dir = meta_dir

        result = DescriptionRefreshScheduler.has_changes_since_last_run(
            sched,
            str(tmp_path),
            {"alias": "my-repo", "last_known_commit": "abc"},
        )

        assert result is False


# ---------------------------------------------------------------------------
# Bug B — on_refresh_complete: provider metadata fallback
# ---------------------------------------------------------------------------


class TestBugBProviderMetadataFallback:
    """
    Bug B: on_refresh_complete only checked metadata.json; golden repos use
    metadata-{provider}.json, so last_known_commit was never written.
    """

    def test_on_refresh_complete_writes_last_known_commit_from_provider_metadata(
        self, tmp_path: Path
    ) -> None:
        """
        When only metadata-voyage-code-3.json exists (no metadata.json),
        on_refresh_complete must still extract and write last_known_commit.
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        # Repo with provider-specific metadata ONLY (no metadata.json)
        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        (code_indexer_dir / "metadata-voyage-code-3.json").write_text(
            json.dumps({"current_commit": "abc123", "files_processed": 42})
        )

        sched = _make_scheduler_bare()
        sched._tracking_backend = MagicMock()
        sched._job_tracker = None
        # configure_mock on _config_manager so calculate_next_run doesn't crash
        config_manager = MagicMock()
        config_manager.load_config.return_value = MagicMock(
            description_refresh_interval_hours=24
        )
        sched._config_manager = config_manager

        DescriptionRefreshScheduler.on_refresh_complete(
            sched, "my-repo", str(tmp_path), success=True
        )

        # upsert_tracking must have been called with last_known_commit="abc123"
        call_kwargs = sched._tracking_backend.upsert_tracking.call_args
        assert call_kwargs is not None, "upsert_tracking was not called"
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        assert "last_known_commit" in kwargs, (
            f"last_known_commit not found in upsert_tracking call kwargs: {kwargs}"
        )
        assert kwargs["last_known_commit"] == "abc123"

    def test_on_refresh_complete_no_regression_with_plain_metadata_json(
        self, tmp_path: Path
    ) -> None:
        """
        Plain metadata.json (non-provider) still works after the fix (no regression).
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        (code_indexer_dir / "metadata.json").write_text(
            json.dumps({"current_commit": "xyz999"})
        )

        sched = _make_scheduler_bare()
        sched._tracking_backend = MagicMock()
        sched._job_tracker = None
        config_manager = MagicMock()
        config_manager.load_config.return_value = MagicMock(
            description_refresh_interval_hours=24
        )
        sched._config_manager = config_manager

        DescriptionRefreshScheduler.on_refresh_complete(
            sched, "plain-repo", str(tmp_path), success=True
        )

        call_kwargs = sched._tracking_backend.upsert_tracking.call_args
        assert call_kwargs is not None
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        assert kwargs.get("last_known_commit") == "xyz999"


# ---------------------------------------------------------------------------
# Bug C — _has_existing_description lightweight gate
# ---------------------------------------------------------------------------


class TestBugCLightweightGate:
    """
    Bug C: _run_loop_single_pass uses the lightweight _has_existing_description()
    method to detect a missing/empty .md without any prompt build.
    """

    def test_has_existing_description_returns_true_when_md_exists(
        self, tmp_path: Path
    ) -> None:
        """Non-empty .md file => True."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        (meta_dir / "my-repo.md").write_text(
            "---\nlast_analyzed: 2024-01-01\n---\n# Some content\n"
        )

        sched = _make_scheduler_bare()
        sched._meta_dir = meta_dir

        result = DescriptionRefreshScheduler._has_existing_description(sched, "my-repo")
        assert result is True

    def test_has_existing_description_returns_false_when_md_absent(
        self, tmp_path: Path
    ) -> None:
        """Missing .md file => False."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        # No .md created

        sched = _make_scheduler_bare()
        sched._meta_dir = meta_dir

        result = DescriptionRefreshScheduler._has_existing_description(
            sched, "absent-repo"
        )
        assert result is False

    def test_has_existing_description_returns_false_when_md_empty(
        self, tmp_path: Path
    ) -> None:
        """Empty/whitespace-only .md file => False."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        (meta_dir / "empty-repo.md").write_text("   \n\n  ")

        sched = _make_scheduler_bare()
        sched._meta_dir = meta_dir

        result = DescriptionRefreshScheduler._has_existing_description(
            sched, "empty-repo"
        )
        assert result is False

    def test_has_existing_description_returns_false_when_meta_dir_none(self) -> None:
        """_meta_dir=None => False (safe default)."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        sched = _make_scheduler_bare()
        sched._meta_dir = None

        result = DescriptionRefreshScheduler._has_existing_description(
            sched, "any-repo"
        )
        assert result is False
