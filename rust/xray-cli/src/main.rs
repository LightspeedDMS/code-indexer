use std::path::PathBuf;
use std::time::Instant;
use xray_core::evaluators::{AllocationInTryEvaluator, CatchRethrowEvaluator};
use xray_core::scanner::{self, Evaluator};

#[derive(serde::Serialize)]
struct JsonOutput {
    findings: Vec<JsonFinding>,
    files_parsed: usize,
    files_errored: usize,
    parse_scan_ms: u128,
    compile_ms: u128,
    cached: bool,
    error: Option<String>,
    /// Debug messages emitted by debug_log() calls in the evaluator.
    /// Empty list when no debug_log() calls were made (zero overhead).
    debug_messages: Vec<String>,
}

#[derive(serde::Serialize)]
struct JsonFinding {
    pattern: String,
    file: String,
    line: usize,
    snippet: String,
}

struct ParsedArgs {
    dynlib_path: Option<String>,
    json_output: bool,
    file_list: Vec<String>,
    /// Bug #1612: path to a newline-delimited file containing the candidate
    /// file list, populated by --files-from parsing (already implemented
    /// below in parse_args). Lets callers hand xray-cli a large candidate
    /// set without ever putting it on argv (which overflows ARG_MAX at
    /// fleet scale).
    files_from_path: Option<String>,
    remaining_args: Vec<String>,
}

/// Evaluators built, the compile time in ms, and whether the .so was served
/// from cache, or an `Err(error_message)` on compilation/load failure.
type EvaluatorsResult = Result<(Vec<Box<dyn Evaluator>>, u128, bool), String>;

fn default_target() -> String {
    let home = std::env::var("HOME").unwrap_or_else(|_| std::env::current_dir()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| ".".to_string()));
    format!("{}/Dev/evolution", home)
}

/// Read a newline-delimited candidate file list from disk (Bug #1612).
///
/// Lets callers hand xray-cli a large candidate set via a file instead of
/// argv, avoiding the ARG_MAX / E2BIG ceiling that argv-based `--files`
/// hits at fleet scale. Lines are trimmed; blank lines are skipped.
///
/// The path must be absolute -- the only real caller (RustNativeBackend on
/// the Python side) always passes an absolute tempfile path it created
/// itself; requiring absolute rejects malformed/relative input early with a
/// clear error instead of silently resolving against an unpredictable cwd.
fn read_file_list(path: &str) -> Result<Vec<PathBuf>, String> {
    let path_buf = PathBuf::from(path);
    if !path_buf.is_absolute() {
        return Err(format!("--files-from path must be absolute, got: {}", path));
    }
    let content = std::fs::read_to_string(&path_buf)
        .map_err(|e| format!("Failed to read --files-from list at {}: {}", path, e))?;
    Ok(content
        .lines()
        .map(|line| line.trim())
        .filter(|line| !line.is_empty())
        .map(PathBuf::from)
        .collect())
}

/// Serialize `out` to a JSON line on stdout, or a plain-text error to stderr
/// if serialization itself fails (never panics via `.unwrap()`).
fn print_json_output(out: &JsonOutput) {
    match serde_json::to_string(out) {
        Ok(json) => println!("{}", json),
        Err(e) => eprintln!("Error: failed to serialize JSON output: {}", e),
    }
}

/// Bug #1784: format the composite cache identity (plus its component
/// fields) for `user_code` as 4 `key=value` lines. This is the ONE
/// implementation of the identity formula (xray_core::compiler) exposed to
/// Python via the `--print-cache-identity` subcommand below, so Python's
/// RustNativeBackend never independently re-implements the hash and cannot
/// drift from what compile_evaluator() actually uses as its cache key.
fn format_cache_identity_output(user_code: &str) -> String {
    let info = xray_core::compiler::cache_identity_info(user_code);
    format!(
        "identity={}\nsource_hash={}\nabi_version={}\nrustc_version={}\n",
        info.identity, info.source_hash, info.abi_version, info.rustc_version
    )
}

