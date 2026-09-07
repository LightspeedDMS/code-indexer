//! Story #1792 (S3): end-to-end proof that `run_analyze_child` (the SAME
//! killable-process container AC7 built for `--analyze-graph`) drives the
//! REAL `xray-cli --refine` binary against a REAL compiled graph-mode
//! evaluator, a REAL graph file, and REAL source files on disk -- AC1's
//! "runs in its own map-shaped xray-cli invocation" and AC5's "carries the
//! same cgroup ceiling and cancellation registration as the other
//! children" claims, proven through the literal process boundary rather
//! than by inspection.
//!
//! `RefineBatchResultMirror`/`RefineFileOutcomeMirror`/`FindingMirror`
//! below are test-local structs mirroring `xray-cli/src/main.rs`'s
//! private `RefineBatchResult`/`RefineChildFileOutcome`/`JsonFinding` wire
//! shapes -- this test crate cannot import xray-cli's binary-private
//! types, so it deserializes the REAL JSON the subprocess writes into an
//! independently-declared but field-identical shape, exactly the way an
//! external consumer (e.g. the Python orchestration layer) would.

use std::process::Command;
use std::time::Duration;
use xray_core::graph::analyze::process::run_analyze_child;
use xray_core::graph::analyze::result::AnalyzeStatus;

const E2E_TIMEOUT: Duration = Duration::from_secs(30);

/// How long the timeout test's compiled evaluator sleeps for -- deliberately
/// far longer than `CHILD_TIMEOUT_FOR_HANG_TEST`, so the child is always
/// still sleeping (never finished) when the parent's timeout fires.
const HANGING_EVALUATOR_SLEEP_SECS: u64 = 10;
/// The `run_analyze_child` timeout for the hang test -- far shorter than
/// `HANGING_EVALUATOR_SLEEP_SECS`, so the kill fires well before the
/// child's own sleep would ever return naturally.
const CHILD_TIMEOUT_FOR_HANG_TEST: Duration = Duration::from_millis(500);
/// Upper bound on how long `run_analyze_child` itself may take to notice
/// the timeout, kill the child, and return -- must be well under
/// `HANGING_EVALUATOR_SLEEP_SECS`, proving the kill actually happened
/// rather than the test merely waiting out the sleep.
const MAX_ACCEPTABLE_KILL_LATENCY: Duration = Duration::from_secs(5);

// `file`/`line` exist only to match the real JSON shape byte-for-byte
// (correct deserialization depends on the field set matching); they are
// not independently asserted on below, unlike `pattern`/`snippet`.
#[derive(Debug, serde::Deserialize)]
struct FindingMirror {
    pattern: String,
    #[allow(dead_code)]
    file: String,
    #[allow(dead_code)]
    line: usize,
    snippet: String,
}

#[derive(Debug, serde::Deserialize)]
struct RefineFileOutcomeMirror {
    file: String,
    findings: Vec<FindingMirror>,
    status: String,
}

#[derive(Debug, serde::Deserialize)]
struct RefineBatchResultMirror {
    files: Vec<RefineFileOutcomeMirror>,
}

/// Writes two real `.java` files under `dir`, a real (empty) graph file,
/// and a real `--files-from` list naming both -- returns
/// `(graph_path, files_from_path)`.
fn write_repo_fixture(dir: &std::path::Path) -> (std::path::PathBuf, std::path::PathBuf) {
    use xray_core::graph::csr::builder::CodeGraphBuilder;
    use xray_core::graph::csr::wire::write_graph_file;

    std::fs::write(dir.join("A.java"), "class A {}").unwrap();
    std::fs::write(dir.join("B.java"), "class B {}").unwrap();

    let graph = CodeGraphBuilder::with_candidate_capacity(0).build();
    let graph_path = dir.join("graph.bin");
    write_graph_file(&graph, &graph_path).expect("write_graph_file must succeed");

    let files_from_path = dir.join("files.txt");
    std::fs::write(&files_from_path, "A.java\nB.java\n").unwrap();

    (graph_path, files_from_path)
}

/// Builds the real `xray-cli --refine ...` `Command` shared by all three
/// tests below -- deduplicates the repeated argument wiring.
fn build_refine_command(
    graph_path: &std::path::Path,
    dylib_path: &std::path::Path,
    repo_root: &std::path::Path,
    files_from_path: &std::path::Path,
) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_xray-cli"));
    command
        .arg("--refine")
        .arg("--graph-in")
        .arg(graph_path)
        .arg("--dylib")
        .arg(dylib_path)
        .arg("--repo-root")
        .arg(repo_root)
        .arg("--files-from")
        .arg(files_from_path);
    command
}

