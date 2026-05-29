use crate::finding::{EvalFinding, Finding};
use crate::languages;
use crate::owned_node::OwnedNode;
use rayon::prelude::*;
use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use tree_sitter::Parser;
use walkdir::WalkDir;

/// Per-thread cached Parser.
///
/// Parser is !Send so it cannot be shared across rayon threads, but it CAN
/// be reused within the same thread. Using thread_local! avoids the cost of
/// Parser::new() (and its internal allocations) for every file processed.
thread_local! {
    static THREAD_PARSER: RefCell<Parser> = RefCell::new(Parser::new());
}

/// Evaluators examine a single OwnedNode and return zero or more findings.
///
/// Implementations must be Send + Sync because they are shared across rayon
/// threads without per-thread cloning.
pub trait Evaluator: Send + Sync {
    fn evaluate_node(&self, node: &OwnedNode) -> Vec<EvalFinding>;

    /// Drain accumulated debug_log() messages from the evaluator's thread-local buffer.
    ///
    /// The default implementation returns an empty vec (zero overhead) for all
    /// built-in evaluators that do not use debug_log().  DynlibEvaluator overrides
    /// this to call the compiled evaluator's xray_drain_debug_log export.
    ///
    /// Must be called on the SAME thread as evaluate_node (thread_local! storage).
    fn drain_debug_log(&self) -> Vec<String> {
        vec![]
    }
}

/// Aggregate result returned by scan_files_parallel.
pub struct ScanResult {
    pub findings: Vec<Finding>,
    pub files_parsed: usize,
    pub files_errored: usize,
    /// Wall time for the parse + scan phase in milliseconds.
    pub parse_scan_ms: u128,
    /// Debug messages emitted by debug_log() calls across all evaluated files.
    /// Empty when no evaluator called debug_log(). Capped at 100 messages and 10KB
    /// in the preamble — the scanner accumulates all messages from all files.
    pub debug_messages: Vec<String>,
}

/// Collect all files under `dir` whose extension is in supported_extensions().
pub fn collect_files(dir: &Path) -> Vec<PathBuf> {
    let supported: std::collections::HashSet<&str> =
        languages::supported_extensions().iter().copied().collect();

    WalkDir::new(dir)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .filter(|e| {
            e.path()
                .extension()
                .and_then(|s| s.to_str())
                .map(|ext| supported.contains(ext))
                .unwrap_or(false)
        })
        .map(|e| e.into_path())
        .collect()
}

/// Parse a single file into an OwnedNode tree.
///
/// Returns None if the file cannot be read, the extension is unsupported, or
/// tree-sitter fails to produce a tree.
pub fn parse_file(path: &Path) -> Option<OwnedNode> {
    let ext = path.extension()?.to_str()?;
    let language = languages::language_for_extension(ext)?;

    let source = std::fs::read(path).ok()?;

    let tree = THREAD_PARSER.with(|p| {
        let mut parser = p.borrow_mut();
        parser.set_language(&language).ok()?;
        parser.parse(&source, None)
    })?;

    Some(OwnedNode::build_from_ts_node(tree.root_node(), &source))
}

/// Call every evaluator once with the file's root node, accumulating findings.
fn evaluate_file(
    root: &OwnedNode,
    evaluators: &[Box<dyn Evaluator>],
    file: &str,
    findings: &mut Vec<Finding>,
) {
    for eval in evaluators {
        for ef in eval.evaluate_node(root) {
            findings.push(Finding {
                pattern: ef.pattern,
                file: file.to_string(),
                line: ef.line,
                snippet: ef.snippet,
            });
        }
    }
}

/// Parse, evaluate, and drain debug messages for a single file.
///
/// Returns `None` when the file cannot be parsed (unsupported extension, I/O error,
/// parser failure).  Returns `Some((findings, debug_msgs))` on success; either vec
/// may be empty.  Must be called on the same rayon thread that will call
/// `drain_debug_log` — thread_local! storage must not cross thread boundaries.
fn process_file(
    path: &PathBuf,
    evaluators: &[Box<dyn Evaluator>],
) -> Option<(Vec<Finding>, Vec<String>)> {
    let ext = path.extension().and_then(|s| s.to_str())?.to_string();
    let language = languages::language_for_extension(&ext)?;
    let source = std::fs::read(path).ok()?;

    let tree = THREAD_PARSER.with(|p| {
        let mut parser = p.borrow_mut();
        parser.set_language(&language).ok()?;
        parser.parse(&source, None)
    })?;

    let root = OwnedNode::build_from_ts_node(tree.root_node(), &source);
    let file_str = path.to_string_lossy().to_string();
    let mut findings = Vec::new();
    evaluate_file(&root, evaluators, &file_str, &mut findings);
    let debug_msgs: Vec<String> = evaluators.iter().flat_map(|e| e.drain_debug_log()).collect();
    Some((findings, debug_msgs))
}

