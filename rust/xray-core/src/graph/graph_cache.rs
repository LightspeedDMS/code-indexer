//! In-process graph cache (Story #1787, S2, AC9): "the graph is served from
//! cache without re-extraction or re-bind" on a second query against an
//! unchanged snapshot.
//!
//! Content-addressed, no schema, rebuild-on-miss, keyed by
//! `(repo_snapshot_identity, engine_index_version, language_pack_version,
//! budget, max_files)` -- `repo_snapshot_identity` is REUSED verbatim from
//! `crate::graph::identity` (Rule 4, anti-duplication; also already
//! dirty-working-tree-aware, AC3), never recomputed here. This cache is
//! DISTINCT from the evaluator compile cache (`crate::cache`,
//! `LOCAL_CACHE_TTL_SECS`): that one has a 300s TTL policy for compiled
//! `.so` artifacts and is NOT reused or consulted here, per the story's
//! explicit instruction. Durable/versioned/quota'd storage is explicitly a
//! LATER slice (S5) -- this is in-process only, as AC9 requires.
//!
//! Mirrors `super::fused_cache::FusedFileCache`'s proven
//! "compute is never invoked on a cache hit" shape, extended with a bounded
//! LRU eviction policy (`capacity`), since AC9 explicitly calls for an
//! "In-process LRU" rather than an unbounded map.
//!
//! Dual-review defect H3 fix: `budget`/`max_files` are RESULT-AFFECTING
//! inputs -- a tighter `IndexBudget` or a narrower `max_files` scope can
//! produce a materially LESS COMPLETE graph for the identical repo
//! snapshot -- so both are part of the cache key, per this project's own
//! Bug #1784 cache-identity invariant ("the key must cover everything that
//! affects the result"). A cached entry also now carries its
//! `fact_graph_complete` metadata alongside the graph (`CachedEntry`), and
//! `get_or_build`'s `require_complete` parameter refuses to serve a stale
//! incomplete entry to a caller whose correctness depends on completeness
//! -- it forces a fresh build instead (Rule 2, anti-fallback).

use super::budget::IndexBudget;
use super::csr::CodeGraph;
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

/// Bump when the extraction+bind SCHEMA changes (e.g. `LocalIndex`/CSR wire
/// layout, binder narrowing levels) in a way that makes a previously cached
/// `CodeGraph` unsafe to serve for a repo whose on-disk content is
/// unchanged.
pub const ENGINE_INDEX_VERSION: u32 = 1;
/// Bump when a per-language extractor's (`crate::graph::extract`) OUTPUT
/// changes for the same source text, for the same reason.
pub const LANGUAGE_PACK_VERSION: u32 = 1;

/// AC9's cache key: `(repo_snapshot_identity, engine_index_version,
/// language_pack_version, budget, max_files)`. `repo_snapshot_identity` is
/// ALREADY dirty-working-tree-aware (AC3) -- an uncommitted edit to any
/// covered file changes it, which is exactly what makes an edited repo
/// miss this cache without this module needing to know anything about
/// files itself. `budget`/`max_files` are H3's fix -- see module docs.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct GraphCacheKey {
    pub repo_snapshot_identity: String,
    pub engine_index_version: u32,
    pub language_pack_version: u32,
    pub budget: IndexBudget,
    pub max_files: Option<usize>,
}

impl GraphCacheKey {
    /// Builds a key at the CURRENT (compiled-in) engine/language-pack
    /// versions -- the only versions a running binary could ever produce a
    /// graph under, so there is no other value a caller could legitimately
    /// pass for those two fields -- plus the exact `budget`/`max_files`
    /// scope the graph will be (or was) built under (H3 fix).
    pub fn new(repo_snapshot_identity: String, budget: IndexBudget, max_files: Option<usize>) -> Self {
        GraphCacheKey {
            repo_snapshot_identity,
            engine_index_version: ENGINE_INDEX_VERSION,
            language_pack_version: LANGUAGE_PACK_VERSION,
            budget,
            max_files,
        }
    }
}

/// H3 fix: a cached entry carries the graph PLUS its `fact_graph_complete`
/// metadata -- never just the bare graph -- so `get_or_build` can refuse a
/// stale/incomplete cache hit to a caller that requires completeness,
/// rather than silently reusing it.
#[derive(Clone)]
struct CachedEntry {
    graph: Arc<CodeGraph>,
    fact_graph_complete: bool,
}

