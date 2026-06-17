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
import random
import threading
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)
from code_indexer.services.provider_backoff import execute_with_backoff

# Module dependency decision (Story #1146, intentional):
# embedding_coalescer -> query_embedding_cache is the ACCEPTED import direction.
# query_embedding_cache MUST NOT import embedding_coalescer (no circular import).
# build_key is imported lazily (inside _dispatch) to avoid top-level circular
# import issues during module load ordering in tests.

# Sentinel for over-cap texts when build_key returns None: we fall back to the
# raw text as the dedup key so identical over-cap texts still collapse.
_NONE_KEY_PREFIX = "\x00text:"

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

    __slots__ = (
        "text",
        "embedding_purpose",
        "fut",
        "audit_ctx",
        "no_embedding_cache_shortcut",
    )

    def __init__(
        self,
        text: str,
        embedding_purpose: str = "query",
        *,
        audit_ctx: Optional[Dict[str, Any]] = None,
        no_embedding_cache_shortcut: bool = False,
    ) -> None:
        self.text = text
        self.embedding_purpose = embedding_purpose
        self.fut: "Future[List[float]]" = Future()
        self.audit_ctx: Optional[Dict[str, Any]] = audit_ctx
        self.no_embedding_cache_shortcut: bool = no_embedding_cache_shortcut


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
        config_digest: str = "",
        anchor_depth_provider: Optional[Callable[[], int]] = None,
    ) -> None:
        self._lane = lane
        self._provider = provider
        self._governor = governor or ProviderConcurrencyGovernor.get_instance()
        self._acquire_timeout = acquire_timeout
        # Story #1146: config_digest namespaces dedup keys (same as cache identity).
        # An empty string is the fallback when no digest is supplied (text-based dedup
        # still works because build_key with an empty digest still normalizes text).
        self._config_digest = config_digest
        # FOLD IN #4 (Story #1146 review): callable returning the live anchor depth.
        # When set, called once per dispatch (under lock) so a runtime anchor-depth
        # change takes effect immediately without rebuilding the coalescer.
        # When None, build_key uses its own default (2). Same source as
        # QueryEmbeddingCache.build_key_for_provider so the two keys never diverge.
        self._anchor_depth_provider: Optional[Callable[[], int]] = anchor_depth_provider

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
        # Story #1146: dedup counters. dedup_savings = requestors_in_live_batch
        # minus unique_provider_texts_sent (how many embed calls were avoided).
        # provider_embed_calls = count of actual HTTP embed calls (one per batch).
        self.dedup_savings: int = 0
        self.provider_embed_calls: int = 0

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

    def submit(
        self,
        text: str,
        embedding_purpose: str = "query",
        *,
        no_embedding_cache_shortcut: bool = False,
        audit_ctx: Optional[Dict[str, Any]] = None,
    ) -> List[float]:
        """Submit one text; block until its embedding vector is available.

        Story #1147: Lock-free pre-enqueue cache check. Cache I/O runs BEFORE
        _enqueue() and OUTSIDE self._lock so on-mode cache HITs never consume a
        governor slot.

        Cache mode logic (Story #1147 sub-tasks 3a + 3b + 3d):
          - accessor get_query_embedding_cache() called HERE at submit time (not
            at constructor time) so CLI paths (None) are handled correctly and a
            cache installed after construction is picked up.
          - on   + HIT  -> return cached vector immediately (no _enqueue, no slot)
          - on   + MISS -> _enqueue -> dispatch -> record_miss after live result
          - shadow       -> always _enqueue -> dispatch; record_hit/miss after
          - off / disabled / None -> _enqueue -> dispatch (existing path)
          - bypass (no_embedding_cache_shortcut=True) -> skip READ, _enqueue ->
            dispatch, record_miss after live result (still WRITES)

        Args:
            text: Text to embed.
            embedding_purpose: Purpose for the embedding call — "query" (default,
                for all serving-path callers) or "document" (indexing path).
                Forwarded to get_embeddings_batch so Cohere maps it to the
                correct input_type (search_query vs search_document).
            no_embedding_cache_shortcut: When True, skip cache READ but still
                WRITE the result after live embedding. Has no effect when cache
                is absent/disabled/off.
            audit_ctx: Optional mutable dict; propagated to cache hit recording
                for Story #1110 deep-fidelity audit (stays at FSV chokepoint).
        """
        # --- Story #1147 3a: accessor at submit time (not constructor time) ---
        # Importing here avoids circular-import issues at module load time.
        from code_indexer.server.services.governed_call import (
            get_query_embedding_cache,
            get_query_embedding_cache_metrics,
        )

        cache = get_query_embedding_cache()

        # Cache context resolved once at submit time; passed to _dispatch so
        # post-dispatch cache writes (MISS write / shadow record_hit) happen
        # outside _lock after the live embed resolves.
        _cache_mode: Optional[str] = None
        _cache_qualifier: Any = None

        # --- Story #1147 3b: lock-free pre-enqueue cache check ---
        # All cache I/O BEFORE _enqueue() so on-mode HITs never touch the lock
        # or consume a governor slot.
        if cache is not None:
            provider_name: str = self._provider.get_provider_name()

            if cache.enabled_for(provider_name) and cache.mode_for(
                provider_name
            ) not in ("off",):
                _cache_mode = cache.mode_for(provider_name)
                _cache_qualifier = cache.qualifier(self._provider)

                if _cache_mode == "on" and not no_embedding_cache_shortcut:
                    # Build cache key (lazy import — same as _dispatch uses)
                    from code_indexer.server.services.query_embedding_cache import (
                        build_key,
                    )

                    _anchor: Optional[int] = (
                        self._anchor_depth_provider()
                        if self._anchor_depth_provider is not None
                        else None
                    )
                    if _anchor is not None:
                        cache_key_opt = build_key(
                            text, _anchor, config_digest=self._config_digest
                        )
                    else:
                        cache_key_opt = build_key(
                            text, config_digest=self._config_digest
                        )

                    if cache_key_opt is not None:
                        cached_blob = cache.lookup(cache_key_opt, _cache_qualifier)
                        if cached_blob is not None:
                            # Validate blob dimension
                            expected_bytes = _cache_qualifier.dimension * 4
                            if len(cached_blob) == expected_bytes:
                                import struct

                                try:
                                    n_floats = len(cached_blob) // 4
                                    decoded_vec: List[float] = list(
                                        struct.unpack(f"<{n_floats}f", cached_blob)
                                    )
                                    # Record hit (outside lock, cache I/O only)
                                    cache.record_hit(cache_key_opt, _cache_qualifier)
                                    metrics = get_query_embedding_cache_metrics()
                                    if metrics is not None:
                                        metrics.record_hit(
                                            mode=_cache_mode, provider=provider_name
                                        )
                                    # BLOCKING 1 fix: populate audit_ctx per-requestor
                                    # on sampled on-mode HITs (mirrors _serve_with_cache).
                                    if audit_ctx is not None:
                                        try:
                                            from code_indexer.server.services.governed_call import (
                                                _audit_sample_rate_for,
                                            )

                                            rate = _audit_sample_rate_for(provider_name)
                                            if rate > 0.0 and random.random() < rate:
                                                audit_ctx["sampled"] = True
                                                audit_ctx["mode"] = _cache_mode
                                                audit_ctx["provider"] = provider_name
                                                audit_ctx["cached_blob"] = cached_blob
                                                # No live_vec: Chunk B re-embeds from blob
                                        except Exception as _ae:  # noqa: BLE001
                                            logger.debug(
                                                "coalescer: audit_ctx population failed"
                                                " (on-mode HIT, lane=%s): %s",
                                                self._lane,
                                                _ae,
                                            )
                                    logger.debug(
                                        "coalescer: cache HIT (mode=on, provider=%s, lane=%s)",
                                        provider_name,
                                        self._lane,
                                    )
                                    # on-mode HIT: return immediately, zero slots consumed
                                    return decoded_vec
                                except struct.error as _se:
                                    logger.warning(
                                        "coalescer: corrupt cache blob (struct.error)"
                                        " provider=%s dim=%d blob_len=%d"
                                        " — treating as MISS: %s",
                                        provider_name,
                                        _cache_qualifier.dimension,
                                        len(cached_blob),
                                        _se,
                                    )
                            else:
                                logger.warning(
                                    "coalescer: corrupt cache blob dimension mismatch"
                                    " provider=%s expected_bytes=%d actual_bytes=%d"
                                    " — treating as MISS",
                                    provider_name,
                                    expected_bytes,
                                    len(cached_blob),
                                )

        # --- No cache / MISS / shadow / bypass / off / disabled ---
        # Enqueue into the coalescer batch (may acquire governor slot)
        entry = _Entry(
            text,
            embedding_purpose,
            audit_ctx=audit_ctx,
            no_embedding_cache_shortcut=no_embedding_cache_shortcut,
        )
        n = self._token_count_fn(text)

        my_batch, i_am_dispatcher = self._enqueue(entry, n)

        if not i_am_dispatcher:
            # Future is set by THIS batch's dispatcher (always completed).
            return entry.fut.result()

        # Story #1147 3b/3d: dispatcher runs live embed then writes to cache
        # (once per unique key, outside _lock) for MISS and shadow modes.
        self._dispatch(
            my_batch,
            cache=cache,
            cache_mode=_cache_mode,
            cache_qualifier=_cache_qualifier,
        )
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

    def _dispatch(
        self,
        my_batch: List[_Entry],
        *,
        cache: Any = None,
        cache_mode: Optional[str] = None,
        cache_qualifier: Any = None,
    ) -> None:
        """Dispatch ``my_batch`` through the governor and fan out shared fate.

        Seals ONCE on the first attempt that gets a slot (inside ``do_call``,
        under lock); ``execute_with_backoff`` may re-run ``do_call`` on a 429
        retry but the snapshot survives via closure nonlocals, so membership is
        stable. On any exception, the batch is sealed even if no slot was ever
        granted, then the exception fans out to EVERY caller.

        Story #1146 — dedup-by-key: within the sealed batch, entries sharing a
        dedup key collapse to ONE provider embed call. The first claimant per
        key supplies its real text; all same-key Futures receive the single
        computed vector. dedup_savings = requestors - unique_texts (live batches
        only). provider_embed_calls increments once per dispatched batch.

        Story #1147 3b/3d — post-dispatch cache writes: after live embed
        succeeds, write to cache once per unique key (outside _lock):
          - on-mode MISS (including bypass): record_miss_or_shadow per key
          - shadow-mode: record_hit if key existed, else record_miss_or_shadow

        Module dependency (intentional): build_key is imported from
        query_embedding_cache. The accepted direction is
            embedding_coalescer -> query_embedding_cache.
        query_embedding_cache MUST NOT import embedding_coalescer.
        """
        sealed = False
        # Snapshot structures set on the FIRST do_call invocation (under lock).
        # unique_texts: ordered unique texts to send to the provider.
        # key_to_first_idx: key -> index in unique_texts (first claimant).
        # entry_key_map: _Entry -> its dedup key (used for demux).
        unique_texts: Optional[List[str]] = None
        key_to_first_idx: Optional[Dict[str, int]] = None
        entry_keys: Optional[List[str]] = None
        purpose: Optional[str] = None

        def do_call() -> List[List[float]]:
            nonlocal sealed, unique_texts, key_to_first_idx, entry_keys, purpose
            with self._lock:
                if not sealed:
                    sealed = True
                    if self._open_batch is my_batch:
                        self._open_batch = None
                        self._open_tokens = 0
                    # --- Story #1146: build dedup structures (under lock, no I/O) ---
                    # Import build_key here (lazy) so that module-load order in tests
                    # does not create a top-level circular import.
                    from code_indexer.server.services.query_embedding_cache import (
                        build_key,
                    )

                    purpose = my_batch[0].embedding_purpose if my_batch else "query"
                    # FOLD IN #4: read live anchor depth once per dispatch (under
                    # lock, no I/O) so a runtime anchor-depth change takes effect
                    # immediately. When no anchor_depth_provider is wired, build_key
                    # uses its own default (2) — unchanged from the prior behaviour.
                    _anchor: Optional[int] = (
                        self._anchor_depth_provider()
                        if self._anchor_depth_provider is not None
                        else None
                    )
                    # For each entry: compute its dedup key. When build_key returns
                    # None (normalized text > 256 chars), fall back to exact-text
                    # dedup via a sentinel prefix so identical over-cap texts still
                    # collapse to one embed.
                    _unique: List[str] = []
                    _key_to_idx: Dict[str, int] = {}
                    _ekeys: List[str] = []
                    for e in my_batch:
                        if _anchor is not None:
                            k = build_key(
                                e.text,
                                _anchor,
                                config_digest=self._config_digest,
                            )
                        else:
                            k = build_key(
                                e.text,
                                config_digest=self._config_digest,
                            )
                        if k is None:
                            # Over-cap: key by exact text with a sentinel prefix so
                            # it cannot collide with a valid s:... key.
                            k = _NONE_KEY_PREFIX + e.text
                        _ekeys.append(k)
                        if k not in _key_to_idx:
                            _key_to_idx[k] = len(_unique)
                            _unique.append(e.text)  # REAL text (first claimant)
                    unique_texts = _unique
                    key_to_first_idx = _key_to_idx
                    entry_keys = _ekeys
                    # --- end dedup snapshot ---

            if unique_texts is None:  # pragma: no cover - set on first attempt
                raise RuntimeError("coalescer batch snapshot missing")
            # Exactly ONE HTTP call with only the UNIQUE texts (Story #1146 dedup).
            result: List[List[float]] = self._provider.get_embeddings_batch(
                unique_texts, retry=False, embedding_purpose=purpose or "query"
            )
            return result

        try:
            unique_vectors = execute_with_backoff(
                lambda: self._governor.execute(
                    self._lane, do_call, acquire_timeout=self._acquire_timeout
                )
            )
            # unique_vectors corresponds 1:1 with unique_texts (deduped provider texts).
            # Validate count against unique texts, not the full batch.
            if unique_texts is None or key_to_first_idx is None or entry_keys is None:
                # pragma: no cover — snapshot always set before execute_with_backoff
                raise RuntimeError("coalescer dedup snapshot missing after dispatch")
            n_unique = len(unique_texts)
            if len(unique_vectors) != n_unique:
                raise ValueError(
                    f"provider returned {len(unique_vectors)} vectors, "
                    f"expected {n_unique} (unique texts in deduplicated batch)"
                )
            # Multiplex: fan unique vectors back to all same-key Futures.
            # BLOCKING 1 fix (shadow audit_ctx): for shadow mode, we need to know
            # whether each unique key was a cache HIT or MISS to populate per-entry
            # audit_ctx in the fan-out. We pre-compute the shadow lookup results here
            # (outside _lock, before setting Futures) so the fan-out loop can use them.
            # For on-mode, audit_ctx population is not needed at dispatch time (MISSes
            # don't populate audit_ctx; HITs were handled earlier in submit()).
            _shadow_blobs: Optional[Dict[str, Optional[bytes]]] = None
            if (
                cache is not None
                and cache_mode == "shadow"
                and cache_qualifier is not None
                and key_to_first_idx is not None
            ):
                _shadow_blobs = {}
                for ukey in key_to_first_idx:
                    if ukey.startswith(_NONE_KEY_PREFIX):
                        _shadow_blobs[ukey] = None
                        continue
                    try:
                        _shadow_blobs[ukey] = cache.lookup(ukey, cache_qualifier)
                    except Exception as _sl_exc:  # noqa: BLE001
                        logger.debug(
                            "coalescer: shadow pre-lookup failed (lane=%s key=%.20s): %s",
                            self._lane,
                            ukey,
                            _sl_exc,
                        )
                        _shadow_blobs[ukey] = None

            for e, k in zip(my_batch, entry_keys):
                idx = key_to_first_idx[k]
                vec = unique_vectors[idx]
                e.fut.set_result(vec)

                # BLOCKING 1 fix: populate per-requestor audit_ctx for shadow HITs.
                # Each entry gets its OWN independent random draw (per-requestor, not
                # per-key). Fail-open: errors never propagate.
                if (
                    _shadow_blobs is not None
                    and e.audit_ctx is not None
                    and not k.startswith(_NONE_KEY_PREFIX)
                ):
                    shadow_b = _shadow_blobs.get(k)
                    if shadow_b is not None:
                        try:
                            from code_indexer.server.services.governed_call import (
                                _audit_sample_rate_for,
                            )

                            provider_name_for_audit = self._provider.get_provider_name()
                            rate = _audit_sample_rate_for(provider_name_for_audit)
                            if rate > 0.0 and random.random() < rate:
                                e.audit_ctx["sampled"] = True
                                e.audit_ctx["mode"] = "shadow"
                                e.audit_ctx["provider"] = provider_name_for_audit
                                e.audit_ctx["cached_blob"] = shadow_b
                                e.audit_ctx["live_vec"] = list(vec)
                        except Exception as _ae:  # noqa: BLE001
                            logger.debug(
                                "coalescer: audit_ctx population failed"
                                " (shadow HIT fan-out, lane=%s): %s",
                                self._lane,
                                _ae,
                            )

            # Story #1147 3b/3d: post-dispatch cache writes (outside _lock).
            # Write once per unique key so repeated-text dedup doesn't multi-write.
            # Bypass (no_embedding_cache_shortcut) skips READ but still WRITES here.
            # Guard: cache must be present, mode must be on or shadow, qualifier set.
            if (
                cache is not None
                and cache_mode in ("on", "shadow")
                and cache_qualifier is not None
                and key_to_first_idx is not None
                and unique_texts is not None
            ):
                try:
                    from code_indexer.server.services.governed_call import (
                        get_query_embedding_cache_metrics,
                    )

                    _metrics = get_query_embedding_cache_metrics()

                    for ukey, uidx in key_to_first_idx.items():
                        # Skip sentinel-prefix keys (over-cap texts that fell back
                        # to exact-text dedup — these have no valid cache key).
                        if ukey.startswith(_NONE_KEY_PREFIX):
                            continue
                        vec = unique_vectors[uidx]
                        pname = self._provider.get_provider_name()
                        if cache_mode == "on":
                            # on-mode: always a MISS at this point (HITs returned
                            # early in submit()). Write the live result.
                            cache.record_miss_or_shadow(ukey, cache_qualifier, vec)
                            # BLOCKING 2 fix: record miss metric (once per unique key).
                            if _metrics is not None:
                                _metrics.record_miss(mode="on", provider=pname)
                        else:
                            # shadow-mode: use pre-computed shadow blob (looked up above).
                            # record_hit if found (touches last_used); else MISS write.
                            # Story #1147: record_shadow_cosine fires ONCE per
                            # key-resolution (spec: "shadow asymmetry — one cosine
                            # per key-resolution inside the coalescer").
                            shadow_blob = (
                                _shadow_blobs.get(ukey)
                                if _shadow_blobs is not None
                                else None
                            )
                            if shadow_blob is None:
                                # Fallback: look up again if pre-computation was skipped.
                                try:
                                    shadow_blob = cache.lookup(ukey, cache_qualifier)
                                except Exception:  # noqa: BLE001
                                    shadow_blob = None
                            if shadow_blob is not None:
                                cache.record_hit(ukey, cache_qualifier)
                                # BLOCKING 2 fix: record shadow hit metric.
                                if _metrics is not None:
                                    try:
                                        _metrics.record_hit(
                                            mode="shadow", provider=pname
                                        )
                                        _metrics.record_shadow_cosine(
                                            cached_blob=shadow_blob, live_vec=vec
                                        )
                                    except Exception as _m_exc:  # noqa: BLE001
                                        logger.debug(
                                            "coalescer: record_shadow_cosine failed"
                                            " (lane=%s): %s",
                                            self._lane,
                                            _m_exc,
                                        )
                            else:
                                cache.record_miss_or_shadow(ukey, cache_qualifier, vec)
                                # BLOCKING 2 fix: record shadow miss metric.
                                if _metrics is not None:
                                    _metrics.record_miss(mode="shadow", provider=pname)
                except Exception as _cache_exc:  # noqa: BLE001
                    logger.warning(
                        "coalescer: post-dispatch cache write failed (lane=%s): %s",
                        self._lane,
                        _cache_exc,
                    )

            # Observability counters (Phase E + Story #1146).
            batch_size = len(my_batch)
            savings = batch_size - n_unique
            with self._lock:
                self.batches_dispatched += 1
                self.texts_coalesced += batch_size
                self.dedup_savings += savings
                self.provider_embed_calls += 1
            logger.debug(
                "coalescer dispatched batch lane=%s size=%d unique=%d savings=%d"
                " (batches=%d texts=%d dedup_savings=%d)",
                self._lane,
                batch_size,
                n_unique,
                savings,
                self.batches_dispatched,
                self.texts_coalesced,
                self.dedup_savings,
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
