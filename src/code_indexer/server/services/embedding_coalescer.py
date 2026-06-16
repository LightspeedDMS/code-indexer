"""EmbeddingCoalescer — server-side embedding request coalescer (Story #1079 Phase D).

One instance per ``:embed`` lane. It accretes single-text embedding requests
into ONE sealed batch that is dispatched through the EXISTING
``ProviderConcurrencyGovernor`` as the **sole limiter**. The coalescer holds NO
semaphore and NO separate ``in_flight`` counter: the governor slot is acquired
and released PER HTTP ATTEMPT via the canonical pattern

    execute_with_backoff(lambda: governor.execute(lane, do_call,
                                                   acquire_timeout=ACQUIRE_TIMEOUT))

so 429 backoff sleeps happen OUTSIDE the slot (bug #1078 invariant). The
governor's slot-wait IS the accumulation window: while the dispatcher is parked
waiting for a slot, late arrivals join the open batch; the first attempt that
gets a slot seals it (membership snapshot), then issues exactly ONE HTTP call.

Dual-constraint sealing GUARANTEES one HTTP call per sealed batch — the batch
never sub-splits inside the provider — because the coalescer's token counter and
per-model limit are IDENTICAL to the provider's internal split predicate:

  - ``token_limit`` = ``int(provider._get_model_token_limit() * 0.9)`` (read from
    spec; voyage-code-3 -> 108000, voyage-2 -> 288000; NEVER hardcoded).
  - per-text count = the provider's OWN adapter: Voyage
    ``_count_tokens_accurately`` / Cohere ``_count_tokens``.
  - ``texts_cap`` = ``min(ceiling, provider._get_texts_per_request())`` when the
    provider exposes that method (Cohere), else ``ceiling`` (Voyage splits on
    tokens only and has no texts cap).

Shared fate: on success every caller gets its own order-preserved vector; on any
exception (429-exhausted, GovernorBusyError when no slot was ever granted,
sinbin, count-mismatch) EVERY coalesced caller receives that same exception, and
the open batch is sealed so a late joiner can't attach to a dead batch.

The ONLY time bound is the governor ``acquire_timeout`` (Messi #14). No
``time.sleep`` in production, no separate timer/threadpool. Every error explicit
(Messi #13).
"""

import logging
import threading
from concurrent.futures import Future
from typing import Any, Callable, List, Optional, Tuple

from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)
from code_indexer.services.provider_backoff import execute_with_backoff

logger = logging.getLogger(__name__)

# Default governor slot-wait timeout (reuse the provider call-site default).
_DEFAULT_ACQUIRE_TIMEOUT: float = 30.0

# Default texts-per-batch ceiling. Never exceeds the smallest provider texts cap
# (Cohere = 96). Phase E passes the configured value.
_DEFAULT_MAX_BATCH_SIZE: int = 96

# Provider split safety margin — matches the providers' own ``* 0.9`` /
# ``* 90 / 100`` margin (truncated to int, exactly as the providers do).
_TOKEN_SAFETY_MARGIN: float = 0.9


class _ProviderConstraints:
    """Resolved ``(texts_cap, token_limit, token_count_fn)`` for a provider.

    Provider-agnostic: a single introspection at construction picks the right
    adapter methods so the hot path carries no isinstance ladder. Voyage exposes
    ``_count_tokens_accurately`` and no ``_get_texts_per_request``; Cohere
    exposes ``_count_tokens`` and ``_get_texts_per_request``.
    """

    def __init__(self, provider: Any, ceiling: int) -> None:
        self.token_count_fn: Callable[[str], int] = _resolve_token_counter(provider)
        # token_limit mirrors the provider's split predicate to the token. The
        # safety margin is read from the provider's OWN spec
        # (model_specs["api_constraints"]["safety_margin_percentage"], default 90)
        # so it can never diverge from the provider's split threshold if a future
        # model spec changes its margin. Cohere computes
        # int(token_limit * pct / 100); we use the SAME arithmetic. Voyage exposes
        # no spec margin -> the 0.9 fallback (== 90/100) is applied.
        self.token_limit: int = _resolve_token_limit(provider)
        self.texts_cap: int = _resolve_texts_cap(provider, ceiling)