/// Result of a `get_or_build` call: which graph came back, whether it was
/// served from cache or freshly built, and whether it is a COMPLETE graph
/// (H3 fix -- mirrors `repo_index::RepoIndexResult::fact_graph_complete`,
/// carried through the cache instead of being discarded on every hit).
pub struct GraphCacheOutcome {
    pub graph: Arc<CodeGraph>,
    pub was_cache_hit: bool,
    pub fact_graph_complete: bool,
}

/// Bounded in-process LRU cache of built `CodeGraph`s, keyed by
/// `GraphCacheKey`. `order` records recency (front = least recently used,
/// back = most recently used); its length never exceeds `capacity`, which
/// is what bounds every loop in this module (Rule 14, anti-unbounded-loop).
pub struct GraphCache {
    capacity: usize,
    entries: HashMap<GraphCacheKey, CachedEntry>,
    order: VecDeque<GraphCacheKey>,
}

impl GraphCache {
    /// `capacity` of 0 would make every call a miss immediately evicted --
    /// legal but almost certainly a caller mistake, so it is clamped up to
    /// 1 rather than silently producing a cache that can never retain
    /// anything (Rule 2, anti-fallback: this is a documented floor, not a
    /// silently-swallowed error).
    pub fn new(capacity: usize) -> Self {
        GraphCache { capacity: capacity.max(1), entries: HashMap::new(), order: VecDeque::new() }
    }

    /// Returns the cached graph for `key` if one exists AND (H3 fix)
    /// either the caller does not `require_complete` or the cached entry
    /// itself is complete; otherwise calls `build` ONCE, stores the result
    /// (evicting the least-recently-used entry first if at capacity, or
    /// replacing the existing stale entry in place if one was skipped for
    /// failing the completeness requirement), and returns it. `build` is
    /// NEVER invoked on a genuine cache hit -- the discriminating property
    /// AC9 exists to provide, and the exact property
    /// `crate::scanner::PARSE_COUNT` instrumentation proves end-to-end in
    /// the real-pipeline integration test alongside this module.
    pub fn get_or_build<F>(&mut self, key: GraphCacheKey, require_complete: bool, build: F) -> GraphCacheOutcome
    where
        F: FnOnce() -> (CodeGraph, bool),
    {
        if let Some(entry) = self.entries.get(&key) {
            if !require_complete || entry.fact_graph_complete {
                let hit = Arc::clone(&entry.graph);
                let fact_graph_complete = entry.fact_graph_complete;
                self.touch(&key);
                return GraphCacheOutcome { graph: hit, was_cache_hit: true, fact_graph_complete };
            }
        }
        let (built_graph, fact_graph_complete) = build();
        let built = Arc::new(built_graph);
        self.insert(key, CachedEntry { graph: Arc::clone(&built), fact_graph_complete });
        GraphCacheOutcome { graph: built, was_cache_hit: false, fact_graph_complete }
    }

    /// Moves `key` to the most-recently-used end of `order`. Bounded: a
    /// single linear scan over `order`, whose length never exceeds
    /// `capacity` (a small, fixed configuration value) -- terminates after
    /// at most `capacity` comparisons.
    fn touch(&mut self, key: &GraphCacheKey) {
        if let Some(pos) = self.order.iter().position(|k| k == key) {
            let existing = self.order.remove(pos).expect("position came from this same deque");
            self.order.push_back(existing);
        }
    }

    /// Inserts a freshly-built entry. If `key` is ALREADY present (H3 fix:
    /// this happens when a `require_complete` request skipped a stale
    /// incomplete entry and rebuilt), this UPDATES it in place and touches
    /// it, rather than pushing a second, duplicate occurrence of the same
    /// key into `order` -- which would silently corrupt both `touch`'s
    /// first-occurrence scan and LRU eviction order. Otherwise, evicts the
    /// single least-recently-used entry first if already at capacity.
    /// Bounded: at most ONE eviction ever happens per call.
    fn insert(&mut self, key: GraphCacheKey, entry: CachedEntry) {
        if self.entries.contains_key(&key) {
            self.entries.insert(key.clone(), entry);
            self.touch(&key);
            return;
        }
        if self.entries.len() >= self.capacity {
            if let Some(lru_key) = self.order.pop_front() {
                self.entries.remove(&lru_key);
            }
        }
        self.entries.insert(key.clone(), entry);
        self.order.push_back(key);
    }
}