/// THE end-to-end proof: the REAL `xray-cli` binary, invoked via `--refine`
/// through the REAL `run_analyze_child` process container, must run a REAL
/// compiled `refine` callback once per file and report `RanOk` with the
/// correct per-file findings.
#[test]
fn real_xray_cli_binary_runs_a_real_refine_evaluator_via_run_analyze_child() {
    let dir = tempfile::tempdir().expect("create temp dir");
    let (graph_path, files_from_path) = write_repo_fixture(dir.path());

    let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {
    vec![EvalFinding { pattern: "refine-e2e".to_string(), line: node.start_line, snippet: ctx.file.clone() }]
}
"#;
    let cr = xray_core::compiler::compile_evaluator(user_code, dir.path()).expect("must compile");
    let command = build_refine_command(&graph_path, &cr.so_path, dir.path(), &files_from_path);

    let (status, result) = run_analyze_child::<RefineBatchResultMirror>(command, E2E_TIMEOUT);

    assert_eq!(status, AnalyzeStatus::RanOk, "the real xray-cli --refine binary must report RanOk");
    let result = result.expect("RanOk must carry a real RefineBatchResult");
    assert_eq!(result.files.len(), 2, "must have one outcome per file in --files-from");
    for (outcome, expected_file) in result.files.iter().zip(["A.java", "B.java"]) {
        assert_eq!(outcome.status, "ran");
        assert_eq!(outcome.file, expected_file);
        assert_eq!(outcome.findings.len(), 1);
        assert_eq!(outcome.findings[0].pattern, "refine-e2e");
        assert_eq!(
            outcome.findings[0].snippet, expected_file,
            "the real FileContext must have reached the compiled refine callback"
        );
    }
}

/// THE central `--refine` invariant, proven at the full process boundary:
/// a graph-mode dylib with NO `fn refine` handed to `--refine` must report
/// `Absent` -- distinct from a successful empty run.
#[test]
fn real_xray_cli_binary_reports_absent_for_a_dylib_with_no_refine_export() {
    let dir = tempfile::tempdir().expect("create temp dir");
    let (graph_path, files_from_path) = write_repo_fixture(dir.path());

    let no_refine_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
"#;
    let cr = xray_core::compiler::compile_evaluator(no_refine_code, dir.path()).expect("must compile");
    let command = build_refine_command(&graph_path, &cr.so_path, dir.path(), &files_from_path);

    let (status, result) = run_analyze_child::<RefineBatchResultMirror>(command, E2E_TIMEOUT);

    assert_eq!(
        status,
        AnalyzeStatus::Absent,
        "a dylib with no fn refine handed to --refine must report Absent, never RanOk or a crash"
    );
    assert!(result.is_none());
}

/// THE AC5 discriminating proof: a `--refine` child running an evaluator
/// that calls `std::thread::sleep` for a PROVABLY-TERMINATING (Rule 14),
/// but deliberately long, duration -- far longer than this test's own
/// timeout -- must be KILLED mid-sleep by the SAME `run_analyze_child`
/// timeout mechanism `--analyze-graph` already relies on, and reported as
/// the distinct `TimedOut` status. This is the literal reuse claim AC5
/// makes: the refine child "carries the same cgroup ceiling and
/// cancellation registration as the other children."
#[test]
fn a_hanging_refine_child_is_killed_at_timeout_via_the_same_container_analyze_graph_uses() {
    let dir = tempfile::tempdir().expect("create temp dir");
    let (graph_path, files_from_path) = write_repo_fixture(dir.path());

    let sleeping_code = format!(
        r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{ Vec::new() }}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{ GraphResult::default() }}
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {{
    std::thread::sleep(std::time::Duration::from_secs({sleep_secs}));
    Vec::new()
}}
"#,
        sleep_secs = HANGING_EVALUATOR_SLEEP_SECS,
    );
    let cr = xray_core::compiler::compile_evaluator(&sleeping_code, dir.path()).expect("must compile");
    let command = build_refine_command(&graph_path, &cr.so_path, dir.path(), &files_from_path);

    let start = std::time::Instant::now();
    let (status, result) = run_analyze_child::<RefineBatchResultMirror>(command, CHILD_TIMEOUT_FOR_HANG_TEST);
    let elapsed = start.elapsed();

    assert_eq!(status, AnalyzeStatus::TimedOut, "a refine child still sleeping past its timeout must be reported as TimedOut");
    assert!(result.is_none());
    assert!(
        elapsed < MAX_ACCEPTABLE_KILL_LATENCY,
        "run_analyze_child must return promptly after killing the hung child, took {elapsed:?}"
    );
}
