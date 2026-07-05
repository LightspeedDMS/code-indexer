"""Unit tests for the WindowedCacheMetrics pure aggregation algorithm
(Story #1294, Epic #1288).

These tests exercise `compute_aggregate()` / `build_windowed_result()` /
`empty_windowed_result()` directly on hand-built row dicts (no DB), proving
the exact Algorithm section of Story #1294:

  hits   = COUNT rows WHERE outcome IN (hit, shadow_hit)      # joiners count as hits
  misses = COUNT rows WHERE outcome IN (miss, shadow_miss)
  hit_rate = hits / (hits + misses) IF (hits+misses) > 0 ELSE 0

  provider_embed_calls = COUNT(DISTINCT live_batch_id)
                        + COUNT(*) WHERE role='direct' AND outcome IN ('miss','shadow_miss')

  batches         = COUNT(DISTINCT live_batch_id)
  texts_coalesced = COUNT(*) WHERE live_batch_id IS NOT NULL
  dedup           = texts_coalesced - SUM_over_batches(COUNT(DISTINCT embed_key) per batch)

  shadow_p50/p05/min/histogram over non-null shadow_cosine values
  long_key_sum = SUM(long_key)      # count of truthy long_key rows
  audit_count/audit_sum/audit_avg over audit_sampled rows (audit_cosine column)

Row dicts use the ACTUAL search_embed_event schema columns (verified against
search_embed_event_writer.py): cache_mode, provider, outcome, role,
live_batch_id, embed_key, shadow_cosine, long_key, audit_sampled, audit_cosine.
"""

from code_indexer.server.services.windowed_cache_metrics import (
    CacheMetricsAggregate,
    build_windowed_result,
    compute_aggregate,
    empty_windowed_result,
)


def _row(
    cache_mode="on",
    provider="voyage-ai",
    outcome="miss",
    role="direct",
    live_batch_id=None,
    embed_key=None,
    shadow_cosine=None,
    long_key=None,
    audit_sampled=None,
    audit_cosine=None,
):
    return {
        "cache_mode": cache_mode,
        "provider": provider,
        "outcome": outcome,
        "role": role,
        "live_batch_id": live_batch_id,
        "embed_key": embed_key,
        "shadow_cosine": shadow_cosine,
        "long_key": long_key,
        "audit_sampled": audit_sampled,
        "audit_cosine": audit_cosine,
    }


class TestHitsMissesHitRate:
    def test_hits_and_misses_counted_correctly(self):
        rows = [
            _row(outcome="hit"),
            _row(outcome="shadow_hit"),
            _row(outcome="miss"),
            _row(outcome="shadow_miss"),
        ]
        agg = compute_aggregate(rows)
        assert agg.hits == 2
        assert agg.misses == 2
        assert agg.hit_rate == 0.5

    def test_joiners_count_as_hits(self):
        """A coalescer_joiner row has outcome='hit' — must count toward hits."""
        rows = [
            _row(role="owner", outcome="miss", live_batch_id="b1"),
            _row(role="joiner", outcome="hit", live_batch_id="b1"),
        ]
        agg = compute_aggregate(rows)
        assert agg.hits == 1
        assert agg.misses == 1

    def test_hit_rate_zero_when_no_hits_or_misses(self):
        rows = [_row(outcome="bypass"), _row(outcome="error")]
        agg = compute_aggregate(rows)
        assert agg.hits == 0
        assert agg.misses == 0
        assert agg.hit_rate == 0.0

    def test_hit_rate_empty_rows(self):
        agg = compute_aggregate([])
        assert agg.hit_rate == 0.0


class TestProviderEmbedCalls:
    def test_coalesced_batch_plus_direct_calls_no_double_count(self):
        """AC1: provider_embed_calls == COUNT(DISTINCT live_batch_id) (coalesced
        batches) + count of successful direct events (role='direct' AND
        outcome IN ('miss','shadow_miss')), with no direct call double-counted.
        """
        rows = [
            # One coalesced batch: owner (miss) + joiner (hit), same live_batch_id.
            _row(role="owner", outcome="miss", live_batch_id="batch-1"),
            _row(role="joiner", outcome="hit", live_batch_id="batch-1"),
            # Two direct live calls (no coalescer).
            _row(role="direct", outcome="miss"),
            _row(role="direct", outcome="shadow_miss"),
            # A warm hit (must not count toward provider_embed_calls at all).
            _row(role="warm_hit", outcome="hit"),
        ]
        agg = compute_aggregate(rows)
        # COUNT(DISTINCT live_batch_id) = 1 (batch-1) + 2 direct live rows = 3.
        assert agg.provider_embed_calls == 3
        assert agg.batches == 1

    def test_warm_hit_burst_adds_zero_to_provider_embed_calls(self):
        """AC3: a burst of warm on-mode hits adds ZERO to provider_embed_calls,
        but each still counts toward hit_rate as a hit.
        """
        rows = [_row(role="warm_hit", outcome="hit") for _ in range(50)]
        agg = compute_aggregate(rows)
        assert agg.provider_embed_calls == 0
        assert agg.hits == 50
        assert agg.misses == 0
        assert agg.hit_rate == 1.0

    def test_direct_hit_does_not_count_as_provider_embed_call(self):
        rows = [_row(role="direct", outcome="hit")]
        agg = compute_aggregate(rows)
        assert agg.provider_embed_calls == 0


