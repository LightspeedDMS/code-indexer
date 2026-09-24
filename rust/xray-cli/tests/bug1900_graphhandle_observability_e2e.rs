//! Bug #1900 (epic #1906 P2/P5): end-to-end proof that the four new
//! `GraphHandle` observability accessors -- `location_for`,
//! `declaration_kind`, `visibility_of`, `edge_reason` -- are reachable from
//! REAL evaluator user code compiled through the REAL `compile_evaluator`
//! pipeline and driven through the REAL `xray-cli --analyze-graph` child
//! process, exactly like `bug1828_graphhandle_e2e.rs` proves for
//! `dense_id_for`/`symbol_count`. A unit test calling these methods
//! directly from Rust would prove the accessor exists; it would NOT prove
//! an evaluator can actually reach it through the PREAMBLE-mirrored
//! `GraphHandle` type compiled into the dylib -- that is what this test
//! is for.

use std::process::Command;
use std::time::Duration;

use xray_core::graph::analyze::process::run_analyze_child;
use xray_core::graph::analyze::result::{AnalyzeStatus, GraphResult};
use xray_core::graph::csr::builder::CodeGraphBuilder;
use xray_core::graph::csr::candidate::Candidate;
use xray_core::graph::csr::wire::write_graph_file;
use xray_core::graph::extract::local_index::{DeclarationKind, Visibility};
use xray_core::graph::identity::make_symbol_id;
use xray_core::graph::reasons;

const E2E_TIMEOUT: Duration = Duration::from_secs(30);

/// Builds a real `CodeGraph`: `caller` (dense 0) has a single-candidate
/// reference to `target` (dense 1, local index 1); `target` carries a
/// recorded declaration location, kind, and visibility. Writes it to a
/// real file via `write_graph_file` -- the same AC7 mmap wire format the
/// real `xray-cli --analyze-graph` process reads.
fn write_graph_with_full_metadata(dir: &std::path::Path) -> std::path::PathBuf {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
    let caller = builder.intern_symbol(make_symbol_id(1, 0));
    let target = builder.intern_symbol(make_symbol_id(1, 1));
    let file_string_id = builder.intern_string("com/example/Target.java");
    builder.add_location(target, file_string_id, 42);
    builder.add_kind(target, DeclarationKind::Method);
    builder.add_visibility(target, Visibility::Private);
    builder.add_reference(caller, 1, 1, 0, &[Candidate::new(target, reasons::UNIQUE_NAME_IN_REPO)]);
    let graph = builder.build();

    let path = dir.join("graph.bin");
    write_graph_file(&graph, &path).expect("write_graph_file must succeed");
    path
}

#[test]
fn graph_handle_observability_accessors_are_callable_from_real_evaluator_code() {
    let dir = tempfile::tempdir().expect("tempdir");
    let graph_path = write_graph_with_full_metadata(dir.path());

    let source = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let caller: u32 = 0;
    let target: u32 = 1;
    let location = g.location_for(target);
    let kind = g.declaration_kind(target);
    let visibility = g.visibility_of(target);
    let reason = g.edge_reason(caller, target);
    let no_edge_reason = g.edge_reason(target, caller);
    let evidence = g.edge_evidence(caller, target);
    let no_edge_evidence = g.edge_evidence(target, caller);
    let evidence_has_unique_name = evidence.map(|bits| bits & UNIQUE_NAME_IN_REPO != 0);

    let mut result = GraphResult::default();
    result.findings.push(ReduceFinding {
        pattern: "graphhandle_observability_probe".to_string(),
        message: format!(
            "location={:?} kind={:?} visibility={:?} reason={:?} no_edge_reason={:?} \
             evidence={:?} no_edge_evidence={:?} evidence_has_unique_name={:?}",
            location, kind, visibility, reason, no_edge_reason,
            evidence, no_edge_evidence, evidence_has_unique_name
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result
}
"#;
    let compiled = xray_core::compiler::compile_evaluator(source, dir.path()).expect("compile evaluator");

    let mut command = Command::new(env!("CARGO_BIN_EXE_xray-cli"));
    command.arg("--analyze-graph").arg("--graph-in").arg(&graph_path).arg("--dylib").arg(&compiled.so_path);
    let (status, result) = run_analyze_child::<GraphResult>(command, E2E_TIMEOUT);

    assert_eq!(status, AnalyzeStatus::RanOk, "the real analyze-graph child process must run the compiled evaluator successfully");
    let result = result.expect("RanOk result");
    assert_eq!(result.findings.len(), 1, "the evaluator must have produced exactly one finding");
    let message = &result.findings[0].message;

    assert!(message.contains(r#"location=Some(("com/example/Target.java", 42))"#), "message must reflect the real declaration location: {message}");
    assert!(message.contains("kind=Some(Method)"), "message must reflect the real declaration kind: {message}");
    assert!(message.contains("visibility=Private"), "message must reflect the real declared visibility: {message}");
    assert!(message.contains("reason=Some(SoleCandidate)"), "message must reflect the real single-candidate edge tier: {message}");
    assert!(message.contains("no_edge_reason=None"), "a pair with no edge at all must report None through the real compile path too: {message}");
    // Bug #1900 review round 2: edge_evidence and the reason-bit constants
    // (GRAPH_PREAMBLE_EXTRA_6) must be reachable from real evaluator code
    // compiled through the real pipeline -- the fixture's single reference
    // was built with UNIQUE_NAME_IN_REPO evidence, so the evaluator's own
    // bitwise check against that named constant must observe it set.
    assert!(
        message.contains(&format!("evidence=Some({})", reasons::UNIQUE_NAME_IN_REPO)),
        "message must reflect the real evidence bitmask (UNIQUE_NAME_IN_REPO={}): {message}",
        reasons::UNIQUE_NAME_IN_REPO
    );
    assert!(message.contains("no_edge_evidence=None"), "a pair with no edge at all must report None for edge_evidence too: {message}");
    assert!(
        message.contains("evidence_has_unique_name=Some(true)"),
        "the evaluator's own bitwise check against the named UNIQUE_NAME_IN_REPO constant \
         (mirrored into GRAPH_PREAMBLE_EXTRA_6) must observe the bit set: {message}"
    );
}
