//! In-memory per-file cache for fused extract+collect output, keyed by
//! `crate::graph::identity::per_file_cache_key` (Story #1787, S2, AC2 +
//! AC3). This is NOT the AC9 durable, snapshot-keyed graph cache (out of
//! scope for this slice) -- it is a narrower, in-process cache proving the
//! property AC9 will build on: per-file output IS cacheable by content
//! hash, and because the key is `per_file_cache_key` (dirty-working-tree
//! aware, never a git blob/commit hash), an uncommitted edit always misses.

use super::fused::FusedFileResult;
use crate::graph::identity::per_file_cache_key;
use std::collections::HashMap;
use std::sync::Arc;

/// Result of a `get_or_compute` call: which `FusedFileResult` came back,
/// and whether it was served from cache or freshly computed.
pub struct CacheOutcome {
    pub result: Arc<FusedFileResult>,
    pub was_cache_hit: bool,
}

pub struct FusedFileCache {
    entries: HashMap<String, Arc<FusedFileResult>>,
}

impl FusedFileCache {
    pub fn new() -> Self {
        FusedFileCache { entries: HashMap::new() }
    }

    /// Returns the cached result for `(repo_relative_path, content)` if one
    /// exists at that exact content hash; otherwise calls `compute` ONCE,
    /// stores the result, and returns it. `compute` is never invoked on a
    /// cache hit -- the discriminating property this cache exists to
    /// provide.
    pub fn get_or_compute<F>(
        &mut self,
        repo_relative_path: &str,
        content: &[u8],
        compute: F,
    ) -> CacheOutcome
    where
        F: FnOnce() -> FusedFileResult,
    {
        let key = per_file_cache_key(repo_relative_path, content);
        if let Some(existing) = self.entries.get(&key) {
            return CacheOutcome { result: Arc::clone(existing), was_cache_hit: true };
        }
        let computed = Arc::new(compute());
        self.entries.insert(key, Arc::clone(&computed));
        CacheOutcome { result: computed, was_cache_hit: false }
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

impl Default for FusedFileCache {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::fused::{CollectFactsStatus, ExtractionStatus};
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn fake_result(file: &str) -> FusedFileResult {
        FusedFileResult {
            file: file.to_string(),
            index: None,
            extraction_status: ExtractionStatus::LanguageNotSupported,
            facts: Vec::new(),
            collect_facts_status: CollectFactsStatus::SkippedNoIndex,
        }
    }

    #[test]
    fn unchanged_content_is_a_cache_hit_without_recomputation() {
        let mut cache = FusedFileCache::new();
        let calls = AtomicUsize::new(0);
        let content = b"class Foo {}";

        let first = cache.get_or_compute("Foo.java", content, || {
            calls.fetch_add(1, Ordering::SeqCst);
            fake_result("Foo.java")
        });
        assert!(!first.was_cache_hit);
        assert_eq!(calls.load(Ordering::SeqCst), 1);

        let second = cache.get_or_compute("Foo.java", content, || {
            calls.fetch_add(1, Ordering::SeqCst);
            fake_result("Foo.java")
        });
        assert!(second.was_cache_hit);
        assert_eq!(calls.load(Ordering::SeqCst), 1, "compute must NOT run again on a cache hit");
        assert_eq!(cache.len(), 1);
    }

    #[test]
    fn edited_content_is_a_cache_miss_and_recomputes() {
        let mut cache = FusedFileCache::new();
        let calls = AtomicUsize::new(0);

        let before = cache.get_or_compute("Foo.java", b"class Foo {}", || {
            calls.fetch_add(1, Ordering::SeqCst);
            fake_result("Foo.java")
        });
        assert!(!before.was_cache_hit);

        // Uncommitted edit: different content, same path.
        let after = cache.get_or_compute("Foo.java", b"class Foo { void bar() {} }", || {
            calls.fetch_add(1, Ordering::SeqCst);
            fake_result("Foo.java")
        });
        assert!(!after.was_cache_hit, "an edited file must MISS the cache");
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn different_paths_with_identical_content_do_not_collide() {
        let mut cache = FusedFileCache::new();
        cache.get_or_compute("Foo.java", b"class X {}", || fake_result("Foo.java"));
        let second = cache.get_or_compute("Bar.java", b"class X {}", || fake_result("Bar.java"));
        assert!(!second.was_cache_hit);
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn a_new_cache_is_empty() {
        assert!(FusedFileCache::new().is_empty());
    }
}