/// Parsed `--graph-in <path>`/`--dylib <path>` arguments for the
/// `--analyze-graph` subcommand.
struct AnalyzeGraphArgs {
    graph_in: PathBuf,
    dylib: PathBuf,
}

/// Parses the `--analyze-graph` subcommand's own two REQUIRED flags,
/// `--graph-in <path>` and `--dylib <path>`. Order-independent; errors
/// with a clear message naming which flag is missing, never silently
/// defaulting either.
fn parse_analyze_graph_args(args: &[String]) -> Result<AnalyzeGraphArgs, String> {
    let mut graph_in: Option<PathBuf> = None;
    let mut dylib: Option<PathBuf> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--graph-in" => {
                let (value, next_i) = parse_value_flag(args, i, "--graph-in requires a path");
                graph_in = Some(PathBuf::from(value));
                i = next_i;
            }
            "--dylib" => {
                let (value, next_i) = parse_value_flag(args, i, "--dylib requires a path");
                dylib = Some(PathBuf::from(value));
                i = next_i;
            }
            other => return Err(format!("--analyze-graph: unrecognized argument '{other}'")),
        }
    }
    Ok(AnalyzeGraphArgs {
        graph_in: graph_in.ok_or_else(|| "--analyze-graph requires --graph-in <path>".to_string())?,
        dylib: dylib.ok_or_else(|| "--analyze-graph requires --dylib <path>".to_string())?,
    })
}

/// Story #1787 AC7+AC8: the core of the `--analyze-graph` subcommand,
/// factored out from argv/exit-code plumbing so it is directly unit
/// testable. Reads `graph_in` (the AC7 mmap wire format), loads `dylib` as
/// a graph-mode evaluator, and maps the outcome onto a `ChildReport` --
/// every terminal status is DISTINCT and explicit (Rule 13,
/// anti-silent-failure), never inferred from an empty result:
///
/// - graph file fails to read/parse -> `GraphInvalid`
/// - dylib fails to load (ABI mismatch, missing `xray_abi_version`, etc.)
///   -> `LoadFailed`
/// - dylib loads but does not export `analyze_graph` (a legacy-mode `.so`
///   handed to this subcommand) -> `Absent`
/// - `analyze_graph` panicked (caught INSIDE the dylib, see
///   `compiler::GRAPH_EPILOGUE`) -> `Panicked`
/// - `analyze_graph` returned a real result -> `RanOk`
///
/// Facts are an EMPTY `FactIndex` for this slice -- wiring real per-file
/// `collect_facts` output into a `FactIndex` here is fused-pipeline
/// integration, explicitly out of scope for the ABI/process-container work
/// this subcommand demonstrates.
fn run_analyze_graph(graph_in: &std::path::Path, dylib: &std::path::Path) -> xray_core::graph::analyze::process::ChildReport {
    use xray_core::graph::analyze::process::ChildReport;
    use xray_core::graph::analyze::result::AnalyzeStatus;

    let graph = match xray_core::graph::csr::wire::read_graph_file(graph_in) {
        Ok(g) => g,
        Err(_) => return ChildReport { status: AnalyzeStatus::GraphInvalid, result: None },
    };
    let evaluator = match xray_core::dynlib::GraphDynlibEvaluator::load(dylib) {
        Ok(e) => e,
        Err(_) => return ChildReport { status: AnalyzeStatus::LoadFailed, result: None },
    };

    let facts = xray_core::graph::user_facts::FactIndex::new();
    let graph_handle = xray_core::graph::csr::handle::GraphHandle::from_graph(&graph);
    let facts_handle = xray_core::graph::user_facts::FactsHandle::from_facts(&facts);

    match evaluator.call_analyze_graph(&graph_handle, &facts_handle) {
        None => ChildReport { status: AnalyzeStatus::Absent, result: None },
        Some(None) => ChildReport { status: AnalyzeStatus::Panicked, result: None },
        Some(Some(result)) => ChildReport { status: AnalyzeStatus::RanOk, result: Some(result) },
    }
}

