"""
Unit tests for startup description backfill sweep in DescriptionRefreshScheduler.

reconcile_terse_descriptions() is a one-shot sweep that runs at start()
(after reconcile_broken_lifecycle_metadata()) to find golden repos with terse
cidx-meta descriptions (body <= 500 chars) and route them asynchronously
through LifecycleBatchRunner for regeneration.

Fixes production gap: repos with short/broken descriptions never get regenerated
by any event-driven code path.

Test strategy:
- Use object.__new__(DescriptionRefreshScheduler) + manual attribute injection
  for lightweight direct-method tests (same pattern as lifecycle_backfill sibling).
- No mocking of code under test (Messi Rule #1 — anti-mock).
- Filesystem fixtures use tmp_path for real file I/O (Messi Rule #1 — anti-mock).

Classes:
  TestFindTerseDescriptionAliases  (5 tests) — file scanning logic
  TestReconcileTerseDescriptions   (3 tests) — orchestration + dispatch wiring
  TestStartCallsReconcileTerse     (1 test)  — wiring into start()
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEDULER_MODULE = "code_indexer.server.services.description_refresh_scheduler"
LOGGER_NAME = SCHEDULER_MODULE

SHORT_BODY = "Short body"
LONG_BODY = "x" * 501


def _make_cidx_meta_file(meta_dir: Path, alias: str, body: str) -> Path:
    """Write a cidx-meta markdown file with YAML frontmatter and the given body."""
    content = (
        f'---\nlast_analyzed: "2026-01-01"\nlifecycle:\n'
        f"  confidence: high\n  lifecycle_schema_version: 3\n---\n{body}"
    )
    path = meta_dir / f"{alias}.md"
    path.write_text(content)
    return path


def _make_cidx_meta_file_no_frontmatter(meta_dir: Path, alias: str, body: str) -> Path:
    """Write a cidx-meta markdown file WITHOUT YAML frontmatter."""
    path = meta_dir / f"{alias}.md"
    path.write_text(body)
    return path


def _make_scheduler_bare(meta_dir: Path) -> Any:
    """
    Construct a DescriptionRefreshScheduler without calling __init__.

    Injects only the minimal attributes needed by the methods under test.
    All lifecycle collaborators are wired (mirrors production state with
    full wiring) since description backfill reuses the same wiring check.
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    sched = object.__new__(DescriptionRefreshScheduler)

    # Lifecycle collaborators — all wired (description backfill reuses same check)
    sched._lifecycle_invoker = MagicMock()
    sched._golden_repos_dir = MagicMock()
    sched._lifecycle_debouncer = MagicMock()
    sched._refresh_scheduler = MagicMock()
    sched._job_tracker = MagicMock()
    sched._tracking_backend = MagicMock()

    # meta_dir for file scanning
    sched._meta_dir = meta_dir

    # golden_backend for alias listing
    sched._golden_backend = MagicMock()

    return sched


# ---------------------------------------------------------------------------
# Class 1: _find_terse_description_aliases
# ---------------------------------------------------------------------------


class TestFindTerseDescriptionAliases:
    """File scanning: which aliases are flagged as terse."""

    def test_find_terse_description_aliases_flags_short_body(self, tmp_path):
        """cidx-meta file with body <= 500 chars is flagged as terse."""
        sched = _make_scheduler_bare(tmp_path)
        _make_cidx_meta_file(tmp_path, "repo-a", SHORT_BODY)

        result = sched._find_terse_description_aliases(["repo-a"])

        assert result is not None
        assert "repo-a" in result

    def test_find_terse_description_aliases_skips_adequate_body(self, tmp_path):
        """cidx-meta file with body > 500 chars is NOT flagged."""
        sched = _make_scheduler_bare(tmp_path)
        _make_cidx_meta_file(tmp_path, "repo-b", LONG_BODY)

        result = sched._find_terse_description_aliases(["repo-b"])

        assert result is not None
        assert "repo-b" not in result
        assert result == []

    def test_find_terse_description_aliases_skips_missing_file(self, tmp_path):
        """Nonexistent cidx-meta file is silently skipped — not flagged, no error."""
        sched = _make_scheduler_bare(tmp_path)
        # Do NOT create the file for "missing-repo"

        result = sched._find_terse_description_aliases(["missing-repo"])

        assert result is not None
        assert "missing-repo" not in result
        assert result == []

    def test_find_terse_description_aliases_skips_cidx_meta_self(self, tmp_path):
        """The alias 'cidx-meta' is always skipped regardless of body length."""
        sched = _make_scheduler_bare(tmp_path)
        # Create the file — even if it's short, cidx-meta itself must be excluded
        _make_cidx_meta_file(tmp_path, "cidx-meta", SHORT_BODY)

        result = sched._find_terse_description_aliases(["cidx-meta"])

        assert result is not None
        assert "cidx-meta" not in result
        assert result == []

    def test_find_terse_description_aliases_handles_no_frontmatter(self, tmp_path):
        """File with no YAML frontmatter: body is the whole file content, length checked."""
        sched = _make_scheduler_bare(tmp_path)
        _make_cidx_meta_file_no_frontmatter(tmp_path, "repo-c", SHORT_BODY)

        result = sched._find_terse_description_aliases(["repo-c"])

        assert result is not None
        # SHORT_BODY is <= 500 chars so repo-c should be flagged
        assert "repo-c" in result


