// ^ invariant evaluator access to GraphHandle/FactsHandle = pub methods only; DEBUG_LOG access = debug_log only
// ^ see rust/xray-core/src/compiler/graph_preamble.rs:GRAPH_PREAMBLE_EXTRA_1; docs/architecture/invariants/xray.md

use super::*;
use tempfile::TempDir;

const GRAPH_COLLECT_FACTS: &str =
    "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n    let _ = (node, file);\n    Vec::new()\n}\n";

fn graph_evaluator(analyze_graph_body: &str) -> String {
    format!(
        "{GRAPH_COLLECT_FACTS}fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{\n{analyze_graph_body}\n    GraphResult::default()\n}}\n"
    )
}

fn legacy_evaluator(evaluate_node_body: &str) -> String {
    format!("fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {{\n    let _ = node;\n{evaluate_node_body}\n    Vec::new()\n}}\n")
}

// ^ invariant rejection must come from rustc, not the validator; test:evaluator_cannot_read_handle_fields
fn rustc_rejection_details(user_code: &str) -> String {
    let dir = TempDir::new().unwrap();
    match compile_evaluator(user_code, dir.path()) {
        Ok(_) => panic!("evaluator compiled; rustc must reject it:\n{user_code}"),
        Err(e) => {
            assert_eq!(e.kind, CompileErrorKind::Compile, "{e}");
            assert_eq!(e.message, "Evaluator compilation failed", "must be a rustc rejection, not validation: {e}");
            e.details.join("\n")
        }
    }
}

fn assert_contains_all(details: &str, needles: &[&str]) {
    for needle in needles {
        assert!(details.contains(needle), "expected `{needle}` in rustc output:\n{details}");
    }
}

#[test]
fn evaluator_cannot_read_handle_fields() {
    let details = rustc_rejection_details(&graph_evaluator("    let _g = g.ctx;\n    let _f = facts.for_symbol_fn;"));
    assert_contains_all(
        &details,
        &["error[E0616]", "field `ctx` of struct `GraphHandle` is private", "field `for_symbol_fn` of struct `FactsHandle` is private"],
    );
}

#[test]
fn evaluator_cannot_assign_handle_fields() {
    let body = "    let mut h = *g;\n    h.symbol_count_fn = g.symbol_count_fn;\n    let mut f = *facts;\n    f.for_custom_fn = facts.for_custom_fn;\n    let _ = (h.symbol_count(), f.for_custom(\"k\"));";
    let details = rustc_rejection_details(&graph_evaluator(body));
    assert_contains_all(
        &details,
        &["error[E0616]", "field `symbol_count_fn` of struct `GraphHandle` is private", "field `for_custom_fn` of struct `FactsHandle` is private"],
    );
}

#[test]
fn evaluator_cannot_construct_handles_with_struct_literals() {
    let details = rustc_rejection_details(&graph_evaluator("    let _h = GraphHandle { ..*g };\n    let _f = FactsHandle { ..*facts };"));
    assert_contains_all(&details, &["error[E0451]", "`ctx`", "of struct `GraphHandle` are private", "of struct `FactsHandle` are private"]);
}

#[test]
fn evaluator_cannot_name_handle_internals() {
    let bare = rustc_rejection_details(&graph_evaluator("    let _ = (g, facts);\n    let _p: Option<CtxPtr> = None;"));
    assert_contains_all(&bare, &["error[E0425]", "cannot find type `CtxPtr`"]);

    let qualified = rustc_rejection_details(&graph_evaluator("    let _ = (g, facts);\n    let _p: Option<xray_host_handles::CtxPtr> = None;"));
    assert_contains_all(&qualified, &["error[E0603]", "type alias `CtxPtr` is private"]);
}

#[test]
fn evaluator_reaches_debug_log_buffer_only_through_debug_log() {
    let bare = rustc_rejection_details(&legacy_evaluator("    DEBUG_LOG.with(|log| log.borrow_mut().clear());\n    let _ = xray_drain_debug_log();"));
    assert_contains_all(&bare, &["cannot find value `DEBUG_LOG`", "cannot find function `xray_drain_debug_log`"]);

    let qualified = rustc_rejection_details(&legacy_evaluator(
        "    xray_host_debug_log::DEBUG_LOG.with(|log| log.borrow_mut().clear());\n    let _ = xray_host_debug_log::xray_drain_debug_log();",
    ));
    assert_contains_all(&qualified, &["error[E0603]", "constant `DEBUG_LOG` is private", "function `xray_drain_debug_log` is private"]);
}
