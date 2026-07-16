"""Temporal-shard fault-injection testability lever -- Story #1400 Phase 10.

Both independent design passes found the same flaw in the naive approach:
temporal fusion computes its query embedding ONCE up front and reuses it
across all shards -- each shard is then a purely local HNSW lookup with NO
outbound HTTP call. The EXISTING FaultProfile-based harness intercepts
outbound HTTP (FaultInjectingSyncTransport/FaultInjectingTransport, keyed
by hostname via match_profile_snapshot) -- that mechanism cannot reach an
internal, non-network per-shard loop at all.

This module reuses the SAME FaultProfile dataclass, RNG, and history-
recording primitives via an alternate, exact-key lookup
(FaultInjectionService.get_profile(), already existing -- NOT the
URL/hostname-based match_profile_snapshot(), since "temporal-shard" is not
a hostname), rather than building a parallel mechanism.

CLI-safety: the shard loop (services/temporal/temporal_fusion_dispatch.py)
accepts an OPTIONAL `maybe_inject_internal_latency` callable, defaulting to
None (no-op) -- CLI/solo/daemon call sites never construct one, so their
behavior is byte-identical. Only the SERVER-side caller (wherever the
async temporal path is wired) constructs the real callable via
build_temporal_shard_latency_injector(app.state.fault_injection_service)
and passes it in -- mirroring the existing "None in CLI/solo, present in
server" injection pattern already used elsewhere in this codebase (e.g.
the embedding coalescer registry).
"""

import random
from typing import Callable

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.fault_profile import (
    jitter_uniform,
    roll_bernoulli,
)


def build_temporal_shard_latency_injector(
    service: FaultInjectionService,
) -> Callable[[str], None]:
    """Return a callable(target) that applies additive latency per the
    registered FaultProfile for `target`, or no-ops when the service is
    disabled or no profile is registered for that target.

    Only latency_rate/latency_ms_range are consulted -- this lever is
    additive-latency-only, unlike the full outcome-dispatch the HTTP
    transport layer implements (no terminating faults for an internal,
    non-network call site).
    """
    import time

    def _inject(target: str) -> None:
        if not service.enabled:
            return
        profile = service.get_profile(target)
        if profile is None or not profile.enabled:
            return

        rng = random.Random(service.draw_per_request_seed())
        if roll_bernoulli(profile.latency_rate, rng):
            delay_ms = jitter_uniform(*profile.latency_ms_range, rng)
            correlation_id = f"{target}-{rng.getrandbits(32):08x}"
            service.record_injection(target, "latency", correlation_id)
            time.sleep(delay_ms / 1000.0)

    return _inject
