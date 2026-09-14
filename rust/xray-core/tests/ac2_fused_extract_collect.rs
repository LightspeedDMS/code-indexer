//! Story #1787, S2, AC2 -- Fused Extract + Collect with mandatory
//! sequencing. Six discriminating integration tests exercising the REAL
//! fused pipeline (real tree-sitter parse, real `JavaExtractor`, no
//! mocking) built in this slice: `xray_core::graph::fused::process_file_fused`,
//! `process_parsed_file`, and `xray_core::graph::fused_cache::FusedFileCache`.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::fused::{process_file_fused, CollectFactsStatus, ExtractionStatus};
use xray_core::graph::fused_cache::FusedFileCache;
use xray_core::graph::user_facts::{FactCollector, UserFact};
use xray_core::owned_node::{self, OwnedNode};
use xray_core::scanner;

fn write_java(dir: &tempfile::TempDir, name: &str, source: &str) -> std::path::PathBuf {
    let path = dir.path().join(name);
    std::fs::write(&path, source).unwrap();
    path
}

/// Bounded, iterative node count -- mirrors `OwnedNode`'s own traversal
/// style (Rule 14): each iteration pops one node and pushes its finite
/// children, so total pushes equal the tree's finite node count.
fn count_nodes(node: &OwnedNode) -> usize {
    let mut count = 0usize;
    let mut stack: Vec<&OwnedNode> = vec![node];
    while let Some(n) = stack.pop() {
        count += 1;
        stack.extend(n.children.iter());
    }
    count
}

struct NoOpCollector;
impl FactCollector for NoOpCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        Vec::new()
    }
}

// ---------------------------------------------------------------------
// 1. Forward reference: the LAST declaration must be visible to a
//    collect_facts call examining the FIRST.
// ---------------------------------------------------------------------

struct ForwardReferenceProbe {
    last_visible_when_examining_first: AtomicBool,
}

impl FactCollector for ForwardReferenceProbe {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, index: &LocalIndex) -> Vec<UserFact> {
        let first = index.declarations.first().expect("fixture always has >=2 decls");
        let last = index.declarations.last().expect("fixture always has >=2 decls");
        assert_ne!(first.name, last.name, "fixture must have distinct first/last decls");
        let visible = index.declaration_named(&last.name).is_some();
        self.last_visible_when_examining_first.store(visible, Ordering::SeqCst);
        Vec::new()
    }
}

/// THE central discriminating test for AC2. A fixture whose LAST
/// declaration (`Last`) must be visible when `collect_facts` looks at
/// state gathered while examining the FIRST declaration (`First`).
///
/// This catches a WRONG implementation that interleaves per-node
/// extraction with fact collection in a single combined walk (e.g. a
/// recursive function that, for each declaration node, both adds it to
/// the index AND immediately computes/accumulates facts using the index
/// as it stands SO FAR) rather than running extraction to full
/// completion first: such an implementation would see an index
/// containing only `First` (and nothing declared after it) at the point
/// it examines `First`, since `Last` has not been visited yet. A test
/// over a single-declaration file cannot catch this -- it would pass
/// trivially either way.
#[test]
fn forward_reference_last_declaration_is_visible_to_collect_facts_examining_first() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_java(
        &dir,
        "Sample.java",
        "class First {\n    void callsLast() {}\n}\nclass Last {}\n",
    );
    let probe = ForwardReferenceProbe { last_visible_when_examining_first: AtomicBool::new(false) };

    let result = process_file_fused(&path, "Sample.java", &probe).unwrap();

    assert_eq!(result.extraction_status, ExtractionStatus::Completed);
    assert_eq!(result.collect_facts_status, CollectFactsStatus::Ran);
    assert!(
        probe.last_visible_when_examining_first.load(Ordering::SeqCst),
        "collect_facts must see the file's LAST declaration -- proving extraction fully \
         completed (two sequential walks) before collect_facts ever ran, never interleaved"
    );
}

// ---------------------------------------------------------------------
// 2. The file is parsed exactly ONCE.
// ---------------------------------------------------------------------

/// Catches a wrong implementation that re-parses the file once per walk
/// (e.g. extraction parses its own tree, and `collect_facts` -- naively
/// implemented as an independent function operating on a fresh re-parse
/// rather than the SAME already-parsed tree -- parses again).
#[test]
fn file_is_parsed_exactly_once_by_the_fused_pipeline() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_java(&dir, "Sample.java", "class Foo {\n    void run() {}\n}\n");

    scanner::reset_parse_count();
    let result = process_file_fused(&path, "Sample.java", &NoOpCollector).unwrap();

    assert_eq!(result.extraction_status, ExtractionStatus::Completed);
    assert_eq!(scanner::parse_count(), 1, "tree-sitter Parser::parse must run exactly once per file");
}

// ---------------------------------------------------------------------
// 3. collect_facts is invoked exactly once per file.
// ---------------------------------------------------------------------

struct CountingCollector {
    calls: AtomicUsize,
}

impl FactCollector for CountingCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Vec::new()
    }
}

