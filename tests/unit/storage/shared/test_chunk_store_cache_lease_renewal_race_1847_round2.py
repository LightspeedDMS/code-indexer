"""Bug #1847 Round 2, Defect 2 -- renewal thread startup is single-flight."""

import threading
import time

from code_indexer.storage.shared import chunk_store_cache


def test_renewal_thread_check_and_start_is_single_flight(monkeypatch):
    real_thread = threading.Thread

    class ProbeThread:
        starts = 0
        starts_lock = threading.Lock()

        def __init__(self, *, target, name, daemon):
            self.target = target
            self.started = False

        def is_alive(self):
            # Give both callers time to observe the same pre-start state.
            time.sleep(0.01)
            return self.started

        def start(self):
            with self.starts_lock:
                type(self).starts += 1
                self.started = True

    monkeypatch.setattr(chunk_store_cache.threading, "Thread", ProbeThread)
    cache = chunk_store_cache.ChunkStoreThreadCache(
        lease_root=None, is_versioned_snapshot=lambda _path: False
    )
    # ProbeThread is a deliberate duck-typed stand-in for threading.Thread
    # (not a real subclass), so mypy's declared Optional[Thread] type on
    # _lease_thread cannot see it as compatible -- safe to ignore here.
    cache._lease_thread = ProbeThread(  # type: ignore[assignment]
        target=lambda: None, name="probe", daemon=True
    )

    barrier = threading.Barrier(2)

    def invoke():
        barrier.wait(timeout=2)
        cache._ensure_lease_renewal_thread()

    callers = [real_thread(target=invoke) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=2)

    assert all(not caller.is_alive() for caller in callers)
    assert ProbeThread.starts == 1