/// Scan `files` in parallel using rayon, applying `evaluators` to each file's
/// root node.
///
/// Each rayon thread reuses its thread-local Parser (Parser is !Send), so there
/// is no cross-thread sharing of mutable parser state, and no allocation cost
/// for Parser::new() on every file.
pub fn scan_files_parallel(files: &[PathBuf], evaluators: &[Box<dyn Evaluator>]) -> ScanResult {
    let files_parsed = AtomicUsize::new(0);
    let files_errored = AtomicUsize::new(0);
    let all_findings: Mutex<Vec<Finding>> = Mutex::new(Vec::new());
    let all_debug_messages: Mutex<Vec<String>> = Mutex::new(Vec::new());
    let start = std::time::Instant::now();

    files.par_iter().for_each(|path| match process_file(path, evaluators) {
        None => { files_errored.fetch_add(1, Ordering::Relaxed); }
        Some((findings, debug_msgs)) => {
            files_parsed.fetch_add(1, Ordering::Relaxed);
            if !findings.is_empty() { all_findings.lock().unwrap().extend(findings); }
            if !debug_msgs.is_empty() { all_debug_messages.lock().unwrap().extend(debug_msgs); }
        }
    });

    let parse_scan_ms = start.elapsed().as_millis();
    ScanResult {
        findings: all_findings.into_inner().unwrap(),
        files_parsed: files_parsed.load(Ordering::Relaxed),
        files_errored: files_errored.load(Ordering::Relaxed),
        parse_scan_ms,
        debug_messages: all_debug_messages.into_inner().unwrap(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::finding::EvalFinding;
    use std::io::Write;
    use tempfile::TempDir;

    struct AlwaysFindsEvaluator;

    impl Evaluator for AlwaysFindsEvaluator {
        fn evaluate_node(&self, node: &OwnedNode) -> Vec<EvalFinding> {
            if node.kind == "program" || node.kind == "compilation_unit" {
                vec![EvalFinding {
                    pattern: "test-hit".to_string(),
                    line: 1,
                    snippet: "root".to_string(),
                }]
            } else {
                vec![]
            }
        }
    }

    fn write_temp_file(dir: &TempDir, name: &str, content: &str) -> PathBuf {
        let path = dir.path().join(name);
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(content.as_bytes()).unwrap();
        path
    }

    #[test]
    fn test_collect_files_finds_java() {
        let dir = TempDir::new().unwrap();
        write_temp_file(&dir, "Foo.java", "class Foo {}");
        write_temp_file(&dir, "notes.txt", "ignore me");
        let files = collect_files(dir.path());
        assert_eq!(files.len(), 1);
        assert!(files[0].to_str().unwrap().ends_with(".java"));
    }

    #[test]
    fn test_collect_files_empty_dir() {
        let dir = TempDir::new().unwrap();
        let files = collect_files(dir.path());
        assert!(files.is_empty());
    }

    #[test]
    fn test_parse_file_java() {
        let dir = TempDir::new().unwrap();
        let path = write_temp_file(&dir, "Hello.java", "class Hello { }");
        let root = parse_file(&path);
        assert!(root.is_some());
        assert_eq!(root.unwrap().kind, "program");
    }

    #[test]
    fn test_parse_file_unsupported_extension() {
        let dir = TempDir::new().unwrap();
        let path = write_temp_file(&dir, "notes.txt", "hello");
        assert!(parse_file(&path).is_none());
    }

    #[test]
    fn test_scan_files_parallel_produces_findings() {
        let dir = TempDir::new().unwrap();
        write_temp_file(&dir, "A.java", "class A {}");
        write_temp_file(&dir, "B.java", "class B {}");
        let files = collect_files(dir.path());
        let evaluators: Vec<Box<dyn Evaluator>> = vec![Box::new(AlwaysFindsEvaluator)];
        let result = scan_files_parallel(&files, &evaluators);
        assert_eq!(result.files_parsed, 2);
        assert_eq!(result.files_errored, 0);
        assert_eq!(result.findings.len(), 2);
    }

    #[test]
    fn test_scan_files_parallel_empty_list() {
        let evaluators: Vec<Box<dyn Evaluator>> = vec![Box::new(AlwaysFindsEvaluator)];
        let result = scan_files_parallel(&[], &evaluators);
        assert_eq!(result.files_parsed, 0);
        assert_eq!(result.files_errored, 0);
        assert!(result.findings.is_empty());
    }

    // --- AC2/AC6: debug_messages in ScanResult ---

    struct DebugLoggingEvaluator;

    impl Evaluator for DebugLoggingEvaluator {
        fn evaluate_node(&self, _node: &OwnedNode) -> Vec<EvalFinding> {
            vec![]
        }
        fn drain_debug_log(&self) -> Vec<String> {
            vec!["test debug message".to_string()]
        }
    }

    #[test]
    fn test_scan_result_collects_debug_messages() {
        // AC2: debug messages from evaluators must appear in ScanResult.debug_messages.
        let dir = TempDir::new().unwrap();
        write_temp_file(&dir, "A.java", "class A {}");
        let files = collect_files(dir.path());
        let evaluators: Vec<Box<dyn Evaluator>> = vec![Box::new(DebugLoggingEvaluator)];
        let result = scan_files_parallel(&files, &evaluators);
        assert!(
            !result.debug_messages.is_empty(),
            "ScanResult must contain debug messages when evaluator produces them"
        );
        assert!(
            result.debug_messages.contains(&"test debug message".to_string()),
            "debug messages must include the evaluator's output: {:?}",
            result.debug_messages
        );
    }

    #[test]
    fn test_scan_result_empty_debug_messages_when_none() {
        // AC6: zero overhead — debug_messages empty when evaluator produces none.
        let dir = TempDir::new().unwrap();
        write_temp_file(&dir, "B.java", "class B {}");
        let files = collect_files(dir.path());
        let evaluators: Vec<Box<dyn Evaluator>> = vec![Box::new(AlwaysFindsEvaluator)];
        let result = scan_files_parallel(&files, &evaluators);
        assert!(
            result.debug_messages.is_empty(),
            "debug_messages must be empty when evaluator makes no debug_log calls: {:?}",
            result.debug_messages
        );
    }
}
