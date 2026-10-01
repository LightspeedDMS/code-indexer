"""Bug #1821: TemporalSearchService.query_temporal() SearchEventContext race.

Root cause: `SearchEventContext` is one mutable object installed on a
`ContextVar`. Omni fan-out submits one search task per repo via
`contextvars.copy_context().run(...)` -- a documented, correct pattern for
propagating correlation_id across the executor boundary. But a context copy
is SHALLOW: every fan-out worker's copy still resolves `_search_event_ctx`
to the IDENTICAL `SearchEventContext` instance. Two repos whose embed calls
complete concurrently (both using the same provider, e.g. Voyage) therefore
race, UNSYNCHRONIZED, on the SAME three-field group (voyage_cache_hit /
voyage_cache_mode / voyage_latency_ms, or the cohere equivalent) at:

  - services/temporal/temporal_search_service.py's `query_temporal()`
    non-FSV backend branch (~line 641-666).

This is the exact same defect class Bug #1813 fixed at
`storage/filesystem_vector_store.py`'s `_write_embed_meta_to_event_ctx()` and
`server/services/search_service.py`'s inline Backend branch -- left unfixed
at the temporal call site (Bug #1821).

Because each triple-write is THREE separate attribute assignments with no
synchronization, a second writer's assignments can land in between a first
writer's assignments, producing a TORN triple that mixes fields from two
different repos' outcomes.

This test proves the race deterministically (not by hoping for a lucky GIL
interleaving): it drives two writers through the REAL
`TemporalSearchService.query_temporal()` production entry point (not a copy
of its logic), using a test double that gives the test explicit,
synchronized control over exactly when each writer is allowed to proceed
past the FIRST field of its triple. `coalesced_query_embedding` (a real
network/provider call in production) is patched to return canned metadata
per-writer -- this is the sole mock in the test; the SearchEventContext
double and the shared `TemporalSearchService` instance are real production
objects driven through the real `query_temporal()` code path.

Two measures rule out "writer-b simply hadn't been scheduled yet" as an
alternative explanation for a negative (non-interleaved) result:
  1. The test first waits for writer-b's OWN thread to confirm it has
     started running (received real CPU time from the OS scheduler) before
     starting the interleave-probe window at all.
  2. That probe window is generous relative to the handful of Python
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
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
from code_indexer.server.services.search_event_context import (
    SearchEventContext,
    _search_event_ctx,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchService,
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
    instead of depending on GIL-scheduling luck.
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


class _FakeNonFSVVectorStoreClient:
    """Stand-in for a non-FilesystemVectorStore backend (e.g. a shared
    server-side vector store client). isinstance(x, FilesystemVectorStore)
    must be False so query_temporal() takes the inline-embedding branch
    (lines ~627-672) that writes the racy triple.
    """

    def search(
        self,
        *,
        query_vector: List[float],
        filter_conditions: Dict[str, Any],
        limit: int,
        collection_name: str,
    ) -> List[Dict[str, Any]]:
        # Empty result set makes query_temporal() return immediately after
        # the ctx write (the "if not raw_results" early return), keeping
        # the test focused purely on the racy write itself.
        return []


class _FakeEmbeddingProvider:
    def get_provider_name(self) -> str:
        return "voyage-ai"


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


def _fake_coalesced_query_embedding(embedding_provider: Any, text: str, **_kwargs: Any):
    """Patched stand-in for the real (network-calling) coalesced_query_embedding.
    Returns per-writer canned metadata so writer-a and writer-b race with
    genuinely DIFFERENT outcomes -- proving a torn triple, not a coincidental
    identical-value collision.
    """
    writer_id = threading.current_thread().name
    meta = _META_A if writer_id == _WRITER_A else _META_B
    return [0.1, 0.2, 0.3], meta


def _start_writer(
    writer_id: str, service: TemporalSearchService, vector_store_client: Any
) -> _WriterHandle:
    """Start a thread that calls the REAL production entry point
    (TemporalSearchService.query_temporal), mirroring
    multi_search_service.py's own contextvars.copy_context() fan-out
    dispatch pattern exactly. Any exception raised on the worker thread
    (including the pausable double's own release-timeout guard) is
    captured instead of being silently lost.
    """
    ctx_snapshot = contextvars.copy_context()
    handle = _WriterHandle(thread=threading.Thread(name=writer_id))

    def _worker() -> None:
        handle.started.set()
        try:
            ctx_snapshot.run(
                service.query_temporal,
                query="q",
                time_range=("2020-01-01", "2020-01-02"),
                limit=10,
            )
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
    ctx: _PausableSearchEventContext,
    service: TemporalSearchService,
    vector_store_client: Any,
    writers: List[_WriterHandle],
) -> bool:
    """Start writer-a, wait for it to pause mid-write, start writer-b,
    confirm writer-b's thread is genuinely running, then probe whether
    writer-b can reach its own checkpoint while writer-a is still paused.
    Returns True iff writer-b interleaved (the DEFECT).
    """
    wa = _start_writer(_WRITER_A, service, vector_store_client)
    writers.append(wa)
    assert _wait_for_checkpoint(
        ctx, _WRITER_A, timeout=_CHECKPOINT_WAIT_TIMEOUT_SECS
    ), "writer-a never reached its checkpoint"

    wb = _start_writer(_WRITER_B, service, vector_store_client)
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


class TestTemporalSearchEventContextConcurrentWriteRace:
    """Bug #1821: concurrent omni fan-out writers on the temporal query path
    must not corrupt the shared SearchEventContext's provider cache-field
    triple.
    """

    def test_second_writer_cannot_mutate_context_while_first_writer_in_progress(
        self, tmp_path: Path
    ) -> None:
        vector_store_client = _FakeNonFSVVectorStoreClient()
        service = TemporalSearchService(
            config_manager=None,
            project_root=tmp_path,
            vector_store_client=vector_store_client,
            embedding_provider=_FakeEmbeddingProvider(),
            collection_name="test_collection",
        )

        ctx = _build_pausable_context()
        token = _search_event_ctx.set(ctx)
        writers: List[_WriterHandle] = []
        try:
            with patch(
                "code_indexer.server.services.governed_call.coalesced_query_embedding",
                side_effect=_fake_coalesced_query_embedding,
            ):
                b_interleaved = _run_race_probe(
                    ctx, service, vector_store_client, writers
                )
        finally:
            _cleanup_writers(ctx, writers)
            _search_event_ctx.reset(token)

        _assert_writers_finished_cleanly(writers)
        assert b_interleaved is False, (
            "BUG #1821: writer-b's field write interleaved with writer-a's "
            "still-in-progress triple write on the SAME shared "
            "SearchEventContext, driven through the REAL "
            "TemporalSearchService.query_temporal() production path -- "
            "unsynchronized concurrent omni fan-out writers can produce a "
            "torn (hit, mode, latency) triple."
        )
        _assert_coherent_final_triple(ctx)
