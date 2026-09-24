//! Issue #1934: `compiler.rs`'s unit tests covering the assemble/ABI/
//! cache-identity/preamble-structure seam, relocated verbatim out of its
//! single `#[cfg(test)] mod tests { ... }` body (declared at `compiler.rs`
//! via `#[cfg(test)] #[path = "tests_identity.rs"] mod tests_identity;`,
//! mirroring the pattern `graph/bind/resolve.rs` already uses) so
//! `compiler.rs` itself stays well under the project's line limit.

use super::*;
use tempfile::TempDir;

/// RED phase: `assemble_graph_evaluator_source` does not exist yet.
/// This proves what its GREEN implementation must do -- assemble
/// graph-mode source using the GRAPH_PREAMBLE_EXTRA_* mirror text and
/// GRAPH_EPILOGUE, NEVER the legacy `xray_evaluate_node` export.
#[test]
fn assemble_graph_evaluator_source_uses_graph_preamble_and_epilogue_not_legacy() {
    let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
"#;
    let assembled = assemble_graph_evaluator_source(user_code);
    assert!(assembled.contains("pub struct GraphHandle"), "must contain GraphHandle mirror");
    assert!(assembled.contains("pub struct FactsHandle"), "must contain FactsHandle mirror");
    assert!(assembled.contains("xray_collect_facts"), "must export xray_collect_facts");
    assert!(assembled.contains("xray_analyze_graph"), "must export xray_analyze_graph");
    assert!(assembled.contains(user_code), "must contain user code verbatim");
    assert!(!assembled.contains("xray_evaluate_node"), "graph mode must NEVER export xray_evaluate_node");
}

/// Story #1792 (S3, AC1): every graph-mode evaluator's assembled source
/// must carry the `FileContext` mirror -- the per-file host context an
/// optional `refine` callback receives -- regardless of whether THIS
/// particular evaluator defines `refine` at all (it is part of the
/// shared graph-mode preamble, exactly like `GraphHandle`/`FactsHandle`).
#[test]
fn assemble_graph_evaluator_source_includes_file_context_mirror() {
    let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
"#;
    let assembled = assemble_graph_evaluator_source(user_code);
    assert!(assembled.contains("pub struct FileContext"), "must contain the FileContext mirror");
}

/// Story #1792 (S3, AC1): "OPTIONAL export ... `xray_refine` is
/// all-or-none with the graph family." A graph-mode evaluator that
/// defines `fn refine(...)` must get the `xray_refine` export; one that
/// does NOT define it (the pre-existing collect_facts+analyze_graph-only
/// shape) must NEVER get it -- there is no such symbol to load, which is
/// exactly what `GraphDynlibEvaluator::has_refine()` distinguishes later.
#[test]
fn assemble_graph_evaluator_source_conditionally_exports_xray_refine_only_when_user_defines_refine() {
    let without_refine = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
"#;
    assert!(
        !assemble_graph_evaluator_source(without_refine).contains("xray_refine"),
        "an evaluator with no fn refine must NEVER export xray_refine"
    );

    let with_refine = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> { Vec::new() }
"#;
    let assembled = assemble_graph_evaluator_source(with_refine);
    assert!(assembled.contains("xray_refine"), "an evaluator defining fn refine must export xray_refine");
    assert!(assembled.contains(with_refine), "must contain user code verbatim");
}

#[test]
fn test_assemble_contains_preamble_and_epilogue() {
    let user_code = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }";
    let assembled = assemble_evaluator_source(user_code);
    assert!(assembled.contains("pub struct OwnedNode"), "should contain preamble OwnedNode");
    assert!(assembled.contains("pub struct EvalFinding"), "should contain preamble EvalFinding");
    assert!(assembled.contains("xray_evaluate_node"), "should contain epilogue symbol");
    assert!(assembled.contains("evaluate_node(node)"), "should call evaluate_node in epilogue");
    assert!(assembled.contains(user_code), "should contain user code verbatim");
}

#[test]
fn test_sha256_consistent() {
    let a = sha256_hex("hello world");
    let b = sha256_hex("hello world");
    assert_eq!(a, b, "sha256 must be deterministic");
    assert_eq!(a.len(), 64, "SHA-256 hex must be 64 chars");
    assert!(a.chars().all(|c| c.is_ascii_hexdigit()), "must be hex");

    let c = sha256_hex("different input");
    assert_ne!(a, c, "different inputs must produce different hashes");
}

