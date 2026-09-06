//! Dual-review defect D2 (Critical): proves an evaluator running under
//! budget pressure, through a REAL compiled dylib and a REAL `xray-cli`
//! child process, does NOT emit a dead-code finding for a symbol whose
//! only edge was capped away by the AC6 per-reference top-N ladder step.
//!
//! Before this fix, `GraphHandle` (the ONLY surface `analyze_graph` ever
//! receives, per ADR-002/AC7) exposed just `callees_of`/`callers_of`,
//! `reachable_from`, `shortest_path_to_any`, `strongly_connected_components`,
//! `resolve_symbol`, `resolve_string` -- none of which can see the AC6/D1
//! guarantee that a symbol's referenced-bit (and the whole-graph
//! completeness state) survive candidate-arena truncation. An evaluator's
//! only way to ask "is this referenced?" was `callers_of(sym).is_empty()`,
//! which reads the POST-CAP arena and reports a FALSE POSITIVE dead-code
//! verdict for exactly the scenario this test builds. `is_symbol_referenced`
//! / `is_definitely_dead_code` read the decoupled pre-cap state instead,
//! and this test proves that guarantee survives the FULL round trip: real
//! `bind_with_budget` -> real AC7 wire file -> real compiled evaluator
//! dylib -> real `xray-cli --analyze-graph` child process -> real
//! `GraphHandle` FFI accessor call.

use std::process::Command;
use std::time::Duration;
use xray_core::graph::analyze::process::run_analyze_child;
use xray_core::graph::analyze::result::{AnalyzeStatus, GraphResult};
use xray_core::graph::bind::{bind_with_budget, FileForBind};
use xray_core::graph::budget::{AnalysisCompleteness, IndexBudget};
use xray_core::graph::csr::code_graph::CodeGraph;
use xray_core::graph::extract::local_index::{Declaration, DeclarationKind, InvocationSite, LocalIndex};
use xray_core::graph::identity::make_symbol_id;

/// Generous upper bound on how long the real `xray-cli --analyze-graph`
/// child process should ever take for this test's tiny fixture graph.
const E2E_TIMEOUT: Duration = Duration::from_secs(30);

/// Builds the D2 review's exact scenario -- three same-named, equally
/// (NameOnly) confident "run" declarations across three files (zero
/// distinguishing evidence), invoked once from a fourth file -- binds it
/// under `IndexBudget::new(0, 1)` (repo-wide ceiling exceeded, cap each
/// reference to its single strongest candidate), and returns the built
/// graph plus the dense id of a "run" symbol the cap DISCARDED from the
/// CSR candidate arena. Asserts, at the host level before ever crossing
/// the dylib boundary, the exact blind spot D2 is about: zero POST-CAP
/// `callers_of`, yet `CodeGraph` itself already reports the pre-cap
/// referenced bit and `is_definitely_dead_code` correctly.
fn build_capped_graph_and_dead_symbol() -> (CodeGraph, u32) {
    let decl = |name: &str, file_id: u32| Declaration {
        kind: DeclarationKind::Method,
        name: name.to_string(),
        line: 1,
        symbol: make_symbol_id(file_id, 0),
        param_count: None,
    };
    let mut files: Vec<FileForBind> = (1u32..=3)
        .map(|file_id| {
            let mut index = LocalIndex::new();
            index.declarations.push(decl("run", file_id));
            FileForBind { file_id, language: "java".to_string(), index }
        })
        .collect();
    let mut caller = LocalIndex::new();
    caller.invocations.push(InvocationSite { callee_name: "run".to_string(), line: 10, arg_count: None });
    files.push(FileForBind { file_id: 4, language: "java".to_string(), index: caller });
    let run_symbols: Vec<u64> = (1u32..=3).map(|f| make_symbol_id(f, 0)).collect();

    let graph = bind_with_budget(files, &IndexBudget::new(0, 1));
    assert_eq!(graph.completeness(), AnalysisCompleteness::IndexBudgetExceeded, "fixture must engage the budget ladder");

    let run_reference = graph.references().iter().find(|r| !r.is_unresolved()).expect("'run' call must resolve");
    assert_eq!(graph.candidates_for(run_reference).len(), 1, "cap must narrow the 3-way ambiguous set to 1");
    // `CodeGraph::resolve_symbol` returns a plain `SymbolId` (`u64`), never
    // an `Option` -- unlike `GraphHandle::resolve_symbol` (the separate FFI
    // accessor the embedded evaluator source below calls, which IS
    // `Option<u64>` since it must never panic across the dylib boundary).
    let survivor: u64 = graph.resolve_symbol(graph.candidates_for(run_reference)[0].symbol());
    let capped_away: u64 = *run_symbols.iter().find(|&&s| s != survivor).expect("one 'run' symbol must be capped away");
    let capped_dense = graph.dense_id_for(capped_away).expect("capped-away symbol must still be interned");

    assert!(graph.callers_of(capped_dense).is_empty(), "fixture sanity: zero POST-CAP callers");
    assert!(graph.is_symbol_referenced(capped_dense), "fixture sanity: pre-cap referenced bit must survive");
    assert_eq!(graph.is_definitely_dead_code(capped_dense), Some(false), "fixture sanity: CodeGraph must already be correct");

    (graph, capped_dense)
}

