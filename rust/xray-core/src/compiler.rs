/// Evaluator compilation pipeline: validate → assemble → compile → cache.
use crate::cache::{self, CacheMetadata};
use crate::validator;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::time::Instant;

const MAX_CACHE_ENTRIES: usize = 100;

/// Result of a successful compilation.
#[derive(Debug)]
pub struct CompileResult {
    pub so_path: PathBuf,
    pub compile_ms: u128,
    pub cached: bool,
}

/// Error from the compilation pipeline.
#[derive(Debug)]
pub struct CompileError {
    pub message: String,
    pub details: Vec<String>,
}

impl std::fmt::Display for CompileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)?;
        for d in &self.details {
            write!(f, "\n  {}", d)?;
        }
        Ok(())
    }
}

/// The evaluator preamble defines OwnedNode and EvalFinding so user code
/// can reference them without imports. Layout MUST match xray_core types.
///
/// `truncate_snippet` is included as a utility helper for user evaluator code
/// that needs to trim long text snippets before storing them in EvalFinding.
///
/// `XRAY_ABI_VERSION` is exported so the loader can verify the compiled .so
/// was built with a compatible type layout before calling the evaluate function.
const PREAMBLE: &str = r#"
/// ABI version sentinel — must match EXPECTED_ABI_VERSION in dynlib.rs.
const XRAY_ABI_VERSION: u64 = 2;

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
    pub fn has_descendant_of_kind(&self, kind: &str) -> bool {
        for child in &self.children {
            if child.kind == kind { return true; }
            if child.has_descendant_of_kind(kind) { return true; }
        }
        false
    }
    pub fn descendants_of_kind(&self, kind: &str) -> Vec<&OwnedNode> {
        let mut results = Vec::new();
        self.collect_descendants_of_kind(kind, &mut results);
        results
    }
    fn collect_descendants_of_kind<'a>(&'a self, kind: &str, results: &mut Vec<&'a OwnedNode>) {
        for child in &self.children {
            if child.kind == kind { results.push(child); }
            child.collect_descendants_of_kind(kind, results);
        }
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

const EPILOGUE: &str = r#"
#[no_mangle]
pub fn xray_evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    evaluate_node(node)
}

#[no_mangle]
pub fn xray_abi_version() -> u64 {
    XRAY_ABI_VERSION
}

#[no_mangle]
pub fn xray_drain_debug_log() -> Vec<String> {
    DEBUG_LOG.with(|log| {
        let mut log = log.borrow_mut();
        std::mem::take(&mut *log)
    })
}
"#;

/// Assemble a complete compilable .rs source from user evaluator code.
pub fn assemble_evaluator_source(user_code: &str) -> String {
    format!("{}\n// ---- USER CODE ----\n{}\n// ---- END USER CODE ----\n{}", PREAMBLE, user_code, EPILOGUE)
}

/// Number of lines in the preamble (for adjusting rustc error line numbers).
pub fn preamble_line_count() -> usize {
    PREAMBLE.lines().count() + 1 // +1 for the "USER CODE" comment
}

