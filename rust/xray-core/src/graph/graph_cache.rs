//! In-process graph cache (Story #1787, S2, AC9): "the graph is served from
//! cache without re-extraction or re-bind" on a second query against an
//! unchanged snapshot.
//!
//! Content-addressed, no schema, rebuild-on-miss, keyed by
//! `(repo_snapshot_identity, engine_index_version, language_pack_version)`
//! -- `repo_snapshot_identity` is REUSED verbatim from
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
/// language_pack_version)`. `repo_snapshot_identity` is ALREADY
/// dirty-working-tree-aware (AC3) -- an uncommitted edit to any covered
/// file changes it, which is exactly what makes an edited repo miss this
/// cache without this module needing to know anything about files itself.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct GraphCacheKey {
    pub repo_snapshot_identity: String,
    pub engine_index_version: u32,
    pub language_pack_version: u32,
}

impl GraphCacheKey {
    /// Builds a key at the CURRENT (compiled-in) engine/language-pack
    /// versions -- the only versions a running binary could ever produce a
    /// graph under, so there is no other value a caller could legitimately
    /// pass for those two fields.
    pub fn new(repo_snapshot_identity: String) -> Self {
        GraphCacheKey {
            repo_snapshot_identity,
            engine_index_version: ENGINE_INDEX_VERSION,
            language_pack_version: LANGUAGE_PACK_VERSION,
        }
    }
}

/// Result of a `get_or_build` call: which graph came back, and whether it
/// was served from cache or freshly built.
pub struct GraphCacheOutcome {
    pub graph: Arc<CodeGraph>,
    pub was_cache_hit: bool,
}

/// Bounded in-process LRU cache of built `CodeGraph`s, keyed by
/// `GraphCacheKey`. `order` records recency (front = least recently used,
/// back = most recently used); its length never exceeds `capacity`, which
/// is what bounds every loop in this module (Rule 14, anti-unbounded-loop).
pub struct GraphCache {
    capacity: usize,
    entries: HashMap<GraphCacheKey, Arc<CodeGraph>>,
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

    /// Returns the cached graph for `key` if one exists; otherwise calls
    /// `build` ONCE, stores the result (evicting the least-recently-used
    /// entry first if at capacity), and returns it. `build` is NEVER
    /// invoked on a cache hit -- the discriminating property AC9 exists to
    /// provide, and the exact property `crate::scanner::PARSE_COUNT`
    /// instrumentation proves end-to-end in the real-pipeline integration
    /// test alongside this module.
    pub fn get_or_build<F>(&mut self, key: GraphCacheKey, build: F) -> GraphCacheOutcome
    where
        F: FnOnce() -> CodeGraph,
    {
        if let Some(graph) = self.entries.get(&key) {
            let hit = Arc::clone(graph);
            self.touch(&key);
            return GraphCacheOutcome { graph: hit, was_cache_hit: true };
        }
        let built = Arc::new(build());
        self.insert(key, Arc::clone(&built));
        GraphCacheOutcome { graph: built, was_cache_hit: false }
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

    /// Inserts a freshly-built entry, evicting the single
    /// least-recently-used entry first if already at capacity. Bounded: at
    /// most ONE eviction ever happens per call, since `insert` is the only
    /// place entries are added and it never adds more than one over
    /// capacity before evicting.
    fn insert(&mut self, key: GraphCacheKey, graph: Arc<CodeGraph>) {
        if self.entries.len() >= self.capacity {
            if let Some(lru_key) = self.order.pop_front() {
                self.entries.remove(&lru_key);
            }
        }
        self.entries.insert(key.clone(), graph);
        self.order.push_back(key);
    }
}

#[cfg(test)]
mod tests {
    use crate::graph::csr::CodeGraphBuilder;
    use crate::graph::graph_cache::{GraphCache, GraphCacheKey, GraphCacheOutcome};

    /// Small, arbitrary bound sufficient for this cache's single entry;
    /// named so the number itself carries no hidden meaning.
    const TEST_CACHE_CAPACITY: usize = 4;
    /// This synthetic graph carries zero references/candidates -- only its
    /// IDENTITY (via `GraphCacheKey`) matters for these cache-behavior tests.
    const EMPTY_GRAPH_CANDIDATE_CAPACITY: usize = 0;

    #[test]
    fn unchanged_snapshot_is_a_cache_hit_without_rebuilding() {
        let mut cache = GraphCache::new(TEST_CACHE_CAPACITY);
        let key = GraphCacheKey::new("snapshot-a".to_string());

        let first: GraphCacheOutcome = cache.get_or_build(key.clone(), || {
            CodeGraphBuilder::with_candidate_capacity(EMPTY_GRAPH_CANDIDATE_CAPACITY).build()
        });
        assert!(!first.was_cache_hit);

        let second: GraphCacheOutcome = cache.get_or_build(key, || {
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
        let key_a = GraphCacheKey::new("snapshot-a".to_string());
        let key_b = GraphCacheKey::new("snapshot-b".to_string());

        cache.get_or_build(key_a, || {
            CodeGraphBuilder::with_candidate_capacity(EMPTY_GRAPH_CANDIDATE_CAPACITY).build()
        });
        let outcome_b: GraphCacheOutcome = cache.get_or_build(key_b, || {
            CodeGraphBuilder::with_candidate_capacity(EMPTY_GRAPH_CANDIDATE_CAPACITY).build()
        });

        assert!(!outcome_b.was_cache_hit, "a distinct snapshot identity must never hit an unrelated entry");
    }

    /// AC9 "In-process LRU": with capacity 2, touching `key_a` again before
    /// inserting a third, distinct key must evict `key_b` (the genuinely
    /// least-recently-used entry) rather than `key_a`.
    #[test]
    fn least_recently_used_entry_is_evicted_when_capacity_is_exceeded() {
        const SMALL_CAPACITY: usize = 2;
        let mut cache = GraphCache::new(SMALL_CAPACITY);
        let key_a = GraphCacheKey::new("snapshot-a".to_string());
        let key_b = GraphCacheKey::new("snapshot-b".to_string());
        let key_c = GraphCacheKey::new("snapshot-c".to_string());
        let build = || CodeGraphBuilder::with_candidate_capacity(EMPTY_GRAPH_CANDIDATE_CAPACITY).build();

        cache.get_or_build(key_a.clone(), build);
        cache.get_or_build(key_b.clone(), build);
        // Re-touch key_a: it is now the MOST recently used; key_b becomes
        // the LEAST recently used of the two.
        let retouch_a: GraphCacheOutcome = cache.get_or_build(key_a.clone(), || {
            panic!("key_a must still be cached at this point");
        });
        assert!(retouch_a.was_cache_hit);

        // Inserting a third distinct key exceeds capacity 2 -- key_b (LRU)
        // must be evicted, key_a (just touched) must survive.
        cache.get_or_build(key_c, build);

        let a_after_eviction: GraphCacheOutcome = cache.get_or_build(key_a, || {
            panic!("key_a must have survived eviction -- it was the most recently used");
        });
        assert!(a_after_eviction.was_cache_hit, "key_a must survive: it was touched most recently");

        let b_after_eviction: GraphCacheOutcome = cache.get_or_build(key_b, build);
        assert!(!b_after_eviction.was_cache_hit, "key_b must have been evicted as the least recently used");
    }
}