/// The real graph-mode evaluator source under test: checks BOTH new D2
/// accessors on `GraphHandle` for the capped-away symbol, recording a
/// finding only if either one wrongly reports it dead or unreferenced.
fn evaluator_source_for(capped_dense: u32) -> String {
    format!(
        r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{ Vec::new() }}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{
    let mut result = GraphResult::default();
    let dense: u32 = {capped_dense};
    if g.is_definitely_dead_code(dense) == Some(true) {{
        let symbol = g.resolve_symbol(dense).unwrap_or(0);
        result.findings.push(ReduceFinding {{
            pattern: "dead-code".to_string(),
            message: "false positive: symbol was only capped away, never actually dead".to_string(),
            involved: vec![symbol],
            signatures: Vec::new(),
        }});
    }}
    if !g.is_symbol_referenced(dense) {{
        result.findings.push(ReduceFinding {{
            pattern: "referenced-bit-lost".to_string(),
            message: "is_symbol_referenced must be true for a capped-away, but still-referenced, symbol".to_string(),
            involved: Vec::new(),
            signatures: Vec::new(),
        }});
    }}
    result
}}
"#,
        capped_dense = capped_dense,
    )
}

/// THE discriminating end-to-end proof (D2): a real compiled evaluator
/// that checks `g.is_definitely_dead_code(dense)` and
/// `g.is_symbol_referenced(dense)` for the capped-away symbol -- the ONLY
/// surface ADR-002/AC7 gives `analyze_graph` to ask this question --
/// driven through the REAL `xray-cli --analyze-graph` child process, must
/// report it as NOT dead. Before the D2 fix, `evaluator_source_for`'s
/// source does not even compile (`GraphHandle` has no such method), which
/// is exactly the defect: the guarantee AC6/D1 established on `CodeGraph`
/// was unreachable from the one surface that produces findings.
#[test]
fn real_evaluator_does_not_flag_a_budget_capped_symbol_as_dead_code() {
    let dir = tempfile::tempdir().expect("create temp dir");
    let (graph, capped_dense) = build_capped_graph_and_dead_symbol();
    let graph_path = dir.path().join("graph.bin");
    xray_core::graph::csr::wire::write_graph_file(&graph, &graph_path).expect("write_graph_file must succeed");

    let user_code = evaluator_source_for(capped_dense);
    let cr = xray_core::compiler::compile_evaluator(&user_code, dir.path()).expect("must compile");

    let mut command = Command::new(env!("CARGO_BIN_EXE_xray-cli"));
    command.arg("--analyze-graph").arg("--graph-in").arg(&graph_path).arg("--dylib").arg(&cr.so_path);
    let (status, result) = run_analyze_child(command, E2E_TIMEOUT);

    assert_eq!(status, AnalyzeStatus::RanOk, "the real xray-cli binary must run the real graph-mode evaluator");
    let result: GraphResult = result.expect("RanOk must carry a real result");
    assert!(
        result.findings.is_empty(),
        "D2: a symbol whose only edge was capped away by the AC6 budget ladder must never be reported as \
         dead or unreferenced through the real GraphHandle FFI accessor -- found: {:?}",
        result.findings
    );
}