/// Compile user evaluator code into a cached .so file.
///
/// Pipeline: validate → hash → cache check → assemble → compile → save
pub fn compile_evaluator(user_code: &str, cache_dir: &Path) -> Result<CompileResult, CompileError> {
    // Step 1: Validate
    if let Err(errors) = validator::validate_evaluator_source(user_code) {
        return Err(CompileError {
            message: "Evaluator validation failed".to_string(),
            details: errors.iter().map(|e| e.to_string()).collect(),
        });
    }

    // Step 2: Check that evaluate_node function exists (AST-level, not substring)
    if !has_evaluate_node_fn(user_code) {
        return Err(CompileError {
            message: "Evaluator must define: fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>".to_string(),
            details: vec![],
        });
    }

    // Step 3: Hash
    let hash = sha256_hex(user_code);
    let so_path = cache_dir.join(format!("{}.so", hash));
    let meta_path = cache_dir.join(format!("{}.meta", hash));
    let rs_path = cache_dir.join(format!("{}.rs", hash));

    // Step 4: Cache check (version + hash + TTL freshness)
    let rustc_version = cache::get_rustc_version();
    if so_path.exists() {
        if let Some(meta) = cache::read_metadata(&meta_path) {
            if meta.rustc_version == rustc_version
                && meta.source_hash == hash
                && cache::is_fresh(&meta.compiled_at, cache::LOCAL_CACHE_TTL_SECS)
            {
                return Ok(CompileResult {
                    so_path,
                    compile_ms: 0,
                    cached: true,
                });
            }
        }
    }

    // Step 5: Create cache dir and assemble source
    std::fs::create_dir_all(cache_dir).map_err(|e| CompileError {
        message: format!("Failed to create cache directory '{}': {}", cache_dir.display(), e),
        details: vec![],
    })?;
    let full_source = assemble_evaluator_source(user_code);
    std::fs::write(&rs_path, &full_source).map_err(|e| CompileError {
        message: format!("Failed to write evaluator source: {}", e),
        details: vec![],
    })?;

    // Step 6: Compile
    let compile_start = Instant::now();
    let output = std::process::Command::new("rustc")
        .args([
            "--edition", "2021",
            "--crate-type", "cdylib",
            "-C", "opt-level=2",
            "-o", so_path.to_str().unwrap(),
            rs_path.to_str().unwrap(),
        ])
        .output()
        .map_err(|e| CompileError {
            message: format!("Failed to invoke rustc: {}", e),
            details: vec!["Is rustc installed and on PATH?".to_string()],
        })?;
    let compile_ms = compile_start.elapsed().as_millis();

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).to_string();
        let preamble_lines = preamble_line_count();
        let adjusted = adjust_error_lines(&stderr, preamble_lines);
        return Err(CompileError {
            message: "Evaluator compilation failed".to_string(),
            details: adjusted,
        });
    }

    // Step 7: Write metadata (best-effort — .so already exists, warn but don't fail)
    let now = chrono_now_iso();
    if let Err(e) = cache::write_metadata(&meta_path, &CacheMetadata {
        source_hash: hash,
        rustc_version,
        compiled_at: now,
        compile_ms,
    }) {
        eprintln!("xray: warning: failed to write cache metadata {}: {}", meta_path.display(), e);
    }

    // Step 8: LRU eviction
    cache::evict_lru(cache_dir, MAX_CACHE_ENTRIES);

    Ok(CompileResult {
        so_path,
        compile_ms,
        cached: false,
    })
}

/// Returns true only if `source` contains an actual `fn evaluate_node` function
/// definition at the top level — not just the text in a comment or string literal.
fn has_evaluate_node_fn(source: &str) -> bool {
    let file: syn::File = match syn::parse_str(source) {
        Ok(f) => f,
        Err(_) => return false,
    };
    file.items.iter().any(|item| {
        if let syn::Item::Fn(func) = item {
            func.sig.ident == "evaluate_node"
        } else {
            false
        }
    })
}

