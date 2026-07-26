"""CRITICAL #2 (2026-07-23 code review, Codex): TemporalShardResolver's
pin stack must be thread-local.

`self._pin_stack` was a single resolver-wide `Dict[key, List[Path]]`.
Concurrent threads pinning the SAME (embedder_slug, quarter) namespace
push onto the SAME list -- `get_pinned()` returns `stack[-1]`, so a second
thread's pin silently shadows the first thread's, and a thread reading
`get_pinned()` while another thread holds a DIFFERENT version pinned
observes the WRONG path. This breaks the refcount-matches-path-read
invariant AC8 Step 6 exists to guarantee: a thread could read from a path
whose refcount it never actually acquired (the other thread's).

This test uses a real QueryTracker, real AliasManager, real threads, and a
real Event + Barrier handshake to deterministically force BOTH pins to be
simultaneously active (no sleep-based timing) before asserting each thread
observes only its own pinned path.
"""

from __future__ import annotations

import threading
from pathlib import Path

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)


def _make_resolver(tmp_path: Path) -> TemporalShardResolver:
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "clone" / ".code-indexer" / "index"
    legacy_index_path.mkdir(parents=True)
    return TemporalShardResolver(
        alias_manager=AliasManager(str(aliases_dir)),
        repo_alias="evolution",
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
        query_tracker=QueryTracker(),
    )


def test_concurrent_pins_of_different_versions_are_thread_local(tmp_path):
    resolver = _make_resolver(tmp_path)
    pointer_namespace = "evolution-temporal-voyage_code_3-2024Q1"

    v1_dir = tmp_path / "sister" / ".versioned" / "ns" / "v_1000"
    v1_dir.mkdir(parents=True)
    resolver._alias_manager.create_alias(pointer_namespace, str(v1_dir))
    v2_dir = tmp_path / "sister" / ".versioned" / "ns" / "v_2000"

    a_pinned_v1 = threading.Event()
    entered_barrier = threading.Barrier(2)
    checked_barrier = threading.Barrier(2)
    results: dict = {}
    results_lock = threading.Lock()
    errors: list = []

    def thread_a():
        try:
            with resolver.pin("voyage_code_3", "2024Q1") as pinned:
                with results_lock:
                    results["a_pinned_path"] = pinned.path
                a_pinned_v1.set()
                entered_barrier.wait(timeout=5)
                # Both pins are now simultaneously active.
                observed = resolver.get_pinned("voyage_code_3", "2024Q1")
                with results_lock:
                    results["a_observed"] = observed
                checked_barrier.wait(timeout=5)
        except Exception as exc:  # noqa: BLE001
            with results_lock:
                errors.append(exc)

    def thread_b():
        try:
            assert a_pinned_v1.wait(timeout=5), "thread A never pinned V1"
            v2_dir.mkdir(parents=True)
            resolver._alias_manager.swap_alias(
                pointer_namespace, str(v2_dir), str(v1_dir)
            )
            with resolver.pin("voyage_code_3", "2024Q1") as pinned:
                with results_lock:
                    results["b_pinned_path"] = pinned.path
                entered_barrier.wait(timeout=5)
                observed = resolver.get_pinned("voyage_code_3", "2024Q1")
                with results_lock:
                    results["b_observed"] = observed
                checked_barrier.wait(timeout=5)
        except Exception as exc:  # noqa: BLE001
            with results_lock:
                errors.append(exc)

    t_a = threading.Thread(target=thread_a)
    t_b = threading.Thread(target=thread_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert not errors, f"unexpected exceptions in threads: {errors}"
    assert results.get("a_pinned_path") == v1_dir
    assert results.get("b_pinned_path") == v2_dir
    assert results["a_observed"] == v1_dir, (
        "thread A must observe ITS OWN pinned path (V1) via get_pinned(), "
        "not thread B's (V2) -- the pin stack must be thread-local"
    )
    assert results["b_observed"] == v2_dir, (
        "thread B must observe ITS OWN pinned path (V2) via get_pinned(), "
        "not thread A's (V1) -- the pin stack must be thread-local"
    )