/// Consolidated review finding H9 (Issue #1811/Bug #1812, Codex): the
/// Bug #1784 class recurring at the graph-mode bridge. `--print-cache-
/// identity` (backed by `cache_identity_info`) ALWAYS assembles via the
/// LEGACY `assemble_evaluator_source`, regardless of the evaluator's
/// real mode -- but `compile_evaluator_impl` (Step 3) assembles a
/// graph-mode evaluator via `assemble_graph_evaluator_source` instead,
/// and derives the REAL `.so` filename identity from THAT assembled
/// source. A mode-aware `cache_identity_info_graph` must produce
/// EXACTLY the identity `compile_evaluator` actually uses for a
/// graph-mode evaluator -- otherwise a cluster-cache pre-fill keyed on
/// the wrong identity can never be consumed by the graph compile path.
#[test]
fn cache_identity_info_graph_matches_compile_evaluators_real_identity_for_graph_mode() {
    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
"#;
    assert_eq!(detect_evaluator_mode(user_code).unwrap(), EvaluatorMode::Graph);

    let compiled = compile_evaluator(user_code, dir.path())
        .expect("a genuine graph-mode evaluator must compile successfully");
    let real_identity = compiled
        .so_path
        .file_stem()
        .and_then(|s| s.to_str())
        .expect("so_path must have a valid file stem")
        .to_string();

    let graph_identity_info = cache_identity_info_graph(user_code);

    assert_eq!(
        graph_identity_info.identity, real_identity,
        "cache_identity_info_graph() must produce EXACTLY the identity \
         compile_evaluator() actually uses for a graph-mode evaluator"
    );
}

#[test]
fn test_compile_valid_evaluator() {
    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    if node.kind == "try_statement" {
        findings.push(EvalFinding {
            pattern: "test".to_string(),
            line: node.start_line,
            snippet: String::new(),
        });
    }
    findings
}
"#;
    let result = compile_evaluator(user_code, dir.path());
    assert!(result.is_ok(), "valid evaluator should compile: {:?}", result.err().map(|e| e.to_string()));
    let cr = result.unwrap();
    assert!(cr.so_path.exists(), ".so file must exist on disk");
    assert!(!cr.cached, "first compile should not be cached");
    assert!(cr.compile_ms > 0, "compile time should be positive");
}

// ---- Bug #1784: cache identity must cover PREAMBLE/EPILOGUE + ABI ----

const SHA256_HEX_DIGEST_LEN: usize = 64;
const TEST_ABI_VERSION_A: u64 = 1;
const TEST_ABI_VERSION_B: u64 = 2;
const TEST_RUSTC_VERSION: &str = "rustc 1.91.0";

