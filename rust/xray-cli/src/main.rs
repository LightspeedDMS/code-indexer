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
    /// Bug #1827: `"compile"` when `error` is a genuine problem in the
    /// USER's evaluator source (a real rustc diagnostic, a sandbox
    /// validation rejection, an ambiguous/missing mode, or an oversized
    /// source), `"infrastructure"` when it is an xray-cli/toolchain/
    /// filesystem problem unrelated to the source's content, `None` when
    /// `error` itself is `None`. Mirrors `xray_core::compiler::
    /// CompileErrorKind` -- callers (RustNativeBackend) use this to avoid
    /// telling an agent "fix your code" when the real problem is e.g. a
    /// broken cache directory.
    error_kind: Option<String>,
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
/// from cache, or an `Err(CompileError)` (Bug #1827 -- typed, carrying
/// `.kind` so main() can classify a legitimate JSON-mode failure as a
/// Compile vs Infrastructure problem) on read/compilation/load failure.
type EvaluatorsResult = Result<(Vec<Box<dyn Evaluator>>, u128, bool), xray_core::compiler::CompileError>;

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

/// Bug #1822 Defect 2: total number of raw trailing bytes the post-cap
/// truncation scan (`scan_trailing_content_bounded`) is willing to inspect
/// while looking for the first non-whitespace byte beyond the cap. A
/// `--files-from` list can be adversarial/corrupt (e.g. gigabytes of
/// whitespace with no newline anywhere) -- this budget guarantees the scan
/// performs a bounded amount of work regardless of how much trailing
/// content actually exists on disk.
const TRUNCATION_SCAN_BYTE_BUDGET: usize = 64 * 1024;

/// Bug #1822 Defect 2: fixed chunk size the scan reads at a time via
/// `Read::read()`, so a single "line" with no newline anywhere (which
/// defeated the old `read_until`-based scan -- it read such a line in ONE
/// unbounded call) can never be consumed in one shot either.
const TRUNCATION_SCAN_CHUNK_SIZE: usize = 8 * 1024;

/// Bug #1822 Defect 2: scans up to `byte_budget` raw bytes from `reader`,
/// in fixed `chunk_size` chunks via `Read::read()` (never `BufRead::
/// read_until`, which can read an arbitrarily large single "line" in one
/// call), looking for the first non-ASCII-whitespace byte.
///
/// Returns:
///  - `Ok(true)`  if a non-whitespace byte was found within the budget
///    (real content follows beyond the cap -- truncated).
///  - `Ok(false)` if EOF was reached within the budget with only
///    whitespace seen (genuinely exhausted -- not truncated).
///  - `Err(..)`   if the budget was exhausted before finding either a
///    non-whitespace byte or EOF -- the scan genuinely cannot tell
///    whether real content follows, and must say so explicitly rather
///    than silently assuming `false`.
///
/// Content is judged byte-wise via `is_ascii_whitespace()` -- never UTF-8
/// decoded -- so malformed content beyond the cap can never cause a
/// decode error, exactly as before this fix.
///
/// `chunk_size` MUST be non-zero: `Read::read()` on an empty destination
/// slice always returns `Ok(0)` per the trait's own documented contract,
/// which is indistinguishable from a real EOF signal -- a `chunk_size ==
/// 0` caller would therefore silently misreport `Ok(false)` ("not
/// truncated") without ever inspecting a single byte or reaching a real
/// EOF. Guarded explicitly rather than left as a latent trap, even though
/// every current call site passes the fixed `TRUNCATION_SCAN_CHUNK_SIZE`
/// constant.
fn scan_trailing_content_bounded<R: std::io::Read>(
    reader: &mut R,
    byte_budget: usize,
    chunk_size: usize,
    path: &str,
) -> Result<bool, String> {
    if chunk_size == 0 {
        return Err(format!(
            "Internal error scanning --files-from list at {}: truncation-scan chunk_size must \
             be non-zero",
            path
        ));
    }
    let mut buf = vec![0u8; chunk_size];
    let mut total_read = 0usize;
    while total_read < byte_budget {
        let to_read = (byte_budget - total_read).min(chunk_size);
        let n = reader
            .read(&mut buf[..to_read])
            .map_err(|e| format!("Failed to read --files-from list at {}: {}", path, e))?;
        if n == 0 {
            return Ok(false); // genuinely exhausted within budget, not truncated
        }
        total_read += n;
        if buf[..n].iter().any(|b| !b.is_ascii_whitespace()) {
            return Ok(true);
        }
    }
    Err(format!(
        "--files-from list at {} has trailing content beyond the configured cap that exceeds \
         the {}-byte truncation-scan budget; unable to determine whether the list was \
         truncated without unbounded reading",
        path, byte_budget
    ))
}

/// R3-2 (Codex re-review, ROUND 3): a bounded variant of `read_file_list`
/// for `--build-graph` specifically. `read_file_list` (above) reads the
/// ENTIRE file into memory via `read_to_string` before parsing a single
/// line -- on a `--files-from` list with millions of entries that alone
/// materializes the full text before any limit engages. This variant uses
/// `BufReader::read_line`, which reads lazily line-by-line, and stops
/// collecting once `max_lines` valid (non-blank) paths are found.
///
/// Truncation ("was there at least one further real entry?") is detected
/// via `scan_trailing_content_bounded` -- a fixed-byte-budget, chunked
/// scan of the underlying reader that checks whether any further
/// non-ASCII-whitespace byte exists beyond the cap -- deliberately NEVER
/// UTF-8-decoding anything beyond the cap, so malformed content past the
/// cap can never cause a decode error; it is only ever classified as
/// "blank", "non-blank content", or (Bug #1822 Defect 2) "budget
/// exhausted, unknown". Bug #1814: a naive "any bytes remain" check (the
/// previous `fill_buf`-based peek) treated trailing blank lines/whitespace
/// as truncation, falsely downgrading a genuinely complete list. Bug
/// #1822 Defect 2: the ORIGINAL fix for #1814 used `BufRead::
/// read_until(b'\n', ..)` in an unbounded loop -- a single trailing "line"
/// with no newline anywhere (e.g. gigabytes of whitespace) was consumed
/// ENTIRELY by one `read_until` call before truncation could be decided,
/// which is unbounded work on adversarial/corrupt input. The scan is now
/// chunked and budget-bounded (see `scan_trailing_content_bounded`); if
/// the budget is exhausted before the answer is known, this function
/// returns an explicit `Err` rather than silently assuming "not
/// truncated". A genuine I/O error while scanning is likewise always
/// propagated as `Err` (never silently treated as "not truncated") --
/// only the CONTENT beyond the cap is never inspected past the budget.
///
/// Left as a SEPARATE function rather than changing `read_file_list`
/// itself: that function is also used by `--refine` and the legacy
/// (non-`--files-from`) default scan path, both of which have their own
/// existing, unbounded-by-design contracts and tests this change must not
/// touch.
fn read_file_list_capped(path: &str, max_lines: usize) -> Result<(Vec<PathBuf>, bool), String> {
    use std::io::{BufRead, BufReader};

    let path_buf = PathBuf::from(path);
    if !path_buf.is_absolute() {
        return Err(format!("--files-from path must be absolute, got: {}", path));
    }
    let file = std::fs::File::open(&path_buf)
        .map_err(|e| format!("Failed to read --files-from list at {}: {}", path, e))?;
    let mut reader = BufReader::new(file);

    let mut paths: Vec<PathBuf> = Vec::new();
    let mut line = String::new();
    while paths.len() < max_lines {
        line.clear();
        let bytes_read = reader
            .read_line(&mut line)
            .map_err(|e| format!("Failed to read --files-from list at {}: {}", path, e))?;
        if bytes_read == 0 {
            return Ok((paths, false)); // genuinely exhausted, not truncated
        }
        let trimmed = line.trim();
        if !trimmed.is_empty() {
            paths.push(PathBuf::from(trimmed));
        }
    }
    let truncated = scan_trailing_content_bounded(
        &mut reader,
        TRUNCATION_SCAN_BYTE_BUDGET,
        TRUNCATION_SCAN_CHUNK_SIZE,
        path,
    )?;
    Ok((paths, truncated))
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
///
/// H9 (consolidated review, Issue #1811/Bug #1812): `graph_mode` selects
/// which of the two assembly-aware identity functions to use --
/// `cache_identity_info` (legacy assembly) or `cache_identity_info_graph`
/// (graph assembly, matching what `compile_evaluator` uses for a
/// `EvaluatorMode::Graph` evaluator). Without this, a graph-mode caller
/// pre-fills/post-fills the cluster cache under an identity the graph
/// compile path can never look up.
fn format_cache_identity_output(user_code: &str, graph_mode: bool) -> String {
    let info = if graph_mode {
        xray_core::compiler::cache_identity_info_graph(user_code)
    } else {
        xray_core::compiler::cache_identity_info(user_code)
    };
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
        // H10 (consolidated review, Issue #1811/Bug #1812): a facts file
        // that EXISTS but fails to read/parse is a real failure, not
        // "no facts collected" -- hard-fail with FactsInvalid rather than
        // silently degrading to an empty FactIndex (Rule 2/13).
        Some(path) => match read_facts_file(path) {
            Ok(f) => f,
            Err(e) => {
                eprintln!("Error: failed to read --facts-in {}: {}", path.display(), e);
                return ChildReport { status: AnalyzeStatus::FactsInvalid, result: None };
            }
        },
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
        // H10 (consolidated review, Issue #1811/Bug #1812): mirrors
        // run_analyze_graph's identical fix -- a facts file that EXISTS
        // but fails to read/parse is a real failure, hard-fail with
        // FactsInvalid rather than silently degrading to an empty
        // FactIndex (Rule 2/13).
        Some(path) => match read_facts_file(path) {
            Ok(f) => f,
            Err(e) => {
                eprintln!("Error: failed to read --facts-in {}: {}", path.display(), e);
                return ChildReport { status: AnalyzeStatus::FactsInvalid, result: None };
            }
        },
        None => FactIndex::new(),
    };

    let (valid_files, escaped_outcomes) =
        partition_files_by_containment(&canonical_repo_root, repo_root, repo_relative_paths);
    let results = xray_core::graph::refine::run_refine_over_files(&valid_files, &graph, &facts, &evaluator);
    let files_out = build_refine_file_outcomes(results, escaped_outcomes);
    ChildReport { status: AnalyzeStatus::RanOk, result: Some(RefineBatchResult { files: files_out }) }
}

