//! Story #1787 AC7+AC8: end-to-end proof that `run_analyze_child` (the
//! killable-process container built for AC7) drives the REAL `xray-cli
//! --analyze-graph` binary against a REAL compiled evaluator dylib and a
//! REAL graph file, receiving back a genuine `AnalyzeStatus` -- never a
//! stub, never a mock. This is the literal end-to-end path a production
//! analyze-graph invocation takes: parent spawns `xray-cli
//! --analyze-graph --graph-in <path> --dylib <path>` as its own process
//! group, the child loads the compiled dylib and the mmap'd graph, runs
//! `analyze_graph` through the `GraphHandle` accessor ABI, and reports its
//! outcome back over stdout as JSON.

use std::process::Command;
use std::time::Duration;
use xray_core::graph::analyze::process::run_analyze_child;
use xray_core::graph::analyze::result::{AnalyzeStatus, GraphResult};
use xray_core::graph::csr::builder::CodeGraphBuilder;
use xray_core::graph::csr::candidate::Candidate;
use xray_core::graph::csr::wire::write_graph_file;
use xray_core::graph::identity::make_symbol_id;
use xray_core::graph::reasons;

/// Generous upper bound on how long the real `xray-cli --analyze-graph`
/// child process should ever take for this test's trivial 2-symbol
/// graph -- far more than needed, but this test's purpose is proving
/// correctness end-to-end, not measuring latency.
const E2E_TIMEOUT: Duration = Duration::from_secs(30);

/// The fixture graph's two interned symbols: file id `1`, local indices
/// `0` (A, the reference site) and `1` (B, the sole candidate target).
const FIXTURE_FILE_ID: u32 = 1;
const FIXTURE_SYMBOL_A_LOCAL_INDEX: u32 = 0;
const FIXTURE_SYMBOL_B_LOCAL_INDEX: u32 = 1;
/// The single reference's line number -- arbitrary but must be a valid
/// (nonzero) `u32` for `CodeGraphBuilder::add_reference`.
const FIXTURE_REFERENCE_LINE: u32 = 1;

/// Builds a tiny real `CodeGraph` (A -> B) and writes it to a real file
/// via `write_graph_file` -- the AC7 mmap wire format the real `xray-cli
/// --analyze-graph` process reads via `read_graph_file`.
fn write_small_graph(dir: &std::path::Path) -> (std::path::PathBuf, u64) {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
    let a = builder.intern_symbol(make_symbol_id(FIXTURE_FILE_ID, FIXTURE_SYMBOL_A_LOCAL_INDEX));
    let b_symbol = make_symbol_id(FIXTURE_FILE_ID, FIXTURE_SYMBOL_B_LOCAL_INDEX);
    let b = builder.intern_symbol(b_symbol);
    builder.add_reference(a, FIXTURE_FILE_ID, FIXTURE_REFERENCE_LINE, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
    let graph = builder.build();

    let path = dir.join("graph.bin");
    write_graph_file(&graph, &path).expect("write_graph_file must succeed");
    (path, b_symbol)
}

/// Compiles `user_code`, writes a real fixture graph, and drives the REAL
/// `xray-cli` binary through the REAL `run_analyze_child` process
/// container -- the shared end-to-end sequence both tests below exercise,
/// differing only in `user_code`. Returns the fixture's expected B
/// `SymbolId` alongside the observed `(status, result)` so callers can
/// assert on both without recomputing the fixture.
fn run_real_analyze_graph_e2e(user_code: &str, dir: &std::path::Path) -> (u64, AnalyzeStatus, Option<GraphResult>) {
    let (graph_path, b_symbol) = write_small_graph(dir);
    let cr = xray_core::compiler::compile_evaluator(user_code, dir).expect("must compile");

    let mut command = Command::new(env!("CARGO_BIN_EXE_xray-cli"));
    command.arg("--analyze-graph").arg("--graph-in").arg(&graph_path).arg("--dylib").arg(&cr.so_path);

    let (status, result) = run_analyze_child(command, E2E_TIMEOUT);
    (b_symbol, status, result)
}

/// THE end-to-end proof: a REAL compiled graph-mode evaluator, calling a
/// REAL `GraphHandle` accessor, driven through the REAL `xray-cli`
/// binary via the REAL AC7 process container, must report `RanOk` with
/// the CORRECT resolved data.
#[test]
fn real_xray_cli_binary_runs_a_real_graph_mode_evaluator_via_run_analyze_child() {
    let dir = tempfile::tempdir().expect("create temp dir");
    let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    for callee in g.callees_of(0) {
        result.refine.push(g.resolve_symbol(callee));
    }
    result
}
"#;
    let (b_symbol, status, result) = run_real_analyze_graph_e2e(user_code, dir.path());

    assert_eq!(status, AnalyzeStatus::RanOk, "the real xray-cli binary must report RanOk for a real graph-mode evaluator");
    let result = result.expect("RanOk must carry a real result");
    assert_eq!(result.refine, vec![b_symbol], "must resolve to B's real SymbolId through the real GraphHandle accessor");
}

/// THE central AC7/AC8 invariant, proven at the full process boundary: a
/// legacy-mode dylib handed to `--analyze-graph` must report `Absent`
/// (not exported) -- DISTINCT from `NotRequested` (never reached: the
/// parent only spawns this process when graph analysis WAS requested) and
/// from a successful empty analysis.
#[test]
fn real_xray_cli_binary_reports_absent_for_a_legacy_mode_dylib() {
    let dir = tempfile::tempdir().expect("create temp dir");
    let legacy_code = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }";
    let (_b_symbol, status, result) = run_real_analyze_graph_e2e(legacy_code, dir.path());

    assert_eq!(status, AnalyzeStatus::Absent, "a legacy-mode dylib handed to --analyze-graph must report Absent, never RanOk or a crash");
    assert!(result.is_none());
}