fn main() {
    let wall_start = Instant::now();
    let args: Vec<String> = std::env::args().skip(1).collect();

    // Bug #1784: early-exit subcommand -- reads evaluator source from stdin,
    // prints its cache identity, and exits. No compilation, no file I/O
    // beyond stdin/stdout, near-instant.
    if args.first().map(|s| s.as_str()) == Some("--print-cache-identity") {
        use std::io::Read as _;
        let mut user_code = String::new();
        if let Err(e) = std::io::stdin().read_to_string(&mut user_code) {
            eprintln!("Error: failed to read evaluator source from stdin: {}", e);
            std::process::exit(1);
        }
        print!("{}", format_cache_identity_output(&user_code));
        std::process::exit(0);
    }

    // Story #1787 AC7+AC8: `--analyze-graph --graph-in <path> --dylib
    // <path>` -- the child-process side of `run_analyze_child`'s handoff.
    // ALWAYS exits 0 (a legitimate terminal AnalyzeStatus, including
    // GraphInvalid/LoadFailed/Absent/Panicked, is reported via the JSON
    // ChildReport on stdout, never via a nonzero exit code) -- exiting 1
    // here is reserved for a malformed invocation (missing required
    // flags), which the parent's run_analyze_child already maps to
    // Panicked via its own nonzero-exit fallback.
    if args.first().map(|s| s.as_str()) == Some("--analyze-graph") {
        let parsed = match parse_analyze_graph_args(&args[1..]) {
            Ok(p) => p,
            Err(msg) => {
                eprintln!("Error: {}", msg);
                std::process::exit(1);
            }
        };
        let report = run_analyze_graph(&parsed.graph_in, &parsed.dylib);
        match serde_json::to_string(&report) {
            Ok(json) => println!("{}", json),
            Err(e) => eprintln!("Error: failed to serialize ChildReport: {}", e),
        }
        std::process::exit(0);
    }

    let parsed = parse_args(&args);
    let json_output = parsed.json_output;

    // Determine target directory (only used when neither --files nor
    // --files-from is provided)
    let target = std::env::var("XRAY_TARGET")
        .or_else(|_| parsed.remaining_args.first().cloned().ok_or(()))
        .unwrap_or_else(|_| default_target());

    // Collect files: --files-from (Bug #1612 -- avoids argv overflow) takes
    // precedence, then --files, then a full directory walk of `target`.
    let files: Vec<PathBuf> = if let Some(ref files_from_path) = parsed.files_from_path {
        match read_file_list(files_from_path) {
            Ok(list) => list,
            Err(msg) => {
                if json_output {
                    print_json_output(&JsonOutput {
                        findings: vec![],
                        files_parsed: 0,
                        files_errored: 0,
                        parse_scan_ms: 0,
                        compile_ms: 0,
                        cached: false,
                        error: Some(msg),
                        debug_messages: vec![],
                    });
                } else {
                    eprintln!("Error: {}", msg);
                }
                std::process::exit(1);
            }
        }
    } else if !parsed.file_list.is_empty() {
        parsed.file_list.iter().map(PathBuf::from).collect()
    } else {
        let target_path = PathBuf::from(&target);
        if !json_output {
            println!("=== Rust XRay Scanner ===");
            println!("Target: {}", target);
        }
        let collect_start = Instant::now();
        let collected = scanner::collect_files(&target_path);
        let collect_ms = collect_start.elapsed().as_millis();
        if !json_output {
            println!("Files found: {} (collection time: {}ms)", collected.len(), collect_ms);
        }
        collected
    };

    // Build evaluators — may fail compilation
    let evaluators_result: EvaluatorsResult =
        if let Some(ref eval_path) = parsed.dynlib_path {
            build_dynlib_evaluators(eval_path, json_output)
        } else {
            if !json_output {
                println!("Mode: built-in evaluators");
            }
            Ok((
                vec![
                    Box::new(AllocationInTryEvaluator),
                    Box::new(CatchRethrowEvaluator),
                ],
                0,
                false,
            ))
        };

    match evaluators_result {
        Err(err_msg) => {
            if json_output {
                let out = JsonOutput {
                    findings: vec![],
                    files_parsed: 0,
                    files_errored: 0,
                    parse_scan_ms: 0,
                    compile_ms: 0,
                    cached: false,
                    error: Some(err_msg),
                    debug_messages: vec![],
                };
                print_json_output(&out);
            }
            // Human-readable error already printed inside build_dynlib_evaluators
            std::process::exit(1);
        }
        Ok((evaluators, compile_ms, cached)) => {
            let result = scanner::scan_files_parallel(&files, &evaluators);
            let wall_ms = wall_start.elapsed().as_millis();

            if json_output {
                let json_findings: Vec<JsonFinding> = result
                    .findings
                    .iter()
                    .map(|f| JsonFinding {
                        pattern: f.pattern.clone(),
                        file: f.file.clone(),
                        line: f.line,
                        snippet: f.snippet.clone(),
                    })
                    .collect();
                let out = JsonOutput {
                    findings: json_findings,
                    files_parsed: result.files_parsed,
                    files_errored: result.files_errored,
                    parse_scan_ms: result.parse_scan_ms,
                    compile_ms,
                    cached,
                    error: None,
                    debug_messages: result.debug_messages,
                };
                print_json_output(&out);
            } else {
                let alloc_count = result
                    .findings
                    .iter()
                    .filter(|f| f.pattern == "allocation-in-try")
                    .count();
                let rethrow_count = result
                    .findings
                    .iter()
                    .filter(|f| f.pattern == "catch-rethrow")
                    .count();

                println!(
                    "Files parsed: {} (errors: {}, parse+scan time: {}ms)",
                    result.files_parsed, result.files_errored, result.parse_scan_ms
                );
                println!(
                    "Findings: {} (allocation-in-try: {}, catch-rethrow: {})",
                    result.findings.len(),
                    alloc_count,
                    rethrow_count
                );
                println!("Total wall time: {}ms", wall_ms);

                let mut sorted = result.findings.clone();
                sorted.sort_by(|a, b| a.file.cmp(&b.file).then(a.line.cmp(&b.line)));

                println!("\nSample findings (first 10):");
                let prefix = format!("{}/", target.trim_end_matches('/'));
                for f in sorted.iter().take(10) {
                    let rel = f.file.strip_prefix(&prefix).unwrap_or(&f.file);
                    println!("  [{}] {}:{} -- {}", f.pattern, rel, f.line, f.snippet);
                }
            }
        }
    }
}