/// Story #1811 (S5, AC1): parsed flags for the `--build-graph` subcommand.
/// Every flag except `--facts-out` is required -- there is no bare
/// directory-walk mode, mirroring `--refine`'s own `RefineArgs` exactly:
/// the caller has already computed the candidate file list and must hand
/// it over via `--files-from`.
struct BuildGraphArgs {
    repo_root: PathBuf,
    files_from: PathBuf,
    dylib: PathBuf,
    graph_out: PathBuf,
    facts_out: Option<PathBuf>,
}

/// Parses the `--build-graph` subcommand's four REQUIRED flags
/// (`--repo-root`, `--files-from`, `--dylib`, `--graph-out`) plus the
/// OPTIONAL `--facts-out` -- order-independent, mirrors `parse_refine_
/// args`'s exact loop structure (Rule 4, anti-duplication).
fn parse_build_graph_args(args: &[String]) -> Result<BuildGraphArgs, String> {
    let mut repo_root: Option<PathBuf> = None;
    let mut files_from: Option<PathBuf> = None;
    let mut dylib: Option<PathBuf> = None;
    let mut graph_out: Option<PathBuf> = None;
    let mut facts_out: Option<PathBuf> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
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
            "--dylib" => {
                let (value, next_i) = parse_value_flag(args, i, "--dylib requires a path");
                dylib = Some(PathBuf::from(value));
                i = next_i;
            }
            "--graph-out" => {
                let (value, next_i) = parse_value_flag(args, i, "--graph-out requires a path");
                graph_out = Some(PathBuf::from(value));
                i = next_i;
            }
            "--facts-out" => {
                let (value, next_i) = parse_value_flag(args, i, "--facts-out requires a path");
                facts_out = Some(PathBuf::from(value));
                i = next_i;
            }
            other => return Err(format!("--build-graph: unrecognized argument '{other}'")),
        }
    }
    Ok(BuildGraphArgs {
        repo_root: repo_root.ok_or_else(|| "--build-graph requires --repo-root <path>".to_string())?,
        files_from: files_from.ok_or_else(|| "--build-graph requires --files-from <path>".to_string())?,
        dylib: dylib.ok_or_else(|| "--build-graph requires --dylib <path>".to_string())?,
        graph_out: graph_out.ok_or_else(|| "--build-graph requires --graph-out <path>".to_string())?,
        facts_out,
    })
}

/// Adapts a loaded `GraphDynlibEvaluator` into the `FactCollector` trait
/// `build_repo_graph` requires (Rule 4: reuses the REAL dylib call, never
/// reimplements fact collection). `_index` (the completed `LocalIndex`) is
/// unused here -- `GraphDynlibEvaluator::call_collect_facts` does not take
/// it, mirroring how the dylib ABI itself only passes `node`/`file`.
///
/// Only the "not exported" case (outer `None`) degrades to an empty fact
/// list -- that is a legitimate, non-failure outcome (mirrors AC7/AC8's
/// own `Absent` treatment for a dylib with no graph-mode exports at all).
/// A genuine dylib-internal panic (inner `None`, already caught once by
/// the dylib's own `catch_unwind` at the FFI boundary) is deliberately
/// RE-PANICKED here rather than silently swallowed into an empty `Vec`:
/// `graph::fused::run_collect_facts` (the ONLY real caller of this trait
/// method, via `build_repo_graph`) wraps every `collect_facts` call in its
/// OWN `catch_unwind` and records `CollectFactsStatus::Panicked`, which
/// `record_fused_result` rolls up into `RepoIndexResult::files_with_
/// collector_panics` -- surfaced verbatim in `BuildGraphResult`. Silently
/// returning `Vec::new()` for a real panic would make that counter always
/// read zero regardless of what actually happened inside the dylib,
/// hiding a genuine failure behind a "ran successfully, found nothing"
/// report (Rule 13, anti-silent-failure).
struct DylibFactCollector<'a> {
    evaluator: &'a xray_core::dynlib::GraphDynlibEvaluator,
}

impl<'a> xray_core::graph::user_facts::FactCollector for DylibFactCollector<'a> {
    fn collect_facts(
        &self,
        root: &xray_core::owned_node::OwnedNode,
        file: &str,
        _index: &xray_core::graph::extract::local_index::LocalIndex,
    ) -> Vec<xray_core::graph::user_facts::UserFact> {
        match self.evaluator.call_collect_facts(root, file) {
            None => Vec::new(),
            Some(None) => panic!("dylib collect_facts panicked while processing {file}"),
            Some(Some(facts)) => facts,
        }
    }
}

/// Story #1811 (S5, AC1): `--build-graph`'s terminal statuses -- every one
/// DISTINCT and explicit (Rule 13, anti-silent-failure), mirroring
/// `AnalyzeStatus`'s discipline. `RepoRootInvalid` exists specifically
/// because `build_repo_graph` itself `.expect()`s a successful
/// `repo_root.canonicalize()` -- `run_build_graph` guards that BEFORE
/// delegating, so an invalid `--repo-root` reports cleanly instead of
/// crashing the whole process.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum BuildGraphStatus {
    RepoRootInvalid,
    LoadFailed,
    FileIdCollision,
    GraphWriteFailed,
    FactsWriteFailed,
    Ok,
}

