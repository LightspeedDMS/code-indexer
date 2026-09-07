//! Story #1792 (S3), AC6 reference use case #1: "Deprecated-API census with
//! real call-site context (enclosing method, argument shape, in-loop,
//! in-try)."
//!
//! This is the shape #1786's epic exists to enable: `analyze_graph` finds
//! WHICH call sites reach a known-deprecated symbol (a pure graph +
//! fact-index question, answered via `g.callers_of` + `facts.for_symbol`),
//! then hands each caller's file to `refine` for the per-file AST detail
//! that graph queries alone cannot see -- the enclosing method name, the
//! call's argument count, and whether it sits inside a loop or a `try`
//! block.
//!
//! Unlike a synthetic fixture, this test runs the REAL pipeline end to
//! end: `repo_index::build_repo_graph` extracts and binds three real
//! `.java` files (real tree-sitter parse, real Java extractor, real
//! binder), a real `FactCollector` detects `@Deprecated` annotations via
//! genuine AST inspection, and the file list actually handed to `refine`
//! is DERIVED from `analyze_graph`'s own `RefineSet` output -- never a
//! hardcoded list standing in for it.
//!
//! Grammar node kinds (`method_declaration`, `modifiers`,
//! `marker_annotation`, `method_invocation`, `for_statement`,
//! `try_statement`, `argument_list`) were verified against real parses
//! before writing this fixture (Rule 10, fact-verification) -- never
//! assumed from memory of tree-sitter-java's grammar.

use std::collections::{BTreeSet, HashMap};
use std::path::PathBuf;
use xray_core::dynlib::GraphDynlibEvaluator;
use xray_core::graph::csr::handle::GraphHandle;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::{file_id, make_symbol_id};
use xray_core::graph::refine::{narrow_refine_set_to_driver_matched, run_refine_over_files};
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, FactsHandle, UserFact};
use xray_core::graph::budget::IndexBudget;
use xray_core::owned_node::OwnedNode;

/// A REAL host-side `FactCollector`: walks the file's genuine AST looking
/// for `method_declaration` nodes carrying a `@Deprecated` marker
/// annotation (`modifiers -> marker_annotation -> identifier("Deprecated")`,
/// verified against a real parse), reporting a fact at the METHOD'S OWN
/// declaration line -- `bind::enclosing_symbol` (host-side, not this
/// collector's concern) then keys it to the method's own SymbolId.
struct DeprecatedAnnotationCollector;

fn find_deprecated_methods(node: &OwnedNode, out: &mut Vec<UserFact>) {
    if node.kind == "method_declaration" {
        let is_deprecated = node
            .child_by_kind("modifiers")
            .and_then(|modifiers| modifiers.child_by_kind("marker_annotation"))
            .and_then(|annotation| annotation.child_by_kind("identifier"))
            .map(|name| name.text() == "Deprecated")
            .unwrap_or(false);
        if is_deprecated {
            if let Some(method_name) = node.child_by_kind("identifier") {
                out.push(UserFact {
                    kind: "deprecated".to_string(),
                    line: node.start_line,
                    message: method_name.text().to_string(),
                });
            }
        }
    }
    for child in &node.children {
        find_deprecated_methods(child, out);
    }
}

impl FactCollector for DeprecatedAnnotationCollector {
    fn collect_facts(&self, root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        let mut facts = Vec::new();
        find_deprecated_methods(root, &mut facts);
        facts
    }
}

/// The real compiled evaluator under test. `deprecated_dense` is
/// discovered from the REAL bound graph (never assumed to be a fixed
/// literal like `0`), and substituted into the assembled source exactly
/// like `d2_dead_code_survives_budget_cap_e2e.rs`'s `evaluator_source_for`
/// already establishes as this codebase's pattern for a query that
/// already knows which symbol it investigates.
fn evaluator_source_for(deprecated_dense: u32) -> String {
    format!(
        r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{
    Vec::new()
}}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{
    let mut result = GraphResult::default();
    let deprecated_symbol = g.resolve_symbol({deprecated_dense}).expect("deprecated_dense always resolves");
    if !facts.for_symbol(deprecated_symbol).is_empty() {{
        for caller_dense in g.callers_of({deprecated_dense}) {{
            if let Some(caller_symbol) = g.resolve_symbol(caller_dense) {{
                result.refine.push(caller_symbol);
            }}
        }}
    }}
    result
}}

fn walk_for_calls(
    node: &OwnedNode,
    enclosing_method: &str,
    in_loop: bool,
    in_try: bool,
    target_name: &str,
    out: &mut Vec<EvalFinding>,
) {{
    let mut method_name = enclosing_method.to_string();
    let mut loop_flag = in_loop;
    let mut try_flag = in_try;

    if node.kind == "method_declaration" {{
        if let Some(name_node) = node.child_by_kind("identifier") {{
            method_name = name_node.text().to_string();
        }}
    }}
    if node.kind == "for_statement" || node.kind == "while_statement" || node.kind == "enhanced_for_statement" {{
        loop_flag = true;
    }}
    if node.kind == "try_statement" {{
        try_flag = true;
    }}
    if node.kind == "method_invocation" {{
        if let Some(name_node) = node.child_by_kind("identifier") {{
            if name_node.text() == target_name {{
                let arg_count = node
                    .child_by_kind("argument_list")
                    .map(|args| args.named_children().len())
                    .unwrap_or(0);
                out.push(EvalFinding {{
                    pattern: "deprecated-api-call".to_string(),
                    line: node.start_line,
                    snippet: format!("enclosing={{}};args={{}};in_loop={{}};in_try={{}}", method_name, arg_count, loop_flag, try_flag),
                }});
            }}
        }}
    }}

    for child in &node.children {{
        walk_for_calls(child, &method_name, loop_flag, try_flag, target_name, out);
    }}
}}

fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {{
    let signature = g.signature_for({deprecated_dense}).unwrap_or("");
    let target_name = signature.split('(').next().unwrap_or("");
    let mut findings = Vec::new();
    walk_for_calls(node, "<top-level>", false, false, target_name, &mut findings);
    findings
}}
"#,
        deprecated_dense = deprecated_dense,
    )
}

/// THE end-to-end proof, through the REAL extract/bind pipeline: three
/// real `.java` files (a deprecated method, a caller inside a `for` loop,
/// a caller inside a `try` block, and a genuinely UNINVOLVED file that
/// never calls the deprecated method) are indexed for real. `analyze_graph`
/// finds both real callers via `callers_of` + `facts.for_symbol`; the file
/// list handed to `refine` is DERIVED from that real RefineSet (never a
/// hardcoded list) and correctly EXCLUDES the uninvolved file; `refine`
/// reports the correct real AST context for each call site.
#[test]
fn deprecated_api_census_reports_real_call_site_context_via_the_real_pipeline() {
    let dir = tempfile::tempdir().unwrap();
    let legacy_file = "Legacy.java";
    let caller1_file = "Caller1.java";
    let caller2_file = "Caller2.java";
    let uninvolved_file = "Uninvolved.java";

    std::fs::write(dir.path().join(legacy_file), "class Legacy {\n    @Deprecated\n    void oldApi() {}\n}\n").unwrap();
    std::fs::write(
        dir.path().join(caller1_file),
        "class Caller1 { void run() { for (int i = 0; i < 3; i++) { oldApi(); } } }",
    )
    .unwrap();
    std::fs::write(
        dir.path().join(caller2_file),
        "class Caller2 { void run() { try { oldApi(); } catch (Exception e) {} } }",
    )
    .unwrap();
    std::fs::write(dir.path().join(uninvolved_file), "class Uninvolved { void run() { System.out.println(1); } }").unwrap();

    let repo_relative_paths = vec![
        legacy_file.to_string(),
        caller1_file.to_string(),
        caller2_file.to_string(),
        uninvolved_file.to_string(),
    ];
    let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
    let index_result = build_repo_graph(dir.path(), &repo_relative_paths, &options, &DeprecatedAnnotationCollector)
        .expect("no file_id collision in this fixture");
    assert!(index_result.fact_graph_complete, "fixture must index cleanly");
    let graph = index_result.graph;
    let facts = index_result.facts;

    // `Legacy.java` declares exactly one method -- the class itself is
    // local_index 0 (per repo_index.rs's own documented convention), so
    // `oldApi` is local_index 1. Verified via its real cached signature
    // rather than blindly trusted.
    let deprecated_symbol = make_symbol_id(file_id(legacy_file), 1);
    let deprecated_dense = graph.dense_id_for(deprecated_symbol).expect("oldApi must be interned");
    assert_eq!(
        graph.signature_for(deprecated_dense),
        Some("oldApi(0 params)"),
        "fixture assumption: oldApi is Legacy.java's local_index 1 declaration"
    );

    let cr = xray_core::compiler::compile_evaluator(&evaluator_source_for(deprecated_dense), dir.path())
        .expect("evaluator must compile");
    let evaluator = GraphDynlibEvaluator::load(&cr.so_path).expect("evaluator must load");

    let graph_handle = GraphHandle::from_graph(&graph);
    let facts_handle = FactsHandle::from_facts(&facts);
    let analyze_result = evaluator
        .call_analyze_graph(&graph_handle, &facts_handle)
        .expect("analyze_graph IS exported")
        .expect("analyze_graph must not panic");

    assert_eq!(analyze_result.refine.len(), 2, "exactly the two real callers of the deprecated method must enter the RefineSet");

    // AC2: derive the actual refine file list from the REAL RefineSet,
    // never a hardcoded list -- resolved against a driver-matched set
    // naming ALL FOUR files, proving the exclusion of `Uninvolved.java`
    // comes from the RefineSet itself, not from a narrower driver match.
    let file_id_lookup: HashMap<u32, (PathBuf, String)> = repo_relative_paths
        .iter()
        .map(|rel| (file_id(rel), (dir.path().join(rel), rel.clone())))
        .collect();
    let driver_matched: BTreeSet<u32> = file_id_lookup.keys().copied().collect();
    let narrowed = narrow_refine_set_to_driver_matched(&analyze_result.refine, &driver_matched);
    let files_to_refine: Vec<(PathBuf, String)> =
        narrowed.into_iter().map(|id| file_id_lookup[&id].clone()).collect();
    assert_eq!(files_to_refine.len(), 2);
    assert!(
        !files_to_refine.iter().any(|(_, rel)| rel == uninvolved_file),
        "Uninvolved.java must never be derived into the refine file list"
    );

    let refine_results = run_refine_over_files(&files_to_refine, &graph, &facts, &evaluator);
    assert_eq!(refine_results.len(), 2);

    let caller1 = refine_results.iter().find(|r| r.file == caller1_file).expect("Caller1.java must be refined");
    assert_eq!(caller1.findings.len(), 1);
    assert_eq!(caller1.findings[0].snippet, "enclosing=run;args=0;in_loop=true;in_try=false");

    let caller2 = refine_results.iter().find(|r| r.file == caller2_file).expect("Caller2.java must be refined");
    assert_eq!(caller2.findings.len(), 1);
    assert_eq!(caller2.findings[0].snippet, "enclosing=run;args=0;in_loop=false;in_try=true");
}
