//! Story #1785: `FactKey::Custom(InternedStr)` was a fully-typed,
//! serde-backed, unit-tested enum variant with ZERO production writer and
//! ZERO reader (Bug #1665's "registered-but-unwired" trap, Messi Rule 12
//! anti-orphan). This is the story's own DISCRIMINATING test, named
//! explicitly in its "Method" section: an end-to-end round trip through the
//! REAL pipeline -- a `FactCollector` emits a fact naming a `custom_key`,
//! and a COMPILED graph-mode evaluator reads it back via `facts.for_custom`
//! -- proving it is NOT also attributed to whichever symbol happens to
//! enclose its reported line.
//!
//! A test that only constructs a `FactKey::Custom` and reads it out of a
//! `FactIndex` (as `user_facts.rs`'s own pre-existing unit tests already
//! did, and continue to do) is explicitly called out by the story as NOT
//! discriminating -- that already passed before any of this story's
//! production wiring existed, which is exactly why the defect shipped
//! orphaned in the first place. This test instead runs the REAL
//! `repo_index::build_repo_graph` aggregation, compiles a REAL evaluator
//! `.so` through `compiler::compile_evaluator`, loads it through the REAL
//! `dynlib::GraphDynlibEvaluator`, and calls `facts.for_custom`/
//! `facts.for_symbol` from INSIDE that compiled dylib -- exercising the
//! full `FactsHandle` FFI accessor wiring (PREAMBLE mirror, ABI version,
//! ctx-pointer thunk) that a wrong or half-wired implementation could
//! still fail even after every pure-Rust unit test above it passes.

use xray_core::dynlib::GraphDynlibEvaluator;
use xray_core::graph::csr::handle::GraphHandle;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::{file_id, make_symbol_id};
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, FactsHandle, UserFact};
use xray_core::graph::budget::IndexBudget;
use xray_core::owned_node::OwnedNode;

/// A REAL host-side `FactCollector`: reports exactly one fact naming the
/// custom key `"db.host"` at line 1 of every file it sees -- a config key
/// has no `SymbolId` of its own, so this collector deliberately never
/// constructs a `FactKey`/`SymbolId`/interned id itself (per ADR-001, that
/// is the aggregation site's job, not the collector's).
struct ConfigKeyCollector;

impl FactCollector for ConfigKeyCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        vec![UserFact {
            kind: "config_key".to_string(),
            line: 1,
            message: "db.host".to_string(),
            custom_key: Some("db.host".to_string()),
        }]
    }
}

/// The real compiled evaluator under test: probes BOTH `facts.for_custom`
/// (the correct destination) and `facts.for_symbol` on the symbol that
/// WOULD have enclosed line 1 had this fact been symbol-shaped (the
/// incorrect destination a pre-#1785 build silently used for every fact).
/// Reports both counts in one `ReduceFinding` so the host assertion below
/// can distinguish "found only via for_custom" from any other outcome.
fn evaluator_source_for(would_be_enclosing_dense: u32) -> String {
    format!(
        r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{
    Vec::new()
}}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{
    let mut result = GraphResult::default();
    let custom_facts = facts.for_custom("db.host");
    let would_be_enclosing_symbol = g.resolve_symbol({would_be_enclosing_dense})
        .expect("would_be_enclosing_dense always resolves");
    let symbol_facts = facts.for_symbol(would_be_enclosing_symbol);
    result.findings.push(ReduceFinding {{
        pattern: "custom-fact-probe".to_string(),
        message: format!("custom_count={{}};symbol_count={{}}", custom_facts.len(), symbol_facts.len()),
        involved: Vec::new(),
        signatures: Vec::new(),
    }});
    result
}}
"#,
        would_be_enclosing_dense = would_be_enclosing_dense,
    )
}

/// THE end-to-end proof, through the REAL pipeline: `build_repo_graph`
/// aggregates the collector's custom-keyed fact; a compiled `analyze_graph`
/// evaluator reads it back via `facts.for_custom("db.host")` and finds
/// EXACTLY one, while `facts.for_symbol` on the class's own enclosing
/// symbol -- the exact symbol a pre-#1785 build would have silently
/// attributed this fact to -- finds ZERO. A wrong implementation that
/// still (or ALSO) attributed a custom-keyed fact to its enclosing symbol
/// would report `symbol_count=1`, failing this assertion.
#[test]
fn a_custom_keyed_fact_is_read_back_via_for_custom_through_a_compiled_evaluator_and_never_via_for_symbol() {
    let dir = tempfile::tempdir().unwrap();
    let config_file = "Config.java";
    std::fs::write(dir.path().join(config_file), "class Config {\n    void load() {}\n}\n").unwrap();

    let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
    let index_result = build_repo_graph(dir.path(), &[config_file.to_string()], &options, &ConfigKeyCollector)
        .expect("no file_id collision in this fixture");
    assert!(index_result.fact_graph_complete, "fixture must index cleanly");
    let graph = index_result.graph;
    let facts = index_result.facts;

    // Host-side sanity check BEFORE crossing into the compiled evaluator:
    // the fact must be reachable via get_custom, and Config's own class
    // declaration (local index 0, the symbol enclosing_symbol would have
    // picked for a line-1 fact) must carry ZERO facts.
    assert_eq!(facts.get_custom("db.host").len(), 1);
    let config_file_id = file_id(config_file);
    let would_be_enclosing_symbol = make_symbol_id(config_file_id, 0);
    let would_be_enclosing_dense =
        graph.dense_id_for(would_be_enclosing_symbol).expect("Config's class declaration must be interned");
    assert!(
        facts.get(&xray_core::graph::user_facts::FactKey::Symbol(would_be_enclosing_symbol)).is_empty(),
        "host-side: a custom-keyed fact must never also be attributed to its enclosing symbol"
    );

    let cr = xray_core::compiler::compile_evaluator(&evaluator_source_for(would_be_enclosing_dense), dir.path())
        .expect("evaluator must compile");
    let evaluator = GraphDynlibEvaluator::load(&cr.so_path).expect("evaluator must load");

    let graph_handle = GraphHandle::from_graph(&graph);
    let facts_handle = FactsHandle::from_facts(&facts);
    let analyze_result = evaluator
        .call_analyze_graph(&graph_handle, &facts_handle)
        .expect("analyze_graph IS exported")
        .expect("analyze_graph must not panic");

    assert_eq!(analyze_result.findings.len(), 1);
    assert_eq!(
        analyze_result.findings[0].message, "custom_count=1;symbol_count=0",
        "the compiled evaluator must find the fact via for_custom (count=1) and must NEVER find it \
         via for_symbol on its would-be enclosing symbol (count=0) -- a value other than \
         'custom_count=1;symbol_count=0' means the fact was either not reachable via for_custom, or \
         was ALSO (wrongly) attributed to its enclosing symbol"
    );
}
