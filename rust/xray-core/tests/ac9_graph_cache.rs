//! Story #1787, S2, AC9 -- end-to-end proof that a second graph query
//! against an UNCHANGED snapshot is served from cache "without
//! re-extraction or re-bind", and that an UNCOMMITTED edit misses the
//! cache. Wires together two already-implemented, already-unit-tested
//! pieces through their real public API: `build_repo_graph`
//! (`xray_core::graph::repo_index`) and `GraphCache`
//! (`xray_core::graph::graph_cache`) -- real tree-sitter parses of real
//! files on disk drive the extraction/bind/cache logic actually under
//! test. `NoOpCollector` below is a stub for `FactCollector`, an OPTIONAL
//! user-authored plugin seam that is orthogonal to what this test exercises
//! (mirrors the same `NoOpCollector` pattern already used throughout
//! `fused.rs`'s and `repo_index.rs`'s own tests) -- it is not a mock of any
//! code under test here.
//!
//! The absence-of-work proof uses the SAME `scanner::PARSE_COUNT`
//! instrumentation pattern the story names (from S2.2/AC2): if the cache
//! hit, `build_repo_graph` (which is what actually calls
//! `scanner::parse_file_with_error_flag` per file) was never invoked at
//! all, so `parse_count()` must read exactly zero after a hit -- a far
//! stronger proof than merely comparing two results for equality (which a
//! silently-re-executing "cache" would also satisfy).

use std::process::Command;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::graph_cache::{GraphCache, GraphCacheKey};
use xray_core::graph::identity::repo_snapshot_identity;
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, UserFact};
use xray_core::owned_node::OwnedNode;
use xray_core::scanner;

struct NoOpCollector;
impl FactCollector for NoOpCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        Vec::new()
    }
}

const TEST_CACHE_CAPACITY: usize = 4;

fn write_java(dir: &tempfile::TempDir, name: &str, source: &str) {
    std::fs::write(dir.path().join(name), source).unwrap();
}

/// Runs one `git` subcommand in `dir`, asserting it succeeded -- used only
/// to establish a REAL committed baseline for
/// `an_uncommitted_edit_misses_the_graph_cache` below, so that test proves
/// the dirty-WORKING-TREE case specifically (a real commit exists and is
/// unchanged; only the on-disk bytes differ from it), not merely "two
/// different byte strings hash differently".
fn run_git(dir: &std::path::Path, args: &[&str]) {
    let status = Command::new("git").args(args).current_dir(dir).status().expect("git must be installed");
    assert!(status.success(), "git {args:?} failed in {dir:?}");
}

#[test]
fn second_query_against_unchanged_snapshot_serves_from_cache_with_zero_reparsing() {
    let dir = tempfile::tempdir().unwrap();
    write_java(&dir, "A.java", "class A { void run() { helper(); } }\n");
    write_java(&dir, "B.java", "class B { void helper() {} }\n");
    let files = vec!["A.java".to_string(), "B.java".to_string()];
    let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };

    let identity = repo_snapshot_identity(dir.path(), &files).expect("compute snapshot identity");
    let mut cache = GraphCache::new(TEST_CACHE_CAPACITY);
    let key = GraphCacheKey::new(identity);

    scanner::reset_parse_count();
    let first = cache.get_or_build(key.clone(), || {
        build_repo_graph(dir.path(), &files, &options, &NoOpCollector).expect("no file_id collision").graph
    });
    assert!(!first.was_cache_hit, "first query against a never-seen snapshot must be a miss");
    assert!(scanner::parse_count() > 0, "the first (miss) query must actually parse real files");

    scanner::reset_parse_count();
    let second = cache.get_or_build(key, || {
        panic!("build_repo_graph must NOT be invoked again on a cache hit");
    });
    assert!(second.was_cache_hit, "the second query against the SAME unchanged snapshot must hit");
    assert_eq!(
        scanner::parse_count(),
        0,
        "a cache hit must perform ZERO re-parsing -- proof of no re-extraction and no re-bind"
    );
}

#[test]
fn an_uncommitted_edit_misses_the_graph_cache() {
    let dir = tempfile::tempdir().unwrap();
    write_java(&dir, "A.java", "class A { void run() {} }\n");
    let files = vec!["A.java".to_string()];
    let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
    let mut cache = GraphCache::new(TEST_CACHE_CAPACITY);

    // Establish a REAL committed baseline. The identity/cache key are
    // computed from the WORKING TREE, never the commit -- this proves the
    // dirty-tree case specifically, not just "two different files hash
    // differently".
    run_git(dir.path(), &["init", "-q"]);
    run_git(dir.path(), &["add", "A.java"]);
    run_git(dir.path(), &["-c", "user.email=test@test.local", "-c", "user.name=test", "commit", "-q", "-m", "init"]);

    let identity_before = repo_snapshot_identity(dir.path(), &files).expect("compute snapshot identity");
    let key_before = GraphCacheKey::new(identity_before.clone());
    scanner::reset_parse_count();
    let first = cache.get_or_build(key_before, || {
        build_repo_graph(dir.path(), &files, &options, &NoOpCollector).expect("no file_id collision").graph
    });
    assert!(!first.was_cache_hit);
    assert!(scanner::parse_count() > 0);

    // Edit WITHOUT committing: the git HEAD commit is UNCHANGED, only the
    // working-tree bytes differ from it.
    write_java(&dir, "A.java", "class A { void run() { /* edited */ } }\n");
    let identity_after = repo_snapshot_identity(dir.path(), &files).expect("recompute snapshot identity");
    assert_ne!(identity_before, identity_after, "editing a covered file must change the identity");

    let key_after = GraphCacheKey::new(identity_after);
    scanner::reset_parse_count();
    let second = cache.get_or_build(key_after, || {
        build_repo_graph(dir.path(), &files, &options, &NoOpCollector).expect("no file_id collision").graph
    });
    assert!(!second.was_cache_hit, "an uncommitted edit must MISS the graph cache");
    assert!(scanner::parse_count() > 0, "a genuine miss must actually re-parse the edited file");
}