/// Catches a wrong implementation that calls `collect_facts` once per
/// declaration/node (interleaved) instead of once for the whole file.
#[test]
fn collect_facts_is_invoked_exactly_once_per_file() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_java(
        &dir,
        "Sample.java",
        "class First {}\nclass Second {}\nclass Third {}\n",
    );
    let collector = CountingCollector { calls: AtomicUsize::new(0) };

    process_file_fused(&path, "Sample.java", &collector).unwrap();

    assert_eq!(
        collector.calls.load(Ordering::SeqCst),
        1,
        "collect_facts must be invoked exactly once for the whole file, not once per declaration"
    );
}

// ---------------------------------------------------------------------
// 4. The tree is dropped before the next file is processed.
// ---------------------------------------------------------------------

/// Catches a wrong implementation that RETAINS the parsed tree past the
/// per-file call (e.g. stashing it in a cache or a batch accumulator)
/// instead of letting it drop when `process_file_fused` returns. Uses
/// `OwnedNode`'s manual iterative `Drop` impl (Bug #1795) via the
/// `test-support`-gated `DROP_COUNT` instrumentation: it counts every
/// `OwnedNode` actually dropped, so a retained tree would leave the count
/// short of the tree's real node count.
#[test]
fn tree_is_fully_dropped_before_next_file_is_processed() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_java(
        &dir,
        "Sample.java",
        "class First {\n    void a() {}\n    void b() {}\n}\nclass Second {\n    int x;\n}\n",
    );

    // Learn the tree's exact node count from an INDEPENDENT parse, in its
    // own scope so that tree drops naturally before instrumentation
    // starts (its drop must not be counted as part of the assertion).
    let expected_node_count = {
        let root = scanner::parse_file(&path).unwrap();
        count_nodes(&root)
    };

    owned_node::reset_drop_count();
    let result = process_file_fused(&path, "Sample.java", &NoOpCollector).unwrap();
    assert_eq!(result.extraction_status, ExtractionStatus::Completed);

    assert_eq!(
        owned_node::drop_count(),
        expected_node_count,
        "the WHOLE parsed tree ({expected_node_count} nodes) must be dropped by the time \
         process_file_fused returns -- before any next file could begin"
    );
}

// ---------------------------------------------------------------------
// 5. A panic inside collect_facts is contained and does not abort.
// ---------------------------------------------------------------------

struct PanickingCollector;
impl FactCollector for PanickingCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        panic!("PanickingCollector always panics");
    }
}

/// Catches a wrong implementation that wraps BOTH walks in one shared
/// `catch_unwind` (or none at all): a panic in `collect_facts` must never
/// take down the whole process, AND must not retroactively erase
/// extraction's own already-completed results.
#[test]
fn panic_inside_collect_facts_is_contained_and_extraction_results_survive() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_java(&dir, "Sample.java", "class Foo {}\n");

    let result = process_file_fused(&path, "Sample.java", &PanickingCollector).unwrap();

    assert_eq!(result.collect_facts_status, CollectFactsStatus::Panicked);
    assert!(result.facts.is_empty());
    assert_eq!(result.extraction_status, ExtractionStatus::Completed);
    assert!(result.index.is_some(), "extraction's own results must survive a later collect_facts panic");
    // Reaching this assertion at all is itself part of the proof: a
    // genuinely uncaught panic would have aborted the test process.
}

// ---------------------------------------------------------------------
// 6. Per-file output is cacheable by content hash: hit on unchanged
//    content, miss on an uncommitted edit.
// ---------------------------------------------------------------------

/// Catches a wrong cache implementation keyed by something OTHER than
/// working-tree content (e.g. path alone, or a git blob/commit hash):
/// such a key would either never miss on a real edit, or would miss even
/// when content is byte-identical.
#[test]
fn per_file_output_is_a_cache_hit_on_unchanged_content_and_a_miss_on_an_edit() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_java(&dir, "Sample.java", "class Foo {}\n");
    let mut cache = FusedFileCache::new();
    let calls = AtomicUsize::new(0);

    let content_v1 = std::fs::read(&path).unwrap();
    let first = cache.get_or_compute("Sample.java", &content_v1, || {
        calls.fetch_add(1, Ordering::SeqCst);
        process_file_fused(&path, "Sample.java", &NoOpCollector).unwrap()
    });
    assert!(!first.was_cache_hit);
    assert_eq!(calls.load(Ordering::SeqCst), 1);

    // Re-read the SAME, unchanged content -> cache HIT.
    let content_v1_again = std::fs::read(&path).unwrap();
    let second = cache.get_or_compute("Sample.java", &content_v1_again, || {
        calls.fetch_add(1, Ordering::SeqCst);
        process_file_fused(&path, "Sample.java", &NoOpCollector).unwrap()
    });
    assert!(second.was_cache_hit, "unchanged content must be a cache hit");
    assert_eq!(calls.load(Ordering::SeqCst), 1, "compute must not run again on a cache hit");

    // Edit WITHOUT committing -> cache MISS.
    std::fs::write(&path, "class Foo { void bar() {} }\n").unwrap();
    let content_v2 = std::fs::read(&path).unwrap();
    let third = cache.get_or_compute("Sample.java", &content_v2, || {
        calls.fetch_add(1, Ordering::SeqCst);
        process_file_fused(&path, "Sample.java", &NoOpCollector).unwrap()
    });
    assert!(!third.was_cache_hit, "an uncommitted edit must be a cache miss");
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}
