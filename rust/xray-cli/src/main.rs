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
    remaining_args: Vec<String>,
}

fn default_target() -> String {
    let home = std::env::var("HOME").unwrap_or_else(|_| std::env::current_dir()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| ".".to_string()));
    format!("{}/Dev/evolution", home)
}

fn main() {
    let wall_start = Instant::now();
    let args: Vec<String> = std::env::args().skip(1).collect();

    let parsed = parse_args(&args);
    let json_output = parsed.json_output;

    // Determine target directory (only used when --files is not provided)
    let target = std::env::var("XRAY_TARGET")
        .or_else(|_| parsed.remaining_args.first().cloned().ok_or(()))
        .unwrap_or_else(|_| default_target());

    // Collect files: either from --files list or by walking target directory
    let files: Vec<PathBuf> = if !parsed.file_list.is_empty() {
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
    let evaluators_result: Result<(Vec<Box<dyn Evaluator>>, u128, bool), String> =
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
                };
                println!("{}", serde_json::to_string(&out).unwrap());
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
                };
                println!("{}", serde_json::to_string(&out).unwrap());
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

fn parse_args(args: &[String]) -> ParsedArgs {
    let mut dynlib_path = None;
    let mut json_output = false;
    let mut file_list = Vec::new();
    let mut remaining = Vec::new();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--dynlib" {
            if i + 1 < args.len() {
                dynlib_path = Some(args[i + 1].clone());
                i += 2;
                continue;
            } else {
                eprintln!("Error: --dynlib requires a path to an evaluator .rs file");
                std::process::exit(1);
            }
        }
        if args[i] == "--json" {
            json_output = true;
            i += 1;
            continue;
        }
        if args[i] == "--files" {
            i += 1;
            // Consume all subsequent args that do not start with "--" as file paths
            while i < args.len() && !args[i].starts_with("--") {
                file_list.push(args[i].clone());
                i += 1;
            }
            continue;
        }
        remaining.push(args[i].clone());
        i += 1;
    }
    ParsedArgs {
        dynlib_path,
        json_output,
        file_list,
        remaining_args: remaining,
    }
}

/// Build evaluators from a dynamic library evaluator source file.
///
/// Returns `Ok((evaluators, compile_ms, cached))` on success.
/// Returns `Err(error_message)` on compilation failure (human-readable message
/// already printed to stderr for non-JSON callers).
fn build_dynlib_evaluators(
    eval_path: &str,
    json_output: bool,
) -> Result<(Vec<Box<dyn Evaluator>>, u128, bool), String> {
    let path = PathBuf::from(eval_path);
    if !path.exists() {
        let msg = format!("Evaluator file not found: {}", eval_path);
        if !json_output {
            eprintln!("Error: {}", msg);
        }
        return Err(msg);
    }

    let user_code = match std::fs::read_to_string(&path) {
        Ok(c) => c,
        Err(e) => {
            let msg = format!("Failed to read {}: {}", eval_path, e);
            if !json_output {
                eprintln!("Error: {}", msg);
            }
            return Err(msg);
        }
    };

    let cache_dir = xray_core::cache::get_cache_dir();
    if !json_output {
        println!("Mode: dynamic library (evaluator: {})", eval_path);
        println!("Cache dir: {}", cache_dir.display());
    }

    let compile_start = Instant::now();
    let cr = match xray_core::compiler::compile_evaluator(&user_code, &cache_dir) {
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

    let evaluator = match xray_core::dynlib::DynlibEvaluator::load(&cr.so_path) {
        Ok(e) => e,
        Err(e) => {
            let msg = format!("Failed to load compiled evaluator: {}", e);
            if !json_output {
                eprintln!("Error: {}", msg);
            }
            return Err(msg);
        }
    };

    Ok((vec![Box::new(evaluator)], cr.compile_ms, cr.cached))
}
