"""Bug #1533: BOTH temporal front doors must inject the DI-wired
ActivatedRepoManager into the worker.

The worker cannot fetch the manager for itself -- constructing one is exactly
the defect (it lands on a node-local metadata store, which on a cluster node
is empty, so a real activation reads as "no golden lineage" and the query
silently returns zero results). The manager therefore has to be threaded from
the door, which holds it on ``app.state``, down through
``execute_live_temporal_search``'s ``submit_job(...)`` kwargs.

Bug #1482 is the cautionary precedent for the door half of this file: the
identical ``query_tracker`` kwarg was wired into the MCP door and NOT into the
REST door, and that omission silently forced every REST temporal query back
onto an empty read path. One door wired is not "wired". Structural (AST)
assertions are used for the two door call sites because invoking those
handlers requires a whole authenticated request stack, whereas the property
being protected -- "this kwarg is present at this call site" -- is exactly a
structural one. The dispatch layer's own forwarding is proven behaviorally
instead, by running the real function.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.server.services.temporal_live_dispatch import (
    execute_live_temporal_search,
)

KWARG_NAME = "activated_repo_manager"
DISPATCH_CALL = "execute_live_temporal_search"

DOOR_FILES = {
    "MCP (mcp/handlers/search.py)": "src/code_indexer/server/mcp/handlers/search.py",
    "REST (routers/inline_query.py)": "src/code_indexer/server/routers/inline_query.py",
}

# Query-shape values for the stub worker input. Arbitrary but named, since
# nothing in this file depends on their magnitudes -- only on the kwargs the
# dispatch layer forwards.
REQUESTED_LIMIT = 10
FUSION_FETCH_LIMIT = 30
OPEN_ENDED_TIME_RANGE = ("0001-01-01", "9999-12-31")

# 0.0 is execute_live_temporal_search's documented "always hand off
# immediately" contract: it returns the deferred envelope WITHOUT consulting
# job status, so this test neither races nor sleeps.
IMMEDIATE_HANDOFF_WAIT_SECONDS = 0.0
NO_RESPONSE_RESERVE_SECONDS = 0.0

STUB_JOB_ID = "job-1533"


def _repo_root() -> Path:
    # tests/unit/server/services/<this file> -> repo root
    return Path(__file__).resolve().parents[4]


def _dispatch_call_keywords(source_path: Path) -> List[List[str]]:
    """Every execute_live_temporal_search(...) call's kwarg names, from the
    REAL parsed source of a production door."""
    tree = ast.parse(source_path.read_text())
    calls: List[List[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (func.id if isinstance(func, ast.Name) else None)
        )
        if name == DISPATCH_CALL:
            calls.append([kw.arg for kw in node.keywords if kw.arg is not None])
    return calls


@pytest.mark.parametrize("door_label,rel_path", sorted(DOOR_FILES.items()))
def test_door_passes_activated_repo_manager(door_label: str, rel_path: str) -> None:
    """Every dispatch call site in each door must pass the kwarg.

    Asserted per call site, not merely "somewhere in the file": a door that
    grows a second temporal dispatch path must wire it too.
    """
    calls = _dispatch_call_keywords(_repo_root() / rel_path)

    assert calls, (
        f"{door_label}: no {DISPATCH_CALL}(...) call found -- this guard has "
        "gone stale and would silently stop protecting anything"
    )
    for keywords in calls:
        assert KWARG_NAME in keywords, (
            f"{door_label}: a {DISPATCH_CALL}(...) call omits {KWARG_NAME}. "
            "Without it the worker falls back to constructing its own "
            "node-local ActivatedRepoManager, which on a cluster node cannot "
            "see the activation and makes the temporal query return zero "
            "results (Bug #1533; same failure shape as Bug #1482's REST gap)."
        )


class _RecordingBackgroundJobManager:
    """Records the worker kwargs a real submit_job() call would receive.

    ``Any`` on the signature is unavoidable and faithful here: BGM's real
    ``submit_job`` accepts an arbitrary worker callable plus arbitrary
    worker kwargs, so no narrower type exists to express.
    """

    def __init__(self) -> None:
        self.worker_kwargs: Dict[str, Any] = {}

    def submit_job(self, operation_type: str, worker_fn: Any, **kwargs: Any) -> str:
        self.worker_kwargs = dict(kwargs)
        return STUB_JOB_ID

    def get_job_status(
        self, job_id: str, username: str, is_admin: bool
    ) -> Dict[str, str]:
        return {"status": "running"}


class _StubWorkerInput:
    """The TemporalWorkerInput attributes the dispatch layer reads to build
    its dedup signature."""

    username = "alice"
    repository_alias = "myclone"
    query_text = "auth"
    requested_limit = REQUESTED_LIMIT
    fusion_fetch_limit = FUSION_FETCH_LIMIT
    time_range = OPEN_ENDED_TIME_RANGE
    time_range_all = True
    file_path_filter = None
    at_commit = None
    language = None
    exclude_language = None
    exclude_path = None
    diff_types = None
    author = None
    chunk_type = None
    temporal_embedder = None
    rerank_query = None
    rerank_instruction = None


class _PassthroughDedupCache:
    """Submits unconditionally, so the test observes the real submit path.

    ``Any`` matches the real cache's own contract: the signature is an opaque
    canonicalized value and the two arguments are caller-supplied callables.
    """

    def get_or_submit(self, signature: Any, status_check: Any, submit: Any) -> str:
        job_id: str = submit()
        return job_id


def test_dispatch_forwards_manager_into_the_worker() -> None:
    """The dispatch layer must actually hand the manager to the worker.

    Runs the REAL execute_live_temporal_search with its documented worker_fn
    test seam, then inspects what the worker would have been called with.
    """
    bgm = _RecordingBackgroundJobManager()
    sentinel_manager = object()

    result = execute_live_temporal_search(
        worker_input=_StubWorkerInput(),  # type: ignore[arg-type]
        background_job_manager=bgm,  # type: ignore[arg-type]
        payload_cache=None,  # type: ignore[arg-type]
        access_filtering_service=None,
        is_admin=False,
        inline_wait_seconds=IMMEDIATE_HANDOFF_WAIT_SECONDS,
        handler_deadline_monotonic=None,
        response_reserve_seconds=NO_RESPONSE_RESERVE_SECONDS,
        dedup_cache=_PassthroughDedupCache(),  # type: ignore[arg-type]
        worker_fn=lambda **kwargs: {"result_ready": True},
        activated_repo_manager=sentinel_manager,
    )

    assert result["job_id"] == STUB_JOB_ID
    assert bgm.worker_kwargs.get(KWARG_NAME) is sentinel_manager, (
        "execute_live_temporal_search must forward activated_repo_manager "
        "into submit_job's worker kwargs (the same mechanism query_tracker "
        f"already uses); submitted kwargs were {sorted(bgm.worker_kwargs)}"
    )


def test_worker_accepts_the_forwarded_kwarg() -> None:
    """The forwarding above is only meaningful if the worker declares the
    parameter -- BGM passes worker kwargs through by name."""
    from code_indexer.server.services.temporal_worker import run_temporal_worker

    assert KWARG_NAME in inspect.signature(run_temporal_worker).parameters