/// Parses a flag that requires a following value (e.g. "--dynlib PATH").
/// Exits the process with `error_msg` printed to stderr if no value follows.
fn parse_value_flag(args: &[String], i: usize, error_msg: &str) -> (String, usize) {
    if i + 1 < args.len() {
        (args[i + 1].clone(), i + 2)
    } else {
        eprintln!("Error: {}", error_msg);
        std::process::exit(1);
    }
}

/// Consumes args starting at index `i` (pointing at the "--files" flag
/// itself, Bug #1612's backward-compat path) as file paths, stopping at the
/// first KNOWN flag. This allows filenames that start with "--" (e.g.
/// "--weird.rs"). Returns the collected file paths and the index of the
/// first arg that was not consumed.
fn consume_files_flag(args: &[String], i: usize) -> (Vec<String>, usize) {
    let mut i = i + 1; // skip "--files" itself
    let mut file_list = Vec::new();
    while i < args.len() {
        if args[i] == "--json" || args[i] == "--dynlib" || args[i] == "--files-from" {
            break;
        }
        file_list.push(args[i].clone());
        i += 1;
    }
    (file_list, i)
}

fn parse_args(args: &[String]) -> ParsedArgs {
    let mut dynlib_path = None;
    let mut json_output = false;
    let mut file_list = Vec::new();
    let mut files_from_path = None;
    let mut remaining = Vec::new();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--dynlib" => {
                let msg = "--dynlib requires a path to an evaluator .rs file";
                let (path, next_i) = parse_value_flag(args, i, msg);
                dynlib_path = Some(path);
                i = next_i;
            }
            "--files-from" => {
                let msg = "--files-from requires a path to a newline-delimited file list";
                let (path, next_i) = parse_value_flag(args, i, msg);
                files_from_path = Some(path);
                i = next_i;
            }
            "--json" => {
                json_output = true;
                i += 1;
            }
            "--files" => {
                let (files, next_i) = consume_files_flag(args, i);
                file_list.extend(files);
                i = next_i;
            }
            _ => {
                remaining.push(args[i].clone());
                i += 1;
            }
        }
    }
    ParsedArgs {
        dynlib_path,
        json_output,
        file_list,
        files_from_path,
        remaining_args: remaining,
    }
}

