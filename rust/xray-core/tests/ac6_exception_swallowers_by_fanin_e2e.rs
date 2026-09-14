//! Story #1792 (S3), AC6 reference use case #2: "Exception-swallowers
//! ranked by fan-in -- the shape x usage join."
//!
//! "Shape" is a purely structural, per-file signal `collect_facts` reports
//! at extraction time: a `catch_clause` whose `block` body has zero named
//! children (a genuinely empty catch, swallowing the exception silently).
//! "Usage" is a purely graph-connectivity signal `analyze_graph` computes
//! from the ALREADY-BOUND repo-wide graph: `g.callers_of(method).len()`,
//! the method's fan-in. Neither signal alone tells an investigator which
//! swallowed exception matters most; the JOIN does -- a swallower called
//! from many places is a bigger blast radius than one called from a
//! single, rarely-exercised path. This use case needs NO `refine` pass at
//! all: collect_facts (shape) + analyze_graph (usage + ranking) already
//! answer it completely, which is itself the point AC1's doc comment makes
//! ("refine is OPTIONAL when analysis is fully graph-based").
//!
//! Runs through the REAL `repo_index::build_repo_graph` pipeline (real
//! parse, real Java extractor, real binder) exactly like the deprecated-API
//! census test -- never a manually assembled graph.

use xray_core::dynlib::GraphDynlibEvaluator;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::csr::handle::GraphHandle;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::{file_id, make_symbol_id};
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, FactsHandle, UserFact};
use xray_core::owned_node::OwnedNode;

/// A REAL host-side `FactCollector`: walks the file's genuine AST looking
/// for `catch_clause` nodes whose `block` body has ZERO named children
/// (verified against a real parse: an empty `{}` block's only children
/// are the unnamed `{`/`}` tokens) -- a genuine empty-catch shape, never a
/// text/regex guess.
struct EmptyCatchCollector;

fn find_empty_catches(node: &OwnedNode, out: &mut Vec<UserFact>) {
    if node.kind == "catch_clause" {
        let is_empty = node.child_by_kind("block").map(|b| b.named_children().is_empty()).unwrap_or(false);
        if is_empty {
            out.push(UserFact { kind: "empty_catch".to_string(), line: node.start_line, message: "swallowed".to_string(), custom_key: None });
        }
    }
    for child in &node.children {
        find_empty_catches(child, out);
    }
}

impl FactCollector for EmptyCatchCollector {
    fn collect_facts(&self, root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        let mut facts = Vec::new();
        find_empty_catches(root, &mut facts);
        facts
    }
}

/// The real compiled evaluator: `analyze_graph` checks each of the two
/// KNOWN candidate methods (the "shape" side already narrowed the search
/// to methods that genuinely swallow an exception; ranking which of those
/// matters most is this evaluator's job) for the `empty_catch` fact, then
/// computes each one's REAL fan-in via `callers_of` and emits findings
/// sorted DESCENDING by fan-in -- the shape x usage join.
fn evaluator_source_for(swallow_a_dense: u32, swallow_b_dense: u32) -> String {
    format!(
        r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{
    Vec::new()
}}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{
    let mut result = GraphResult::default();
    let candidates: [u32; 2] = [{swallow_a_dense}, {swallow_b_dense}];
    let mut ranked: Vec<(u32, usize)> = Vec::new();
    for dense in candidates {{
        if let Some(symbol) = g.resolve_symbol(dense) {{
            if !facts.for_symbol(symbol).is_empty() {{
                ranked.push((dense, g.callers_of(dense).len()));
            }}
        }}
    }}
    ranked.sort_by(|a, b| b.1.cmp(&a.1));
    for (dense, fan_in) in ranked {{
        let symbol = g.resolve_symbol(dense).expect("dense came from candidates, always valid");
        let signature = g.signature_for(dense).unwrap_or("<no signature>").to_string();
        result.findings.push(ReduceFinding {{
            pattern: "exception-swallower".to_string(),
            message: format!("fan_in={{}}", fan_in),
            involved: vec![symbol],
            signatures: vec![signature],
        }});
    }}
    result
}}
"#,
        swallow_a_dense = swallow_a_dense,
        swallow_b_dense = swallow_b_dense,
    )
}