class TestBatchesAndDedup:
    def test_texts_coalesced_counts_all_coalesced_rows(self):
        rows = [
            _row(role="owner", outcome="miss", live_batch_id="b1", embed_key="k1"),
            _row(role="joiner", outcome="hit", live_batch_id="b1", embed_key="k1"),
            _row(role="joiner", outcome="hit", live_batch_id="b1", embed_key="k2"),
        ]
        agg = compute_aggregate(rows)
        assert agg.texts_coalesced == 3
        assert agg.batches == 1

    def test_dedup_savings_computed_from_distinct_embed_key_per_batch(self):
        """dedup = texts_coalesced - SUM_over_batches(COUNT(DISTINCT embed_key))."""
        rows = [
            # batch-1: 3 requestors, 2 unique keys (k1 duplicated) -> dedup 1.
            _row(role="owner", outcome="miss", live_batch_id="b1", embed_key="k1"),
            _row(role="joiner", outcome="hit", live_batch_id="b1", embed_key="k1"),
            _row(role="joiner", outcome="hit", live_batch_id="b1", embed_key="k2"),
            # batch-2: 2 requestors, 2 unique keys -> dedup 0.
            _row(role="owner", outcome="miss", live_batch_id="b2", embed_key="k3"),
            _row(role="joiner", outcome="hit", live_batch_id="b2", embed_key="k4"),
        ]
        agg = compute_aggregate(rows)
        assert agg.texts_coalesced == 5
        # sum of distinct-embed_key-per-batch = 2 (b1) + 2 (b2) = 4
        assert agg.dedup == 1

    def test_dedup_zero_when_no_coalesced_rows(self):
        rows = [_row(role="direct", outcome="miss")]
        agg = compute_aggregate(rows)
        assert agg.texts_coalesced == 0
        assert agg.dedup == 0


class TestShadowCosineStats:
    def test_p50_p05_min_over_shadow_values(self):
        vals = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
        rows = [_row(shadow_cosine=v) for v in vals]
        agg = compute_aggregate(rows)
        assert agg.shadow_cosine_min == 0.10
        # median of 10 sorted values = avg of index 4,5 = (0.5+0.6)/2 = 0.55
        assert agg.shadow_cosine_p50 == 0.55
        # p05 index = int(0.05*10)=0 -> sorted[0] = 0.10
        assert agg.shadow_cosine_p05 == 0.10

    def test_none_when_no_shadow_values(self):
        rows = [_row(shadow_cosine=None)]
        agg = compute_aggregate(rows)
        assert agg.shadow_cosine_p50 is None
        assert agg.shadow_cosine_p05 is None
        assert agg.shadow_cosine_min is None

    def test_histogram_always_present_40_buckets(self):
        rows = [_row(shadow_cosine=0.97)]
        agg = compute_aggregate(rows)
        assert len(agg.shadow_cosine_histogram) == 40
        total = sum(c for (_, _, c) in agg.shadow_cosine_histogram)
        assert total == 1

    def test_histogram_all_zero_when_no_shadow_values(self):
        agg = compute_aggregate([])
        assert len(agg.shadow_cosine_histogram) == 40
        assert sum(c for (_, _, c) in agg.shadow_cosine_histogram) == 0


class TestLongKeyAndAudit:
    def test_long_key_sums_truthy_rows(self):
        rows = [
            _row(long_key=True),
            _row(long_key=True),
            _row(long_key=False),
            _row(long_key=None),
        ]
        agg = compute_aggregate(rows)
        assert agg.long_key == 2

    def test_audit_count_sum_avg(self):
        rows = [
            _row(audit_sampled=True, audit_cosine=0.8),
            _row(audit_sampled=True, audit_cosine=0.6),
            _row(audit_sampled=False, audit_cosine=0.1),
            _row(audit_sampled=None, audit_cosine=None),
        ]
        agg = compute_aggregate(rows)
        assert agg.audit_count == 2
        assert agg.audit_sum == 1.4
        assert agg.audit_avg == 0.7

    def test_audit_avg_zero_when_no_audit_samples(self):
        agg = compute_aggregate([])
        assert agg.audit_count == 0
        assert agg.audit_sum == 0.0
        assert agg.audit_avg == 0.0


