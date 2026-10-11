"""Shared harness for the search-event recording tests of epic #2104.

Every test drives a REAL front door (MCP handler or REST route) and reads the
row that lands in a REAL search_event_log store (SQLite and PostgreSQL, see
search_event_real_stores) through the REAL SearchEventLogWriter.

Only job infrastructure that is not under test is replaced by small fakes
(BackgroundJobManager and JobTracker): they run the submitted job function
exactly as the real manager would, on demand.
"""

import inspect
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.search_event_log_writer import SearchEventLogWriter
from tests.unit.server.services.search_event_real_stores import RealStore, open_store

# A Rust X-Ray evaluator that reports one finding per candidate file.
ALWAYS_MATCH_EVALUATOR = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    vec![EvalFinding { pattern: "any".to_string(), line: node.start_line,'
    " snippet: String::new() }]\n"
    "}\n"
)


def make_user(username: str = "alice") -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def make_repo(directory: Path, files: int) -> Path:
    """A repository with `files` Python files, each containing one `def`."""
    repo = directory / "repo"
    repo.mkdir()
    for i in range(files):
        (repo / f"module_{i}.py").write_text(f"def example_{i}():\n    return {i}\n")
    return repo


class FakeBackgroundJobManager:
    """Runs submitted job functions on demand (BackgroundJobManager stand-in).

    Calls each job as the real manager does: `func(*args, **kwargs)`, adding
    `progress_callback` and `cancel_check` only when the function declares
    them. `cancel(job_id)` marks a job cancelled as BGM.cancel_job does (the
    running job is not interrupted; it sees the flag through cancel_check).
    With `cancel_on_progress`, the running job is cancelled the first time
    it reports progress -- a user cancel arriving while it works.
    """

    def __init__(self, cancel_on_progress: bool = False) -> None:
        self.jobs: Dict[str, Any] = {}
        self.results: Dict[str, Any] = {}
        self.cancelled: set = set()
        self.running_job_id: Optional[str] = None
        self.cancel_on_progress = cancel_on_progress
        self._pending: List[Tuple[str, Callable[..., Any], tuple, dict]] = []

    def cancel(self, job_id: str) -> None:
        self.cancelled.add(job_id)
        self.jobs[job_id] = SimpleNamespace(cancelled=True)

    def submit_job(
        self,
        operation_type: str,
        func: Callable[..., Any],
        *args: Any,
        submitter_username: str,
        repo_alias: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        job_id = f"job-{len(self._pending) + len(self.results)}"
        self._pending.append((job_id, func, args, kwargs))
        return job_id

    def run_pending(self) -> None:
        while self._pending:
            job_id, func, args, kwargs = self._pending.pop(0)
            declared = inspect.signature(func).parameters

            def _progress(*_: Any, _jid: str = job_id, **__: Any) -> None:
                if self.cancel_on_progress:
                    self.cancel(_jid)

            if "progress_callback" in declared:
                kwargs = {**kwargs, "progress_callback": _progress}
            if "cancel_check" in declared:
                kwargs = {
                    **kwargs,
                    "cancel_check": lambda _jid=job_id: _jid in self.cancelled,
                }
            self.running_job_id = job_id
            try:
                self.results[job_id] = func(*args, **kwargs)
            finally:
                self.running_job_id = None

    def get_job_status(self, job_id: str, username: str, **_: Any) -> None:
        return None

    def register_child_process(self, job_id: str, proc: Any) -> None:
        pass

    def unregister_child_processes(self, job_id: str) -> None:
        pass


class FakeJobTracker:
    """Records job lifecycle calls (JobTracker stand-in).

    `get_job` answers the job's current status as JobTracker.get_job does.
    With `cancel_on_start`, a job is cancelled the moment it reports
    "running": BackgroundJobManager.cancel_job fails a local X-Ray job in the
    tracker with the error "cancelled" (and kills its child processes, which
    makes the engine return early with whatever it has). `start_error` fails
    a job the moment it reports "running" with any other error.
    """

    def __init__(self, cancel_on_start: bool = False) -> None:
        self.completed: Dict[str, Any] = {}
        self.failed: Dict[str, Any] = {}
        self.statuses: Dict[str, str] = {}
        self.start_error: Optional[str] = "cancelled" if cancel_on_start else None

    def register_job(self, job_id: str, **_: Any) -> None:
        self.statuses[job_id] = "pending"

    def update_status(self, job_id: str, status: str, **_: Any) -> None:
        self.statuses[job_id] = status
        if self.start_error is not None and status == "running":
            self.fail_job(job_id, self.start_error)

    def get_job(self, job_id: str) -> Optional[SimpleNamespace]:
        status = self.statuses.get(job_id)
        if status is None:
            return None
        return SimpleNamespace(status=status, error=self.failed.get(job_id))

    def complete_job(self, job_id: str, result: Any) -> None:
        self.completed[job_id] = result
        if self.statuses.get(job_id) != "failed":
            self.statuses[job_id] = "completed"

    def fail_job(self, job_id: str, error: Any) -> None:
        self.failed[job_id] = error
        self.statuses[job_id] = "failed"


class Recorder(SimpleNamespace):
    store: RealStore
    writer: SearchEventLogWriter

    def rows(self, **filters: Any) -> List[Dict[str, Any]]:
        """Flush the writer into the store and return the matching rows.

        The writer's stop() drains everything queued; it is restarted at once
        so events enqueued later are still accepted.
        """
        self.writer.stop()
        events, _ = self.store.backend.query(limit=1000, **filters)
        self.writer.start()
        return list(reversed(events))


@contextmanager
def payload_cache_of(kind: str, directory: Path) -> Iterator[Any]:
    """A real server-mode PayloadCache of `kind` ("sqlite" or "postgres").

    SQLite: PayloadCacheSqliteBackend on a temp file (solo server).
    PostgreSQL: PayloadCachePostgresBackend on TEST_POSTGRES_DSN inside a
    throwaway schema, dropped on exit (cluster).
    """
    from code_indexer.server.cache.payload_cache import (
        PayloadCache,
        PayloadCacheConfig,
    )

    def _cache(backend: Any) -> Any:
        cache = PayloadCache(
            db_path=directory / "unused_payload_cache.db",
            config=PayloadCacheConfig(),
            storage_backend=backend,
        )
        cache.initialize()
        return cache

    if kind == "sqlite":
        from code_indexer.server.storage.sqlite_backends.payload_cache_backend import (
            PayloadCacheSqliteBackend,
        )

        yield _cache(PayloadCacheSqliteBackend(str(directory / "payload_cache.db")))
        return

    import uuid

    import psycopg
    from psycopg.conninfo import make_conninfo

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.payload_cache_backend import (
        PayloadCachePostgresBackend,
    )
    from tests.unit.server.services.search_event_real_stores import TEST_DSN

    schema = f"t_pc_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    pool = ConnectionPool(
        make_conninfo(TEST_DSN, options=f"-csearch_path={schema}"), max_size=4
    )
    try:
        yield _cache(PayloadCachePostgresBackend(pool))
    finally:
        pool.close()
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


@contextmanager
def recorder(kind: str, directory: Path) -> Iterator[Recorder]:
    """A real store of `kind` with a real, running writer over it."""
    with open_store(kind, directory) as store:
        writer = SearchEventLogWriter(store.backend)
        writer.start()
        try:
            yield Recorder(store=store, writer=writer)
        finally:
            writer.stop()