/// Story #1811 (S5, AC1): "a caller MUST be able to tell a complete graph
/// from a degraded one" -- surfaces `RepoIndexResult`'s own completeness
/// and degradation counters verbatim (Rule 4: never re-derives them),
/// including `files_with_collector_panics` (see `DylibFactCollector`'s
/// doc comment for why a dylib-side `collect_facts` panic reaches this
/// counter rather than being silently absorbed).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
struct BuildGraphResult {
    fact_graph_complete: bool,
    files_with_parse_errors: usize,
    unreadable_or_unsupported_files: usize,
    files_with_read_errors: usize,
    files_with_extractor_panics: usize,
    files_with_collector_panics: usize,
    /// Consolidated review finding C2 (Issue #1811/Bug #1812): surfaces
    /// `RepoIndexResult::files_with_unsupported_language` verbatim -- files
    /// with a recognized source-language extension for which the engine
    /// has no graph extractor (Java is currently the only one). Alongside
    /// the six pre-existing counters, never re-derived.
    files_with_unsupported_language: usize,
    truncated_by_max_files: bool,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct BuildGraphReport {
    status: BuildGraphStatus,
    result: Option<BuildGraphResult>,
}

/// R2-6 (Codex re-review): `run_build_graph` previously used
/// `IndexBudget::unlimited()` and `max_files: None`, so a whole-repo
/// `--build-graph` request could allocate candidate paths and graph state
/// with NO graph-specific bound -- an exhaustion risk at ~900-repo fleet
/// scale (a single very large or adversarial repo could consume unbounded
/// memory building its graph). `IndexBudget`'s degradation ladder and
/// `max_files`'s truncation reporting (`truncated_by_max_files`,
/// `fact_graph_complete` downgrade) were ALREADY implemented and tested in
/// `repo_index.rs` -- this was purely a missing finite default at the one
/// production call site, never a missing mechanism.
///
/// Values are hardcoded (not a new Web UI setting -- project convention:
/// no new config surface to gate a bug fix) and deliberately generous so
/// no realistic legitimate repo is truncated; they exist to cap the
/// PATHOLOGICAL case, not to constrain everyday use. Revisit with real
/// fleet telemetry if a genuinely huge monorepo needs a higher ceiling.
const GRAPH_INDEX_MAX_FILES: usize = 50_000;
const GRAPH_INDEX_MAX_TOTAL_CANDIDATES: usize = 2_000_000;
const GRAPH_INDEX_MAX_CANDIDATES_PER_REFERENCE: usize = 1_000;

fn build_graph_index_options() -> xray_core::graph::repo_index::RepoIndexOptions {
    xray_core::graph::repo_index::RepoIndexOptions {
        budget: xray_core::graph::budget::IndexBudget::new(
            GRAPH_INDEX_MAX_TOTAL_CANDIDATES,
            GRAPH_INDEX_MAX_CANDIDATES_PER_REFERENCE,
        ),
        max_files: Some(GRAPH_INDEX_MAX_FILES),
    }
}

/// Story #1811 (S5, AC1): the core of `--build-graph`, factored out from
/// argv/exit-code plumbing exactly like `run_analyze_graph`/`run_refine`
/// are. Drives the EXISTING `build_repo_graph` + `write_graph_file` +
/// (when `facts_out` is `Some`) `write_facts_file` pipeline -- never
/// reimplements any of the three. Every terminal status is distinct:
///
/// - `--repo-root` fails to canonicalize -> `RepoRootInvalid`
/// - `--dylib` fails to load/verify -> `LoadFailed`
/// - two distinct `repo_relative_paths` hash to the same `file_id` ->
///   `FileIdCollision`
/// - `write_graph_file` fails (e.g. an unwritable `--graph-out` parent
///   directory) -> `GraphWriteFailed`
/// - `write_facts_file` fails -> `FactsWriteFailed`
/// - otherwise -> `Ok`, carrying `RepoIndexResult`'s completeness/
///   degradation counters verbatim.
///
/// `dylib`/`graph_out`/`facts_out` are TRUSTED paths -- xray-cli is always
/// invoked directly by a trusted parent process (Python's
/// `RustNativeBackend`, never a network-facing service proxying untrusted
/// end-user paths), exactly like every neighboring subcommand's
/// `--dylib`/`--graph-in` above already assumes: `run_analyze_graph` and
/// `run_refine` apply zero path-containment validation to either flag
/// either. `--refine`'s OWN `resolve_repo_relative_path` containment check
/// exists for a genuinely different reason -- narrowing untrusted-shaped
/// repo-relative FILE paths against `--repo-root` before reading their
/// contents -- and is orthogonal to the CLI's own trusted invocation
/// paths. This subcommand does not lower that pre-existing trust boundary,
/// it reuses it.
fn run_build_graph(
    repo_root: &std::path::Path,
    repo_relative_paths: &[PathBuf],
    dylib: &std::path::Path,
    graph_out: &std::path::Path,
    facts_out: Option<&std::path::Path>,
) -> BuildGraphReport {
    use xray_core::graph::repo_index::build_repo_graph;
    use xray_core::graph::user_facts::write_facts_file;
    use xray_core::graph::csr::wire::write_graph_file;

    if repo_root.canonicalize().is_err() {
        return BuildGraphReport { status: BuildGraphStatus::RepoRootInvalid, result: None };
    }
    let evaluator = match xray_core::dynlib::GraphDynlibEvaluator::load(dylib) {
        Ok(e) => e,
        Err(_) => return BuildGraphReport { status: BuildGraphStatus::LoadFailed, result: None },
    };
    let collector = DylibFactCollector { evaluator: &evaluator };

    let path_strings: Vec<String> =
        repo_relative_paths.iter().map(|p| p.to_string_lossy().to_string()).collect();
    let options = build_graph_index_options();
    let index_result = match build_repo_graph(repo_root, &path_strings, &options, &collector) {
        Ok(r) => r,
        Err(_) => return BuildGraphReport { status: BuildGraphStatus::FileIdCollision, result: None },
    };

    if write_graph_file(&index_result.graph, graph_out).is_err() {
        return BuildGraphReport { status: BuildGraphStatus::GraphWriteFailed, result: None };
    }
    if let Some(facts_path) = facts_out {
        if write_facts_file(&index_result.facts, facts_path).is_err() {
            return BuildGraphReport { status: BuildGraphStatus::FactsWriteFailed, result: None };
        }
    }

    BuildGraphReport {
        status: BuildGraphStatus::Ok,
        result: Some(BuildGraphResult {
            fact_graph_complete: index_result.fact_graph_complete,
            files_with_parse_errors: index_result.files_with_parse_errors,
            unreadable_or_unsupported_files: index_result.unreadable_or_unsupported_files,
            files_with_read_errors: index_result.files_with_read_errors,
            files_with_extractor_panics: index_result.files_with_extractor_panics,
            files_with_collector_panics: index_result.files_with_collector_panics,
            files_with_unsupported_language: index_result.files_with_unsupported_language,
            truncated_by_max_files: index_result.truncated_by_max_files,
        }),
    }
}

/// Story #1811 (S5, AC1/AC2): the `--compile-only` subcommand's JSON
/// report. `--build-graph`/`--analyze-graph`/`--refine` all require an
/// ALREADY-COMPILED `.so` via `--dylib`, but the only existing xray-cli
/// path that compiles a raw `.rs` source (the default legacy scan's
/// `compile_and_load_evaluator`) also immediately tries to load the result
/// as a LEGACY evaluator (`DynlibEvaluator::load`, which requires the
/// `xray_evaluate_node` export) -- rejecting a graph-mode-only artifact
/// outright even though compilation itself succeeded. `run_compile_only`
/// calls `compiler::compile_evaluator` directly and stops there: it is
/// mode-agnostic (legacy OR graph OR malformed -- `detect_evaluator_mode`
/// inside `compile_evaluator` is the single authority on which), so this
/// is the missing bridge Python's graph-mode driver needs to obtain a real
/// `.so` path before invoking `--build-graph`/`--analyze-graph`.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct CompileOnlyOutput {
    so_path: String,
    compile_ms: u128,
    cached: bool,
    error: Option<String>,
}

/// Parses `--compile-only`'s one required flag, `--dynlib <path>` --
/// deliberately the SAME flag name the legacy default path already uses
/// for a raw `.rs` source file (Rule 4: one name for "a source file to
/// compile" across both subcommands, never a second name for the same
/// concept).
fn parse_compile_only_args(args: &[String]) -> Result<PathBuf, String> {
    let mut dynlib: Option<PathBuf> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--dynlib" => {
                let (value, next_i) = parse_value_flag(args, i, "--dynlib requires a path");
                dynlib = Some(PathBuf::from(value));
                i = next_i;
            }
            other => return Err(format!("--compile-only: unrecognized argument '{other}'")),
        }
    }
    dynlib.ok_or_else(|| "--compile-only requires --dynlib <path>".to_string())
}

/// Reads `dynlib_path` and compiles it via the REAL `compiler::
/// compile_evaluator`, writing into `cache_dir` (the SAME cache the
/// legacy default path and `--print-cache-identity` share) -- never
/// reimplements compilation. `cache_dir` is an explicit parameter (rather
/// than always resolving `xray_core::cache::get_cache_dir()` internally)
/// so tests can point it at an isolated directory.
///
/// `dynlib_path` is a TRUSTED path -- the SAME `--dynlib <path.rs>` flag
/// the pre-existing legacy default path's `read_evaluator_source` (above
/// in this file) already reads via a bare `std::fs::read_to_string` with
/// zero extension/containment validation. This subcommand does not lower
/// that pre-existing trust boundary, it reuses it exactly.
fn run_compile_only(dynlib_path: &std::path::Path, cache_dir: &std::path::Path) -> CompileOnlyOutput {
    let user_code = match std::fs::read_to_string(dynlib_path) {
        Ok(c) => c,
        Err(e) => {
            return CompileOnlyOutput {
                so_path: String::new(),
                compile_ms: 0,
                cached: false,
                error: Some(format!("Failed to read {}: {}", dynlib_path.display(), e)),
            }
        }
    };
    match xray_core::compiler::compile_evaluator(&user_code, cache_dir) {
        Ok(cr) => CompileOnlyOutput {
            so_path: cr.so_path.to_string_lossy().to_string(),
            compile_ms: cr.compile_ms,
            cached: cr.cached,
            error: None,
        },
        Err(e) => CompileOnlyOutput {
            so_path: String::new(),
            compile_ms: 0,
            cached: false,
            error: Some(format!("{}", e)),
        },
    }
}