#[cfg(test)]
mod tests {
    use crate::graph::budget::IndexBudget;
    use crate::graph::csr::CodeGraphBuilder;
    use crate::graph::graph_cache::{GraphCache, GraphCacheKey, GraphCacheOutcome};

    /// Small, arbitrary bound sufficient for this cache's single entry;
    /// named so the number itself carries no hidden meaning.
    const TEST_CACHE_CAPACITY: usize = 4;
    /// This synthetic graph carries zero references/candidates -- only its
    /// IDENTITY (via `GraphCacheKey`) matters for these cache-behavior tests.
    const EMPTY_GRAPH_CANDIDATE_CAPACITY: usize = 0;

    fn empty_graph() -> crate::graph::csr::CodeGraph {
        CodeGraphBuilder::with_candidate_capacity(EMPTY_GRAPH_CANDIDATE_CAPACITY).build()
    }

    /// H3 fix (dual-review High): two DIFFERENT `IndexBudget`s (or two
    /// different `max_files` scopes) against the identical
    /// `repo_snapshot_identity` must never collide in the cache -- a graph
    /// built under a tight budget/narrow file scope must not be served to
    /// a later, unconstrained query, and vice versa. Before this fix,
    /// `GraphCacheKey` omitted both fields entirely, so this scenario was
    /// a guaranteed collision.
    #[test]
    fn distinct_budget_or_max_files_for_the_same_snapshot_identity_never_collide_in_the_cache() {
        let mut cache = GraphCache::new(TEST_CACHE_CAPACITY);
        let identity = "same-snapshot".to_string();

        let tight_key = GraphCacheKey::new(identity.clone(), IndexBudget::new(0, 1), Some(1));
        let tight: GraphCacheOutcome = cache.get_or_build(tight_key.clone(), false, || (empty_graph(), false));
        assert!(!tight.was_cache_hit);

        // Same identity, UNLIMITED budget and no file cap -- must be a
        // genuine miss, never served the tight-budget entry above.
        let unlimited_key = GraphCacheKey::new(identity.clone(), IndexBudget::unlimited(), None);
        let unlimited: GraphCacheOutcome = cache.get_or_build(unlimited_key.clone(), false, || (empty_graph(), true));
        assert!(!unlimited.was_cache_hit, "a distinct budget/max_files must never collide with an unrelated cache entry");

        // Both entries must now be independently retrievable.
        let tight_again: GraphCacheOutcome =
            cache.get_or_build(tight_key, false, || panic!("tight entry must still be cached"));
        assert!(tight_again.was_cache_hit);
        let unlimited_again: GraphCacheOutcome =
            cache.get_or_build(unlimited_key, false, || panic!("unlimited entry must still be cached"));
        assert!(unlimited_again.was_cache_hit);
    }

    /// H3 fix: a cache entry that was built INCOMPLETE (e.g. a transient
    /// read error unrelated to budget/max_files) must never be silently
    /// served to a later request that requires a complete graph --
    /// `require_complete = true` must force a rebuild instead (Rule 2,
    /// anti-fallback: fail fast to a fresh build, never quietly settle for
    /// a worse-than-required cached answer). A request that does NOT
    /// require completeness may still reuse the same stale entry.
    #[test]
    fn an_incomplete_cached_entry_is_never_served_to_a_request_that_requires_completeness() {
        let mut cache = GraphCache::new(TEST_CACHE_CAPACITY);
        let key = GraphCacheKey::new("flaky-snapshot".to_string(), IndexBudget::unlimited(), None);

        let first: GraphCacheOutcome = cache.get_or_build(key.clone(), false, || (empty_graph(), false));
        assert!(!first.was_cache_hit);
        assert!(!first.fact_graph_complete);

        // A caller that does NOT require completeness may reuse it as-is.
        let lax: GraphCacheOutcome = cache.get_or_build(key.clone(), false, || panic!("must reuse the incomplete entry"));
        assert!(lax.was_cache_hit);
        assert!(!lax.fact_graph_complete);

        // A caller that DOES require completeness must force a rebuild,
        // never accept the stale incomplete entry.
        let strict: GraphCacheOutcome = cache.get_or_build(key.clone(), true, || (empty_graph(), true));
        assert!(!strict.was_cache_hit, "an incomplete entry must never be served to a require_complete request");
        assert!(strict.fact_graph_complete);

        // The rebuilt COMPLETE result must now be what a future
        // require_complete request hits on.
        let now_complete: GraphCacheOutcome =
            cache.get_or_build(key, true, || panic!("the now-complete entry must be reused"));
        assert!(now_complete.was_cache_hit);
        assert!(now_complete.fact_graph_complete);
    }