/// THE end-to-end proof: `swallowA` (called from 3 places) and `swallowB`
/// (called from 1 place) both swallow an exception via a genuinely empty
/// `catch` block; `analyze_graph` must rank `swallowA` FIRST (higher
/// fan-in) with the correct fan-in counts for both -- proving the shape
/// (empty catch, from `collect_facts`) x usage (fan-in, from the graph)
/// join, computed entirely without a `refine` pass.
#[test]
fn exception_swallowers_are_ranked_by_real_fan_in_via_the_real_pipeline() {
    let dir = tempfile::tempdir().unwrap();
    let swallower_a_file = "SwallowerA.java";
    let swallower_b_file = "SwallowerB.java";

    std::fs::write(
        dir.path().join(swallower_a_file),
        "class SwallowerA { void swallowA() { try { doWork(); } catch (java.io.IOException e) {} } }",
    )
    .unwrap();
    std::fs::write(
        dir.path().join(swallower_b_file),
        "class SwallowerB { void swallowB() { try { doWork(); } catch (java.io.IOException e) {} } }",
    )
    .unwrap();

    let mut repo_relative_paths = vec![swallower_a_file.to_string(), swallower_b_file.to_string()];
    for i in 1..=3 {
        let name = format!("CallerA{i}.java");
        std::fs::write(dir.path().join(&name), format!("class CallerA{i} {{ void go() {{ swallowA(); }} }}")).unwrap();
        repo_relative_paths.push(name);
    }
    let caller_b1 = "CallerB1.java".to_string();
    std::fs::write(dir.path().join(&caller_b1), "class CallerB1 { void go() { swallowB(); } }").unwrap();
    repo_relative_paths.push(caller_b1);

    let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
    let index_result = build_repo_graph(dir.path(), &repo_relative_paths, &options, &EmptyCatchCollector)
        .expect("no file_id collision in this fixture");
    assert!(index_result.fact_graph_complete, "fixture must index cleanly");
    let graph = index_result.graph;
    let facts = index_result.facts;

    // Each swallower file declares exactly one method (local_index 1,
    // after the class declaration at local_index 0), verified via its
    // real cached signature rather than blindly trusted.
    let swallow_a_symbol = make_symbol_id(file_id(swallower_a_file), 1);
    let swallow_a_dense = graph.dense_id_for(swallow_a_symbol).expect("swallowA must be interned");
    assert_eq!(graph.signature_for(swallow_a_dense), Some("swallowA(0 params)"));
    let swallow_b_symbol = make_symbol_id(file_id(swallower_b_file), 1);
    let swallow_b_dense = graph.dense_id_for(swallow_b_symbol).expect("swallowB must be interned");
    assert_eq!(graph.signature_for(swallow_b_dense), Some("swallowB(0 params)"));

    assert_eq!(graph.callers_of(swallow_a_dense).len(), 3, "fixture sanity: swallowA must have exactly 3 real callers");
    assert_eq!(graph.callers_of(swallow_b_dense).len(), 1, "fixture sanity: swallowB must have exactly 1 real caller");

    let cr = xray_core::compiler::compile_evaluator(&evaluator_source_for(swallow_a_dense, swallow_b_dense), dir.path())
        .expect("evaluator must compile");
    let evaluator = GraphDynlibEvaluator::load(&cr.so_path).expect("evaluator must load");

    let graph_handle = GraphHandle::from_graph(&graph);
    let facts_handle = FactsHandle::from_facts(&facts);
    let result = evaluator
        .call_analyze_graph(&graph_handle, &facts_handle)
        .expect("analyze_graph IS exported")
        .expect("analyze_graph must not panic");

    assert_eq!(result.findings.len(), 2, "both real exception-swallowers must be reported");
    assert_eq!(result.findings[0].message, "fan_in=3", "the higher-fan-in swallower must rank FIRST");
    assert_eq!(result.findings[0].involved, vec![swallow_a_symbol]);
    assert_eq!(result.findings[1].message, "fan_in=1", "the lower-fan-in swallower must rank SECOND");
    assert_eq!(result.findings[1].involved, vec![swallow_b_symbol]);
}
