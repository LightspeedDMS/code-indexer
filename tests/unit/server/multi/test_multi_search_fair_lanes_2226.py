import threading
import time
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from code_indexer.server.multi.models import MultiSearchRequest
from code_indexer.server.multi.multi_search_config import MultiSearchConfig
from code_indexer.server.multi.multi_search_service import MultiSearchService

WAIT = 10.0


class _Gate:
    def __init__(self) -> None:
        self.permits = threading.Semaphore(0)
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.started: List[str] = []
        self.threads: set = set()

    def search(self, repo_id: str, request: Any) -> List[Dict[str, Any]]:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.started.append(repo_id)
            self.threads.add(threading.get_ident())
        try:
            if repo_id.startswith("slow-"):
                assert self.permits.acquire(timeout=WAIT), "test never released"
            if repo_id.startswith("boom-"):
                raise RuntimeError("index exploded")
            return [{"file_path": f"{repo_id}.py", "score": 0.5, "content": ""}]
        finally:
            with self.lock:
                self.running -= 1

    def wait_started(self, count: int) -> None:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.started) >= count:
                    return
            time.sleep(0.01)
        raise AssertionError(f"only {len(self.started)} of {count} searches started")


class _Request(threading.Thread):
    def __init__(self, service: MultiSearchService, request: MultiSearchRequest):
        super().__init__(daemon=True)
        self.service = service
        self.request = request
        self.response: Optional[Any] = None

    def run(self) -> None:
        self.response = self.service.search(self.request)


def _request(repos: List[str], search_type: str = "semantic") -> MultiSearchRequest:
    return MultiSearchRequest.model_validate(
        {"repositories": repos, "query": "q", "search_type": search_type, "limit": 10}
    )


def _file_paths(response: Any) -> Dict[str, List[str]]:
    return {
        repo: [row["file_path"] for row in rows]
        for repo, rows in response.results.items()
    }


@pytest.fixture
def gate():
    gate = _Gate()
    service = MultiSearchService(MultiSearchConfig(max_workers=2))
    with patch.object(service, "_search_semantic_sync", gate.search):
        with patch.object(service, "_search_fts_sync", gate.search):
            yield gate, service
            for _ in range(64):
                gate.permits.release()
            service.shutdown()


def test_second_request_is_not_starved_by_queued_fan_out(gate) -> None:
    gate, service = gate
    big = _Request(service, _request([f"slow-a{i}" for i in range(8)]))
    big.start()
    gate.wait_started(2)
    small = _Request(service, _request(["fast-b"]))
    small.start()
    time.sleep(0.3)

    gate.permits.release()
    small.join(timeout=3.0)

    assert not small.is_alive(), "one-repository request waited behind the fan-out"
    assert small.response is not None
    assert _file_paths(small.response) == {"fast-b": ["fast-b.py"]}


def test_fts_request_does_not_queue_behind_semantic_fan_out(gate) -> None:
    gate, service = gate
    semantic = _Request(service, _request([f"slow-s{i}" for i in range(6)]))
    semantic.start()
    gate.wait_started(2)

    fts = _Request(service, _request(["fast-f1", "fast-f2"], search_type="fts"))
    fts.start()
    fts.join(timeout=3.0)

    assert not fts.is_alive(), "fts fan-out queued behind semantic tasks"
    assert _file_paths(fts.response) == {
        "fast-f1": ["fast-f1.py"],
        "fast-f2": ["fast-f2.py"],
    }


def test_thread_bound_holds_across_concurrent_fan_outs(gate) -> None:
    gate, service = gate
    requests = [
        _Request(service, _request([f"slow-r{r}-{i}" for i in range(4)]))
        for r in range(5)
    ]
    for request in requests:
        request.start()
    gate.wait_started(2)

    stats = service.thread_executor.stats()["semantic"]
    assert stats["running"] == 2
    assert stats["threads"] == 2
    deadline = time.monotonic() + WAIT
    while service.thread_executor.stats()["semantic"]["queued"] < 18:
        assert time.monotonic() < deadline, "tasks never queued"
        time.sleep(0.01)

    for _ in range(20):
        gate.permits.release()
    for request in requests:
        request.join(timeout=WAIT)
        assert not request.is_alive()

    assert gate.peak == 2
    assert len(gate.threads) == 2
    assert service.thread_executor.max_threads == 6
    for index, request in enumerate(requests):
        assert request.response is not None
        assert set(request.response.results) == {f"slow-r{index}-{i}" for i in range(4)}


def test_per_repository_errors_and_results_are_merged(gate) -> None:
    gate, service = gate
    response = service.search(_request(["fast-ok", "boom-bad"]))

    assert _file_paths(response) == {"fast-ok": ["fast-ok.py"]}
    assert response.errors is not None and set(response.errors) == {"boom-bad"}
    assert response.metadata.total_repos_searched == 1


def test_queued_task_can_be_cancelled() -> None:
    from code_indexer.server.multi.fair_lane_executor import FairLaneExecutor

    release = threading.Event()
    executor = FairLaneExecutor({"only": 1})
    try:
        running = executor.submit(release.wait, WAIT)
        queued = executor.submit(lambda: "never")
        assert queued.cancel()
        release.set()
        assert running.result(timeout=WAIT) is True
        assert queued.cancelled()
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_backlog_is_logged_once_per_interval(caplog, monkeypatch) -> None:
    from code_indexer.server.multi import fair_lane_executor as module

    monkeypatch.setattr(module, "_BACKLOG_LOG_WAIT_SECONDS", 0.0)
    executor = module.FairLaneExecutor({"only": 1})
    try:
        with caplog.at_level("INFO", logger=module.__name__):
            futures = [executor.submit(lambda: None) for _ in range(5)]
            for future in futures:
                future.result(timeout=WAIT)
        messages = [
            r.getMessage() for r in caplog.records if "lane only" in r.getMessage()
        ]
        assert len(messages) == 1
        assert "queued=" in messages[0] and "threads=1/1" in messages[0]
    finally:
        executor.shutdown(wait=True)


def test_shutdown_refuses_new_tasks_and_validates_lanes() -> None:
    from code_indexer.server.multi.fair_lane_executor import FairLaneExecutor

    executor = FairLaneExecutor({"only": 1})
    assert executor.submit(lambda: 7).result(timeout=WAIT) == 7
    executor.shutdown(wait=True)
    with pytest.raises(RuntimeError):
        executor.submit(lambda: 8)
    with pytest.raises(ValueError):
        FairLaneExecutor({})
    with pytest.raises(ValueError):
        FairLaneExecutor({"zero": 0})
