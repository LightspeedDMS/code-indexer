//! Issue #1934: `compiler.rs`'s unit tests covering the compile pipeline
//! (rustc invocation, caching, mode dispatch, concurrency), relocated
//! verbatim out of its single `#[cfg(test)] mod tests { ... }` body
//! (declared at `compiler.rs` via `#[cfg(test)] #[path =
//! "tests_pipeline.rs"] mod tests_pipeline;`, mirroring the pattern
//! `graph/bind/resolve.rs` already uses) so `compiler.rs` itself stays
//! well under the project's line limit.

use super::*;
use crate::cache;
use std::path::Path;
use tempfile::TempDir;

// --- HIGH (Codex follow-up review): no compile timeout/resource
// limit; killing xray-cli orphans rustc ---

/// A source exceeding the size cap must be rejected IMMEDIATELY,
/// before rustc is ever invoked -- proven by using a size so large
/// (10 MiB) that a real compile attempt would take dramatically
/// longer than a fast, no-compile rejection.
#[test]
fn compile_evaluator_rejects_oversized_source_before_invoking_rustc() {
    let dir = TempDir::new().unwrap();
    let huge_user_code = format!(
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {{\n// {}\n    Vec::new()\n}}",
        "x".repeat(10 * 1024 * 1024)
    );
    let start = std::time::Instant::now();
    let result = compile_evaluator(&huge_user_code, dir.path());
    let elapsed = start.elapsed();

    assert!(result.is_err(), "an oversized source must be rejected, not compiled");
    assert!(
        elapsed < std::time::Duration::from_secs(2),
        "rejection must be near-instant (no rustc invocation attempted), took {:?}",
        elapsed
    );
}

/// Bug #1816 (THE fix's discriminating test, evaluator-compile side):
/// the actual `rustc` invocation that compiles a user evaluator `.so`
/// must carry `RUSTUP_TOOLCHAIN` pinned to `cache::pinned_toolchain_channel()`.
/// This is the MORE important of the two Bug #1816 toolchain-pin call
/// sites (the other is `cache::get_rustc_version`'s version probe) --
/// this is the command that produces the artifact loaded across the
/// `GraphHandle` FFI boundary, so an unpinned toolchain here is exactly
/// what let the compiled evaluator diverge from the rustc version that
/// built `xray-cli` itself, corrupting the heap the moment
/// `analyze_graph` called both `signature_for` and
/// `shortest_path_to_any` (see the module doc comment on
/// `cache::pinned_toolchain_channel` for the full root-cause writeup).
#[test]
fn evaluator_rustc_command_pins_rustup_toolchain_env_var() {
    let dir = TempDir::new().unwrap();
    let build_rs_path = dir.path().join("evaluator.rs");
    let build_so_path = dir.path().join("evaluator.so");
    let command = evaluator_rustc_command(&build_rs_path, &build_so_path);
    let envs: std::collections::HashMap<_, _> = command.get_envs().collect();
    assert_eq!(
        envs.get(std::ffi::OsStr::new("RUSTUP_TOOLCHAIN")),
        Some(&Some(std::ffi::OsStr::new(crate::cache::pinned_toolchain_channel()))),
        "the evaluator-compiling rustc command must pin RUSTUP_TOOLCHAIN to the workspace channel"
    );
}

/// `run_rustc_with_timeout` must return promptly with a timeout error
/// for a hanging command, rather than blocking for the command's full
/// runtime -- proving the timeout+process-group-kill mechanism
/// actually works, not just that a timeout constant exists somewhere.
#[test]
fn run_rustc_with_timeout_returns_promptly_instead_of_blocking_forever() {
    let mut hanging_command = std::process::Command::new("sh");
    hanging_command.arg("-c").arg("sleep 30");

    let start = std::time::Instant::now();
    let result = run_rustc_with_timeout(hanging_command, std::time::Duration::from_millis(200));
    let elapsed = start.elapsed();

    assert!(result.is_err(), "a hanging command must time out, not succeed");
    assert!(
        elapsed < std::time::Duration::from_secs(5),
        "must return promptly after the timeout, not block for the full 30s sleep, took {:?}",
        elapsed
    );
}

/// AC8: `compile_evaluator_impl`'s mode-aware branching must accept a
/// genuine graph-mode evaluator (one `detect_evaluator_mode` classifies
/// as `EvaluatorMode::Graph`), assemble it with `GRAPH_PREAMBLE_EXTRA_*`
/// and `GRAPH_EPILOGUE`, compile it to a real `.so`, and that `.so`
/// must actually EXPORT the graph-mode symbols -- never `xray_evaluate_node`.
#[test]
fn compile_evaluator_compiles_a_valid_graph_mode_evaluator() {
    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    for symbol in g.reachable_from(&[0], 5) {
        result.refine.push(g.resolve_symbol(symbol).expect("symbol came from g.reachable_from, always valid"));
    }
    result
}
"#;
    assert_eq!(
        detect_evaluator_mode(user_code).unwrap(),
        EvaluatorMode::Graph,
        "test fixture assumption broken: this source must classify as Graph mode"
    );

    let result = compile_evaluator(user_code, dir.path())
        .expect("a genuine graph-mode evaluator must compile successfully");
    assert!(result.so_path.exists(), ".so file must exist on disk");

    let lib = unsafe { libloading::Library::new(&result.so_path) }
        .expect("compiled graph-mode .so must load successfully");
    unsafe {
        let has_collect_facts: bool = lib.get::<extern "Rust" fn()>(b"xray_collect_facts\0").is_ok();
        let has_analyze_graph: bool = lib.get::<extern "Rust" fn()>(b"xray_analyze_graph\0").is_ok();
        let has_legacy: bool = lib.get::<extern "Rust" fn()>(b"xray_evaluate_node\0").is_ok();
        assert!(has_collect_facts, "graph-mode .so must export xray_collect_facts");
        assert!(has_analyze_graph, "graph-mode .so must export xray_analyze_graph");
        assert!(!has_legacy, "graph-mode .so must NEVER export xray_evaluate_node");
    }
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
    // Codex H2 (Bug #1827 remediation): a validation rejection is a
    // genuine problem in the USER's evaluator source (the sandbox
    // validator is rejecting THEIR code), never an infrastructure
    // problem -- the agent should read `details` and fix its code.
    assert_eq!(
        err.kind,
        CompileErrorKind::Compile,
        "validation failure must classify as Compile, not Infrastructure"
    );
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
    // Codex H2 (Bug #1827 remediation): mode-detection rejects the
    // user's own source shape -- a genuine Compile-kind problem, not
    // an infrastructure failure.
    assert_eq!(
        err.kind,
        CompileErrorKind::Compile,
        "missing evaluate_node must classify as Compile"
    );
}

/// Codex H2 (Bug #1827 remediation, THE discriminating test): a
/// genuine subprocess-spawn failure (rustc itself could not be
/// invoked -- an infrastructure problem completely unrelated to
/// anything in the user's evaluator source) must classify as
/// Infrastructure, never Compile. Calls `run_rustc_with_timeout`
/// directly with a `Command` pointing at a binary that does not
/// exist, forcing the REAL spawn-failure path (no mocking).
#[test]
fn test_compile_error_kind_is_infrastructure_for_rustc_spawn_failure() {
    let command = std::process::Command::new("definitely-not-a-real-rustc-binary-xyz-1827");
    let result = run_rustc_with_timeout(command, std::time::Duration::from_secs(5));
    assert!(result.is_err(), "spawning a nonexistent binary must fail");
    let err = result.unwrap_err();
    assert_eq!(
        err.kind,
        CompileErrorKind::Infrastructure,
        "a genuine spawn failure must classify as Infrastructure, not \
         Compile -- telling the agent 'CompileError' here would send \
         it to debug perfectly valid Rust: {}",
        err
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

    // Backdate compiled_at in the .meta file to simulate a stale entry.
    // Bug #1784: the seed path must be the REAL post-fix identity, not
    // the retired raw sha256_hex(user_code) key.
    let info = cache_identity_info_from_source(&assemble_evaluator_source(user_code));
    let meta_path = dir.path().join(format!("{}.meta", info.identity));
    let old_epoch = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
        - 600; // 600s ago — beyond TTL of 300
    let stale_meta = cache::CacheMetadata {
        source_hash: info.source_hash.clone(),
        rustc_version: info.rustc_version.clone(),
        abi_version: info.abi_version,
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
fn test_compile_evaluator_cache_miss_when_preamble_changes() {
    // MANDATORY regression guard (Bug #1784 review MAJOR-1): proves,
    // through the REAL compile_evaluator pipeline (not just the
    // standalone compute_cache_identity() function), that changing
    // ONLY the preamble text -- user code, ABI version, and rustc
    // toolchain held fixed -- forces a fresh compile (cached == false),
    // never a stale-artifact reuse. This is the test class that would
    // catch a future regression where the cache identity is again
    // derived from raw user_code alone while the composite-identity
    // helper functions (compute_cache_identity / cache_identity_info_
    // from_source) are left in place unchanged.
    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;

    let preamble_v1 = PREAMBLE;
    let first = compile_evaluator_with_preamble(user_code, dir.path(), preamble_v1)
        .expect("first compile (preamble v1) must succeed");
    assert!(!first.cached, "first compile must not be a cache hit");

    // Sanity check: repeating the SAME preamble + user code must hit the
    // local cache -- proves the harness itself actually exercises
    // caching, so the later cache-miss assertion is meaningful.
    let repeat = compile_evaluator_with_preamble(user_code, dir.path(), preamble_v1)
        .expect("repeat compile with unchanged preamble must succeed");
    assert!(repeat.cached, "repeat compile with unchanged preamble must be a cache hit");

    // preamble_v2 differs from preamble_v1 by a trivial appended comment
    // -- still valid, compilable Rust; the OwnedNode/EvalFinding/
    // debug_log definitions user code depends on are unchanged.
    let preamble_v2 = format!(
        "{}\n// preamble v2 marker (Bug #1784 regression test)\n",
        preamble_v1
    );
    let second = compile_evaluator_with_preamble(user_code, dir.path(), &preamble_v2)
        .expect("second compile (preamble v2) must succeed");
    assert!(
        !second.cached,
        "changing ONLY the preamble text must force a cache MISS, never reuse the preamble-v1 artifact"
    );
    assert_ne!(
        second.so_path, first.so_path,
        "preamble v1 and v2 artifacts must be stored under different identities"
    );
}

/// Test helper: write a fake `.so` + valid `.meta` pair at `{identity}.so`
/// / `{identity}.meta` inside `dir`, so a cache lookup for `identity`
/// finds a pre-existing artifact.
fn seed_cache_entry(dir: &Path, identity: &str, so_bytes: &[u8], meta: &cache::CacheMetadata) {
    std::fs::write(dir.join(format!("{}.so", identity)), so_bytes).expect("seed .so");
    cache::write_metadata(&dir.join(format!("{}.meta", identity)), meta).expect("seed .meta");
}

#[test]
fn test_stale_artifact_under_old_raw_user_code_hash_is_not_reused() {
    // MANDATORY regression guard (Bug #1784): before the fix, the cache
    // key was sha256_hex(user_code) alone. Seed an artifact at exactly
    // that pre-fix lookup path, with metadata pre-fix code treats as
    // fully valid, and prove the fix does not reuse it.
    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
    let old_buggy_key = sha256_hex(user_code);
    let garbage_bytes = b"NOT_A_REAL_SHARED_OBJECT_FROM_OLD_PREAMBLE";
    seed_cache_entry(
        dir.path(),
        &old_buggy_key,
        garbage_bytes,
        &cache::CacheMetadata {
            source_hash: old_buggy_key.clone(),
            rustc_version: cache::get_rustc_version(),
            abi_version: XRAY_ABI_VERSION,
            compiled_at: chrono_now_iso(),
            compile_ms: 5,
        },
    );

    let result = compile_evaluator(user_code, dir.path())
        .expect("compile must succeed despite the seeded stale artifact");

    let garbage_so_path = dir.path().join(format!("{}.so", old_buggy_key));
    assert!(!result.cached, "must NOT be a cache hit on an artifact keyed by the pre-fix scheme");
    assert_ne!(result.so_path, garbage_so_path, "must NOT return the seeded garbage file");
    let so_bytes = std::fs::read(&result.so_path).expect("compiled .so must exist");
    assert!(so_bytes.len() > garbage_bytes.len(), "a real compiled cdylib must be far larger than the garbage placeholder");
}

#[test]
fn test_meta_abi_version_field_mismatch_at_matching_filename_forces_miss() {
    // Defense in depth: even if a .so/.meta pair sits at the CURRENT
    // identity's filename (so_path.exists() == true), a recorded
    // abi_version that doesn't match XRAY_ABI_VERSION must still force a
    // MISS. Guards the per-field freshness check independently of the
    // filename-based key.
    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
    let assembled = assemble_evaluator_source(user_code);
    let info = cache_identity_info_from_source(&assembled);
    let wrong_abi = info.abi_version.saturating_sub(1);
    assert_ne!(wrong_abi, info.abi_version);

    seed_cache_entry(
        dir.path(),
        &info.identity,
        b"GARBAGE_AT_RIGHT_FILENAME_WRONG_ABI",
        &cache::CacheMetadata {
            source_hash: info.source_hash.clone(),
            rustc_version: info.rustc_version.clone(),
            abi_version: wrong_abi,
            compiled_at: chrono_now_iso(),
            compile_ms: 5,
        },
    );

    let result = compile_evaluator(user_code, dir.path())
        .expect("compile must succeed despite the seeded stale-ABI metadata");
    assert!(!result.cached, "an abi_version field mismatch must force a MISS even at a matching filename");
}

const CONCURRENT_TEST_1425_USER_CODE: &str = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    if node.kind == "try_statement" {
        findings.push(EvalFinding {
            pattern: "concurrent_test_1425".to_string(),
            line: node.start_line,
            snippet: String::new(),
        });
    }
    findings
}
"#;

/// Spawns `thread_count` threads that all call `compile_evaluator` with the
/// SAME `user_code` against the SAME `cache_dir` concurrently, returning
/// every thread's result.
fn spawn_concurrent_compiles(
    cache_dir: &Path,
    user_code: &str,
    thread_count: usize,
) -> Vec<Result<CompileResult, CompileError>> {
    let handles: Vec<_> = (0..thread_count)
        .map(|_| {
            let cache_dir = cache_dir.to_path_buf();
            let code = user_code.to_string();
            std::thread::spawn(move || compile_evaluator(&code, &cache_dir))
        })
        .collect();

    handles
        .into_iter()
        .map(|h| h.join().expect("compile_evaluator thread must not panic"))
        .collect()
}

/// Loads a compiled evaluator .so and verifies it produces the expected
/// single finding for a `try_statement` node — proves the artifact is not
/// merely present on disk but is a correct, loadable evaluator.
fn assert_evaluator_finds_try_statement(so_path: &Path, index: usize) {
    use crate::dynlib::DynlibEvaluator;
    use crate::owned_node::OwnedNode;
    use crate::scanner::Evaluator;

    let evaluator = DynlibEvaluator::load(so_path)
        .unwrap_or_else(|e| panic!("compile #{}: .so must load successfully: {}", index, e));
    let node = OwnedNode::new_leaf_for_test("try_statement", "try { } catch { }", 1, true);
    let findings = evaluator.evaluate_node(&node);
    assert_eq!(
        findings.len(),
        1,
        "compile #{}: evaluator must find exactly one match, got {:?}",
        index,
        findings
    );
    assert_eq!(
        findings[0].pattern, "concurrent_test_1425",
        "compile #{}: finding pattern must match evaluator source",
        index
    );
}

/// Bug #1425: two concurrent compiles of the SAME evaluator hash against a
/// cold cache must NOT clobber each other's rustc intermediate .rcgu.o
/// object files. Reproduced by fanning out N threads that all call
/// compile_evaluator() with byte-identical source against a shared,
/// freshly-created (cold) cache_dir at the same time.
///
/// Pre-fix, the losing thread's rustc invocation fails with "rust-lld:
/// error: cannot open <hash>.rcgu.o: No such file or directory" because
/// both invocations wrote their -o output (and thus their codegen-unit
/// object files) directly into the shared cache_dir using the identical
/// crate name (the hash). Post-fix, every thread must succeed AND produce
/// a loadable, CORRECT compiled evaluator — not just "no panic".
#[test]
fn test_concurrent_compile_same_hash_cold_cache_both_succeed() {
    let dir = TempDir::new().unwrap();
    const THREAD_COUNT: usize = 8;
    let results =
        spawn_concurrent_compiles(dir.path(), CONCURRENT_TEST_1425_USER_CODE, THREAD_COUNT);

    for (i, result) in results.iter().enumerate() {
        assert!(
            result.is_ok(),
            "concurrent compile #{} of identical hash must succeed, got: {:?}",
            i,
            result.as_ref().err().map(|e| e.to_string())
        );
    }

    for (i, result) in results.into_iter().enumerate() {
        let cr = result.unwrap();
        assert!(
            cr.so_path.exists(),
            "compile #{}: .so file must exist on disk at {}",
            i,
            cr.so_path.display()
        );
        assert_evaluator_finds_try_statement(&cr.so_path, i);
    }
}