class TestGroupingByModeAndProvider:
    def test_by_group_keyed_by_cache_mode_and_provider(self):
        rows = [
            _row(cache_mode="on", provider="voyage-ai", outcome="hit"),
            _row(cache_mode="on", provider="voyage-ai", outcome="miss"),
            _row(cache_mode="shadow", provider="cohere", outcome="shadow_hit"),
        ]
        result = build_windowed_result(rows)
        assert set(result.by_group.keys()) == {
            ("on", "voyage-ai"),
            ("shadow", "cohere"),
        }
        assert result.by_group[("on", "voyage-ai")].hits == 1
        assert result.by_group[("on", "voyage-ai")].misses == 1
        assert result.by_group[("shadow", "cohere")].hits == 1

    def test_by_cache_mode_collapses_provider(self):
        rows = [
            _row(cache_mode="shadow", provider="voyage-ai", outcome="shadow_hit"),
            _row(cache_mode="shadow", provider="cohere", outcome="shadow_miss"),
            _row(cache_mode="on", provider="voyage-ai", outcome="hit"),
        ]
        result = build_windowed_result(rows)
        shadow_agg = result.by_cache_mode["shadow"]
        assert shadow_agg.hits == 1
        assert shadow_agg.misses == 1
        assert shadow_agg.hit_rate == 0.5

    def test_overall_aggregates_across_everything(self):
        rows = [
            _row(cache_mode="on", provider="voyage-ai", outcome="hit"),
            _row(cache_mode="shadow", provider="cohere", outcome="shadow_miss"),
        ]
        result = build_windowed_result(rows)
        assert result.overall.hits == 1
        assert result.overall.misses == 1

    def test_whole_run_reconciles_with_hand_counts(self):
        """AC1: a window covering the entire scripted run reconciles every
        aggregate with hand-computed counts, across a realistic mixed set of
        rows spanning coalesced batches, direct calls, warm hits, shadow
        comparisons, long_key skips, and audit samples.
        """
        rows = [
            # Coalesced batch 1: owner (miss) + 2 joiners (hit), 1 dup key.
            _row(
                cache_mode="on",
                provider="voyage-ai",
                role="owner",
                outcome="miss",
                live_batch_id="batch-1",
                embed_key="k1",
            ),
            _row(
                cache_mode="on",
                provider="voyage-ai",
                role="joiner",
                outcome="hit",
                live_batch_id="batch-1",
                embed_key="k1",
            ),
            _row(
                cache_mode="on",
                provider="voyage-ai",
                role="joiner",
                outcome="hit",
                live_batch_id="batch-1",
                embed_key="k2",
            ),
            # Direct live call.
            _row(cache_mode="on", provider="voyage-ai", role="direct", outcome="miss"),
            # Warm hit burst (5x) — zero provider calls, all hits.
            *[
                _row(
                    cache_mode="on",
                    provider="voyage-ai",
                    role="warm_hit",
                    outcome="hit",
                )
                for _ in range(5)
            ],
            # Shadow comparison with cosine value + long_key skip.
            _row(
                cache_mode="shadow",
                provider="voyage-ai",
                role="direct",
                outcome="shadow_miss",
                shadow_cosine=0.95,
            ),
            _row(
                cache_mode="on", provider="voyage-ai", role="direct", outcome="bypass"
            ),
            _row(
                cache_mode="on",
                provider="voyage-ai",
                role="direct",
                outcome="miss",
                long_key=True,
            ),
            # Audit sample.
            _row(
                cache_mode="on",
                provider="voyage-ai",
                role="warm_hit",
                outcome="hit",
                audit_sampled=True,
                audit_cosine=0.88,
            ),
        ]
        result = build_windowed_result(rows)
        overall = result.overall

        # Hand counts:
        # hits: 2 joiners + 5 warm_hit + 1 final audit warm_hit = 8
        # misses: owner(1) + direct(1) + shadow_miss(1) + long_key miss(1) = 4
        assert overall.hits == 8
        assert overall.misses == 4
        assert overall.hit_rate == 8 / 12

        # provider_embed_calls: 1 batch (batch-1) + 2 direct miss/shadow_miss rows
        # (the plain direct miss + the long_key direct miss) + 1 shadow_miss direct = 3
        # (owner is NOT counted separately — it's captured via batches)
        assert overall.provider_embed_calls == 1 + 3

        assert overall.batches == 1
        assert overall.texts_coalesced == 3
        assert overall.dedup == 1  # 3 - 2 unique keys

        assert overall.long_key == 1
        assert overall.audit_count == 1
        assert overall.audit_sum == 0.88
        assert overall.audit_avg == 0.88


class TestEmptyAndFailOpenDefaults:
    def test_empty_windowed_result_never_raises(self):
        result = empty_windowed_result()
        assert isinstance(result.overall, CacheMetricsAggregate)
        assert result.overall.hits == 0
        assert result.overall.hit_rate == 0.0
        assert result.overall.audit_avg == 0.0
        assert result.by_group == {}
        assert result.by_cache_mode == {}
        assert len(result.overall.shadow_cosine_histogram) == 40

    def test_build_windowed_result_empty_rows(self):
        result = build_windowed_result([])
        assert result.overall.hits == 0
        assert result.by_group == {}
        assert result.by_cache_mode == {}
