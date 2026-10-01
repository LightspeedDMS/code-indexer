"""Shared helpers for the golden-repository and configuration audit tests.

The golden-repo manager is REAL (SQLite metadata in a temporary directory).
The only double is :class:`RecordingJobManager`, which stands in for the
background job runner: it records every submission and returns a job id
without running the clone/index work (the audit row belongs to the
SUBMISSION, never to execution).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tests.unit.server.recording_job_manager import RecordingJobManager

__all__ = ["RecordingJobManager"]

EXAMPLE_ALIAS = "example-repo"


def make_golden_repo_manager(tmp_path: Path, job_manager: RecordingJobManager):
    """A real GoldenRepoManager on its own temporary data directory."""
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

    data_dir = tmp_path / "golden-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manager = GoldenRepoManager(data_dir=str(data_dir))
    manager.background_job_manager = job_manager
    return manager


def make_refresh_scheduler(
    tmp_path: Path, job_manager: RecordingJobManager, alias: str = EXAMPLE_ALIAS
):
    """A real RefreshScheduler over a real registry holding ``<alias>-global``."""
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.global_repos.global_registry import GlobalRegistry
    from code_indexer.global_repos.query_tracker import QueryTracker
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations

    golden_dir = tmp_path / "scheduler-golden"
    golden_dir.mkdir(parents=True, exist_ok=True)
    registry = GlobalRegistry(str(golden_dir))
    registry.register_global_repo(
        repo_name=alias,
        alias_name=f"{alias}-global",
        repo_url="https://git.example.com/org/example.git",
        index_path=str(golden_dir / alias),
    )
    tracker = QueryTracker()
    return RefreshScheduler(
        golden_repos_dir=str(golden_dir),
        config_source=GlobalRepoOperations(str(golden_dir)),
        query_tracker=tracker,
        cleanup_manager=CleanupManager(tracker),
        background_job_manager=job_manager,  # type: ignore[arg-type]
        registry=registry,
    )


def register_repo(manager: Any, alias: str = EXAMPLE_ALIAS) -> None:
    """Persist a golden repo record (and its clone directory) for *alias*."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo

    clone_path = os.path.join(manager.golden_repos_dir, alias)
    os.makedirs(clone_path, exist_ok=True)
    repo = GoldenRepo(
        alias=alias,
        repo_url="https://git.example.com/org/example.git",
        default_branch="main",
        clone_path=clone_path,
        created_at=datetime.now(timezone.utc).isoformat(),
        enable_temporal=False,
        temporal_options=None,
    )
    manager.golden_repos[alias] = repo
    manager._sqlite_backend.add_repo(
        alias=repo.alias,
        repo_url=repo.repo_url,
        default_branch=repo.default_branch,
        clone_path=repo.clone_path,
        created_at=repo.created_at,
        enable_temporal=repo.enable_temporal,
        temporal_options=repo.temporal_options,
    )