/// Validates the evaluator source file exists and reads its contents.
fn read_evaluator_source(eval_path: &str, json_output: bool) -> Result<String, String> {
    let path = PathBuf::from(eval_path);
    if !path.exists() {
        let msg = format!("Evaluator file not found: {}", eval_path);
        if !json_output {
            eprintln!("Error: {}", msg);
        }
        return Err(msg);
    }

    match std::fs::read_to_string(&path) {
        Ok(c) => Ok(c),
        Err(e) => {
            let msg = format!("Failed to read {}: {}", eval_path, e);
            if !json_output {
                eprintln!("Error: {}", msg);
            }
            Err(msg)
        }
    }
}

/// Compiles `user_code` and loads the resulting dynamic library, printing
/// progress/timing unless `json_output`. Mirrors the original inline logic
/// of `build_dynlib_evaluators` before it was split for readability.
fn compile_and_load_evaluator(
    user_code: &str,
    eval_path: &str,
    json_output: bool,
) -> EvaluatorsResult {
    let cache_dir = xray_core::cache::get_cache_dir();
    if !json_output {
        println!("Mode: dynamic library (evaluator: {})", eval_path);
        println!("Cache dir: {}", cache_dir.display());
    }
    let compile_start = Instant::now();
    let cr = match xray_core::compiler::compile_evaluator(user_code, &cache_dir) {
        Ok(cr) => cr,
        Err(e) => {
            let msg = format!("{}", e);
            if !json_output {
                eprintln!("\n=== Evaluator Error ===\n{}", msg);
            }
            return Err(msg);
        }
    };
    let compile_total_ms = compile_start.elapsed().as_millis();
    if !json_output {
        if cr.cached {
            println!("Compilation: cache HIT ({}ms lookup)", compile_total_ms);
        } else {
            println!("Compilation: {}ms (fresh compile)", cr.compile_ms);
        }
    }
    let evaluator = xray_core::dynlib::DynlibEvaluator::load(&cr.so_path).map_err(|e| {
        let msg = format!("Failed to load compiled evaluator: {}", e);
        if !json_output {
            eprintln!("Error: {}", msg);
        }
        msg
    })?;
    Ok((vec![Box::new(evaluator)], cr.compile_ms, cr.cached))
}

