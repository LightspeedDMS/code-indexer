"""Per-attempt timeout of one outbound embedding request (Bug #2127).

The provider config's ``timeout`` is the QUERY budget: one short text, a
user waiting. An INDEXING request carries a batch of up to the model's token
limit, and abandoning it is expensive: the provider may still process and
bill a request the client gave up on, and the retry loop then resends (and
pays for) the same batch. The provider's periodic latency peaks sit right
at the 30 s default, so indexing waits ``INDEXING_TIMEOUT_MULTIPLIER`` times
the configured timeout instead.

This is the per-request outbound embedding timeout, the only timeout the
indexing path legitimately carries (Bug #1218). It is a code constant, not
a setting, so every deployment gets it without a config migration: the
persisted ``timeout`` value is unchanged and keeps governing queries.

Callers pass the returned timeout on EVERY ``.post()`` (query and indexing
alike): a pooled keep-alive client keeps the timeout of the request that
built it, so without the per-request value a query could inherit the
indexing budget (or a reranker's) from whichever call built the client.
"""

from __future__ import annotations

import httpx

#: Indexing requests wait this many times the configured provider timeout
#: (30 s default -> 120 s).
INDEXING_TIMEOUT_MULTIPLIER = 4


def embedding_request_timeout(
    timeout_s: float, connect_timeout_s: float, *, indexing: bool
) -> httpx.Timeout:
    """The httpx timeout of one embedding attempt.

    Args:
        timeout_s: The provider config's ``timeout`` (the query budget).
        connect_timeout_s: The provider config's ``connect_timeout``;
            establishing a connection is not affected by provider load.
        indexing: True for an indexing (document) request.
    """
    if timeout_s <= 0:
        raise ValueError(f"provider timeout must be positive, got {timeout_s}")
    budget = timeout_s * INDEXING_TIMEOUT_MULTIPLIER if indexing else timeout_s
    return httpx.Timeout(
        connect=connect_timeout_s, read=budget, write=budget, pool=budget
    )
