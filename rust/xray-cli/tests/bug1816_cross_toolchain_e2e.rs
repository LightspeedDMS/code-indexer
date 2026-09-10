//! Bug #1816 regression: `signature_for` + `shortest_path_to_any` in the
//! same `analyze_graph` evaluator corrupted the heap (`free(): double free
//! detected in tcache 2` / SIGSEGV depending on call order) when the
//! evaluator's `rustc` compile step ran under a DIFFERENT toolchain than
//! the one that built `xray-cli` -- see commit `afcd1fe3` for the full
//! root-cause writeup (rustup resolves the toolchain by walking up from
//! the CALLING PROCESS's cwd; production's cwd is nowhere near `rust/`).
//!
//! The fix pins `RUSTUP_TOOLCHAIN` explicitly in `evaluator_rustc_command`
//! (`compiler.rs`), independent of cwd. `dynlib.rs`'s own Bug #1816 tests
//! (added by the same commit) call `compile_evaluator` IN-PROCESS under
//! `cargo test`, whose cwd is always inside `rust/` -- self-consistent
//! with or without the pin, so those tests cannot discriminate the fix at
//! all (this is explicitly noted in the fix commit's own message). This
//! test closes that gap: it spawns `xray-cli --compile-only` as a genuine
//! CHILD PROCESS with `.current_dir()` pointed outside the workspace,
//! reproducing the exact condition the pin exists for, without mutating
//! this test binary's own (process-global, parallel-test-unsafe) cwd.
//!
//! Its discriminating power is only real when the machine actually has
//! toolchain drift (an ambient `rustup default` that differs from the
//! pinned channel) -- `environment_has_toolchain_drift` verifies that
//! empirically before asserting anything, and skips (with a clear stderr
//! message, never a false pass claim) when no drift is present, since
//! that is the same precondition the real Bug #1816 itself needed.

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
/// Matches the bug report's own minimal-reproducer depth exactly.
const MAX_PATH_DEPTH: usize = 20;

/// Creates a tempdir and VERIFIES it is not nested under this crate's
/// workspace root (`rust/`, derived from `CARGO_MANIFEST_DIR` at compile
/// time) -- a bare `tempfile::tempdir()` alone would silently stop
/// reproducing Bug #1816's actual condition if `TMPDIR` were ever
/// configured beneath the workspace.
fn outside_workspace_tempdir() -> tempfile::TempDir {
    let workspace_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("xray-cli's CARGO_MANIFEST_DIR must have a parent (the rust/ workspace root)")
        .canonicalize()
        .expect("canonicalize workspace root");
    let dir = tempfile::tempdir().expect("create outside-workspace temp dir");
    let canonical = dir.path().canonicalize().expect("canonicalize temp dir");
    assert!(
        !canonical.starts_with(&workspace_root),
        "temp dir {canonical:?} is nested under the workspace root {workspace_root:?} -- \
         this test cannot reproduce Bug #1816's condition from here; reconfigure TMPDIR"
    );
    dir
}

/// Empirically verifies THIS environment has real toolchain drift -- an
/// ambient `rustc --version` (resolved with NO `RUSTUP_TOOLCHAIN`
/// override, from `outside_dir`, matching exactly what an UNPINNED
/// `evaluator_rustc_command` would have resolved pre-fix) that differs
/// from the pinned toolchain's version (`xray_core::cache::
/// get_rustc_version()`, `pub`). Without this check, a machine whose
/// `rustup default` already happens to equal the pinned channel would let
/// this test pass trivially regardless of whether the pin exists,
/// exactly the "passes both before and after the fix" trap this mission
/// warns against.
fn environment_has_toolchain_drift(outside_dir: &std::path::Path) -> bool {
    let ambient = Command::new("rustc")
        .arg("--version")
        .env_remove("RUSTUP_TOOLCHAIN")
        .current_dir(outside_dir)
        .output()
        .expect("failed to probe ambient rustc --version");
    let ambient_version = String::from_utf8_lossy(&ambient.stdout).trim().to_string();
    let pinned_version = xray_core::cache::get_rustc_version();
    ambient.status.success() && !ambient_version.is_empty() && ambient_version != pinned_version
}

/// Builds the same tiny real `CodeGraph` (A -> B) `ac8_analyze_graph_e2e.
/// rs` uses, PLUS a real cached signature on A -- `signature_for`'s unsafe
/// raw-pointer-to-`&str` reconstruction is dead code without one, so a
/// signature-less fixture would silently fail to exercise the accessor
/// this bug is about.
fn write_small_graph_with_signature(dir: &std::path::Path) -> std::path::PathBuf {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
    let a = builder.intern_symbol(make_symbol_id(1, 0));
    let b = builder.intern_symbol(make_symbol_id(1, 1));
    builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
    builder.add_signature(a, "run()".to_string());
    let path = dir.join("graph.bin");
    write_graph_file(&builder.build(), &path).expect("write_graph_file must succeed");
    path
}

