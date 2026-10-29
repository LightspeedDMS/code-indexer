# ^ why owner-fair lanes, not one FIFO pool; one fan-out queued 128 tasks ahead of every later request; #2226

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

_BACKLOG_LOG_WAIT_SECONDS = 1.0
_BACKLOG_LOG_INTERVAL_SECONDS = 60.0


class _Task:
    __slots__ = ("future", "fn", "args", "enqueued_at")

    def __init__(self, fn: Callable[..., Any], args: Tuple[Any, ...]) -> None:
        self.future: Future = Future()
        self.fn = fn
        self.args = args
        self.enqueued_at = time.monotonic()


class _Lane:
    def __init__(self, name: str, max_threads: int) -> None:
        self.name = name
        self.max_threads = max_threads
        self._cond = threading.Condition()
        self._queues: "OrderedDict[object, Deque[_Task]]" = OrderedDict()
        self._running: Dict[object, int] = {}
        self._queued = 0
        self._idle = 0
        self._threads: List[threading.Thread] = []
        self._shutdown = False
        self._last_backlog_log = -_BACKLOG_LOG_INTERVAL_SECONDS
        self._max_wait = 0.0

    def submit(self, owner: object, fn: Callable[..., Any], args: Tuple) -> Future:
        task = _Task(fn, args)
        with self._cond:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._queues.setdefault(owner, deque()).append(task)
            self._queued += 1
            if self._idle < self._queued and len(self._threads) < self.max_threads:
                self._spawn()
            self._cond.notify()
        return task.future

    def _spawn(self) -> None:
        thread = threading.Thread(
            target=self._work,
            name=f"multi-search-{self.name}-{len(self._threads)}",
            daemon=True,
        )
        self._threads.append(thread)
        self._idle += 1
        thread.start()

    def _take(self) -> Tuple[object, _Task]:
        owner = min(self._queues, key=lambda o: self._running.get(o, 0))
        queue = self._queues[owner]
        task = queue.popleft()
        if queue:
            self._queues.move_to_end(owner)
        else:
            del self._queues[owner]
        self._queued -= 1
        self._idle -= 1
        self._running[owner] = self._running.get(owner, 0) + 1
        return owner, task

    def _backlog_message(self, waited: float) -> Optional[str]:
        self._max_wait = max(self._max_wait, waited)
        now = time.monotonic()
        if waited < _BACKLOG_LOG_WAIT_SECONDS:
            return None
        if now - self._last_backlog_log < _BACKLOG_LOG_INTERVAL_SECONDS:
            return None
        self._last_backlog_log = now
        stats = self.stats()
        return (
            f"multi_search lane {self.name}: a repository search waited "
            f"{waited:.2f}s for a thread; queued={stats['queued']} "
            f"running={stats['running']} threads={stats['threads']}"
            f"/{stats['max_threads']}"
        )

    def _work(self) -> None:
        # ^ invariant loop ends only at shutdown, after the queues drained
        while True:
            with self._cond:
                while not self._queues and not self._shutdown:
                    self._cond.wait()
                if not self._queues:
                    self._idle -= 1
                    return
                owner, task = self._take()
                message = self._backlog_message(time.monotonic() - task.enqueued_at)
            if message is not None:
                # ^ why INFO, not WARNING; queueing under load is expected and the e2e log audit fails on WARNING
                logger.info(message)
            try:
                self._run(task)
            finally:
                with self._cond:
                    self._idle += 1
                    left = self._running[owner] - 1
                    if left:
                        self._running[owner] = left
                    else:
                        del self._running[owner]

    @staticmethod
    def _run(task: _Task) -> None:
        if not task.future.set_running_or_notify_cancel():
            return
        try:
            result = task.fn(*task.args)
        except BaseException as exc:
            task.future.set_exception(exc)
        else:
            task.future.set_result(result)

    def stats(self) -> Dict[str, Any]:
        with self._cond:
            return {
                "threads": len(self._threads),
                "max_threads": self.max_threads,
                "running": sum(self._running.values()),
                "queued": self._queued,
                "max_wait_seconds": round(self._max_wait, 3),
            }

    def shutdown(self, wait: bool) -> None:
        with self._cond:
            self._shutdown = True
            self._cond.notify_all()
            threads = list(self._threads)
        if wait:
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join()


class FairLaneExecutor:
    def __init__(self, lane_threads: Mapping[str, int]) -> None:
        if not lane_threads:
            raise ValueError("at least one lane is required")
        for name, size in lane_threads.items():
            if size < 1:
                raise ValueError(f"lane {name!r} needs >= 1 thread, got {size}")
        self._lanes = {name: _Lane(name, size) for name, size in lane_threads.items()}
        self._default_lane = next(iter(lane_threads))

    @property
    def max_threads(self) -> int:
        return sum(lane.max_threads for lane in self._lanes.values())

    def submit_for(
        self, lane: str, owner: object, fn: Callable[..., Any], *args: Any
    ) -> Future:
        return self._lanes[lane].submit(owner, fn, args)

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        return self.submit_for(self._default_lane, object(), fn, *args)

    def stats(self) -> Dict[str, Dict[str, Any]]:
        return {name: lane.stats() for name, lane in self._lanes.items()}

    def shutdown(self, wait: bool = True) -> None:
        for lane in self._lanes.values():
            lane.shutdown(wait)