    #[test]
    fn unchanged_snapshot_is_a_cache_hit_without_rebuilding() {
        let mut cache = GraphCache::new(TEST_CACHE_CAPACITY);
        let key = GraphCacheKey::new("snapshot-a".to_string(), IndexBudget::unlimited(), None);

        let first: GraphCacheOutcome = cache.get_or_build(key.clone(), false, || (empty_graph(), true));
        assert!(!first.was_cache_hit);

        let second: GraphCacheOutcome = cache.get_or_build(key, false, || {
            panic!("build must NOT be invoked again on a cache hit");
        });
        assert!(second.was_cache_hit);
    }

    /// AC9: an unrelated snapshot (a different `repo_snapshot_identity`, as
    /// an uncommitted edit would produce -- see AC3) must never be conflated
    /// with an already-cached one.
    #[test]
    fn a_different_snapshot_identity_is_a_cache_miss() {
        let mut cache = GraphCache::new(TEST_CACHE_CAPACITY);
        let key_a = GraphCacheKey::new("snapshot-a".to_string(), IndexBudget::unlimited(), None);
        let key_b = GraphCacheKey::new("snapshot-b".to_string(), IndexBudget::unlimited(), None);

        cache.get_or_build(key_a, false, || (empty_graph(), true));
        let outcome_b: GraphCacheOutcome = cache.get_or_build(key_b, false, || (empty_graph(), true));

        assert!(!outcome_b.was_cache_hit, "a distinct snapshot identity must never hit an unrelated entry");
    }

    /// AC9 "In-process LRU": with capacity 2, touching `key_a` again before
    /// inserting a third, distinct key must evict `key_b` (the genuinely
    /// least-recently-used entry) rather than `key_a`.
    #[test]
    fn least_recently_used_entry_is_evicted_when_capacity_is_exceeded() {
        const SMALL_CAPACITY: usize = 2;
        let mut cache = GraphCache::new(SMALL_CAPACITY);
        let key_a = GraphCacheKey::new("snapshot-a".to_string(), IndexBudget::unlimited(), None);
        let key_b = GraphCacheKey::new("snapshot-b".to_string(), IndexBudget::unlimited(), None);
        let key_c = GraphCacheKey::new("snapshot-c".to_string(), IndexBudget::unlimited(), None);
        let build = || (empty_graph(), true);

        cache.get_or_build(key_a.clone(), false, build);
        cache.get_or_build(key_b.clone(), false, build);
        // Re-touch key_a: it is now the MOST recently used; key_b becomes
        // the LEAST recently used of the two.
        let retouch_a: GraphCacheOutcome = cache.get_or_build(key_a.clone(), false, || {
            panic!("key_a must still be cached at this point");
        });
        assert!(retouch_a.was_cache_hit);

        // Inserting a third distinct key exceeds capacity 2 -- key_b (LRU)
        // must be evicted, key_a (just touched) must survive.
        cache.get_or_build(key_c, false, build);

        let a_after_eviction: GraphCacheOutcome = cache.get_or_build(key_a, false, || {
            panic!("key_a must have survived eviction -- it was the most recently used");
        });
        assert!(a_after_eviction.was_cache_hit, "key_a must survive: it was touched most recently");

        let b_after_eviction: GraphCacheOutcome = cache.get_or_build(key_b, false, build);
        assert!(!b_after_eviction.was_cache_hit, "key_b must have been evicted as the least recently used");
    }
}