def _resolve_token_limit(provider: Any) -> int:
    """Compute the coalescer seal token limit, mirroring the provider's predicate.

    Reads ``provider.model_specs["api_constraints"]["safety_margin_percentage"]``
    when present (Cohere) and applies ``int(model_token_limit * pct / 100)`` — the
    EXACT form the provider uses internally. Falls back to the hardcoded 0.9
    margin (== 90/100) when the provider exposes no spec margin (Voyage).
    """
    model_token_limit = int(provider._get_model_token_limit())
    specs = getattr(provider, "model_specs", None)
    if isinstance(specs, dict):
        pct = specs.get("api_constraints", {}).get("safety_margin_percentage")
        if isinstance(pct, (int, float)) and pct > 0:
            return int(model_token_limit * pct / 100)
    return int(model_token_limit * _TOKEN_SAFETY_MARGIN)


def _resolve_token_counter(provider: Any) -> Callable[[str], int]:
    """Return the provider's own per-text token counter.

    Voyage's ``_count_tokens_accurately`` is preferred when present (and callable
    — a Cohere fake nulls it out); otherwise Cohere's ``_count_tokens``.
    """
    voyage_counter = getattr(provider, "_count_tokens_accurately", None)
    if callable(voyage_counter):
        return voyage_counter  # type: ignore[no-any-return]
    cohere_counter = getattr(provider, "_count_tokens", None)
    if callable(cohere_counter):
        return cohere_counter  # type: ignore[no-any-return]
    raise AttributeError(
        "provider exposes neither _count_tokens_accurately nor _count_tokens"
    )


def _resolve_texts_cap(provider: Any, ceiling: int) -> int:
    """Resolve the texts-per-batch cap.

    ``min(ceiling, provider._get_texts_per_request())`` when the provider defines
    a per-request texts cap (Cohere); else the configured ``ceiling`` (Voyage
    splits on tokens only).
    """
    getter = getattr(provider, "_get_texts_per_request", None)
    if callable(getter):
        return min(ceiling, int(getter()))
    return ceiling


def _resolve_provider_texts_cap(provider: Any) -> Optional[int]:
    """Return the provider's own per-request texts cap, or None if it has none.

    Cohere exposes ``_get_texts_per_request``; Voyage does not (tokens-only split).
    Used by the live (hot-reload) ceiling path to cap the runtime ceiling.
    """
    getter = getattr(provider, "_get_texts_per_request", None)
    if callable(getter):
        return int(getter())
    return None


class _Entry:
    """A single coalesced request: its text, embedding purpose, and the caller's Future."""

    __slots__ = ("text", "embedding_purpose", "fut")

    def __init__(self, text: str, embedding_purpose: str = "query") -> None:
        self.text = text
        self.embedding_purpose = embedding_purpose
        self.fut: "Future[List[float]]" = Future()


