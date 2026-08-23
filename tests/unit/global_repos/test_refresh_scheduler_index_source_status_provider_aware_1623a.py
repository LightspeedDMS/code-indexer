"""
Unit tests for Bug #1623-A: a second, verbatim copy of Bug #1623's exact
defect inside RefreshScheduler._index_source().

_index_source() (used by the golden-repo registration and local-repo-branch
index paths, which do NOT go through _check_stale_index_metadata()) reads
the interrupted-index status signal by opening the bare legacy
`.code-indexer/metadata.json` file directly:

    metadata_path = Path(source_path) / ".code-indexer" / "metadata.json"
    ...
    meta_status = _meta.get("status", "")
    if meta_status in ("in_progress", "failed"):
        needs_reconcile = True

This is blind to provider-suffixed metadata files (metadata-voyage-ai.json,
metadata-cohere.json) -- the exact same gap Bug #1623 already fixed in
_check_stale_index_metadata() by switching to
metadata_reader.read_status(). This test file proves the second call site
is now also provider-aware by exercising the REAL _index_source() method
and inspecting the `cidx index` command it hands to
run_with_popen_progress() for `--reconcile`.

Pattern reused from test_refresh_scheduler_extension_drift.py (the
existing test file covering this same call site's force_reconcile/drift
behavior) and test_refresh_scheduler_stale_index_status_provider_aware_1623.py
(the sibling coverage for _check_stale_index_metadata()).

subprocess.run is deliberately NOT patched: with enable_temporal=False and
enable_scip defaulting to False (repo_info carries no "enable_scip" key),
_index_source()'s only subprocess.run call site (SCIP indexing, guarded by
`if enable_scip:`) is never reached on this path -- patching it would hide
a real behavior change if that guard were ever removed.
"""

import json
from pathlib import Path
from typing import List
from unittest.mock import patch

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler


class _RegistryStub:
    def __init__(self):
        self._repo_info = {
            "repo_url": "git@github.com:org/repo.git",
            "default_branch": "main",
            "enable_temporal": False,
        }

    def get_global_repo(self, alias_name: str) -> dict:
        return self._repo_info

    def update_refresh_timestamp(self, alias_name: str) -> None:
        return None


def _make_scheduler(tmp_path):
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=QueryTracker(),
        cleanup_manager=CleanupManager(QueryTracker()),
        registry=_RegistryStub(),
    ), golden_repos_dir


def _make_source_repo(base: Path) -> Path:
    src = base / "my-repo"
    src.mkdir(parents=True, exist_ok=True)
    (src / ".code-indexer").mkdir(exist_ok=True)
    (src / ".git").mkdir(exist_ok=True)
    return src


def _write_metadata(source_repo: Path, filename: str, **fields) -> None:
    bytes_written = (source_repo / ".code-indexer" / filename).write_text(
        json.dumps(fields)
    )
    assert bytes_written > 0, "metadata fixture write must not be empty"


def _run_index_source_capture_popen(sched, source_repo: Path) -> List[List[str]]:
    """Call the REAL _index_source() and capture the semantic cidx command."""
    captured: List[List[str]] = []

    def mock_popen(**kwargs):
        captured.append(list(kwargs.get("command", [])))
        cwd = kwargs.get("cwd", "")
        (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
        return 0

    with patch(
        "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
        side_effect=mock_popen,
    ):
        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            sched._index_source(
                alias_name="my-repo-global",
                source_path=str(source_repo),
                force_reconcile=False,
            )
    return captured


def _semantic_cmd(captured: List[List[str]]) -> List[str]:
    cmds = [c for c in captured if "--fts" in c]
    assert cmds, f"No semantic (--fts) command found in captured: {captured}"
    return cmds[0]


class TestIndexSourceProviderSuffixedStatusDetected:
    """The blocking gap: an in_progress/failed status recorded ONLY in a
    provider-suffixed metadata file must be detected by _index_source(),
    reproducing the real colorama/markupsafe fleet scenario."""

    def test_in_progress_status_in_voyage_file_only_forces_reconcile(self, tmp_path):
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)
        _write_metadata(source_repo, "metadata-voyage-ai.json", status="in_progress")

        captured = _run_index_source_capture_popen(sched, source_repo)

        assert "--reconcile" in _semantic_cmd(captured), (
            "A status=in_progress recorded ONLY in the provider-suffixed "
            "metadata file (the real production filename) must force "
            "--reconcile in _index_source() -- Bug #1623-A closes this "
            "same gap for the second call site."
        )

    def test_failed_status_in_voyage_file_only_forces_reconcile(self, tmp_path):
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)
        _write_metadata(source_repo, "metadata-voyage-ai.json", status="failed")

        captured = _run_index_source_capture_popen(sched, source_repo)

        assert "--reconcile" in _semantic_cmd(captured)

    def test_provider_status_takes_precedence_over_stale_legacy_status(self, tmp_path):
        """Precedence guard matching read_status()'s documented contract:
        the provider file wins even when the legacy file disagrees."""
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)
        _write_metadata(source_repo, "metadata.json", status="completed")
        _write_metadata(source_repo, "metadata-voyage-ai.json", status="in_progress")

        captured = _run_index_source_capture_popen(sched, source_repo)

        assert "--reconcile" in _semantic_cmd(captured), (
            "The provider-suffixed file's in_progress status must take "
            "precedence over a stale-but-unused legacy file claiming "
            "completed."
        )


class TestIndexSourceConsistentProviderMetadataStillSkips:
    """Regression guard: consistent/completed provider metadata, and the
    pre-existing legacy-only behavior, must be unaffected."""

    def test_completed_status_in_voyage_file_does_not_force_reconcile(self, tmp_path):
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)
        _write_metadata(source_repo, "metadata-voyage-ai.json", status="completed")

        captured = _run_index_source_capture_popen(sched, source_repo)

        assert "--reconcile" not in _semantic_cmd(captured), (
            "Consistent, completed status in the provider-suffixed file "
            "must not force a reconcile pass."
        )

    def test_legacy_only_in_progress_status_still_forces_reconcile(self, tmp_path):
        """Regression guard: the original legacy-file behavior must
        continue to work when no provider file exists at all."""
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)
        _write_metadata(source_repo, "metadata.json", status="in_progress")

        captured = _run_index_source_capture_popen(sched, source_repo)

        assert "--reconcile" in _semantic_cmd(captured)

    def test_no_metadata_files_does_not_force_reconcile(self, tmp_path):
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)

        captured = _run_index_source_capture_popen(sched, source_repo)

        assert "--reconcile" not in _semantic_cmd(captured)