/// Compiles `user_code` via a REAL CHILD PROCESS invocation of `xray-cli
/// --compile-only`, with that CHILD's cwd set to `outside_dir` -- so ITS
/// OWN internal rustc-spawning inherits a cwd from which
/// `rust-toolchain.toml` is not discoverable by directory walk-up. Never
/// touches this test process's own cwd (parallel-test-safe).
fn compile_outside_workspace(user_code: &str, source_dir: &std::path::Path, outside_dir: &std::path::Path, context: &str) -> std::path::PathBuf {
    let source_path = source_dir.join("evaluator.rs");
    std::fs::write(&source_path, user_code).expect("write evaluator source");
    let output = Command::new(env!("CARGO_BIN_EXE_xray-cli"))
        .arg("--compile-only")
        .arg("--dynlib")
        .arg(&source_path)
        .current_dir(outside_dir)
        // `cargo test` itself runs under rustup's shim, which sets
        // RUSTUP_TOOLCHAIN in THIS test process's own environment --
        // inherited by a child Command by default. Without clearing it
        // here, the child's rustc resolution would stay pinned regardless
        // of `.current_dir()`, masking the exact condition this test
        // exists to exercise (verified empirically: this test falsely
        // passed against a pre-fix binary before this env_remove was
        // added). Production's calling process (the MCP server) has no
        // such var set either, so clearing it is the correct simulation.
        .env_remove("RUSTUP_TOOLCHAIN")
        .output()
        .expect("failed to spawn xray-cli --compile-only");
    assert!(
        output.status.success(),
        "{context}: xray-cli --compile-only must exit successfully; stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    let parsed: serde_json::Value = serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("{context}: --compile-only stdout must be valid JSON: {e}; stdout={stdout}"));
    assert!(
        parsed["error"].is_null(),
        "{context}: compile_evaluator must succeed with the toolchain pin fix; error={:?}",
        parsed["error"]
    );
    std::path::PathBuf::from(parsed["so_path"].as_str().expect("so_path must be a string"))
}

/// Runs the real `xray-cli --analyze-graph` process against `so_path`,
/// asserting `RanOk` -- a crash surfaces as a distinct `AnalyzeStatus` via
/// `run_analyze_child`'s real process-exit inspection, never silently.
fn assert_ran_ok(graph_path: &std::path::Path, so_path: &std::path::Path, context: &str) {
    let mut command = Command::new(env!("CARGO_BIN_EXE_xray-cli"));
    command.arg("--analyze-graph").arg("--graph-in").arg(graph_path).arg("--dylib").arg(so_path);
    let (status, result) = run_analyze_child::<GraphResult>(command, E2E_TIMEOUT);
    assert_eq!(
        status,
        AnalyzeStatus::RanOk,
        "{context}: analyze-graph must report RanOk (a crash surfaces as a distinct status, never silently), got {status:?}"
    );
    assert!(result.is_some(), "{context}: RanOk must carry a real result");
}

/// Thin orchestrator shared by both Bug #1816 call orders below. Skips
/// (does not assert) when `environment_has_toolchain_drift` finds no real
/// mismatch to exercise in the current environment.
fn run_bug_1816_order(sig_first: bool, context: &str) {
    let source_dir = tempfile::tempdir().expect("create source temp dir");
    let outside_dir = outside_workspace_tempdir();

    if !environment_has_toolchain_drift(outside_dir.path()) {
        eprintln!(
            "{context}: SKIPPING -- this environment's ambient rustc matches the pinned \
             toolchain, so there is no drift to discriminate (the real Bug #1816 needed the \
             same precondition to manifest)"
        );
        return;
    }

    let graph_path = write_small_graph_with_signature(source_dir.path());

    let (first, second) = if sig_first {
        ("let _ = g.signature_for(0).is_some();", "let _ = g.shortest_path_to_any(0u32, &t, MAX_PATH_DEPTH);")
    } else {
        ("let _ = g.shortest_path_to_any(0u32, &t, MAX_PATH_DEPTH);", "let _ = g.signature_for(0).is_some();")
    };
    let user_code = format!(
        r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{ Vec::new() }}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{
    const MAX_PATH_DEPTH: usize = {MAX_PATH_DEPTH};
    let t: Vec<u32> = Vec::new();
    {first}
    {second}
    GraphResult::default()
}}
"#
    );

    let so_path = compile_outside_workspace(&user_code, source_dir.path(), outside_dir.path(), context);
    assert_ran_ok(&graph_path, &so_path, context);
}

/// Bug #1816, order A: `signature_for` before `shortest_path_to_any`,
/// compiled from OUTSIDE the workspace -- the exact production condition.
#[test]
fn bug_1816_signature_then_shortest_path_survives_cross_toolchain_compile_outside_workspace() {
    run_bug_1816_order(true, "order A (signature_for then shortest_path_to_any)");
}

/// Bug #1816, order B: the reverse call order -- the bug report notes
/// order only changes which signal is raised (SIGSEGV vs SIGABRT), not
/// whether corruption happens, so both orders are required.
#[test]
fn bug_1816_shortest_path_then_signature_survives_cross_toolchain_compile_outside_workspace() {
    run_bug_1816_order(false, "order B (shortest_path_to_any then signature_for)");
}
