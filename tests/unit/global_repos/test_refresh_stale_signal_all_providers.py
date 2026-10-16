"""The refresh scheduler's stale-index check reads the metadata of EVERY
provider that indexed the repository, not only metadata-voyage-ai.json.

`cidx index` writes one `metadata-{provider}.json` per configured provider.
A repository whose only (or whose stale) provider is not voyage-ai must be
evaluated like any other: a failed/in-progress run or a drifted commit in
any provider's metadata is a stale signal, and fully consistent metadata in
every provider file is not. A status signal never lets an unchanged forced
reconcile skip its publish; only commit drift with every provider completed
does.

Real `RefreshScheduler._stale_index_signal()` against a real temp git
repository (no mocking of git).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.stale_index_signal import (
    COMMIT_DRIFT_SIGNAL,
    STATUS_SIGNAL,
)

ALIAS = "example-global"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "main.py").write_text("x = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "first")
    return path


@pytest.fixture
def scheduler(tmp_path: Path) -> RefreshScheduler:
    golden = tmp_path / "golden-repos"
    golden.mkdir()
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=Mock(),
    )


def _write(repo: Path, provider: str, **fields: str) -> None:
    meta_dir = repo / ".code-indexer"
    meta_dir.mkdir(exist_ok=True)
    (meta_dir / f"metadata-{provider}.json").write_text(json.dumps(fields))


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _advance(repo: Path) -> str:
    """Commit once more; return the previous HEAD."""
    old_head = _head(repo)
    (repo / "more.py").write_text("y = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    return old_head


def test_failed_status_in_only_non_voyage_provider_is_a_status_signal(
    scheduler: RefreshScheduler, repo: Path
) -> None:
    _write(repo, "cohere", status="failed", current_commit=_head(repo))
    signal = scheduler._stale_index_signal(str(repo), ALIAS)
    assert signal is not None
    assert signal.kind == STATUS_SIGNAL
    assert "metadata-cohere.json status=failed" in signal.key
    assert not signal.may_skip_unchanged_publish


def test_consistent_only_non_voyage_provider_has_no_signal(
    scheduler: RefreshScheduler, repo: Path
) -> None:
    _write(repo, "cohere", status="completed", current_commit=_head(repo))
    assert scheduler._stale_index_signal(str(repo), ALIAS) is None


def test_drift_in_second_completed_provider_may_skip_unchanged_publish(
    scheduler: RefreshScheduler, repo: Path
) -> None:
    old_head = _advance(repo)
    _write(repo, "voyage-ai", status="completed", current_commit=_head(repo))
    _write(repo, "cohere", status="completed", current_commit=old_head)
    signal = scheduler._stale_index_signal(str(repo), ALIAS)
    assert signal is not None
    assert signal.kind == COMMIT_DRIFT_SIGNAL
    assert "metadata-cohere.json" in signal.key
    assert signal.may_skip_unchanged_publish


def test_drift_with_a_provider_lacking_status_may_not_skip_publish(
    scheduler: RefreshScheduler, repo: Path
) -> None:
    old_head = _advance(repo)
    _write(repo, "voyage-ai", current_commit=_head(repo))  # no status recorded
    _write(repo, "cohere", status="completed", current_commit=old_head)
    signal = scheduler._stale_index_signal(str(repo), ALIAS)
    assert signal is not None
    assert signal.kind == COMMIT_DRIFT_SIGNAL
    assert not signal.may_skip_unchanged_publish


def test_every_provider_consistent_has_no_signal(
    scheduler: RefreshScheduler, repo: Path
) -> None:
    _write(repo, "voyage-ai", status="completed", current_commit=_head(repo))
    _write(repo, "cohere", status="completed", current_commit=_head(repo))
    assert scheduler._stale_index_signal(str(repo), ALIAS) is None