/// Build evaluators from a dynamic library evaluator source file.
///
/// Returns `Ok((evaluators, compile_ms, cached))` on success.
/// Returns `Err(error_message)` on compilation failure (human-readable message
/// already printed to stderr for non-JSON callers).
fn build_dynlib_evaluators(eval_path: &str, json_output: bool) -> EvaluatorsResult {
    let user_code = read_evaluator_source(eval_path, json_output)?;
    compile_and_load_evaluator(&user_code, eval_path, json_output)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sv(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    /// RED phase: `parse_analyze_graph_args` does not exist yet.
    #[test]
    fn parse_analyze_graph_args_extracts_graph_in_and_dylib() {
        let args = sv(&["--graph-in", "/tmp/g.bin", "--dylib", "/tmp/e.so"]);
        let parsed = parse_analyze_graph_args(&args).expect("both required flags present must parse");
        assert_eq!(parsed.graph_in, std::path::PathBuf::from("/tmp/g.bin"));
        assert_eq!(parsed.dylib, std::path::PathBuf::from("/tmp/e.so"));
    }

    #[test]
    fn parse_analyze_graph_args_errors_when_dylib_missing() {
        let args = sv(&["--graph-in", "/tmp/g.bin"]);
        assert!(parse_analyze_graph_args(&args).is_err(), "--dylib is required");
    }

    /// Builds a tiny real `CodeGraph` (A -> B), writes it to a real file
    /// via `write_graph_file`, and returns the path -- the AC7 wire
    /// format `run_analyze_graph` reads via `read_graph_file`.
    fn write_small_graph_file(dir: &std::path::Path) -> (std::path::PathBuf, u64) {
        use xray_core::graph::csr::builder::CodeGraphBuilder;
        use xray_core::graph::csr::candidate::Candidate;
        use xray_core::graph::csr::wire::write_graph_file;
        use xray_core::graph::identity::make_symbol_id;
        use xray_core::graph::reasons;

        let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
        let a = builder.intern_symbol(make_symbol_id(1, 0));
        let b_symbol = make_symbol_id(1, 1);
        let b = builder.intern_symbol(b_symbol);
        builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
        let graph = builder.build();

        let path = dir.join("graph.bin");
        write_graph_file(&graph, &path).expect("write_graph_file must succeed");
        (path, b_symbol)
    }

    /// RED phase: `run_analyze_graph` does not exist yet. Proves the
    /// AC7+AC8 end-to-end path: a REAL graph file (mmap-readable via the
    /// AC7 wire format) plus a REAL compiled graph-mode evaluator dylib
    /// (using the real `GraphHandle` accessor ABI) produces a
    /// `ChildReport { status: RanOk, result: Some(..) }` with the CORRECT
    /// data -- never a stub.
    #[test]
    fn run_analyze_graph_produces_ran_ok_with_a_real_graph_and_dylib() {
        use tempfile::TempDir;
        use xray_core::graph::analyze::result::AnalyzeStatus;

        let dir = TempDir::new().unwrap();
        let (graph_path, b_symbol) = write_small_graph_file(dir.path());

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
        let cr = xray_core::compiler::compile_evaluator(user_code, dir.path()).expect("must compile");

        let report = run_analyze_graph(&graph_path, &cr.so_path);
        assert_eq!(report.status, AnalyzeStatus::RanOk);
        let result = report.result.expect("RanOk must carry a result");
        assert_eq!(result.refine, vec![b_symbol], "must resolve to B's real SymbolId via a REAL accessor call");
    }

    /// THE central AC7/AC8 invariant: an evaluator that does NOT export
    /// `analyze_graph` is reported as `Absent` -- DISTINCT from a
    /// successful empty analysis (`RanOk` with `refine: vec![]`) -- and
    /// DISTINCT again from `GraphInvalid`/`LoadFailed` (four statuses
    /// total; `Panicked` is proven separately at the `GraphDynlibEvaluator`
    /// layer in `xray-core`, and `NotRequested`/`SkippedBudget`/`TimedOut`
    /// are decided by callers upstream of this function, not produced by
    /// it). A wrong implementation that collapsed "not exported" into
    /// "ran, found nothing" would pass a naive "no findings" check but
    /// fail this test's explicit status comparison.
    #[test]
    fn run_analyze_graph_distinguishes_absent_empty_success_and_load_failures() {
        use tempfile::TempDir;
        use xray_core::graph::analyze::result::AnalyzeStatus;

        let dir = TempDir::new().unwrap();
        let (graph_path, _b_symbol) = write_small_graph_file(dir.path());

        // Absent: a legacy-mode .so has no analyze_graph to call at all.
        let legacy_code = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }";
        let legacy_cr = xray_core::compiler::compile_evaluator(legacy_code, dir.path()).expect("must compile");
        let absent_report = run_analyze_graph(&graph_path, &legacy_cr.so_path);
        assert_eq!(absent_report.status, AnalyzeStatus::Absent);
        assert!(absent_report.result.is_none());

        // RanOk with an empty result: a REAL graph-mode evaluator that
        // legitimately finds nothing -- must NOT be confused with Absent.
        let empty_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
"#;
        let empty_cr = xray_core::compiler::compile_evaluator(empty_code, dir.path()).expect("must compile");
        let empty_report = run_analyze_graph(&graph_path, &empty_cr.so_path);
        assert_eq!(empty_report.status, AnalyzeStatus::RanOk);
        assert_eq!(empty_report.result, Some(xray_core::graph::analyze::result::GraphResult::default()));

        // GraphInvalid: the graph file itself is corrupt/unreadable.
        let corrupt_graph_path = dir.path().join("corrupt.bin");
        std::fs::write(&corrupt_graph_path, b"not a real graph file").unwrap();
        let invalid_report = run_analyze_graph(&corrupt_graph_path, &empty_cr.so_path);
        assert_eq!(invalid_report.status, AnalyzeStatus::GraphInvalid);
        assert!(invalid_report.result.is_none());

        // LoadFailed: the dylib path does not exist at all.
        let missing_dylib_path = dir.path().join("does_not_exist.so");
        let load_failed_report = run_analyze_graph(&graph_path, &missing_dylib_path);
        assert_eq!(load_failed_report.status, AnalyzeStatus::LoadFailed);
        assert!(load_failed_report.result.is_none());

        // All four observed statuses must be pairwise distinct (AnalyzeStatus
        // does not derive Hash, so this is a plain pairwise comparison
        // rather than a HashSet-based dedup).
        let statuses = [
            AnalyzeStatus::Absent,
            AnalyzeStatus::RanOk,
            AnalyzeStatus::GraphInvalid,
            AnalyzeStatus::LoadFailed,
        ];
        for i in 0..statuses.len() {
            for j in (i + 1)..statuses.len() {
                assert_ne!(statuses[i], statuses[j], "status at index {i} must differ from index {j}");
            }
        }
    }

    // --- Bug #1784: --print-cache-identity bridges Python to the ONE
    // shared Rust identity implementation (cache_identity_info) ---

    #[test]
    fn test_format_cache_identity_output_contains_all_four_fields() {
        let user_code = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }";
        let output = format_cache_identity_output(user_code);
        assert!(output.contains("identity="), "output must contain identity=: {}", output);
        assert!(output.contains("source_hash="), "output must contain source_hash=: {}", output);
        assert!(output.contains("abi_version="), "output must contain abi_version=: {}", output);
        assert!(output.contains("rustc_version="), "output must contain rustc_version=: {}", output);
    }

    // --- AC2/AC3: debug_messages field in JSON output ---

    #[test]
    fn test_json_output_has_debug_messages_field() {
        // AC2: JsonOutput must include debug_messages field in serialized JSON.
        let out = JsonOutput {
            findings: vec![],
            files_parsed: 0,
            files_errored: 0,
            parse_scan_ms: 0,
            compile_ms: 0,
            cached: false,
            error: None,
            debug_messages: vec!["hello".to_string(), "world".to_string()],
        };
        let json = serde_json::to_string(&out).expect("must serialize");
        assert!(
            json.contains("debug_messages"),
            "serialized JSON must contain debug_messages key: {}",
            json
        );
        assert!(
            json.contains("hello"),
            "serialized JSON must contain debug message content: {}",
            json
        );
    }

    #[test]
    fn test_json_output_debug_messages_empty_by_default() {
        // AC6: debug_messages must serialize as empty array (not absent) when empty.
        let out = JsonOutput {
            findings: vec![],
            files_parsed: 0,
            files_errored: 0,
            parse_scan_ms: 0,
            compile_ms: 0,
            cached: false,
            error: None,
            debug_messages: vec![],
        };
        let json = serde_json::to_string(&out).expect("must serialize");
        assert!(
            json.contains("\"debug_messages\":[]"),
            "empty debug_messages must serialize as empty array: {}",
            json
        );
    }

    #[test]
    fn test_parse_args_files_stops_at_json_flag() {
        let args = sv(&["--files", "a.rs", "b.rs", "--json"]);
        let parsed = parse_args(&args);
        assert_eq!(parsed.file_list, sv(&["a.rs", "b.rs"]));
        assert!(parsed.json_output, "--json must be recognized after --files list");
    }

    #[test]
    fn test_parse_args_files_stops_at_dynlib_flag() {
        let args = sv(&["--files", "a.rs", "--dynlib", "eval.rs"]);
        let parsed = parse_args(&args);
        assert_eq!(parsed.file_list, sv(&["a.rs"]));
        assert_eq!(parsed.dynlib_path, Some("eval.rs".to_string()));
    }

    #[test]
    fn test_parse_args_files_with_double_dash_filename() {
        // A filename starting with "--" that is NOT a known flag must be accepted
        let args = sv(&["--files", "--weird.rs", "--json"]);
        let parsed = parse_args(&args);
        // With the fixed parser, "--weird.rs" is not a known flag so it is a file
        assert!(
            parsed.file_list.contains(&"--weird.rs".to_string()),
            "file named --weird.rs must be in file_list, got: {:?}",
            parsed.file_list
        );
        assert!(parsed.json_output);
    }

    #[test]
    fn test_parse_args_json_flag() {
        let parsed = parse_args(&sv(&["--json"]));
        assert!(parsed.json_output);
        assert!(parsed.file_list.is_empty());
        assert!(parsed.dynlib_path.is_none());
    }

    #[test]
    fn test_parse_args_remaining_target() {
        let parsed = parse_args(&sv(&["/some/path"]));
        assert_eq!(parsed.remaining_args, sv(&["/some/path"]));
    }

    #[test]
    fn test_parse_args_empty() {
        let parsed = parse_args(&sv(&[]));
        assert!(parsed.file_list.is_empty());
        assert!(!parsed.json_output);
        assert!(parsed.dynlib_path.is_none());
    }

    // --- Bug #1612: --files-from <path> avoids passing the candidate list via argv ---

    #[test]
    fn test_parse_args_files_from_flag() {
        let args = sv(&["--files-from", "/tmp/candidates.txt", "--json"]);
        let parsed = parse_args(&args);
        assert_eq!(
            parsed.files_from_path,
            Some("/tmp/candidates.txt".to_string())
        );
        assert!(parsed.json_output);
    }

    #[test]
    fn test_parse_args_files_stops_at_files_from_flag() {
        let args = sv(&["--files", "a.rs", "--files-from", "/tmp/list.txt"]);
        let parsed = parse_args(&args);
        assert_eq!(parsed.file_list, sv(&["a.rs"]));
        assert_eq!(parsed.files_from_path, Some("/tmp/list.txt".to_string()));
    }

    #[test]
    fn test_parse_args_files_from_absent_by_default() {
        let parsed = parse_args(&sv(&["--json"]));
        assert!(parsed.files_from_path.is_none());
    }

    // --- Bug #1612: read_file_list() reads the candidate list from disk ---

    #[test]
    fn test_read_file_list_parses_newline_delimited_paths() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!(
            "xray_cli_test_read_file_list_{}_{}.txt",
            std::process::id(),
            "a"
        ));
        std::fs::write(&path, "/a/One.java\n/a/Two.java\n\n  \n/a/Three.java\n").unwrap();

        let result = read_file_list(path.to_str().unwrap());

        std::fs::remove_file(&path).ok();

        let files = result.expect("read_file_list must succeed for an existing file");
        assert_eq!(
            files,
            vec![
                PathBuf::from("/a/One.java"),
                PathBuf::from("/a/Two.java"),
                PathBuf::from("/a/Three.java"),
            ],
            "blank lines must be skipped and remaining lines preserved in order"
        );
    }

    #[test]
    fn test_read_file_list_missing_file_returns_error() {
        let missing = std::env::temp_dir().join(format!(
            "xray_cli_test_read_file_list_missing_{}.txt",
            std::process::id()
        ));
        // Ensure it really does not exist.
        std::fs::remove_file(&missing).ok();

        let result = read_file_list(missing.to_str().unwrap());

        assert!(
            result.is_err(),
            "read_file_list must return Err for a nonexistent path"
        );
    }
}