fn main() {
    let wall_start = Instant::now();
    let args: Vec<String> = std::env::args().skip(1).collect();

    // Story #1811 (S5, AC1/AC2): `--compile-only --dynlib <path.rs>` --
    // compiles a raw evaluator source (legacy OR graph-mode, mode-agnostic)
    // via the real compile_evaluator() and reports the resulting `.so`
    // path, WITHOUT the legacy default path's follow-on `DynlibEvaluator::
    // load` (which would reject a graph-mode-only artifact). Mirrors
    // `--print-cache-identity`'s early-exit placement and the ONE-flag
    // simplicity of a bridge subcommand. ALWAYS exits 0 with a JSON report
    // on stdout for a legitimate outcome (successful compile OR a real
    // `CompileError`/unreadable source, both carried in `error`) -- exit 1
    // is reserved for a malformed invocation (missing `--dynlib`).
    if args.first().map(|s| s.as_str()) == Some("--compile-only") {
        let dynlib_path = match parse_compile_only_args(&args[1..]) {
            Ok(p) => p,
            Err(msg) => {
                eprintln!("Error: {}", msg);
                std::process::exit(1);
            }
        };
        let cache_dir = xray_core::cache::get_cache_dir();
        let output = run_compile_only(&dynlib_path, &cache_dir);
        match serde_json::to_string(&output) {
            Ok(json) => println!("{}", json),
            Err(e) => eprintln!("Error: failed to serialize CompileOnlyOutput: {}", e),
        }
        std::process::exit(0);
    }

    // Bug #1784: early-exit subcommand -- reads evaluator source from stdin,
    // prints its cache identity, and exits. No compilation, no file I/O
    // beyond stdin/stdout, near-instant.
    //
    // H9 (consolidated review, Issue #1811/Bug #1812): an optional trailing
    // `--graph-mode` flag selects the graph-mode-aware identity
    // (`cache_identity_info_graph`) instead of the legacy one -- callers
    // computing a cache identity for a graph-mode evaluator MUST pass this,
    // or the identity they compute can never match what compile_evaluator()
    // actually uses as the real .so filename.
    if args.first().map(|s| s.as_str()) == Some("--print-cache-identity") {
        use std::io::Read as _;
        let graph_mode = args.get(1).map(|s| s.as_str()) == Some("--graph-mode");
        let mut user_code = String::new();
        if let Err(e) = std::io::stdin().read_to_string(&mut user_code) {
            eprintln!("Error: failed to read evaluator source from stdin: {}", e);
            std::process::exit(1);
        }
        print!("{}", format_cache_identity_output(&user_code, graph_mode));
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

    // Story #1811 (S5, AC1): `--build-graph --repo-root <path> --files-from
    // <path> --dylib <path> --graph-out <path> [--facts-out <path>]` --
    // the one genuinely missing Rust piece: builds and persists a real
    // multi-file graph via `build_repo_graph`, so a subsequent
    // `--analyze-graph`/`--refine` invocation has a `--graph-in` to read.
    // Mirrors `--analyze-graph`/`--refine`'s exact exit-code convention:
    // ALWAYS exits 0 for a legitimate terminal `BuildGraphStatus` reported
    // via JSON on stdout (including `RepoRootInvalid`/`LoadFailed`/
    // `FileIdCollision`/`GraphWriteFailed`/`FactsWriteFailed`) -- exiting 1
    // is reserved for a malformed invocation (missing required flags, or
    // an unreadable `--files-from` list).
    if args.first().map(|s| s.as_str()) == Some("--build-graph") {
        let parsed = match parse_build_graph_args(&args[1..]) {
            Ok(p) => p,
            Err(msg) => {
                eprintln!("Error: {}", msg);
                std::process::exit(1);
            }
        };
        let files_from_path = parsed.files_from.to_string_lossy().to_string();
        // R3-2 (Codex re-review, ROUND 3): read_file_list_capped stops
        // collecting at GRAPH_INDEX_MAX_FILES rather than fully
        // materializing an unbounded --files-from list into memory first.
        let (repo_relative_paths, files_from_truncated) =
            match read_file_list_capped(&files_from_path, GRAPH_INDEX_MAX_FILES) {
                Ok(result) => result,
                Err(msg) => {
                    eprintln!("Error: {}", msg);
                    std::process::exit(1);
                }
            };
        let mut report = run_build_graph(
            &parsed.repo_root,
            &repo_relative_paths,
            &parsed.dylib,
            &parsed.graph_out,
            parsed.facts_out.as_deref(),
        );
        // The file-list read itself may have truncated BEFORE
        // run_build_graph ever saw the full path count -- its own
        // internal max_files check cannot detect that independently once
        // the list handed to it is already capped, so surface it here.
        if files_from_truncated {
            if let Some(result) = report.result.as_mut() {
                result.truncated_by_max_files = true;
                result.fact_graph_complete = false;
            }
        }
        match serde_json::to_string(&report) {
            Ok(json) => println!("{}", json),
            Err(e) => eprintln!("Error: failed to serialize BuildGraphReport: {}", e),
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
                        error_kind: None,
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
        Err(compile_err) => {
            if json_output {
                let error_kind = match compile_err.kind {
                    xray_core::compiler::CompileErrorKind::Compile => "compile",
                    xray_core::compiler::CompileErrorKind::Infrastructure => "infrastructure",
                };
                let out = JsonOutput {
                    findings: vec![],
                    files_parsed: 0,
                    files_errored: 0,
                    parse_scan_ms: 0,
                    compile_ms: 0,
                    cached: false,
                    error: Some(format!("{}", compile_err)),
                    error_kind: Some(error_kind.to_string()),
                    debug_messages: vec![],
                };
                print_json_output(&out);
                // Bug #1827 (primary fix): a compile/read/load failure at
                // this point is a LEGITIMATE terminal outcome, fully
                // reported via the JSON `error`/`error_kind` fields on
                // stdout -- mirrors `--compile-only`'s own established
                // contract (see its doc comment above) exactly: ALWAYS
                // exits 0 with a JSON report for a legitimate outcome in
                // JSON mode; exit 1 is reserved for a malformed
                // invocation, which this branch can never be (it is only
                // reached once `--dynlib` was successfully parsed and a
                // genuine read/compile/load attempt was made). Before
                // this fix, JSON mode unconditionally exited 1 here
                // regardless of outcome -- the exact self-inflicted
                // inconsistency between `--json` and `--compile-only`
                // that caused Bug #1827: the Python caller's H12-safe
                // "never trust a non-zero exit's stdout" rule (Issue
                // #1811/Bug #1812) then discarded the real diagnostic
                // already sitting on stdout, reporting nothing but a
                // contentless "exited with code 1: ".
                std::process::exit(0);
            }
            // Human-readable mode keeps its original exit(1) contract --
            // error already printed inside build_dynlib_evaluators /
            // read_evaluator_source / compile_and_load_evaluator.
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
                    error_kind: None,
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
///
/// Bug #1827: both failure branches here are Infrastructure-kind -- this
/// path is a per-invocation temp file the SERVER writes (RustNativeBackend's
/// `_write_invoke_temp_files`) immediately before spawning xray-cli; its
/// absence or unreadability is a filesystem/process problem, never a fault
/// in the user's own evaluator source text.
fn read_evaluator_source(
    eval_path: &str,
    json_output: bool,
) -> Result<String, xray_core::compiler::CompileError> {
    let path = PathBuf::from(eval_path);
    if !path.exists() {
        let msg = format!("Evaluator file not found: {}", eval_path);
        if !json_output {
            eprintln!("Error: {}", msg);
        }
        return Err(xray_core::compiler::CompileError {
            message: msg,
            details: vec![],
            kind: xray_core::compiler::CompileErrorKind::Infrastructure,
        });
    }

    match std::fs::read_to_string(&path) {
        Ok(c) => Ok(c),
        Err(e) => {
            let msg = format!("Failed to read {}: {}", eval_path, e);
            if !json_output {
                eprintln!("Error: {}", msg);
            }
            Err(xray_core::compiler::CompileError {
                message: msg,
                details: vec![],
                kind: xray_core::compiler::CompileErrorKind::Infrastructure,
            })
        }
    }
}

/// Compiles `user_code` and loads the resulting dynamic library, printing
/// progress/timing unless `json_output`. Mirrors the original inline logic
/// of `build_dynlib_evaluators` before it was split for readability.
///
/// Bug #1827: `compile_evaluator`'s own `CompileError` (already carrying
/// the correct `.kind` -- see compiler.rs) is propagated UNCHANGED, never
/// re-stringified. A post-compile `DynlibEvaluator::load` failure is
/// Infrastructure-kind -- the artifact compiled successfully, so a load
/// failure here means an ABI/toolchain-drift or engine bug, not a problem
/// in the user's source.
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
            if !json_output {
                eprintln!("\n=== Evaluator Error ===\n{}", e);
            }
            return Err(e);
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
        xray_core::compiler::CompileError {
            message: msg,
            details: vec![],
            kind: xray_core::compiler::CompileErrorKind::Infrastructure,
        }
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

    /// R2-6 (Codex re-review): `run_build_graph` used `IndexBudget::
    /// unlimited()` and `max_files: None`, so a whole-repo request could
    /// allocate candidate paths and graph state with NO graph-specific
    /// bound -- an exhaustion risk at ~900-repo fleet scale.
    ///
    /// R3-5 (Codex re-review, ROUND 3): the ORIGINAL version of this test
    /// only asserted `budget != IndexBudget::unlimited()` and `max_files.
    /// is_some()` -- tautological, since ANY finite value (even an
    /// accidentally tiny or huge one) would satisfy both. Asserts the
    /// ACTUAL configured VALUES instead, via `IndexBudget`'s public
    /// `is_exceeded_by`/`max_candidates_per_reference` accessors (its
    /// fields are private).
    #[test]
    fn run_build_graph_options_are_finitely_bounded_not_unlimited() {
        let options = build_graph_index_options();

        assert_eq!(
            options.max_files,
            Some(GRAPH_INDEX_MAX_FILES),
            "run_build_graph's max_files must equal the configured constant"
        );
        assert!(
            !options.budget.is_exceeded_by(GRAPH_INDEX_MAX_TOTAL_CANDIDATES),
            "the budget must NOT be exceeded at exactly its configured ceiling"
        );
        assert!(
            options.budget.is_exceeded_by(GRAPH_INDEX_MAX_TOTAL_CANDIDATES + 1),
            "the budget MUST be exceeded one candidate past its configured ceiling"
        );
        assert_eq!(
            options.budget.max_candidates_per_reference(),
            GRAPH_INDEX_MAX_CANDIDATES_PER_REFERENCE,
            "the per-reference cap must equal the configured constant"
        );
    }

    /// R3-5 (Codex re-review, ROUND 3): proves the SECOND half of the
    /// finite-budget contract -- not just that the configured values are
    /// correct, but that hitting `max_files` during a REAL build actually
    /// truncates the file set AND downgrades `fact_graph_complete`, via
    /// the real `build_repo_graph` pipeline (real compiled evaluator,
    /// real on-disk files).
    ///
    /// Uses `build_graph_index_options().budget` (the REAL production
    /// IndexBudget, tying this test to the actual configured ceiling)
    /// but overrides `max_files` to `TEST_MAX_FILES_CAP` rather than the
    /// real 50,000 -- creating 50,001 real files would make this test
    /// impractically slow; the truncation MECHANISM itself
    /// (`repo_relative_paths.len() > limit`) is exercised identically
    /// regardless of the ceiling's magnitude.
    const TEST_MAX_FILES_CAP: usize = 2;

    #[test]
    fn graph_index_options_max_files_cap_truncates_real_build_and_downgrades_completeness() {
        use tempfile::TempDir;
        use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};

        let dir = TempDir::new().unwrap();
        std::fs::write(dir.path().join("A.java"), "class A {}\n").unwrap();
        std::fs::write(dir.path().join("B.java"), "class B {}\n").unwrap();
        std::fs::write(dir.path().join("C.java"), "class C {}\n").unwrap();

        let cr = xray_core::compiler::compile_evaluator(
            minimal_graph_mode_evaluator_source(),
            dir.path(),
        )
        .expect("must compile");
        let evaluator =
            xray_core::dynlib::GraphDynlibEvaluator::load(&cr.so_path).expect("must load");
        let collector = DylibFactCollector { evaluator: &evaluator };

        let small_cap_options = RepoIndexOptions {
            budget: build_graph_index_options().budget,
            max_files: Some(TEST_MAX_FILES_CAP),
        };
        let paths = vec!["A.java".to_string(), "B.java".to_string(), "C.java".to_string()];
        let result = build_repo_graph(dir.path(), &paths, &small_cap_options, &collector)
            .expect("no file_id collisions among 3 distinct real files");

        assert!(
            result.truncated_by_max_files,
            "3 real files against a cap of {TEST_MAX_FILES_CAP} must report truncated_by_max_files=true"
        );
        assert!(
            !result.fact_graph_complete,
            "a truncated build must downgrade fact_graph_complete to false"
        );
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

    /// Consolidated review finding H10 (Issue #1811/Bug #1812, Codex): a
    /// `--facts-in` file that EXISTS but is malformed (truncated/corrupt
    /// JSON) must NOT silently degrade to an empty `FactIndex` with only a
    /// stderr warning -- that makes a real read/parse failure
    /// indistinguishable from "this build genuinely collected no facts",
    /// exactly the ambiguity `read_facts_file`'s own doc comment says
    /// callers must not introduce. A malformed facts file must report an
    /// explicit `AnalyzeStatus::FactsInvalid`, never `RanOk`.
    #[test]
    fn run_analyze_graph_reports_facts_invalid_for_malformed_facts_in_file() {
        use tempfile::TempDir;
        use xray_core::graph::analyze::result::AnalyzeStatus;

        let dir = TempDir::new().unwrap();
        let (graph_path, _b_symbol) = write_small_graph_file(dir.path());

        let facts_path = dir.path().join("facts.json");
        std::fs::write(&facts_path, b"this is not valid JSON at all {{{").unwrap();

        let user_code = "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n    Vec::new()\n}\nfn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {\n    GraphResult::default()\n}\n";
        let cr = xray_core::compiler::compile_evaluator(user_code, dir.path()).expect("must compile");

        let report = run_analyze_graph(&graph_path, &cr.so_path, Some(&facts_path));
        assert_eq!(
            report.status,
            AnalyzeStatus::FactsInvalid,
            "a malformed --facts-in file must report FactsInvalid, never silently succeed"
        );
        assert!(report.result.is_none(), "FactsInvalid must carry no result");
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

    // --- Story #1811 (S5, AC1/AC2): `--compile-only` bridges the raw `.rs`
    // evaluator source Python holds to the pre-compiled `.so` path
    // `--build-graph`/`--analyze-graph`/`--refine` all require via
    // `--dylib`. RED phase: `run_compile_only` does not exist yet.

    /// A well-formed graph-mode evaluator source must compile successfully
    /// and report a REAL `.so` path that exists on disk -- mode-agnostic:
    /// `compiler::compile_evaluator` itself does not require the legacy
    /// `xray_evaluate_node` export, unlike the default legacy scan path's
    /// `compile_and_load_evaluator`, which would reject this exact source.
    #[test]
    fn run_compile_only_returns_so_path_on_successful_compile() {
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let src_path = dir.path().join("eval.rs");
        std::fs::write(&src_path, minimal_graph_mode_evaluator_source()).unwrap();

        let output = run_compile_only(&src_path, dir.path());
        assert!(output.error.is_none(), "a well-formed evaluator must compile without error: {:?}", output.error);
        let so_path = std::path::PathBuf::from(&output.so_path);
        assert!(so_path.exists(), "the reported so_path must be a real file on disk: {}", output.so_path);
    }

    /// Malformed Rust source (neither `evaluate_node` nor `collect_facts`+
    /// `analyze_graph`) must report a structured `error`, never panic and
    /// never a bare empty `so_path` with no explanation.
    #[test]
    fn run_compile_only_reports_an_error_for_malformed_rust_source() {
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let src_path = dir.path().join("eval.rs");
        std::fs::write(&src_path, "this is not a valid evaluator at all\n").unwrap();

        let output = run_compile_only(&src_path, dir.path());
        assert!(output.error.is_some(), "malformed source must report a structured error, not silently succeed");
        assert!(output.so_path.is_empty());
    }

    // --- Bug #1784: --print-cache-identity bridges Python to the ONE
    // shared Rust identity implementation (cache_identity_info) ---

    #[test]
    fn test_format_cache_identity_output_contains_all_four_fields() {
        let user_code = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }";
        let output = format_cache_identity_output(user_code, false);
        assert!(output.contains("identity="), "output must contain identity=: {}", output);
        assert!(output.contains("source_hash="), "output must contain source_hash=: {}", output);
        assert!(output.contains("abi_version="), "output must contain abi_version=: {}", output);
        assert!(output.contains("rustc_version="), "output must contain rustc_version=: {}", output);
    }

    /// Consolidated review finding H9 (Issue #1811/Bug #1812, Codex): the
    /// CLI-level `--print-cache-identity` bridge must be mode-aware --
    /// `format_cache_identity_output(user_code, graph_mode=true)` must
    /// report EXACTLY the identity `compile_evaluator` actually uses for a
    /// graph-mode evaluator (verified via the real compiled `.so`
    /// filename), not the legacy-assembly identity.
    #[test]
    fn format_cache_identity_output_graph_mode_matches_real_compiled_identity() {
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
"#;
        let compiled = xray_core::compiler::compile_evaluator(user_code, dir.path())
            .expect("a genuine graph-mode evaluator must compile successfully");
        let real_identity = compiled
            .so_path
            .file_stem()
            .and_then(|s| s.to_str())
            .expect("so_path must have a valid file stem");

        let output = format_cache_identity_output(user_code, true);
        let reported_identity = output
            .lines()
            .find_map(|line| line.strip_prefix("identity="))
            .expect("output must contain an identity= line");

        assert_eq!(
            reported_identity, real_identity,
            "graph-mode --print-cache-identity must report the SAME \
             identity compile_evaluator actually used for the real .so"
        );
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
            error_kind: None,
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
            error_kind: None,
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

    // --- Story #1811 (S5, AC1): `--build-graph` subcommand ---
    //
    // RED phase: `parse_build_graph_args` does not exist yet.

    /// `--build-graph --repo-root <path> --files-from <path> --dylib <path>
    /// --graph-out <path> [--facts-out <path>]`: all five flags round-trip,
    /// `--facts-out` is the only optional one.
    #[test]
    fn parse_build_graph_args_extracts_all_required_flags_and_optional_facts_out() {
        let without_facts_out = sv(&[
            "--repo-root", "/repo", "--files-from", "/tmp/list.txt", "--dylib", "/tmp/e.so",
            "--graph-out", "/tmp/g.bin",
        ]);
        let parsed = parse_build_graph_args(&without_facts_out).expect("all required flags present must parse");
        assert_eq!(parsed.repo_root, PathBuf::from("/repo"));
        assert_eq!(parsed.files_from, PathBuf::from("/tmp/list.txt"));
        assert_eq!(parsed.dylib, PathBuf::from("/tmp/e.so"));
        assert_eq!(parsed.graph_out, PathBuf::from("/tmp/g.bin"));
        assert_eq!(parsed.facts_out, None, "facts-out must be optional");

        let with_facts_out = sv(&[
            "--repo-root", "/repo", "--files-from", "/tmp/list.txt", "--dylib", "/tmp/e.so",
            "--graph-out", "/tmp/g.bin", "--facts-out", "/tmp/f.json",
        ]);
        let parsed = parse_build_graph_args(&with_facts_out).expect("all required flags plus facts-out must parse");
        assert_eq!(parsed.facts_out, Some(PathBuf::from("/tmp/f.json")));
    }

    /// Every one of the FOUR required `--build-graph` flags must be
    /// individually required -- mirrors `parse_refine_args_errors_when_any_
    /// required_flag_is_missing`'s exact structure.
    #[test]
    fn parse_build_graph_args_errors_when_any_required_flag_is_missing() {
        let full = sv(&[
            "--repo-root", "/repo", "--files-from", "/tmp/list.txt", "--dylib", "/tmp/e.so",
            "--graph-out", "/tmp/g.bin",
        ]);
        assert!(parse_build_graph_args(&full).is_ok(), "fixture sanity: the full flag set must parse");

        let without_repo_root =
            sv(&["--files-from", "/tmp/list.txt", "--dylib", "/tmp/e.so", "--graph-out", "/tmp/g.bin"]);
        assert!(parse_build_graph_args(&without_repo_root).is_err(), "--repo-root is required");

        let without_files_from = sv(&["--repo-root", "/repo", "--dylib", "/tmp/e.so", "--graph-out", "/tmp/g.bin"]);
        assert!(parse_build_graph_args(&without_files_from).is_err(), "--files-from is required");

        let without_dylib = sv(&["--repo-root", "/repo", "--files-from", "/tmp/list.txt", "--graph-out", "/tmp/g.bin"]);
        assert!(parse_build_graph_args(&without_dylib).is_err(), "--dylib is required");

        let without_graph_out = sv(&["--repo-root", "/repo", "--files-from", "/tmp/list.txt", "--dylib", "/tmp/e.so"]);
        assert!(parse_build_graph_args(&without_graph_out).is_err(), "--graph-out is required");
    }

    /// Test-only fixture shared by every `run_build_graph` test below: a
    /// minimal real evaluator source that compiles successfully (just
    /// enough for `GraphDynlibEvaluator::load` to succeed) so `run_build_
    /// graph` has something to load.
    fn minimal_graph_mode_evaluator_source() -> &'static str {
        "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }\n\
         fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }\n"
    }

    /// RED phase: `run_build_graph` does not exist yet. A dylib that fails
    /// to load (missing entirely) must report `BuildGraphStatus::
    /// LoadFailed`, never panic and never a bare `Ok` with an empty graph.
    #[test]
    fn run_build_graph_reports_load_failed_for_a_missing_dylib() {
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        std::fs::write(dir.path().join("A.java"), "class A {}\n").unwrap();
        let missing_dylib = dir.path().join("does_not_exist.so");
        let graph_out = dir.path().join("graph.bin");

        let report =
            run_build_graph(dir.path(), &[PathBuf::from("A.java")], &missing_dylib, &graph_out, None);
        assert_eq!(report.status, BuildGraphStatus::LoadFailed);
        assert!(report.result.is_none());
        assert!(!graph_out.exists(), "no graph file must be written on a load failure");
    }

    /// A `--repo-root` that does not exist/cannot canonicalize must report
    /// `BuildGraphStatus::RepoRootInvalid` -- NEVER panic. `build_repo_
    /// graph` itself calls `.expect(..)` on `repo_root.canonicalize()`, so
    /// `run_build_graph` MUST guard this before delegating, or an invalid
    /// `--repo-root` would crash the whole process instead of reporting a
    /// clean JSON status (Rule 13, anti-silent-failure -- and here, also
    /// anti-crash).
    #[test]
    fn run_build_graph_reports_repo_root_invalid_instead_of_panicking() {
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let cr = xray_core::compiler::compile_evaluator(minimal_graph_mode_evaluator_source(), dir.path())
            .expect("must compile");

        let nonexistent_repo_root = dir.path().join("does_not_exist_dir");
        let graph_out = dir.path().join("graph.bin");

        let report = run_build_graph(
            &nonexistent_repo_root,
            &[PathBuf::from("A.java")],
            &cr.so_path,
            &graph_out,
            None,
        );
        assert_eq!(report.status, BuildGraphStatus::RepoRootInvalid);
        assert!(report.result.is_none());
    }

    /// `--build-graph` must also write a REAL `--facts-out` file when asked
    /// -- readable back by `read_facts_file` (the same reader `--analyze-
    /// graph --facts-in` uses), proving `write_facts_file` is genuinely
    /// wired here, never silently skipped.
    #[test]
    fn run_build_graph_writes_a_real_facts_out_file_when_requested() {
        use tempfile::TempDir;
        use xray_core::graph::user_facts::read_facts_file;

        let dir = TempDir::new().unwrap();
        std::fs::write(dir.path().join("A.java"), "class A {}\n").unwrap();

        let user_code = "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n\
             vec![UserFact { kind: \"todo\".to_string(), line: node.start_line, message: \"m\".to_string(), custom_key: None }]\n\
             }\n\
             fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }\n";
        let cr = xray_core::compiler::compile_evaluator(user_code, dir.path()).expect("must compile");

        let graph_out = dir.path().join("graph.bin");
        let facts_out = dir.path().join("facts.json");
        let report = run_build_graph(
            dir.path(),
            &[PathBuf::from("A.java")],
            &cr.so_path,
            &graph_out,
            Some(&facts_out),
        );
        assert_eq!(report.status, BuildGraphStatus::Ok);
        assert!(facts_out.exists(), "--facts-out must have been written to disk");
        read_facts_file(&facts_out).expect("the written facts file must be readable back");
    }

    /// Consolidated review finding C2 (Issue #1811/Bug #1812), step 2:
    /// `--build-graph`'s `BuildGraphResult` must surface the new
    /// `RepoIndexResult::files_with_unsupported_language` counter verbatim,
    /// exactly like the six pre-existing degradation counters, so a Python
    /// (or any other non-Java) file in the candidate set is visible to the
    /// MCP/REST caller instead of silently vanishing from every report.
    #[test]
    fn run_build_graph_surfaces_files_with_unsupported_language_counter() {
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        std::fs::write(dir.path().join("A.java"), "class A { void run() {} }\n").unwrap();
        std::fs::write(dir.path().join("script.py"), "def totally_unused():\n    pass\n").unwrap();

        let minimal_cr = xray_core::compiler::compile_evaluator(
            minimal_graph_mode_evaluator_source(),
            dir.path(),
        )
        .expect("must compile");
        let graph_out = dir.path().join("graph.bin");
        let report = run_build_graph(
            dir.path(),
            &[PathBuf::from("A.java"), PathBuf::from("script.py")],
            &minimal_cr.so_path,
            &graph_out,
            None,
        );

        assert_eq!(report.status, BuildGraphStatus::Ok);
        let result = report.result.expect("Ok must carry a result");
        assert_eq!(
            result.files_with_unsupported_language, 1,
            "the .py file (no graph extractor) must be surfaced by --build-graph's report"
        );
        assert!(
            !result.fact_graph_complete,
            "a non-Java file in the candidate set must flip fact_graph_complete to false"
        );
    }

    /// Test-only fixture shared by the cross-file discriminating test below:
    /// two real on-disk Java files where `A.java` calls `helper()`, defined
    /// ONLY in `B.java` -- `helper()` has no caller inside its own file, so
    /// only a graph spanning BOTH files can see it is genuinely referenced.
    fn write_cross_file_java_fixture(dir: &std::path::Path) {
        std::fs::write(dir.join("A.java"), "class A { void run() { helper(); } }\n").unwrap();
        std::fs::write(dir.join("B.java"), "class B { void helper() {} }\n").unwrap();
    }

    /// Reads a graph file back and returns its REAL `symbol_count()` -- used
    /// to derive the exact dense-id scan bound the cross-file test's
    /// `analyze_graph` evaluator needs, rather than a guessed magic number
    /// that could under-scan and produce a false negative.
    fn graph_symbol_count(graph_path: &std::path::Path) -> usize {
        xray_core::graph::csr::wire::read_graph_file(graph_path)
            .expect("a graph file this same test just wrote via write_graph_file must read back")
            .symbol_count()
    }

    /// Builds the cross-file fixture, drives `run_build_graph` over it with
    /// a minimal evaluator, and asserts the build itself succeeded cleanly
    /// -- factored out of the discriminating test below to keep it under
    /// the project's per-function line budget. Returns the written graph
    /// file's path.
    fn build_cross_file_fixture_graph(dir: &std::path::Path) -> PathBuf {
        write_cross_file_java_fixture(dir);
        let minimal_cr = xray_core::compiler::compile_evaluator(minimal_graph_mode_evaluator_source(), dir)
            .expect("must compile");
        let graph_out = dir.join("graph.bin");
        let build_report = run_build_graph(
            dir,
            &[PathBuf::from("A.java"), PathBuf::from("B.java")],
            &minimal_cr.so_path,
            &graph_out,
            None,
        );
        assert_eq!(build_report.status, BuildGraphStatus::Ok);
        let build_result = build_report.result.expect("Ok must carry a result");
        assert!(build_result.fact_graph_complete, "a clean two-file build must be complete");
        graph_out
    }

    /// Compiles a real graph-mode evaluator whose `analyze_graph` scans
    /// dense ids `0..symbol_count` (the graph's OWN real `symbol_count()`,
    /// never a guessed magic number) and records, for every symbol
    /// `is_definitely_dead_code` finds `Some(false)` (referenced, not
    /// dead), a `ReduceFinding` carrying that symbol's REAL `signature_
    /// for(..)` text -- so a caller can identify which SPECIFIC symbol was
    /// found, not merely that some symbol in some file was.
    fn compile_not_dead_scan_evaluator(dir: &std::path::Path, symbol_count: usize) -> xray_core::compiler::CompileResult {
        let code = format!(
            "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{ Vec::new() }}\n\
             fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{\n\
             let mut result = GraphResult::default();\n\
             let mut i: u32 = 0;\n\
             while i < {symbol_count}u32 {{\n\
             if let Some(sym) = g.resolve_symbol(i) {{\n\
             if g.is_definitely_dead_code(i) == Some(false) {{\n\
             let sig = g.signature_for(i).unwrap_or(\"\").to_string();\n\
             result.findings.push(ReduceFinding {{ pattern: \"not_dead\".to_string(), message: sig.clone(), involved: vec![sym], signatures: vec![sig] }});\n\
             }}\n\
             }}\n\
             i += 1;\n\
             }}\n\
             result\n\
             }}\n"
        );
        xray_core::compiler::compile_evaluator(&code, dir).expect("must compile")
    }

    /// THE central end-to-end proof of `--build-graph`'s wiring, and the
    /// story's own required discriminating test: `B.java`'s `helper()` has
    /// NO caller inside its own file -- a single-file (legacy) scan of
    /// `B.java` alone would report it dead. Only a graph spanning BOTH
    /// files can see the real caller in `A.java` and correctly report it
    /// as referenced. `run_build_graph` drives the real `build_repo_graph`
    /// and `write_graph_file` pipeline; the resulting graph file is then
    /// read back by the PRE-EXISTING `run_analyze_graph` (AC7) -- chaining
    /// `--build-graph` into `--analyze-graph` is the real, wired,
    /// cross-command pipeline a caller uses.
    #[test]
    fn build_graph_then_analyze_graph_finds_cross_file_reference() {
        use tempfile::TempDir;
        use xray_core::graph::analyze::result::AnalyzeStatus;
        use xray_core::graph::identity::file_id;

        // `identity::SymbolId`'s own doc comment defines its encoding as
        // `(file_id << 32) | local_index` -- named here (rather than a bare
        // `>> 32`) so the shift width is self-explaining at its one call
        // site below. Mirrors the identical shift already used in
        // production code at `graph::refine::refine_symbols_to_files`.
        const SYMBOL_ID_FILE_ID_SHIFT_BITS: u32 = 32;

        let dir = TempDir::new().unwrap();
        let graph_out = build_cross_file_fixture_graph(dir.path());

        let bound = graph_symbol_count(&graph_out);
        let scan_cr = compile_not_dead_scan_evaluator(dir.path(), bound);

        let analyze_report = run_analyze_graph(&graph_out, &scan_cr.so_path, None);
        assert_eq!(analyze_report.status, AnalyzeStatus::RanOk);
        let analyze_result = analyze_report.result.expect("RanOk must carry a result");

        let b_file_id = file_id("B.java");
        let b_findings: Vec<_> = analyze_result
            .findings
            .iter()
            .filter(|f| {
                f.involved.iter().any(|&sym| (sym >> SYMBOL_ID_FILE_ID_SHIFT_BITS) as u32 == b_file_id)
            })
            .collect();
        assert_eq!(
            b_findings.len(),
            1,
            "exactly one B.java symbol (helper()) must be referenced -- class B itself has no \
             caller anywhere (no `new B()`), so it must remain dead and absent from findings. \
             all findings: {:?}",
            analyze_result.findings
        );
        assert!(
            b_findings[0].message.contains("helper"),
            "the one referenced B.java symbol must be helper() by name (proven via its real \
             signature_for(..) text), not merely some unidentified symbol in that file: {:?}",
            b_findings[0]
        );
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

    // --- R3-2 (Codex re-review, ROUND 3): --build-graph must not fully
    // materialize an unbounded --files-from list before any limit engages.
    // ---

    #[test]
    fn test_read_file_list_capped_stops_before_reading_corrupt_line_beyond_cap() {
        // Real, deterministic proof of "stops reading early": the 3rd
        // line is INVALID UTF-8. If the function ever tried to DECODE it
        // as text (even just to check "is this a real path"), it would
        // hit a UTF-8 error. Truncation is detected via a RAW BYTE peek
        // instead (never decoding), so this must succeed with exactly the
        // 2 valid paths plus truncated=true -- proving the corrupt line
        // was never interpreted as text.
        let dir = std::env::temp_dir();
        let path = dir.join(format!(
            "xray_cli_test_read_file_list_capped_{}.txt",
            std::process::id()
        ));
        let mut content: Vec<u8> = Vec::new();
        content.extend_from_slice(b"/a/One.java\n/a/Two.java\n");
        content.extend_from_slice(&[0xFF, 0xFE, b'\n']); // invalid UTF-8 line
        std::fs::write(&path, &content).unwrap();

        let result = read_file_list_capped(path.to_str().unwrap(), 2);

        // Best-effort cleanup (mirrors the identical `.ok()` convention
        // used by the two `read_file_list` tests directly above): a
        // leftover tempfile here would only affect this process's own
        // /tmp, never test correctness, and the path is PID-scoped so it
        // cannot collide across concurrent test runs.
        std::fs::remove_file(&path).ok();

        let (files, truncated) =
            result.expect("must succeed -- the corrupt 3rd line must never be decoded as text");
        assert_eq!(files, vec![PathBuf::from("/a/One.java"), PathBuf::from("/a/Two.java")]);
        assert!(truncated, "must report that more content existed beyond the cap");
    }

    /// Bug #1814 test helper: writes `content` to a uniquely-named temp
    /// file, runs `read_file_list_capped` against it with `max_lines`,
    /// removes the temp file (asserting cleanup itself succeeded, never
    /// silently discarded), and returns the raw `Result` for the caller
    /// to assert on.
    fn read_file_list_capped_via_temp_file(
        name_suffix: &str,
        content: &str,
        max_lines: usize,
    ) -> Result<(Vec<PathBuf>, bool), String> {
        let dir = std::env::temp_dir();
        let path = dir.join(format!(
            "xray_cli_test_1814_{}_{}.txt",
            name_suffix,
            std::process::id()
        ));
        std::fs::write(&path, content).expect("failed to write test fixture file");
        let result = read_file_list_capped(path.to_str().unwrap(), max_lines);
        std::fs::remove_file(&path).expect("failed to clean up test fixture file");
        result
    }

    /// Bug #1814 test helper: `n` synthetic `/a/File{i}.java\n` lines
    /// concatenated -- the shared fixture-content builder for the
    /// boundary table below.
    fn n_valid_path_lines(n: usize) -> String {
        (0..n).map(|i| format!("/a/File{}.java\n", i)).collect()
    }

    #[test]
    fn test_read_file_list_capped_truncation_boundary_pins_real_values() {
        // Bug #1814: `truncated` must be decided by whether at least one
        // further NON-BLANK entry exists beyond the cap, not by whether
        // any bytes remain -- trailing blank lines/whitespace must NOT
        // report truncation on a genuinely complete list. Every case
        // pins the REAL expected file count and truncated value, not
        // merely "a bound exists".
        let max_lines = GRAPH_INDEX_MAX_FILES;
        let exact = n_valid_path_lines(max_lines);
        let exact_with_trailing_blanks = exact.clone() + "\n\n   \n";
        let exact_no_trailing_newline = exact.trim_end_matches('\n').to_string();
        let one_over_cap = n_valid_path_lines(max_lines + 1);
        // Bug #1822 follow-up (b): tab-only and CRLF-only trailing
        // suffixes must ALSO be classified as blank (not truncated) --
        // both '\t' and '\r' are ASCII whitespace per
        // `u8::is_ascii_whitespace()`, so this should already work, but
        // there was previously zero test proving it.
        let exact_with_tab_only_trailing = exact.clone() + "\t\t\t\n\t\n";
        let exact_with_crlf_only_trailing = exact.clone() + "\r\n\r\n";

        // (case name, file content, expected files.len(), expected truncated)
        let cases: [(&str, &str, usize, bool); 7] = [
            ("trailing_blank_lines", exact_with_trailing_blanks.as_str(), max_lines, false),
            ("one_more_real_path", one_over_cap.as_str(), max_lines, true),
            ("no_trailing_newline", exact_no_trailing_newline.as_str(), max_lines, false),
            ("fifty_thousand_and_one", one_over_cap.as_str(), max_lines, true),
            ("empty_file", "", 0, false),
            ("tab_only_trailing", exact_with_tab_only_trailing.as_str(), max_lines, false),
            ("crlf_only_trailing", exact_with_crlf_only_trailing.as_str(), max_lines, false),
        ];

        for (name, content, expected_len, expected_truncated) in cases {
            let (files, truncated) = read_file_list_capped_via_temp_file(name, content, max_lines)
                .unwrap_or_else(|e| panic!("case '{}' must succeed: {}", name, e));
            assert_eq!(files.len(), expected_len, "case '{}': unexpected file count", name);
            assert_eq!(truncated, expected_truncated, "case '{}': unexpected truncated value", name);
        }
    }

    // --- Bug #1822 Defect 2: the post-cap truncation scan must operate
    // under a FIXED total byte budget via chunked reads, never an
    // unbounded single `read_until` call. ---

    /// Test-only `Read` wrapper that records the TOTAL number of bytes
    /// actually pulled from the underlying reader across all `read()`
    /// calls -- lets a test prove directly (not just infer from an `Err`
    /// result) that `scan_trailing_content_bounded` never reads past its
    /// configured byte budget.
    struct CountingReader<R> {
        inner: R,
        total_read: usize,
    }

    impl<R: std::io::Read> std::io::Read for CountingReader<R> {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            let n = self.inner.read(buf)?;
            self.total_read += n;
            Ok(n)
        }
    }

    #[test]
    fn test_scan_trailing_content_bounded_never_reads_past_byte_budget() {
        // Discriminating construction: the suffix is 5x the budget, is
        // ALL ASCII whitespace, and contains no newline anywhere -- under
        // the OLD unbounded `read_until`-based logic this exact shape is
        // what forced a single call to consume everything in one shot
        // (the RED baseline observed the unbounded old code read a full
        // 20MB such suffix in one pass). Here we prove the NEW bounded
        // scan stops within the budget instead of reading anywhere close
        // to the full suffix.
        let budget = 8 * 1024;
        let chunk = 1024;
        let suffix = vec![b' '; budget * 5];
        let mut counting = CountingReader {
            inner: std::io::Cursor::new(suffix),
            total_read: 0,
        };

        let result = scan_trailing_content_bounded(&mut counting, budget, chunk, "/fake/path");

        assert!(
            result.is_err(),
            "budget exhausted before EOF or a non-whitespace byte must be an explicit Err, \
             never a silently-assumed false"
        );
        assert!(
            counting.total_read <= budget,
            "scan must never read more than the configured byte budget; read {} bytes against \
             an {}-byte budget",
            counting.total_read,
            budget
        );
    }

    #[test]
    fn test_read_file_list_capped_returns_err_when_trailing_scan_exceeds_byte_budget() {
        // End-to-end through the real public entry point: exactly
        // `max_lines` valid paths followed by an ALL-WHITESPACE suffix
        // (no newline) sized well beyond `TRUNCATION_SCAN_BYTE_BUDGET`.
        // The scan genuinely cannot determine, within its byte budget,
        // whether real content follows -- it must surface that as an
        // explicit Err rather than silently reporting truncated=false.
        let max_lines = 3;
        let exact = n_valid_path_lines(max_lines);
        let oversized_whitespace_suffix = " ".repeat(TRUNCATION_SCAN_BYTE_BUDGET * 3);
        let content = exact + &oversized_whitespace_suffix;

        let result =
            read_file_list_capped_via_temp_file("over_budget_whitespace", &content, max_lines);

        assert!(
            result.is_err(),
            "must return Err when the trailing-content scan exceeds its byte budget, not \
             silently report truncated=false"
        );
    }

    // --- Bug #1827 primary fix: the legacy default `--json` path's
    // evaluators_result Err arm must carry a typed CompileError (with
    // `.kind`) rather than a bare String, so main() can classify a
    // legitimate compile/read/load failure as Compile vs Infrastructure
    // and exit 0 for `--json` (mirroring `--compile-only`'s own
    // contract), instead of the old always-exit-1 + empty-stderr bug.

    /// A missing evaluator source file is an INFRASTRUCTURE problem --
    /// the server writes this temp file and hands its path to xray-cli;
    /// a missing/unreadable file at that point is never the user's own
    /// evaluator code's fault.
    #[test]
    fn read_evaluator_source_missing_file_classifies_as_infrastructure() {
        let result = read_evaluator_source("/nonexistent/path/to/eval_1827.rs", true);
        assert!(result.is_err(), "a missing file must be an error");
        let err = result.unwrap_err();
        assert_eq!(
            err.kind,
            xray_core::compiler::CompileErrorKind::Infrastructure,
            "a missing evaluator file is an infrastructure problem, never \
             the user's fault: {}",
            err.message
        );
    }

    /// A genuine rustc compile failure (real E0308 type mismatch, no
    /// mocking) through `compile_and_load_evaluator` must classify as
    /// Compile and carry the real diagnostic -- THE discriminating proof
    /// that the typed CompileError plumbing survives this call site.
    #[test]
    fn compile_and_load_evaluator_real_compile_failure_classifies_as_compile_with_diagnostic() {
        let user_code = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let x: i32 = \"not an integer\";\n    Vec::new()\n}\n";
        let result = compile_and_load_evaluator(user_code, "eval.rs", true);
        // Box<dyn Evaluator> (the Ok type) does not implement Debug, so
        // `.unwrap_err()` cannot be used here -- match instead.
        let err = match result {
            Ok(_) => panic!("a type mismatch must fail to compile"),
            Err(e) => e,
        };
        assert_eq!(
            err.kind,
            xray_core::compiler::CompileErrorKind::Compile,
            "a genuine rustc compile failure must classify as Compile: {}",
            err.message
        );
        let rendered = format!("{}", err);
        assert!(
            rendered.contains("E0308"),
            "the real rustc diagnostic must be present, not swallowed: {}",
            rendered
        );
    }
}
