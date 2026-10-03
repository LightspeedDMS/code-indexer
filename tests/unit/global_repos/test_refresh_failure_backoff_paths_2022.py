"""Bug #2022 Gap 4: a repeatedly failing alias must back off on BOTH
submission paths -- the git schedule (``next_refresh``) and the ``local://``
trace-sync trigger, which never consults ``next_refresh``.

The failure that puts the alias into backoff is real: the real ``cidx
index`` child fails on an unreadable ``chunks.db`` (a permission failure,
never treated as corruption). Only the submission boundary is recorded
(``RecordingJobManager``) and, for trace sync, the external Langfuse fetch.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from code_indexer.server.services.langfuse_trace_sync_service import (
    LangfuseTraceSyncService,
)
from code_indexer.server.utils.config_manager import (
    LangfuseConfig,
    LangfusePullProject,
    ServerConfig,
)
from tests.utils.fatal_chunk_store_fixtures import DUMMY_VOYAGE_KEY, install_cidx_shim
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    REFRESH_INTERVAL_SECONDS,
    REPO,
    Harness,
    RecordingJobManager,
    build_harness,
)

#: Smallest persisted backoff the scheduler may apply after one failure.
MIN_EXPECTED_BACKOFF_SECONDS = 60
#: The ordinary schedule advance is interval +/- 10% jitter.
_ORDINARY_ADVANCE_FLOOR = 0.9


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


@pytest.fixture(autouse=True)
def cidx_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes; cannot produce a permission failure")
    shim_dir = install_cidx_shim(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VOYAGE_API_KEY", DUMMY_VOYAGE_KEY)


def _fail_refresh_on_unreadable_store(
    harness: Harness, force_reset: bool = False
) -> None:
    harness.source_db.chmod(0o000)
    try:
        with pytest.raises(Exception):
            harness.scheduler._execute_refresh(ALIAS, force_reset=force_reset)
    finally:
        harness.source_db.chmod(0o644)


def _run_one_scheduler_iteration(harness: Harness) -> None:
    scheduler = harness.scheduler

    def _stop_after_first_wait(timeout: float) -> bool:
        scheduler._running = False
        return False

    scheduler._running = True
    with patch.object(
        scheduler._stop_event, "wait", side_effect=_stop_after_first_wait
    ):
        scheduler._scheduler_loop()


def _next_refresh(harness: Harness) -> float:
    repo = harness.registry.get_global_repo(ALIAS)
    assert repo is not None
    return float(repo["next_refresh"])


def test_git_schedule_defers_backed_off_alias_via_next_refresh(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean", git_remote=True)
    # force_reset reaches indexing with no upstream change (fetch + reset
    # against the offline origin), so the real child fails on the store.
    _fail_refresh_on_unreadable_store(harness, force_reset=True)

    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    # The ordinary schedule makes the alias due again (next_refresh is
    # written at SUBMIT time, one interval ahead, independently of how the
    # job later ends). The failure backoff is persisted separately, so the
    # scheduler must consult it for a due alias and defer via next_refresh.
    harness.registry.update_next_refresh(ALIAS, time.time() - 5)
    loop_started = time.time()
    _run_one_scheduler_iteration(harness)

    assert jobs.submitted == [], "due but backed-off alias was submitted"
    # next_refresh is the backoff end, not the ordinary jittered interval
    # advance (which is never earlier than 90% of the interval).
    next_refresh = _next_refresh(harness)
    assert next_refresh >= loop_started + MIN_EXPECTED_BACKOFF_SECONDS
    assert (
        next_refresh < loop_started + _ORDINARY_ADVANCE_FLOOR * REFRESH_INTERVAL_SECONDS
    )


def _make_trace_sync_service(
    tmp_path: Path, harness: Harness
) -> LangfuseTraceSyncService:
    config = ServerConfig(
        server_dir=str(tmp_path / "server"),
        langfuse_config=LangfuseConfig(
            pull_enabled=True,
            pull_projects=[
                LangfusePullProject(public_key="pk_test", secret_key="sk_test")
            ],
        ),
    )
    service = LangfuseTraceSyncService(
        config_getter=lambda: config, data_dir=str(tmp_path / "server")
    )
    service._refresh_scheduler = harness.scheduler
    return service


def _sync_once(service: LangfuseTraceSyncService) -> None:
    """One sync in which the trace writer reports that REPO received writes.
    The Langfuse HTTP client and the trace-writing step are the external
    dependency here; the refresh-trigger loop under test is real."""
    creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")
    with (
        patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient"
        ) as client_class,
        patch.object(service, "_sync_project_inner", return_value=({REPO}, {})),
    ):
        client_class.return_value.discover_project.return_value = {"name": "Proj"}
        service.sync_project("https://langfuse.example.com", creds, trace_age_days=1)


def test_trace_sync_trigger_skips_backed_off_alias(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    service = _make_trace_sync_service(tmp_path, harness)

    _sync_once(service)
    assert jobs.submitted == [ALIAS], "control: a healthy alias is triggered"

    harness.scheduler.background_job_manager = None
    _fail_refresh_on_unreadable_store(harness)
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]

    _sync_once(service)
    assert jobs.submitted == [ALIAS], "trace sync re-submitted a backed-off alias"