fn sha256_hex(input: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(input.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// Adjust rustc error line numbers by subtracting the preamble offset.
pub fn adjust_error_lines(stderr: &str, preamble_lines: usize) -> Vec<String> {
    let mut result = Vec::new();
    for line in stderr.lines() {
        // rustc errors look like: "  --> filename.rs:LINE:COL"
        if let Some(arrow_pos) = line.find("--> ") {
            let after = &line[arrow_pos + 4..];
            if let Some(colon1) = after.find(':') {
                let after_colon1 = &after[colon1 + 1..];
                if let Some(colon2) = after_colon1.find(':') {
                    let line_str = &after_colon1[..colon2];
                    if let Ok(orig_line) = line_str.parse::<usize>() {
                        let adjusted = orig_line.saturating_sub(preamble_lines);
                        let new_line = line.replacen(
                            &format!(":{}", orig_line),
                            &format!(":{}", adjusted),
                            1,
                        );
                        result.push(new_line);
                        continue;
                    }
                }
            }
        }
        result.push(line.to_string());
    }
    result
}

/// Simple timestamp without external dependency.
fn chrono_now_iso() -> String {
    let duration = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    format!("{}s-since-epoch", duration.as_secs())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use tempfile::TempDir;

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

    #[test]
    fn test_compile_cache_hit() {
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        // First compile
        let first = compile_evaluator(user_code, dir.path())
            .expect("first compile must succeed");
        assert!(!first.cached);

        // Second compile — must be a cache hit
        let second = compile_evaluator(user_code, dir.path())
            .expect("second compile must succeed");
        assert!(second.cached, "second compile of same code must be cached");
        assert_eq!(second.compile_ms, 0, "cached compile must report 0ms");
        assert_eq!(first.so_path, second.so_path, "same hash → same .so path");
    }

    #[test]
    fn test_compile_invalid_code_returns_error() {
        let dir = TempDir::new().unwrap();
        // unsafe block is rejected by the validator
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    unsafe { Vec::new() }
}
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(result.is_err(), "code with unsafe must be rejected");
        let err = result.unwrap_err();
        assert!(
            err.message.contains("validation") || err.details.iter().any(|d| d.contains("unsafe")),
            "error must mention unsafe or validation: {}",
            err
        );
    }

    #[test]
    fn test_adjust_error_lines() {
        // Preamble is 10 lines; original error at line 15 should adjust to line 5
        let stderr = "error[E0425]: cannot find value\n  --> /tmp/abc.rs:15:5\n  |";
        let adjusted = adjust_error_lines(stderr, 10);
        let joined = adjusted.join("\n");
        assert!(joined.contains(":5:"), "line 15 - 10 preamble = line 5: got {}", joined);
        assert!(!joined.contains(":15:"), "original line 15 should be replaced");
    }

    #[test]
    fn test_adjust_error_lines_no_overflow() {
        // Preamble larger than line number → saturate at 0 (not panic)
        let stderr = "  --> /tmp/abc.rs:3:1";
        let adjusted = adjust_error_lines(stderr, 100);
        let joined = adjusted.join("\n");
        assert!(joined.contains(":0:"), "saturate_sub must produce 0: got {}", joined);
    }

    #[test]
    fn test_compile_missing_evaluate_node_fn() {
        let dir = TempDir::new().unwrap();
        // Valid syntax but missing evaluate_node function
        let user_code = r#"
fn helper() -> Vec<u8> { vec![] }
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(result.is_err(), "code without evaluate_node must be rejected");
        let err = result.unwrap_err();
        assert!(
            err.message.contains("evaluate_node"),
            "error must mention evaluate_node: {}",
            err.message
        );
    }

    #[test]
    fn test_rejects_evaluate_node_in_comment() {
        let dir = TempDir::new().unwrap();
        // The string "fn evaluate_node" appears only in a comment — no actual function
        let user_code = r#"
// fn evaluate_node is documented here
fn helper() -> Vec<u8> { vec![] }
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(result.is_err(), "evaluate_node only in comment must be rejected");
        let err = result.unwrap_err();
        assert!(
            err.message.contains("evaluate_node"),
            "error must mention evaluate_node: {}",
            err.message
        );
    }

    #[test]
    fn test_rejects_evaluate_node_in_string() {
        let dir = TempDir::new().unwrap();
        // The string "fn evaluate_node" appears only inside a string literal
        let user_code = r#"
fn helper() -> &'static str { "fn evaluate_node" }
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(result.is_err(), "evaluate_node only in string must be rejected");
        let err = result.unwrap_err();
        assert!(
            err.message.contains("evaluate_node"),
            "error must mention evaluate_node: {}",
            err.message
        );
    }

    #[test]
    fn test_compile_cache_respects_ttl() {
        // Compile once, then backdate compiled_at to 600s ago (beyond TTL=300).
        // Second compile must NOT return cached=true.
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        // First compile
        let first = compile_evaluator(user_code, dir.path())
            .expect("first compile must succeed");
        assert!(!first.cached, "first compile must not be cached");

        // Backdate compiled_at in the .meta file to simulate a stale entry
        let hash = sha256_hex(user_code);
        let meta_path = dir.path().join(format!("{}.meta", hash));
        let old_epoch = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            - 600; // 600s ago — beyond TTL of 300
        let stale_meta = cache::CacheMetadata {
            source_hash: hash.clone(),
            rustc_version: cache::get_rustc_version(),
            compiled_at: format!("{}s-since-epoch", old_epoch),
            compile_ms: 100,
        };
        cache::write_metadata(&meta_path, &stale_meta).expect("must write stale meta");

        // Second compile — stale meta must trigger recompile (cached=false)
        let second = compile_evaluator(user_code, dir.path())
            .expect("second compile must succeed");
        assert!(!second.cached, "stale cache entry must trigger recompile, not return cached=true");
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

    #[test]
    fn test_compiled_evaluator_can_call_debug_log() {
        // Integration: evaluator code using debug_log() must compile successfully.
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    debug_log("visiting node");
    debug_log(&format!("kind={}", node.kind));
    Vec::new()
}
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(
            result.is_ok(),
            "evaluator using debug_log must compile: {:?}",
            result.err().map(|e| e.to_string())
        );
    }

    #[test]
    fn test_debug_log_truncation_limits() {
        // AC5: evaluator calling debug_log 200 times must compile (truncation is runtime).
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    for i in 0..200usize {
        debug_log(&format!("message {}", i));
    }
    Vec::new()
}
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(
            result.is_ok(),
            "evaluator with looping debug_log must compile: {:?}",
            result.err().map(|e| e.to_string())
        );
    }

    #[test]
    fn test_debug_log_truncation_limits_runtime() {
        // Runtime assertion: evaluator calling debug_log 150 times must yield exactly
        // 100 messages (not 150) — the PREAMBLE enforces a hard cap of 100 per evaluation.
        use crate::dynlib::DynlibEvaluator;
        use crate::owned_node::OwnedNode;
        use crate::scanner::Evaluator;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    for i in 0..150usize {
        debug_log(&format!("msg {}", i));
    }
    Vec::new()
}
"#;
        let cr = compile_evaluator(user_code, dir.path())
            .expect("evaluator with 150 debug_log calls must compile");

        let evaluator = DynlibEvaluator::load(&cr.so_path)
            .expect("compiled .so must load successfully");

        let source: Arc<str> = Arc::from("");
        let node = OwnedNode {
            kind: "root".to_string(),
            start_line: 1,
            start_byte: 0,
            end_byte: 0,
            children: vec![],
            is_named: true,
            source,
        };

        evaluator.evaluate_node(&node);
        let messages = evaluator.drain_debug_log();

        assert_eq!(
            messages.len(),
            100,
            "100-message cap must be enforced at runtime: got {} messages",
            messages.len()
        );
        // Verify the FIRST 100 messages are retained (not arbitrary ones).
        for i in 0..100usize {
            assert_eq!(
                messages[i],
                format!("msg {}", i),
                "message at index {} must be 'msg {}', got: {}",
                i, i, messages[i]
            );
        }
    }

    #[test]
    fn test_debug_log_byte_limit_runtime() {
        // Runtime assertion: when messages exceed 10KB total, further messages are
        // silently dropped — enforced by the PREAMBLE 10240-byte guard.
        // Each message is 200 bytes; 51 * 200 = 10200 <= 10240 (fits).
        // 52nd message would push total to 10400 > 10240 (dropped).
        use crate::dynlib::DynlibEvaluator;
        use crate::owned_node::OwnedNode;
        use crate::scanner::Evaluator;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let big_msg: String = std::iter::repeat('b').take(200).collect();
    for _ in 0..60usize {
        debug_log(&big_msg);
    }
    Vec::new()
}
"#;
        let cr = compile_evaluator(user_code, dir.path())
            .expect("evaluator with large debug_log messages must compile");

        let evaluator = DynlibEvaluator::load(&cr.so_path)
            .expect("compiled .so must load successfully");

        let source: Arc<str> = Arc::from("");
        let node = OwnedNode {
            kind: "root".to_string(),
            start_line: 1,
            start_byte: 0,
            end_byte: 0,
            children: vec![],
            is_named: true,
            source,
        };

        evaluator.evaluate_node(&node);
        let messages = evaluator.drain_debug_log();

        // 51 * 200 = 10200 bytes fits within 10240; 52nd message (200 bytes) would
        // make 10400 > 10240 and is dropped. Exactly 51 messages must be retained.
        assert_eq!(
            messages.len(),
            51,
            "10KB byte cap must be enforced: expected 51 messages, got {}",
            messages.len()
        );
        let expected_msg: String = std::iter::repeat('b').take(200).collect();
        for (i, msg) in messages.iter().enumerate() {
            assert_eq!(
                msg, &expected_msg,
                "message {} must be the 200-byte string, got len={}",
                i,
                msg.len()
            );
        }
    }
}
