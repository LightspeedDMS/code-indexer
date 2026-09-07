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

#[derive(Debug, serde::Serialize, serde::Deserialize)]
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

/// Parsed `--graph-in <path>`/`--dylib <path>`/`--facts-in <path>`
/// arguments for the `--analyze-graph` subcommand.
struct AnalyzeGraphArgs {
    graph_in: PathBuf,
    dylib: PathBuf,
    /// Dual-review defect H2: OPTIONAL path to a `write_facts_file` output
    /// (`repo_index::build_repo_graph`'s aggregated `FactIndex`, persisted
    /// by whatever caller ran the indexing pass). `None` when absent -- a
    /// legacy invocation with no facts file must keep working exactly as
    /// it did before this fix.
    facts_in: Option<PathBuf>,
}

/// Parses the `--analyze-graph` subcommand's two REQUIRED flags
/// (`--graph-in <path>`, `--dylib <path>`) plus the OPTIONAL `--facts-in
/// <path>`. Order-independent; errors with a clear message naming which
/// required flag is missing, never silently defaulting either.
fn parse_analyze_graph_args(args: &[String]) -> Result<AnalyzeGraphArgs, String> {
    let mut graph_in: Option<PathBuf> = None;
    let mut dylib: Option<PathBuf> = None;
    let mut facts_in: Option<PathBuf> = None;
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
            "--facts-in" => {
                let (value, next_i) = parse_value_flag(args, i, "--facts-in requires a path");
                facts_in = Some(PathBuf::from(value));
                i = next_i;
            }
            other => return Err(format!("--analyze-graph: unrecognized argument '{other}'")),
        }
    }
    Ok(AnalyzeGraphArgs {
        graph_in: graph_in.ok_or_else(|| "--analyze-graph requires --graph-in <path>".to_string())?,
        dylib: dylib.ok_or_else(|| "--analyze-graph requires --dylib <path>".to_string())?,
        facts_in,
    })
}

/// Story #1792 (S3, AC1/AC5): parsed flags for the `--refine` subcommand.
/// Unlike `--analyze-graph`, EVERY flag except `--facts-in` is required --
/// there is no bare directory-walk mode: the caller has already computed
/// the narrowed RefineSet-intersect-driver-matched file list and must hand
/// it over explicitly via `--files-from` (repo-relative paths, one per
/// line, resolved against `--repo-root` for the actual reads).
struct RefineArgs {
    graph_in: PathBuf,
    dylib: PathBuf,
    repo_root: PathBuf,
    files_from: PathBuf,
    facts_in: Option<PathBuf>,
}

