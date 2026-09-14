"""Bug #1813 DEFECT 2: SearchEventContext concurrent-write race.

Root cause: mcp/handlers/search.py's search_code() creates exactly ONE
SearchEventContext per top-level call (omni or single-repo) and installs it
on the _search_event_ctx ContextVar. The omni fan-out
(multi/multi_search_service.py's _execute_parallel_search) then submits one
search task PER REPO to a shared ThreadPoolExecutor, each wrapped in
contextvars.copy_context().run(...) so the worker thread inherits the
calling context (a documented, correct pattern for propagating
correlation_id across the executor boundary). But contextvars.Context.run()
propagates a SHALLOW copy of the context: every fan-out worker's copy still
resolves _search_event_ctx.get() to the IDENTICAL SearchEventContext
instance. Two repos whose embed calls complete concurrently (both using the
same provider, e.g. Voyage) therefore race, UNSYNCHRONIZED, on the SAME
three-field group (voyage_cache_hit / voyage_cache_mode / voyage_latency_ms,
or the cohere equivalent) at:
  - storage/filesystem_vector_store.py's _write_embed_meta_to_event_ctx()
    (the FilesystemVectorStore search path), and
  - services/search_service.py's inline write in _perform_semantic_search's
    Backend branch (~line 626-635).

Because each triple-write is THREE separate attribute assignments with no
synchronization, a second writer's assignments can land in between a first
writer's assignments, producing a TORN triple that mixes fields from two
different repos' outcomes -- e.g. hit=True (repo A) with mode/latency from
repo B. This is a genuine data-integrity bug independent of Bug #1813's
Defect 1 (latency).

This test proves the race deterministically (not by hoping for a lucky GIL
interleaving): it drives two writers through the REAL
_write_embed_meta_to_event_ctx() production entry point, using a test
double that gives the test explicit, synchronized control over exactly when
each writer is allowed to proceed past the FIRST field of its triple. This
lets the test assert, without any flakiness, whether a second writer can
mutate the shared context WHILE a first writer's own triple-write is still
in progress -- the precise definition of the race.

Two measures rule out "writer-b simply hadn't been scheduled yet" as an
alternative explanation for a negative (non-interleaved) result:
  1. The test first waits for writer-b's OWN thread to confirm it has
     started running (received real CPU time from the OS scheduler) before
     starting the interleave-probe window at all.
  2. That probe window (1.5s) is generous relative to the handful of Python
     bytecodes between "thread started" and "reached the contested write" --
     on the UNFIXED code that gap is realistically microseconds, so a
     genuine race trivially wins the probe; on the FIXED (mutex-protected)
     code writer-b is not merely delayed but structurally blocked until
     writer-a is released, so no amount of extra probe time changes the
     outcome.

Both writers are always released and joined in a `finally` block so no
thread is ever leaked, even if an earlier assertion fails.
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
from code_indexer.server.services.search_event_context import (
    SearchEventContext,
    _search_event_ctx,
)
from code_indexer.storage.filesystem_vector_store import (
    _write_embed_meta_to_event_ctx,
)

_CHECKPOINT_WAIT_TIMEOUT_SECS = 3.0
_INTERLEAVE_PROBE_TIMEOUT_SECS = 1.5
_JOIN_TIMEOUT_SECS = 5.0

_WRITER_A = "writer-a"
_WRITER_B = "writer-b"

_TRIPLE_FIELD_NAMES = ("voyage_cache_hit", "cohere_cache_hit")

_META_A = EmbeddingCacheMetadata(
    key_found=True, cache_mode="on", provider_latency_ms=111
)
_META_B = EmbeddingCacheMetadata(
    key_found=False, cache_mode="shadow", provider_latency_ms=222
)


class _PausableSearchEventContext(SearchEventContext):
    """Test double: pauses execution right after the FIRST field of a
    provider cache triple ("voyage_cache_hit"/"cohere_cache_hit") is
    written, until the test explicitly releases that specific writer.

    This is NOT a mock of the system under test -- __setattr__ still
    performs plain attribute assignment (object.__setattr__) for every
    field; the pause is a synchronization hook keyed by the calling
    thread's name, giving the test deterministic control over interleaving
    instead of depending on GIL-scheduling luck. `checkpoints`/`releases`
    are populated and read by the module-level helper functions below, not
    by extra methods on this class. Constructor args mirror
    SearchEventContext's own required fields exactly (no `Any` escape).
    """

    checkpoints: Dict[str, threading.Event]
    releases: Dict[str, threading.Event]

    def __init__(
        self,
        username: str,
        repo_alias: Optional[str],
        search_type: str,
        query_text: str,
    ) -> None:
        object.__setattr__(self, "checkpoints", {})
        object.__setattr__(self, "releases", {})
        super().__init__(
            username=username,
            repo_alias=repo_alias,
            search_type=search_type,
            query_text=query_text,
        )

    def __setattr__(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)
        if name in _TRIPLE_FIELD_NAMES:
            checkpoints = object.__getattribute__(self, "checkpoints")
            releases = object.__getattribute__(self, "releases")
            writer_id = threading.current_thread().name
            if writer_id in checkpoints:
                checkpoints[writer_id].set()
                released = releases[writer_id].wait(
                    timeout=_CHECKPOINT_WAIT_TIMEOUT_SECS
                )
                if not released:
                    raise AssertionError(
                        f"writer {writer_id!r} was never released by the "
                        f"test within {_CHECKPOINT_WAIT_TIMEOUT_SECS}s"
                    )


def _register_writer(ctx: _PausableSearchEventContext, writer_id: str) -> None:
    ctx.checkpoints[writer_id] = threading.Event()
    ctx.releases[writer_id] = threading.Event()


def _wait_for_checkpoint(
    ctx: _PausableSearchEventContext, writer_id: str, timeout: float
) -> bool:
    return ctx.checkpoints[writer_id].wait(timeout=timeout)


def _release_writer(ctx: _PausableSearchEventContext, writer_id: str) -> None:
    ctx.releases[writer_id].set()


@dataclass
class _WriterHandle:
    """Result of _start_writer: the thread, any exception it raised, and an
    Event proving the thread genuinely started running (was scheduled by
    the OS) before it entered the production write call.
    """

    thread: threading.Thread
    errors: List[BaseException] = field(default_factory=list)
    started: threading.Event = field(default_factory=threading.Event)


def _start_writer(writer_id: str, meta: EmbeddingCacheMetadata) -> _WriterHandle:
    """Start a thread that writes `meta` via the REAL production entry point,
    mirroring multi_search_service.py's own contextvars.copy_context()
    fan-out dispatch pattern exactly. Any exception raised on the worker
    thread (including the pausable double's own release-timeout guard) is
    captured instead of being silently lost.
    """
    ctx_snapshot = contextvars.copy_context()
    handle = _WriterHandle(thread=threading.Thread(name=writer_id))

    def _worker() -> None:
        handle.started.set()
        try:
            ctx_snapshot.run(_write_embed_meta_to_event_ctx, meta, "voyage-ai")
        except BaseException as exc:  # noqa: BLE001
            handle.errors.append(exc)

    handle.thread = threading.Thread(name=writer_id, target=_worker)
    handle.thread.start()
    return handle


def _build_pausable_context() -> _PausableSearchEventContext:
    ctx = _PausableSearchEventContext(
        username="u", repo_alias=None, search_type="semantic", query_text="q"
    )
    _register_writer(ctx, _WRITER_A)
    _register_writer(ctx, _WRITER_B)
    return ctx


def _run_race_probe(
    ctx: _PausableSearchEventContext, writers: List[_WriterHandle]
) -> bool:
    """Start writer-a, wait for it to pause mid-write, start writer-b,
    confirm writer-b's thread is genuinely running, then probe whether
    writer-b can reach its own checkpoint while writer-a is still paused.
    Returns True iff writer-b interleaved (the DEFECT).
    """
    wa = _start_writer(_WRITER_A, _META_A)
    writers.append(wa)
    assert _wait_for_checkpoint(
        ctx, _WRITER_A, timeout=_CHECKPOINT_WAIT_TIMEOUT_SECS
    ), "writer-a never reached its checkpoint"

    wb = _start_writer(_WRITER_B, _META_B)
    writers.append(wb)
    assert wb.started.wait(timeout=_CHECKPOINT_WAIT_TIMEOUT_SECS), (
        "writer-b thread never started running -- cannot distinguish "
        "'blocked by synchronization' from 'not yet scheduled by the OS'"
    )

    return _wait_for_checkpoint(ctx, _WRITER_B, timeout=_INTERLEAVE_PROBE_TIMEOUT_SECS)


def _cleanup_writers(
    ctx: _PausableSearchEventContext, writers: List[_WriterHandle]
) -> None:
    """Release + join BOTH writers regardless of which assertion (if any)
    failed during the probe, so no thread is ever leaked.
    """
    _release_writer(ctx, _WRITER_A)
    _release_writer(ctx, _WRITER_B)
    for w in writers:
        w.thread.join(timeout=_JOIN_TIMEOUT_SECS)


def _assert_writers_finished_cleanly(writers: List[_WriterHandle]) -> None:
    for w in writers:
        assert not w.thread.is_alive(), f"{w.thread.name} did not terminate"
        assert not w.errors, f"{w.thread.name} raised: {w.errors}"


def _assert_coherent_final_triple(ctx: _PausableSearchEventContext) -> None:
    """The final state must be a coherent triple belonging to exactly ONE
    writer -- never a scrambled mix of both.
    """
    final = (ctx.voyage_cache_hit, ctx.voyage_cache_mode, ctx.voyage_latency_ms)
    assert final in (
        (True, "on", 111),
        (False, "shadow", 222),
    ), f"torn write detected: {final}"


class TestSearchEventContextConcurrentWriteRace:
    """DEFECT 2: concurrent omni fan-out writers must not corrupt the shared
    SearchEventContext's provider cache-field triple.
    """

    def test_second_writer_cannot_mutate_context_while_first_writer_in_progress(
        self,
    ) -> None:
        ctx = _build_pausable_context()
        token = _search_event_ctx.set(ctx)
        writers: List[_WriterHandle] = []
        try:
            b_interleaved = _run_race_probe(ctx, writers)
        finally:
            _cleanup_writers(ctx, writers)
            _search_event_ctx.reset(token)

        _assert_writers_finished_cleanly(writers)
        assert b_interleaved is False, (
            "DEFECT #1813-2: writer-b's field write interleaved with "
            "writer-a's still-in-progress triple write on the SAME shared "
            "SearchEventContext -- unsynchronized concurrent omni fan-out "
            "writers can produce a torn (hit, mode, latency) triple."
        )
        _assert_coherent_final_triple(ctx)
