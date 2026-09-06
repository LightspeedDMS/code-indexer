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
