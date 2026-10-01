//! Issue #1934: the legacy-mode evaluator PREAMBLE/EPILOGUE text,
//! extracted verbatim out of `compiler.rs` (pure move -- no behaviour
//! change, see the module doc comment on `super`). These constants are
//! STRING LITERALS compiled verbatim into every legacy-mode evaluator
//! artifact; their content must never be touched by a refactor -- only the
//! Rust-level visibility of `EPILOGUE` itself changes here (bumped from
//! private to `pub(crate)` so sibling modules in the new `compiler` tree,
//! and the split test files, can still reach it -- this has zero effect on
//! the compiled evaluator, since Rust visibility is a compile-time-only
//! concept that never appears in generated code). The graph-mode mirror
//! constants (`GRAPH_PREAMBLE_EXTRA_*`/`GRAPH_EPILOGUE`/
//! `GRAPH_REFINE_EPILOGUE`) live in the sibling `graph_preamble` module.

/// The evaluator preamble defines OwnedNode and EvalFinding so user code
/// can reference them without imports. Layout MUST match xray_core types.
///
/// `truncate_snippet` is included as a utility helper for user evaluator code
/// that needs to trim long text snippets before storing them in EvalFinding.
///
/// `XRAY_ABI_VERSION` is exported so the loader can verify the compiled .so
/// was built with a compatible type layout before calling the evaluate function.
pub(crate) const PREAMBLE: &str = r#"
/// ABI version sentinel — substituted from the single source of truth
/// (compiler::XRAY_ABI_VERSION) by assemble_evaluator_source_with_preamble
/// before every compile (Bug #1784 review MAJOR-3).
const XRAY_ABI_VERSION: u64 = __XRAY_ABI_VERSION_PLACEHOLDER__;
const XRAY_RUSTC_VERSION: &[u8] = b"__XRAY_RUSTC_VERSION_PLACEHOLDER__";

use std::sync::Arc;

#[derive(Debug, Clone)]
pub struct OwnedNode {
    pub kind: String,
    pub start_line: usize,
    pub start_byte: usize,
    pub end_byte: usize,
    pub children: Vec<OwnedNode>,
    pub is_named: bool,
    source: Arc<str>,
}

impl OwnedNode {
    pub fn text(&self) -> &str {
        self.source.get(self.start_byte..self.end_byte).unwrap_or("")
    }
    pub fn named_children(&self) -> Vec<&OwnedNode> {
        self.children.iter().filter(|c| c.is_named).collect()
    }
    pub fn child_by_kind(&self, kind: &str) -> Option<&OwnedNode> {
        self.children.iter().find(|c| c.kind == kind)
    }
    // Bug #1795: explicit-stack (heap) traversal instead of recursion, so a
    // deeply nested OwnedNode passed into a compiled evaluator cannot
    // SIGABRT the process. Mirrors owned_node.rs's fix exactly (known
    // duplication debt, tracked separately as AC16 on #1787 -- not
    // refactored here).
    pub fn has_descendant_of_kind(&self, kind: &str) -> bool {
        let mut stack: Vec<&OwnedNode> = self.children.iter().collect();
        while let Some(node) = stack.pop() {
            if node.kind == kind { return true; }
            stack.extend(node.children.iter());
        }
        false
    }
    pub fn descendants_of_kind(&self, kind: &str) -> Vec<&OwnedNode> {
        let mut results = Vec::new();
        let mut stack: Vec<&OwnedNode> = self.children.iter().rev().collect();
        while let Some(node) = stack.pop() {
            if node.kind == kind { results.push(node); }
            stack.extend(node.children.iter().rev());
        }
        results
    }
}

#[derive(Debug, Clone)]
pub struct EvalFinding {
    pub pattern: String,
    pub line: usize,
    pub snippet: String,
}

/// Utility for user evaluator code: collapse whitespace and truncate to max_len bytes.
/// Truncation always falls on a UTF-8 char boundary — never panics on multibyte chars.
/// If truncation occurs, appends "...".
fn truncate_snippet(s: &str, max_len: usize) -> String {
    let collapsed: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.len() <= max_len {
        collapsed
    } else {
        let boundary = collapsed
            .char_indices()
            .map(|(i, _)| i)
            .take_while(|&i| i <= max_len)
            .last()
            .unwrap_or(0);
        format!("{}...", &collapsed[..boundary])
    }
}

use std::cell::RefCell;

thread_local! {
    static DEBUG_LOG: RefCell<Vec<String>> = RefCell::new(Vec::new());
}

/// Debug logging helper for evaluator development.
/// Messages are collected in a per-thread buffer (max 100 messages, 10KB total)
/// and returned alongside findings in the JSON output as debug_messages[].
/// Calls past the limits are silently dropped.
fn debug_log(msg: &str) {
    DEBUG_LOG.with(|log| {
        let mut log = log.borrow_mut();
        if log.len() < 100 {
            let total_bytes: usize = log.iter().map(|s| s.len()).sum();
            if total_bytes + msg.len() <= 10240 {
                log.push(msg.to_string());
            }
        }
    });
}
"#;

pub(crate) const EPILOGUE: &str = r#"
#[no_mangle]
pub fn xray_evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    evaluate_node(node)
}

/// Bug #1855 (H1): the compatibility probe itself must be ABI-stable BY
/// CONSTRUCTION, not by convention. These three exports are the ONLY
/// symbols the dynlib loader calls before compatibility between the host
/// and this compiled evaluator has been established (xray_abi_version
/// first, then the rustc_version ptr/len pair) -- so they cannot rely on
/// "both sides happened to use the same rustc" the way the DATA-carrying
/// callbacks below (xray_evaluate_node above; xray_collect_facts/
/// xray_analyze_graph/xray_refine in GRAPH_EPILOGUE) legitimately can,
/// since those only ever run AFTER this probe has already proven it. Using
/// `extern "C"` pins a fixed, documented calling convention across the
/// dylib boundary instead of Rust's own unstable-across-compiler-versions
/// ABI, removing the one case where this codebase asked the plain Rust ABI
/// to prove its own precondition.
#[no_mangle]
pub extern "C" fn xray_abi_version() -> u64 {
    XRAY_ABI_VERSION
}

/// Raw pointer half of the evaluator's embedded rustc version string,
/// read by the host loader BEFORE compatibility is established (H1).
#[no_mangle]
pub extern "C" fn xray_rustc_version_ptr() -> u64 {
    XRAY_RUSTC_VERSION.as_ptr() as u64
}

/// Length half of the evaluator's embedded rustc version string,
/// read by the host loader BEFORE compatibility is established (H1).
#[no_mangle]
pub extern "C" fn xray_rustc_version_len() -> u64 {
    XRAY_RUSTC_VERSION.len() as u64
}

#[no_mangle]
pub fn xray_drain_debug_log() -> Vec<String> {
    DEBUG_LOG.with(|log| {
        let mut log = log.borrow_mut();
        std::mem::take(&mut *log)
    })
}
"#;