/// Parses the `--refine` subcommand's four REQUIRED flags (`--graph-in`,
/// `--dylib`, `--repo-root`, `--files-from`) plus the OPTIONAL
/// `--facts-in`. Order-independent; errors with a clear message naming
/// which required flag is missing, never silently defaulting any of them
/// -- mirrors `parse_analyze_graph_args`'s exact loop structure.
fn parse_refine_args(args: &[String]) -> Result<RefineArgs, String> {
    let mut graph_in: Option<PathBuf> = None;
    let mut dylib: Option<PathBuf> = None;
    let mut repo_root: Option<PathBuf> = None;
    let mut files_from: Option<PathBuf> = None;
    let mut facts_in: Option<PathBuf> = None;
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
            "--repo-root" => {
                let (value, next_i) = parse_value_flag(args, i, "--repo-root requires a path");
                repo_root = Some(PathBuf::from(value));
                i = next_i;
            }
            "--files-from" => {
                let (value, next_i) = parse_value_flag(args, i, "--files-from requires a path");
                files_from = Some(PathBuf::from(value));
                i = next_i;
            }
            "--facts-in" => {
                let (value, next_i) = parse_value_flag(args, i, "--facts-in requires a path");
                facts_in = Some(PathBuf::from(value));
                i = next_i;
            }
            other => return Err(format!("--refine: unrecognized argument '{other}'")),
        }
    }
    Ok(RefineArgs {
        graph_in: graph_in.ok_or_else(|| "--refine requires --graph-in <path>".to_string())?,
        dylib: dylib.ok_or_else(|| "--refine requires --dylib <path>".to_string())?,
        repo_root: repo_root.ok_or_else(|| "--refine requires --repo-root <path>".to_string())?,
        files_from: files_from.ok_or_else(|| "--refine requires --files-from <path>".to_string())?,
        facts_in,
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
/// Dual-review defect H2: `facts_in`, when `Some`, names a
/// `write_facts_file` output -- `repo_index::build_repo_graph`'s real,
/// aggregated `FactIndex` -- loaded via `read_facts_file` so
/// `analyze_graph` finally receives real facts instead of a permanently
/// empty `FactIndex`. `None` (no facts file supplied, e.g. a legacy
/// invocation, or an evaluator with no `collect_facts`) keeps the
/// pre-fix behavior exactly: an empty `FactIndex`. A facts file that
/// exists but fails to read/parse DEGRADES to empty with a stderr
/// warning -- facts are auxiliary evidence for `analyze_graph`, never a
/// hard requirement for it to run at all.
fn run_analyze_graph(
    graph_in: &std::path::Path,
    dylib: &std::path::Path,
    facts_in: Option<&std::path::Path>,
) -> xray_core::graph::analyze::process::ChildReport<xray_core::graph::analyze::result::GraphResult> {
    use xray_core::graph::analyze::process::ChildReport;
    use xray_core::graph::analyze::result::AnalyzeStatus;
    use xray_core::graph::user_facts::{read_facts_file, FactIndex};

    let graph = match xray_core::graph::csr::wire::read_graph_file(graph_in) {
        Ok(g) => g,
        Err(_) => return ChildReport { status: AnalyzeStatus::GraphInvalid, result: None },
    };
    let evaluator = match xray_core::dynlib::GraphDynlibEvaluator::load(dylib) {
        Ok(e) => e,
        Err(_) => return ChildReport { status: AnalyzeStatus::LoadFailed, result: None },
    };

    let facts = match facts_in {
        Some(path) => read_facts_file(path).unwrap_or_else(|e| {
            eprintln!("Warning: failed to read --facts-in {}: {}", path.display(), e);
            FactIndex::new()
        }),
        None => FactIndex::new(),
    };
    let graph_handle = xray_core::graph::csr::handle::GraphHandle::from_graph(&graph);
    let facts_handle = xray_core::graph::user_facts::FactsHandle::from_facts(&facts);

    match evaluator.call_analyze_graph(&graph_handle, &facts_handle) {
        None => ChildReport { status: AnalyzeStatus::Absent, result: None },
        Some(None) => ChildReport { status: AnalyzeStatus::Panicked, result: None },
        Some(Some(result)) => ChildReport { status: AnalyzeStatus::RanOk, result: Some(result) },
    }
}

/// Story #1792 (S3, AC1): one file's outcome inside the `--refine`
/// subcommand's JSON report. `status` is a snake_case label mirroring
/// `graph::refine::RefineFileStatus`'s variant names exactly ("ran",
/// "panicked", "parse_failed", "not_exported"), plus this subcommand's own
/// `"path_escapes_repo_root"` (see `resolve_repo_relative_path`) -- kept as
/// a plain `String` (not the enum itself) so this wire type carries no
/// xray-core enum dependency beyond the data already flowing through
/// `JsonFinding`.
#[derive(Debug, serde::Serialize, serde::Deserialize)]
struct RefineChildFileOutcome {
    file: String,
    findings: Vec<JsonFinding>,
    status: String,
}

/// Story #1792 (S3, AC5): the `--refine` subcommand's PAYLOAD, carried
/// inside `ChildReport<RefineBatchResult>.result` -- the SAME
/// `{status, result}` wire envelope `run_analyze_graph`/`ChildReport<
/// GraphResult>` already uses. This is what makes `--refine` a genuine
/// drop-in for `run_analyze_child`/`run_analyze_child_with_memory_limit`
/// (Rule 4, anti-duplication): a bespoke `{status, files}` top-level shape
/// would silently fail `finish<T>`'s `ChildReport<T>` deserialization,
/// reporting every real refine child as `Panicked` regardless of what it
/// actually did.
#[derive(Debug, serde::Serialize, serde::Deserialize)]
struct RefineBatchResult {
    files: Vec<RefineChildFileOutcome>,
}

/// Maps `RefineFileStatus` to its snake_case wire label -- the ONE place
/// that resolves this mapping (Rule 4), so `RefineChildFileOutcome.status`
/// can never independently drift from the real enum's variant set.
fn refine_status_label(status: xray_core::graph::refine::RefineFileStatus) -> &'static str {
    use xray_core::graph::refine::RefineFileStatus::*;
    match status {
        Ran => "ran",
        Panicked => "panicked",
        ParseFailed => "parse_failed",
        NotExported => "not_exported",
    }
}

/// Validates `rel` resolves to a path genuinely CONTAINED within
/// `repo_root` -- mirrors `graph::repo_index`'s own `path_is_contained`
/// canonicalize-and-`starts_with` containment check (Rule 4: same
/// technique, re-derived here because the real one is a private fn in a
/// different crate module). Rejects `rel` OUTRIGHT if it is absolute --
/// `PathBuf::join` silently REPLACES the base with an absolute second
/// operand instead of appending it, so an absolute `rel` that happens to
/// canonicalize under `repo_root` would otherwise slip past the
/// `starts_with` check even though it never went through `repo_root` at
/// all; this guard closes that gap before any join/canonicalize happens.
/// Also rejects a `..`-style escape and a path that cannot be
/// canonicalized (does not exist, dangling symlink) -- `run_refine` could
/// not safely read it either way. Returns the resolved absolute path on
/// success.
fn resolve_repo_relative_path(
    canonical_repo_root: &std::path::Path,
    repo_root: &std::path::Path,
    rel: &std::path::Path,
) -> Option<PathBuf> {
    if rel.is_absolute() {
        return None;
    }
    let candidate = repo_root.join(rel);
    match candidate.canonicalize() {
        Ok(canonical) if canonical.starts_with(canonical_repo_root) => Some(candidate),
        _ => None,
    }
}

/// Splits `repo_relative_paths` into files safe to hand to
/// `run_refine_over_files` (valid `(abs_path, repo_relative_str)` pairs)
/// and pre-built `RefineChildFileOutcome`s for any entry that escapes
/// `repo_root` -- factored out of `run_refine` to keep it under the
/// project's per-function line budget.
fn partition_files_by_containment(
    canonical_repo_root: &std::path::Path,
    repo_root: &std::path::Path,
    repo_relative_paths: &[PathBuf],
) -> (Vec<(PathBuf, String)>, Vec<RefineChildFileOutcome>) {
    let mut valid_files = Vec::new();
    let mut escaped_outcomes = Vec::new();
    for rel in repo_relative_paths {
        let rel_str = rel.to_string_lossy().to_string();
        match resolve_repo_relative_path(canonical_repo_root, repo_root, rel) {
            Some(abs) => valid_files.push((abs, rel_str)),
            None => escaped_outcomes.push(RefineChildFileOutcome {
                file: rel_str,
                findings: vec![],
                status: "path_escapes_repo_root".to_string(),
            }),
        }
    }
    (valid_files, escaped_outcomes)
}

/// Maps `run_refine_over_files`'s real per-file results into the JSON wire
/// shape, appending the already-built `escaped_outcomes` -- factored out
/// of `run_refine` to keep it under the project's per-function line budget.
fn build_refine_file_outcomes(
    results: Vec<xray_core::graph::refine::RefineFileResult>,
    escaped_outcomes: Vec<RefineChildFileOutcome>,
) -> Vec<RefineChildFileOutcome> {
    let mut files_out: Vec<RefineChildFileOutcome> = results
        .into_iter()
        .map(|r| RefineChildFileOutcome {
            file: r.file,
            findings: r
                .findings
                .into_iter()
                .map(|f| JsonFinding { pattern: f.pattern, file: f.file, line: f.line, snippet: f.snippet })
                .collect(),
            status: refine_status_label(r.status).to_string(),
        })
        .collect();
    files_out.extend(escaped_outcomes);
    files_out
}

/// Story #1792 (S3, AC1): the core of the `--refine` subcommand, mirroring
/// `run_analyze_graph`'s status-mapping discipline exactly. Every terminal
/// status is DISTINCT and explicit (Rule 13, anti-silent-failure):
///
/// - graph file fails to read/parse -> `GraphInvalid`
/// - dylib fails to load, OR `repo_root` itself cannot be resolved ->
///   `LoadFailed` (nothing downstream could safely proceed either way)
/// - dylib loads but exports no `xray_refine` at all -> `Absent`
/// - otherwise -> `RanOk`, with each file's own outcome (ran/panicked/
///   parse_failed/path_escapes_repo_root) itemized in `files` -- a
///   per-file panic, parse failure, or path-containment violation never
///   aborts the whole batch or the top-level status.
fn run_refine(
    graph_in: &std::path::Path,
    dylib: &std::path::Path,
    repo_root: &std::path::Path,
    repo_relative_paths: &[PathBuf],
    facts_in: Option<&std::path::Path>,
) -> xray_core::graph::analyze::process::ChildReport<RefineBatchResult> {
    use xray_core::graph::analyze::process::ChildReport;
    use xray_core::graph::analyze::result::AnalyzeStatus;
    use xray_core::graph::user_facts::{read_facts_file, FactIndex};

    let graph = match xray_core::graph::csr::wire::read_graph_file(graph_in) {
        Ok(g) => g,
        Err(_) => return ChildReport { status: AnalyzeStatus::GraphInvalid, result: None },
    };
    let evaluator = match xray_core::dynlib::GraphDynlibEvaluator::load(dylib) {
        Ok(e) => e,
        Err(_) => return ChildReport { status: AnalyzeStatus::LoadFailed, result: None },
    };
    if !evaluator.has_refine() {
        return ChildReport { status: AnalyzeStatus::Absent, result: None };
    }
    let canonical_repo_root = match repo_root.canonicalize() {
        Ok(c) => c,
        Err(_) => return ChildReport { status: AnalyzeStatus::LoadFailed, result: None },
    };

    let facts = match facts_in {
        Some(path) => read_facts_file(path).unwrap_or_else(|e| {
            eprintln!("Warning: failed to read --facts-in {}: {}", path.display(), e);
            FactIndex::new()
        }),
        None => FactIndex::new(),
    };

    let (valid_files, escaped_outcomes) =
        partition_files_by_containment(&canonical_repo_root, repo_root, repo_relative_paths);
    let results = xray_core::graph::refine::run_refine_over_files(&valid_files, &graph, &facts, &evaluator);
    let files_out = build_refine_file_outcomes(results, escaped_outcomes);
    ChildReport { status: AnalyzeStatus::RanOk, result: Some(RefineBatchResult { files: files_out }) }
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
        let report = run_analyze_graph(&parsed.graph_in, &parsed.dylib, parsed.facts_in.as_deref());
        match serde_json::to_string(&report) {
            Ok(json) => println!("{}", json),
            Err(e) => eprintln!("Error: failed to serialize ChildReport: {}", e),
        }
        std::process::exit(0);
    }

    // Story #1792 (S3, AC1): `--refine --graph-in <path> --dylib <path>
    // --repo-root <path> --files-from <path>` -- runs in its OWN map-shaped
    // xray-cli invocation, driven by the SAME `run_analyze_child`/
    // `run_analyze_child_with_memory_limit` process container AC7 built for
    // `--analyze-graph` (AC5's cgroup ceiling + cancellation reuse).
    // ALWAYS exits 0 for a legitimate terminal status (GraphInvalid/
    // LoadFailed/Absent/RanOk, the last carrying per-file outcomes) --
    // exiting 1 is reserved for a malformed invocation (missing required
    // flags or an unreadable --files-from list), which the parent's
    // run_analyze_child already maps to Panicked via its own nonzero-exit
    // fallback.
    if args.first().map(|s| s.as_str()) == Some("--refine") {
        let parsed = match parse_refine_args(&args[1..]) {
            Ok(p) => p,
            Err(msg) => {
                eprintln!("Error: {}", msg);
                std::process::exit(1);
            }
        };
        let files_from_path = parsed.files_from.to_string_lossy().to_string();
        let repo_relative_paths = match read_file_list(&files_from_path) {
            Ok(list) => list,
            Err(msg) => {
                eprintln!("Error: {}", msg);
                std::process::exit(1);
            }
        };
        let report = run_refine(
            &parsed.graph_in,
            &parsed.dylib,
            &parsed.repo_root,
            &repo_relative_paths,
            parsed.facts_in.as_deref(),
        );
        match serde_json::to_string(&report) {
            Ok(json) => println!("{}", json),
            Err(e) => eprintln!("Error: failed to serialize RefineChildReport: {}", e),
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

    /// Dual-review defect H2: `--facts-in <path>` is OPTIONAL (a legacy
    /// invocation with no facts file must keep working exactly as before)
    /// but, when present, must be captured so `run_analyze_graph` can load
    /// the real `FactIndex` `repo_index::build_repo_graph` aggregated.
    #[test]
    fn parse_analyze_graph_args_accepts_an_optional_facts_in_flag() {
        let without_facts = sv(&["--graph-in", "/tmp/g.bin", "--dylib", "/tmp/e.so"]);
        let parsed = parse_analyze_graph_args(&without_facts).expect("facts-in must be optional");
        assert_eq!(parsed.facts_in, None, "no --facts-in flag must leave facts_in as None");

        let with_facts = sv(&["--graph-in", "/tmp/g.bin", "--dylib", "/tmp/e.so", "--facts-in", "/tmp/f.json"]);
        let parsed = parse_analyze_graph_args(&with_facts).expect("both required flags plus facts-in must parse");
        assert_eq!(parsed.facts_in, Some(std::path::PathBuf::from("/tmp/f.json")));
    }

    /// Story #1792 (S3, AC1/AC5): the `--refine` subcommand's own argument
    /// parsing -- `--graph-in`, `--dylib`, `--repo-root`, and `--files-from`
    /// are all REQUIRED (unlike `--analyze-graph`, refine has no bare
    /// directory-walk mode: the caller has already computed the narrowed
    /// RefineSet-intersect-driver-matched file list and must hand it over
    /// explicitly); `--facts-in` remains OPTIONAL, mirroring
    /// `parse_analyze_graph_args` exactly.
    #[test]
    fn parse_refine_args_extracts_all_required_flags_and_optional_facts_in() {
        let without_facts = sv(&[
            "--graph-in", "/tmp/g.bin", "--dylib", "/tmp/e.so", "--repo-root", "/repo", "--files-from", "/tmp/list.txt",
        ]);
        let parsed = parse_refine_args(&without_facts).expect("all required flags present must parse");
        assert_eq!(parsed.graph_in, std::path::PathBuf::from("/tmp/g.bin"));
        assert_eq!(parsed.dylib, std::path::PathBuf::from("/tmp/e.so"));
        assert_eq!(parsed.repo_root, std::path::PathBuf::from("/repo"));
        assert_eq!(parsed.files_from, std::path::PathBuf::from("/tmp/list.txt"));
        assert_eq!(parsed.facts_in, None, "facts-in must be optional");

        let with_facts = sv(&[
            "--graph-in", "/tmp/g.bin", "--dylib", "/tmp/e.so", "--repo-root", "/repo", "--files-from", "/tmp/list.txt",
            "--facts-in", "/tmp/f.json",
        ]);
        let parsed = parse_refine_args(&with_facts).expect("all required flags plus facts-in must parse");
        assert_eq!(parsed.facts_in, Some(std::path::PathBuf::from("/tmp/f.json")));
    }

    /// Every one of the FOUR required `--refine` flags must be individually
    /// required -- omitting ANY ONE of them (with the other three present)
    /// must error loudly, never silently default to an empty/placeholder
    /// value. Unlike the legacy scan mode, `--refine` has no bare
    /// directory-walk fallback for `--files-from`.
    #[test]
    fn parse_refine_args_errors_when_any_required_flag_is_missing() {
        let full = sv(&[
            "--graph-in", "/tmp/g.bin", "--dylib", "/tmp/e.so", "--repo-root", "/repo", "--files-from", "/tmp/list.txt",
        ]);
        assert!(parse_refine_args(&full).is_ok(), "fixture sanity: the full flag set must parse");

        let without_graph_in = sv(&["--dylib", "/tmp/e.so", "--repo-root", "/repo", "--files-from", "/tmp/list.txt"]);
        assert!(parse_refine_args(&without_graph_in).is_err(), "--graph-in is required");

        let without_dylib = sv(&["--graph-in", "/tmp/g.bin", "--repo-root", "/repo", "--files-from", "/tmp/list.txt"]);
        assert!(parse_refine_args(&without_dylib).is_err(), "--dylib is required");

        let without_repo_root = sv(&["--graph-in", "/tmp/g.bin", "--dylib", "/tmp/e.so", "--files-from", "/tmp/list.txt"]);
        assert!(parse_refine_args(&without_repo_root).is_err(), "--repo-root is required");

        let without_files_from = sv(&["--graph-in", "/tmp/g.bin", "--dylib", "/tmp/e.so", "--repo-root", "/repo"]);
        assert!(parse_refine_args(&without_files_from).is_err(), "--files-from is required");
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
        result.refine.push(g.resolve_symbol(callee).expect("callee came from g.callees_of, always valid"));
    }
    result
}
"#;
        let cr = xray_core::compiler::compile_evaluator(user_code, dir.path()).expect("must compile");

        let report = run_analyze_graph(&graph_path, &cr.so_path, None);
        assert_eq!(report.status, AnalyzeStatus::RanOk);
        let result = report.result.expect("RanOk must carry a result");
        assert_eq!(result.refine, vec![b_symbol], "must resolve to B's real SymbolId via a REAL accessor call");
    }

    /// Dual-review defect H2 (the letter of "aggregate collected facts
    /// into a real FactIndex and PASS IT THROUGH"): a real evaluator whose
    /// `analyze_graph` queries `facts.for_symbol(..)` must actually SEE a
    /// fact once one exists in a real `write_facts_file` output and
    /// `--facts-in` names it -- proving facts genuinely reach
    /// `analyze_graph`, never a permanently-empty `FactIndex::new()`.
    #[test]
    fn run_analyze_graph_passes_real_facts_from_a_facts_file_to_analyze_graph() {
        use tempfile::TempDir;
        use xray_core::graph::analyze::result::AnalyzeStatus;
        use xray_core::graph::user_facts::{write_facts_file, FactIndex, FactKey, UserFact};

        let dir = TempDir::new().unwrap();
        let (graph_path, b_symbol) = write_small_graph_file(dir.path());

        let mut facts = FactIndex::new();
        facts.insert(
            FactKey::Symbol(b_symbol),
            UserFact { kind: "deprecated".to_string(), line: 1, message: "old API".to_string(), custom_key: None },
        );
        let facts_path = dir.path().join("facts.json");
        write_facts_file(&facts, &facts_path).expect("write_facts_file must succeed");

        let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let b_symbol = g.resolve_symbol(1).expect("dense id 1 (B) came from the real fixture graph");
    if !facts.for_symbol(b_symbol).is_empty() {
        result.refine.push(b_symbol);
    }
    result
}
"#;
        let cr = xray_core::compiler::compile_evaluator(user_code, dir.path()).expect("must compile");

        let report = run_analyze_graph(&graph_path, &cr.so_path, Some(&facts_path));
        assert_eq!(report.status, AnalyzeStatus::RanOk);
        let result = report.result.expect("RanOk must carry a result");
        assert_eq!(
            result.refine,
            vec![b_symbol],
            "analyze_graph must have observed the REAL fact via facts.for_symbol -- it was never empty"
        );
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
        let absent_report = run_analyze_graph(&graph_path, &legacy_cr.so_path, None);
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
        let empty_report = run_analyze_graph(&graph_path, &empty_cr.so_path, None);
        assert_eq!(empty_report.status, AnalyzeStatus::RanOk);
        assert_eq!(empty_report.result, Some(xray_core::graph::analyze::result::GraphResult::default()));

        // GraphInvalid: the graph file itself is corrupt/unreadable.
        let corrupt_graph_path = dir.path().join("corrupt.bin");
        std::fs::write(&corrupt_graph_path, b"not a real graph file").unwrap();
        let invalid_report = run_analyze_graph(&corrupt_graph_path, &empty_cr.so_path, None);
        assert_eq!(invalid_report.status, AnalyzeStatus::GraphInvalid);
        assert!(invalid_report.result.is_none());

        // LoadFailed: the dylib path does not exist at all.
        let missing_dylib_path = dir.path().join("does_not_exist.so");
        let load_failed_report = run_analyze_graph(&graph_path, &missing_dylib_path, None);
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

    /// Shared setup for the two `run_refine` tests below: writes each
    /// `file_names` entry as a trivial real `.java` file under `dir`, and
    /// writes a real (empty) graph file -- returns the graph's path.
    /// `--refine`'s logic only needs a REAL, mmap-readable graph file and
    /// real on-disk source files; the graph's own content is irrelevant to
    /// either test, which exercise `refine`'s dispatch, not graph queries.
    fn write_empty_graph_and_files(dir: &std::path::Path, file_names: &[&str]) -> std::path::PathBuf {
        use xray_core::graph::csr::builder::CodeGraphBuilder;
        use xray_core::graph::csr::wire::write_graph_file;

        for name in file_names {
            std::fs::write(dir.join(name), format!("class {} {{}}", name.trim_end_matches(".java"))).unwrap();
        }
        let graph = CodeGraphBuilder::with_candidate_capacity(0).build();
        let graph_path = dir.join("graph.bin");
        write_graph_file(&graph, &graph_path).expect("write_graph_file must succeed");
        graph_path
    }

    /// THE end-to-end proof of the `--refine` subcommand's core logic: a
    /// real graph, TWO real files written under a real `repo_root`, and a
    /// real compiled evaluator whose `refine` reports the file it ran on.
    /// `run_refine` must load the graph, resolve each repo-relative path
    /// against `repo_root`, call `refine` once per file, and report
    /// `RanOk` with one `RefineChildFileOutcome` per file, each carrying
    /// its real findings.
    #[test]
    fn run_refine_produces_ran_ok_with_real_findings_for_each_file() {
        use tempfile::TempDir;
        use xray_core::graph::analyze::result::AnalyzeStatus;

        let dir = TempDir::new().unwrap();
        let graph_path = write_empty_graph_and_files(dir.path(), &["A.java", "B.java"]);

        let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {
    vec![EvalFinding { pattern: "refine-visited".to_string(), line: node.start_line, snippet: ctx.file.clone() }]
}
"#;
        let cr = xray_core::compiler::compile_evaluator(user_code, dir.path()).expect("must compile");

        let report = run_refine(
            &graph_path,
            &cr.so_path,
            dir.path(),
            &[std::path::PathBuf::from("A.java"), std::path::PathBuf::from("B.java")],
            None,
        );

        assert_eq!(report.status, AnalyzeStatus::RanOk);
        let result = report.result.expect("RanOk must carry a real RefineBatchResult");
        assert_eq!(result.files.len(), 2);
        for (outcome, expected_file) in result.files.iter().zip(["A.java", "B.java"]) {
            assert_eq!(outcome.status, "ran");
            assert_eq!(outcome.file, expected_file);
            assert_eq!(outcome.findings.len(), 1);
            assert_eq!(outcome.findings[0].snippet, expected_file);
        }
    }

    /// THE central `--refine` invariant, mirroring `--analyze-graph`'s own
    /// `Absent` test: a graph-mode dylib with NO `fn refine` at all must
    /// report the top-level `Absent` status -- DISTINCT from a successful
    /// empty run (`RanOk` with an empty `files` list would look identical
    /// to "ran over zero files", which this must never be confused with).
    #[test]
    fn run_refine_reports_absent_when_dylib_has_no_refine_export() {
        use tempfile::TempDir;
        use xray_core::graph::analyze::result::AnalyzeStatus;

        let dir = TempDir::new().unwrap();
        let graph_path = write_empty_graph_and_files(dir.path(), &["A.java"]);

        let no_refine_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
"#;
        let cr = xray_core::compiler::compile_evaluator(no_refine_code, dir.path()).expect("must compile");

        let report = run_refine(&graph_path, &cr.so_path, dir.path(), &[std::path::PathBuf::from("A.java")], None);

        assert_eq!(report.status, AnalyzeStatus::Absent, "no fn refine at all must report Absent, never RanOk");
        assert!(report.result.is_none(), "Absent must carry no result, distinct from a successful empty RanOk");
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
