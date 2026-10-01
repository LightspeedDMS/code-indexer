//! Bug #1828 regression: graph-mode evaluators must be able to enumerate the
//! exact dense-id range and reverse-resolve a SymbolId without guessing a
//! dense id.  This drives a real compiled evaluator through xray-cli's real
//! analyze-graph child process.

use std::process::Command;
use std::time::Duration;

use xray_core::graph::analyze::process::run_analyze_child;
use xray_core::graph::analyze::result::{AnalyzeStatus, GraphResult};
use xray_core::graph::csr::builder::CodeGraphBuilder;
use xray_core::graph::csr::candidate::Candidate;
use xray_core::graph::csr::wire::write_graph_file;
use xray_core::graph::identity::make_symbol_id;
use xray_core::graph::reasons;

const E2E_TIMEOUT: Duration = Duration::from_secs(30);

fn write_graph(dir: &std::path::Path) -> (std::path::PathBuf, u64) {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
    let a = builder.intern_symbol(make_symbol_id(1, 0));
    let b_symbol = make_symbol_id(1, 1);
    let b = builder.intern_symbol(b_symbol);
    builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
    let path = dir.join("graph.bin");
    write_graph_file(&builder.build(), &path).expect("write graph");
    (path, b_symbol)
}

#[test]
fn graph_handle_enumerates_symbols_and_resolves_dense_id_for_known_symbol() {
    let dir = tempfile::tempdir().expect("tempdir");
    let (graph_path, b_symbol) = write_graph(dir.path());
    let source = format!(
        r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{
    let _identified_symbol: SymbolId = {b_symbol};
    Vec::new()
}}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{
    let symbol: SymbolId = {b_symbol};
    let dense = g.dense_id_for(symbol).expect("collected symbol must resolve");
    let mut result = GraphResult::default();
    for id in 0..g.symbol_count() {{
        if g.resolve_symbol(id as u32).is_some() {{
            result.refine.push(id as SymbolId);
        }}
    }}
    result.refine.push(dense as SymbolId);
    result.refine.extend(g.reachable_from(&[dense], 20).into_iter().map(SymbolId::from));
    result
}}
"#
    );
    let compiled = xray_core::compiler::compile_evaluator(&source, dir.path()).expect("compile evaluator");
    let mut command = Command::new(env!("CARGO_BIN_EXE_xray-cli"));
    command.arg("--analyze-graph").arg("--graph-in").arg(&graph_path).arg("--dylib").arg(&compiled.so_path);
    let (status, result) = run_analyze_child::<GraphResult>(command, E2E_TIMEOUT);
    assert_eq!(status, AnalyzeStatus::RanOk);
    let result = result.expect("RanOk result");
    assert_eq!(result.refine, vec![0, 1, 1, 1]);
}