class EmbeddingCoalescer:
    """Coalesce single-text embeds into one governor-dispatched batch (per lane).

    Thread-safe via ONE lock. Holds no semaphore / in_flight — the governor is
    the sole limiter.
    """

    def __init__(
        self,
        lane: str,
        provider: Any,
        *,
        governor: Optional[ProviderConcurrencyGovernor] = None,
        acquire_timeout: float = _DEFAULT_ACQUIRE_TIMEOUT,
        coalesce_max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        ceiling_provider: Optional[Callable[[], int]] = None,
    ) -> None:
        self._lane = lane
        self._provider = provider
        self._governor = governor or ProviderConcurrencyGovernor.get_instance()
        self._acquire_timeout = acquire_timeout

        constraints = _ProviderConstraints(provider, coalesce_max_batch_size)
        self._token_count_fn = constraints.token_count_fn
        self.token_limit = constraints.token_limit
        # Static texts cap = min(config ceiling, provider per-request cap). Used
        # when no live ceiling_provider is supplied (tests / Phase D callers).
        self.texts_cap = constraints.texts_cap
        # Hot-reload plumbing (Phase E): when ceiling_provider is set,
        # effective_texts_cap() recomputes min(live_ceiling, provider_raw_cap) at
        # seal time so a runtime coalesce_max_batch_size change takes effect WITHOUT
        # rebuilding the coalescer. provider_raw_cap is the provider's own
        # _get_texts_per_request() (or None for Voyage — tokens-only split).
        self._ceiling_provider = ceiling_provider
        self._provider_raw_cap = _resolve_provider_texts_cap(provider)

        self._lock = threading.Lock()
        self._open_batch: Optional[List[_Entry]] = None
        self._open_tokens: int = 0

        # Observability counters (Phase E). Read for metrics/logging — the
        # coalescing ratio is texts_coalesced / batches_dispatched. Incremented
        # under self._lock when a batch is successfully dispatched (one HTTP call).
        self.batches_dispatched: int = 0
        self.texts_coalesced: int = 0

    # ------------------------------------------------------------------
    # Introspection (resolver telemetry — used by tests + Phase E)
    # ------------------------------------------------------------------

    def count_tokens(self, text: str) -> int:
        """Per-text token count via the provider's own adapter."""
        return self._token_count_fn(text)

    def effective_texts_cap(self) -> int:
        """Current texts-per-batch cap (live when a ceiling_provider is set).

        With a ``ceiling_provider`` (Phase E hot-reload): recompute
        ``min(live_ceiling, provider_raw_cap)`` so a runtime
        ``coalesce_max_batch_size`` change takes effect WITHOUT rebuilding the
        coalescer. Without one: the static ``texts_cap`` resolved at construction.
        """
        if self._ceiling_provider is None:
            return self.texts_cap
        live_ceiling = int(self._ceiling_provider())
        if self._provider_raw_cap is not None:
            return min(live_ceiling, self._provider_raw_cap)
        return live_ceiling

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def submit(self, text: str, embedding_purpose: str = "query") -> List[float]:
        """Submit one text; block until its embedding vector is available.

        Exactly one caller per batch is elected dispatcher; non-dispatchers block
        on a Future guaranteed to be completed (success OR exception — shared
        fate). The dispatcher seals the batch on the first attempt that gets a
        governor slot and issues exactly ONE HTTP call, then demuxes vectors back
        to every caller in submit order.

        Args:
            text: Text to embed.
            embedding_purpose: Purpose for the embedding call — "query" (default,
                for all serving-path callers) or "document" (indexing path).
                Forwarded to get_embeddings_batch so Cohere maps it to the
                correct input_type (search_query vs search_document).
        """
        entry = _Entry(text, embedding_purpose)
        n = self._token_count_fn(text)

        my_batch, i_am_dispatcher = self._enqueue(entry, n)

        if not i_am_dispatcher:
            # Future is set by THIS batch's dispatcher (always completed).
            return entry.fut.result()

        self._dispatch(my_batch)
        return entry.fut.result()

    # ------------------------------------------------------------------
    # Accretion (under lock) — dual-constraint sealing
    # ------------------------------------------------------------------

    def _enqueue(self, entry: _Entry, n: int) -> Tuple[List[_Entry], bool]:
        """Add ``entry`` to the open batch (or start a new one). Returns the
        batch this caller belongs to and whether this caller is its dispatcher.

        Sealing is would-exceed (``open_tokens + n > token_limit`` OR
        ``len >= texts_cap``), IDENTICAL to the provider split predicate, so a
        sealed batch never sub-splits in the provider.
        """
        with self._lock:
            # Resolve the texts cap ONCE per enqueue (live when a ceiling_provider
            # is set — Phase E hot-reload) so the join + seal predicates agree.
            cap = self.effective_texts_cap()
            if self._open_batch is None:
                self._open_batch = [entry]
                self._open_tokens = n
                my_batch = self._open_batch
                self._seal_if_full(cap)
                return my_batch, True

            # An open batch exists. Can this entry join it without exceeding?
            if (
                len(self._open_batch) < cap
                and (self._open_tokens + n) <= self.token_limit
            ):
                self._open_batch.append(entry)
                self._open_tokens += n
                my_batch = self._open_batch
                self._seal_if_full(cap)
                return my_batch, False

            # Adding would exceed a cap -> seal the current batch, start a new one
            # for which THIS caller is the dispatcher.
            self._open_batch = [entry]
            self._open_tokens = n
            my_batch = self._open_batch
            self._seal_if_full(cap)
            return my_batch, True

    def _seal_if_full(self, cap: int) -> None:
        """Seal the open batch (stop accretion) if it has hit either cap.

        Must be called under ``self._lock``. ``cap`` is the live texts cap resolved
        by the caller (``_enqueue``) so the join + seal predicates use the same
        value. Clearing ``open_batch`` means the next arrival opens a fresh batch
        with its own dispatcher (handles the cap==1 / oversized-single-text edge:
        a late joiner can't exceed the cap, so it opens its own batch).
        """
        if self._open_batch is None:
            return
        if len(self._open_batch) >= cap or self._open_tokens >= self.token_limit:
            self._open_batch = None
            self._open_tokens = 0

    # ------------------------------------------------------------------
    # Dispatch (governor is the only limiter)
    # ------------------------------------------------------------------

    def _dispatch(self, my_batch: List[_Entry]) -> None:
        """Dispatch ``my_batch`` through the governor and fan out shared fate.

        Seals ONCE on the first attempt that gets a slot (inside ``do_call``,
        under lock); ``execute_with_backoff`` may re-run ``do_call`` on a 429
        retry but the snapshot survives via closure nonlocals, so membership is
        stable. On any exception, the batch is sealed even if no slot was ever
        granted, then the exception fans out to EVERY caller.
        """
        sealed = False
        texts: Optional[List[str]] = None

        purpose: Optional[str] = None

        def do_call() -> List[List[float]]:
            nonlocal sealed, texts, purpose
            with self._lock:
                if not sealed:
                    sealed = True
                    if self._open_batch is my_batch:
                        self._open_batch = None
                        self._open_tokens = 0
                    texts = [e.text for e in my_batch]
                    # All entries in a query-path batch share the same purpose
                    # (all callers of coalesced_query_embedding pass "query").
                    # Read from the first entry; default to "query" defensively.
                    purpose = my_batch[0].embedding_purpose if my_batch else "query"
            if texts is None:  # pragma: no cover - set on first attempt, stable after
                raise RuntimeError("coalescer batch snapshot missing")
            # Exactly ONE HTTP call (retry=False -> provider makes a single attempt;
            # backoff is handled by execute_with_backoff OUTSIDE the governor slot).
            result: List[List[float]] = self._provider.get_embeddings_batch(
                texts, retry=False, embedding_purpose=purpose or "query"
            )
            return result

        try:
            vectors = execute_with_backoff(
                lambda: self._governor.execute(
                    self._lane, do_call, acquire_timeout=self._acquire_timeout
                )
            )
            if len(vectors) != len(my_batch):
                # Defensive invariant — RAISED (not assert; assert is stripped
                # under python -O). Fans out as shared fate below.
                raise ValueError(
                    f"provider returned {len(vectors)} vectors, "
                    f"expected {len(my_batch)}"
                )
            for e, v in zip(my_batch, vectors):
                e.fut.set_result(v)  # order-preserved demux
            # Observability (Phase E): one batch dispatched == one HTTP call.
            batch_size = len(my_batch)
            with self._lock:
                self.batches_dispatched += 1
                self.texts_coalesced += batch_size
            logger.debug(
                "coalescer dispatched batch lane=%s size=%d (batches=%d texts=%d)",
                self._lane,
                batch_size,
                self.batches_dispatched,
                self.texts_coalesced,
            )
        except BaseException as ex:  # noqa: BLE001
            # Shared-fate fan-out even if NO slot was ever granted (e.g.
            # GovernorBusyError): seal so a late joiner can't attach to a dead
            # batch, then set the exception on EVERY caller.
            with self._lock:
                if not sealed:
                    sealed = True
                    if self._open_batch is my_batch:
                        self._open_batch = None
                        self._open_tokens = 0
            for e in my_batch:
                if not e.fut.done():
                    e.fut.set_exception(ex)