#[test]
fn test_compute_cache_identity_deterministic() {
    let a = compute_cache_identity("some source", TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
    let b = compute_cache_identity("some source", TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
    assert_eq!(a, b, "identity must be deterministic for identical inputs");
    assert_eq!(a.len(), SHA256_HEX_DIGEST_LEN, "identity must be a SHA-256 hex digest");
}

#[test]
fn test_compute_cache_identity_differs_on_abi_version_alone() {
    // Proves ABI version is an independent identity component, not
    // merely subsumed by hashing the source text: same source, same
    // rustc, only abi_version differs.
    let source = "identical assembled source text";
    let id_abi1 = compute_cache_identity(source, TEST_ABI_VERSION_A, TEST_RUSTC_VERSION);
    let id_abi2 = compute_cache_identity(source, TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
    assert_ne!(id_abi1, id_abi2, "identity must change when ONLY abi_version differs");
}

#[test]
fn test_graph_handle_accessor_addition_bumps_abi_version() {
    assert_eq!(
        XRAY_ABI_VERSION, 14,
        "#1924/#1925: adding the five evidence-filtered traversal accessors \
         (callees_of_filtered/callers_of_filtered/reachable_from_filtered/ \
         reachable_to_filtered/strongly_connected_components_filtered) to \
         GraphHandle requires the ABI-14 bump"
    );
}

#[test]
fn test_compute_cache_identity_differs_on_source_change() {
    // The core of Bug #1784: identity must be sensitive to the assembled
    // source (PREAMBLE + EPILOGUE), not just the raw user code.
    let id_a = compute_cache_identity("preamble-v1 + user code", TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
    let id_b = compute_cache_identity("preamble-v2 + user code", TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
    assert_ne!(id_a, id_b, "identity must change when the assembled source text differs");
}

#[test]
fn test_compute_cache_identity_differs_on_rustc_version() {
    let source = "identical assembled source text";
    let id_a = compute_cache_identity(source, TEST_ABI_VERSION_B, "rustc 1.91.0");
    let id_b = compute_cache_identity(source, TEST_ABI_VERSION_B, "rustc 1.92.0");
    assert_ne!(id_a, id_b, "identity must change when rustc_version differs");
}

#[test]
fn test_assembled_source_embeds_correct_abi_version_via_substitution() {
    // Bug #1784 review MAJOR-3: XRAY_ABI_VERSION is now the ONE source
    // of truth -- PREAMBLE contains a placeholder token, never a
    // hardcoded literal duplicate. This test proves the substitution
    // performed by assemble_evaluator_source_with_preamble actually
    // happens (the real value is embedded) and never leaks the raw
    // placeholder token into compilable source.
    let assembled = assemble_evaluator_source(
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }",
    );
    assert!(
        assembled.contains(&format!("XRAY_ABI_VERSION: u64 = {};", XRAY_ABI_VERSION)),
        "assembled source must embed the current XRAY_ABI_VERSION value ({})",
        XRAY_ABI_VERSION
    );
    assert!(
        !assembled.contains(ABI_VERSION_PLACEHOLDER),
        "assembled source must not leak the raw placeholder token"
    );
}

// --- AC8: synchronous evaluator-mode classification ---

/// AC8: "A scan provides evaluate_node OR graph mode — validated
/// synchronously before job submission, not discovered at runtime."
/// ADR-001: "A graph evaluator must not export evaluate_node; a
/// legacy evaluator must not be treated as graph mode. The loader
/// rejects a mixed or incomplete callback family." Six cases:
/// legacy-only (Ok), graph-complete (Ok), both families mixed (Err),
/// collect_facts-without-analyze_graph (Err), analyze_graph-without-
/// collect_facts (Err), and neither present (Err).
#[test]
fn detect_evaluator_mode_classifies_legacy_graph_and_rejects_missing_or_mixed_families() {
    let legacy = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }";
    assert_eq!(detect_evaluator_mode(legacy).unwrap(), EvaluatorMode::Legacy);

    let graph = r#"
fn collect_facts(node: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult { GraphResult::default() }
"#;
    assert_eq!(detect_evaluator_mode(graph).unwrap(), EvaluatorMode::Graph);

    let mixed = format!("{legacy}\n{graph}");
    assert!(detect_evaluator_mode(&mixed).is_err(), "mixed legacy+graph families must be rejected");

    let collect_only = "fn collect_facts(node: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> { Vec::new() }";
    assert!(detect_evaluator_mode(collect_only).is_err(), "collect_facts without analyze_graph must be rejected");

    let analyze_only = "fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult { GraphResult::default() }";
    assert!(detect_evaluator_mode(analyze_only).is_err(), "analyze_graph without collect_facts must be rejected");

    let neither = "fn helper() -> i32 { 42 }";
    assert!(detect_evaluator_mode(neither).is_err(), "an evaluator with no recognized callback family must be rejected");
}

#[test]
fn test_preamble_types_match_crate_types() {
    // Verify PREAMBLE contains the same fields as the real types.
    // This catches drift between preamble and crate definitions.
    assert!(PREAMBLE.contains("pub kind: String"), "OwnedNode.kind missing from preamble");
    assert!(PREAMBLE.contains("pub start_line: usize"), "OwnedNode.start_line missing");
    assert!(PREAMBLE.contains("pub start_byte: usize"), "OwnedNode.start_byte missing");
    assert!(PREAMBLE.contains("pub end_byte: usize"), "OwnedNode.end_byte missing");
    assert!(PREAMBLE.contains("pub children: Vec<OwnedNode>"), "OwnedNode.children missing");
    assert!(PREAMBLE.contains("pub is_named: bool"), "OwnedNode.is_named missing");
    // ABI v2: text is now a method, not a field
    assert!(PREAMBLE.contains("pub fn text("), "OwnedNode.text() method missing from preamble");
    assert!(PREAMBLE.contains("source: Arc<str>"), "OwnedNode.source field missing from preamble");
    assert!(PREAMBLE.contains("pub pattern: String"), "EvalFinding.pattern missing");
    assert!(PREAMBLE.contains("pub line: usize"), "EvalFinding.line missing");
    assert!(PREAMBLE.contains("pub snippet: String"), "EvalFinding.snippet missing");
}

// --- AC1/AC2/AC5: debug_log tests ---

#[test]
fn test_preamble_contains_debug_log() {
    // AC1: debug_log(msg) must be in preamble so user code can call it.
    assert!(
        PREAMBLE.contains("fn debug_log(msg: &str)"),
        "PREAMBLE must define fn debug_log(msg: &str)"
    );
    assert!(
        PREAMBLE.contains("thread_local!"),
        "PREAMBLE must define thread_local! storage for debug messages"
    );
    assert!(
        PREAMBLE.contains("RefCell"),
        "PREAMBLE must use RefCell for interior mutability of debug log"
    );
    assert!(
        PREAMBLE.contains("10240"),
        "PREAMBLE must enforce 10KB (10240 byte) size limit"
    );
}

#[test]
fn test_epilogue_contains_drain_debug_log() {
    // AC2: xray_drain_debug_log must be exported so the loader can retrieve messages.
    assert!(
        EPILOGUE.contains("xray_drain_debug_log"),
        "EPILOGUE must export xray_drain_debug_log symbol"
    );
    assert!(
        EPILOGUE.contains("Vec<String>"),
        "xray_drain_debug_log must return Vec<String>"
    );
}

/// Issue #1934 (pure-move regression guard): proves the composite cache
/// identity is a pure, deterministic function of the assembled source +
/// ABI version + rustc version with nothing else silently mixed in, for
/// BOTH evaluator modes, and that the two modes' assembled sources (and
/// therefore identities) never collide. Deliberately does NOT pin a
/// hardcoded expected hash -- `XRAY_ABI_VERSION` has bumped 13 times in
/// this file's own history (see its doc comment) and a hash-pinned test
/// would spuriously fail on every legitimate future bump, unrelated to
/// this split. The actual before/after byte-identity of the split itself
/// was verified separately by diffing the extracted string constants
/// against the pre-split `compiler.rs` and by comparing
/// `cache_identity_info_graph`/`cache_identity_info` output directly
/// against a scratch build of the pre-split HEAD (see the issue's PR
/// description for the captured values).
#[test]
fn assembled_source_identity_is_self_consistent_and_mode_disjoint() {
    const FIXED_LEGACY_USER_CODE: &str = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    Vec::new()\n}";
    const FIXED_GRAPH_USER_CODE: &str = "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n    Vec::new()\n}\nfn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {\n    GraphResult::default()\n}";

    let legacy_info = cache_identity_info(FIXED_LEGACY_USER_CODE);
    let graph_info = cache_identity_info_graph(FIXED_GRAPH_USER_CODE);

    // Self-consistency: cache_identity_info_from_source must reproduce the
    // exact same identity when fed the already-assembled source directly,
    // proving compute_cache_identity's inputs are exactly
    // (assembled_source, XRAY_ABI_VERSION, rustc_version) with nothing else
    // silently mixed in.
    let legacy_reassembled = cache_identity_info_from_source(&assemble_evaluator_source(FIXED_LEGACY_USER_CODE));
    let graph_reassembled = cache_identity_info_from_source(&assemble_graph_evaluator_source(FIXED_GRAPH_USER_CODE));
    assert_eq!(legacy_info.identity, legacy_reassembled.identity, "legacy identity must be reproducible from the assembled source alone");
    assert_eq!(graph_info.identity, graph_reassembled.identity, "graph identity must be reproducible from the assembled source alone");

    // The two modes' assembled sources (and therefore identities) must
    // differ, since Legacy and Graph preambles/epilogues are disjoint text.
    assert_ne!(legacy_info.identity, graph_info.identity, "legacy and graph mode must never collide on cache identity");

    // Both identities are still 64-hex-char SHA-256 digests -- the split
    // did not change the digest algorithm or encoding.
    assert_eq!(legacy_info.identity.len(), SHA256_HEX_DIGEST_LEN);
    assert_eq!(graph_info.identity.len(), SHA256_HEX_DIGEST_LEN);
}