# ---------------------------------------------------------------------------
# Class 2: reconcile_terse_descriptions + _dispatch_description_backfill_thread
# ---------------------------------------------------------------------------


class TestReconcileTerseDescriptions:
    """Orchestration tests for reconcile_terse_descriptions().

    test_reconcile_terse_descriptions_dispatches_thread also validates the
    _dispatch_description_backfill_thread wiring (thread kwargs, daemon flag,
    name, target method, .start() call) because the dispatch is triggered
    through reconcile_terse_descriptions() which calls the dispatch method.
    """

    def test_reconcile_terse_descriptions_dispatches_thread(self, tmp_path):
        """When terse aliases found, returns count and dispatches daemon thread via
        _dispatch_description_backfill_thread with name='description-backfill'."""
        sched = _make_scheduler_bare(tmp_path)
        _make_cidx_meta_file(tmp_path, "repo-x", SHORT_BODY)
        sched._golden_backend.list_repos.return_value = [{"alias": "repo-x"}]

        captured: dict = {}
        fake_thread = MagicMock()

        def fake_thread_factory(**kwargs):
            captured.update(kwargs)
            return fake_thread

        with patch(f"{SCHEDULER_MODULE}.threading") as mock_threading:
            mock_threading.Thread.side_effect = fake_thread_factory
            result = sched.reconcile_terse_descriptions()

        assert result == 1

        # Thread was created with daemon=True and correct name
        assert mock_threading.Thread.call_count == 1
        assert captured.get("daemon") is True
        assert captured.get("name") == "description-backfill"

        # target is _run_description_backfill_async (validates _dispatch wiring)
        target = captured.get("target")
        assert target is not None
        assert target.__func__.__name__ == "_run_description_backfill_async"
        assert target.__self__ is sched

        # thread.start() was called
        fake_thread.start.assert_called_once()

    def test_reconcile_terse_descriptions_returns_zero_when_all_adequate(
        self, tmp_path
    ):
        """No terse aliases found — returns 0, no thread dispatched."""
        sched = _make_scheduler_bare(tmp_path)
        _make_cidx_meta_file(tmp_path, "repo-fat", LONG_BODY)
        sched._golden_backend.list_repos.return_value = [{"alias": "repo-fat"}]

        with patch(f"{SCHEDULER_MODULE}.threading") as mock_threading:
            result = sched.reconcile_terse_descriptions()

        assert result == 0
        mock_threading.Thread.assert_not_called()

    def test_reconcile_terse_descriptions_returns_zero_when_wiring_missing(
        self, tmp_path
    ):
        """When lifecycle wiring is missing, returns 0 (reuses _check_lifecycle_backfill_wiring)."""
        sched = _make_scheduler_bare(tmp_path)
        # Break the wiring
        sched._lifecycle_invoker = None
        sched._golden_backend.list_repos.return_value = [{"alias": "some-repo"}]

        result = sched.reconcile_terse_descriptions()

        assert result == 0
        # list_repos should NOT have been called — short-circuited by wiring check
        sched._golden_backend.list_repos.assert_not_called()


# ---------------------------------------------------------------------------
# Class 3: start() wiring
# ---------------------------------------------------------------------------


class TestStartCallsReconcileTerse:
    """Verify start() calls reconcile_terse_descriptions() after
    reconcile_broken_lifecycle_metadata()."""

    def test_start_calls_reconcile_terse_descriptions(self):
        """
        start() must invoke reconcile_terse_descriptions().
        Use object.__new__ to bypass __init__, inject a config stub that enables
        description refresh, and patch the internal methods to prevent real I/O.
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        sched = object.__new__(DescriptionRefreshScheduler)

        # Minimal attributes needed by start()
        sched._shutdown_event = threading.Event()
        sched._thread = None
        sched._meta_dir = None
        sched._lifecycle_invoker = None
        sched._golden_repos_dir = None
        sched._lifecycle_debouncer = None
        sched._refresh_scheduler = None
        sched._job_tracker = None
        sched._tracking_backend = MagicMock()
        sched._golden_backend = MagicMock()

        # Build a config stub that enables description refresh
        config_stub = MagicMock()
        config_stub.claude_integration_config.description_refresh_enabled = True
        config_stub.claude_integration_config.description_refresh_interval_hours = 24

        mock_config_manager = MagicMock()
        mock_config_manager.load_config.return_value = config_stub
        sched._config_manager = mock_config_manager

        terse_called = []

        with patch.object(sched, "reconcile_orphan_tracking", return_value=0):
            with patch.object(
                sched, "reconcile_broken_lifecycle_metadata", return_value=0
            ):
                with patch.object(
                    sched,
                    "reconcile_terse_descriptions",
                    side_effect=lambda: terse_called.append(True) or 0,  # type: ignore[func-returns-value]
                ):
                    with patch(f"{SCHEDULER_MODULE}.threading") as mock_threading:
                        mock_threading.Event = threading.Event
                        mock_thread = MagicMock()
                        mock_threading.Thread.return_value = mock_thread
                        sched.start()

        assert len(terse_called) == 1, (
            "start() must call reconcile_terse_descriptions() exactly once"
        )
