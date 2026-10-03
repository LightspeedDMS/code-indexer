"""Real RefreshScheduler harness for Bug #2022 Gap 4 tests.

Builds a golden repo whose source holds a real CHUNKS_DB collection, an
optional published snapshot under ``.versioned/`` (the alias target), the
real SQLite global registry, the real snapshot manager with the real
``LocalCloneBackend`` reflink primitive, and whatever real metadata store
the caller passes in.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List, Optional, Set
from unittest.mock import Mock

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from tests.utils.fatal_chunk_store_fixtures import (
    COLLECTION_NAME,
    corrupt_btree_pages,
    make_chunks_db_repo,
    write_metadata_marker,
)

REPO = "my-repo"
ALIAS = "my-repo-global"
OLD_SNAPSHOT_NAME = "v_1000000000"
GIT_REPO_URL = "https://git.example.com/org/my-repo.git"
SNAPSHOT_MODES = ("clean", "corrupt", "none")
REFRESH_INTERVAL_SECONDS = 3600
_MAX_CONCURRENT_JOBS = 4
_MAX_CONCURRENT_REFRESH_JOBS = 2
_MAX_CAUSE_CHAIN_DEPTH = 32


class RecordingJobManager:
    """Stands in for BackgroundJobManager at the submission boundary only:
    records which aliases were submitted, never runs them."""

    def __init__(self) -> None:
        self.submitted: List[str] = []
        self.max_concurrent_jobs = _MAX_CONCURRENT_JOBS
        self._background_jobs_config = SimpleNamespace(
            max_concurrent_refresh_jobs=_MAX_CONCURRENT_REFRESH_JOBS
        )

    def submit_job(self, **kwargs: Any) -> str:
        self.submitted.append(kwargs["repo_alias"])
        return f"job-{len(self.submitted)}"

    def count_active_refresh_jobs(self) -> int:
        return 0


@dataclass
class Harness:
    scheduler: RefreshScheduler
    metadata: Any
    registry: GlobalRegistry
    source: Path
    snapshot: Optional[Path]

    @property
    def source_db(self) -> Path:
        return self.source / ".code-indexer" / "index" / COLLECTION_NAME / "chunks.db"

    def snapshot_db(self) -> Path:
        assert self.snapshot is not None
        return self.snapshot / ".code-indexer" / "index" / COLLECTION_NAME / "chunks.db"

    def strikes(self) -> int:
        state = self.metadata.get_refresh_integrity_failure_state(ALIAS)
        return 0 if state is None else int(state["consecutive_failure_count"])


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iter_chain(exc: Optional[BaseException]) -> Iterator[BaseException]:
    seen: Set[int] = set()
    while (
        exc is not None and id(exc) not in seen and len(seen) < _MAX_CAUSE_CHAIN_DEPTH
    ):
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def typed_kinds(exc: BaseException) -> List[str]:
    """Every ``kind`` carried by an exception in ``exc``'s cause chain."""
    kinds = [getattr(e, "kind", None) for e in iter_chain(exc)]
    return [str(getattr(k, "value", k)) for k in kinds if k is not None]


def run_one_scheduler_iteration(harness: "Harness") -> None:
    """Run the real scheduler loop for exactly one pass (it stops at its
    first poll wait)."""
    from unittest.mock import patch

    scheduler = harness.scheduler

    def _stop_after_first_wait(timeout: float) -> bool:
        scheduler._running = False
        return False

    scheduler._running = True
    with patch.object(
        scheduler._stop_event, "wait", side_effect=_stop_after_first_wait
    ):
        scheduler._scheduler_loop()


def _attach_local_origin(source: Path, tmp_path: Path) -> None:
    """Give ``source`` a real, offline ``origin`` it tracks."""
    # Named like a real remote (<repo>.git): cidx derives the project id
    # from the remote's basename, which must match the existing index.
    origin = tmp_path / f"{source.name}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(origin)], check=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)], cwd=source, check=True
    )
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=source, check=True)
    subprocess.run(
        ["git", "branch", "-q", "--set-upstream-to=origin/master", "master"],
        cwd=source,
        check=True,
    )


def build_harness(
    tmp_path: Path, metadata: Any, snapshot_mode: str, git_remote: bool = False
) -> Harness:
    """snapshot_mode: 'clean' | 'corrupt' | 'none' (first-ever refresh).
    git_remote=True registers a remote-git repo whose clone pulls from a
    local bare origin; otherwise a ``local://`` repo."""
    if snapshot_mode not in SNAPSHOT_MODES:
        raise ValueError(f"snapshot_mode must be one of {SNAPSHOT_MODES}")
    golden = tmp_path / "data" / "golden-repos"
    golden.mkdir(parents=True)
    source = golden / REPO
    make_chunks_db_repo(source, metadata_marker="source-new")

    snapshot: Optional[Path] = None
    if snapshot_mode != "none":
        snapshot = golden / ".versioned" / REPO / OLD_SNAPSHOT_NAME
        shutil.copytree(source, snapshot, symlinks=True)
        write_metadata_marker(snapshot, "snapshot-good")
        if snapshot_mode == "corrupt":
            corrupt_btree_pages(
                snapshot / ".code-indexer" / "index" / COLLECTION_NAME / "chunks.db"
            )
    if git_remote:
        _attach_local_origin(source, tmp_path)
    current_target = snapshot if snapshot is not None else source

    db_path = golden.parent / "cidx_server.db"
    DatabaseSchema(str(db_path)).initialize_database()
    registry = GlobalRegistry(str(golden), use_sqlite=True, db_path=str(db_path))
    repo_url = GIT_REPO_URL if git_remote else f"local://{REPO}"
    registry.register_global_repo(REPO, ALIAS, repo_url, index_path=str(current_target))
    config = Mock()
    config.get_global_refresh_interval.return_value = REFRESH_INTERVAL_SECONDS
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
        snapshot_manager=VersionedSnapshotManager(
            versioned_base=str(golden),
            clone_backend=LocalCloneBackend(versioned_base=str(golden)),
        ),
        golden_repo_metadata_backend=metadata,
    )
    scheduler.alias_manager.create_alias(ALIAS, str(current_target), repo_name=REPO)
    return Harness(scheduler, metadata, registry, source, snapshot)
