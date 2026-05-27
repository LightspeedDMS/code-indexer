use crate::finding::{EvalFinding, Finding};
use crate::languages;
use crate::owned_node::OwnedNode;
use rayon::prelude::*;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use tree_sitter::Parser;
use walkdir::WalkDir;

/// Evaluators examine a single OwnedNode and return zero or more findings.
///
/// Implementations must be Send + Sync because they are shared across rayon
/// threads without per-thread cloning.
pub trait Evaluator: Send + Sync {
    fn evaluate_node(&self, node: &OwnedNode) -> Vec<EvalFinding>;
}

/// Aggregate result returned by scan_files_parallel.
pub struct ScanResult {
    pub findings: Vec<Finding>,
    pub files_parsed: usize,
    pub files_errored: usize,
    /// Wall time for the parse + scan phase in milliseconds.
    pub parse_scan_ms: u128,
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

    let mut parser = Parser::new();
    parser.set_language(&language).ok()?;

    let tree = parser.parse(&source, None)?;
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

/// Scan `files` in parallel using rayon, applying `evaluators` to each file's
/// root node.
///
/// Each rayon thread creates its own Parser (Parser is !Send), so there is no
/// cross-thread sharing of mutable parser state.
pub fn scan_files_parallel(files: &[PathBuf], evaluators: &[Box<dyn Evaluator>]) -> ScanResult {
    let files_parsed = AtomicUsize::new(0);
    let files_errored = AtomicUsize::new(0);
    let all_findings: Mutex<Vec<Finding>> = Mutex::new(Vec::new());

    let start = std::time::Instant::now();

    files.par_iter().for_each(|path| {
        // Determine language from extension — skip unsupported files
        let ext = match path.extension().and_then(|s| s.to_str()) {
            Some(e) => e.to_string(),
            None => {
                files_errored.fetch_add(1, Ordering::Relaxed);
                return;
            }
        };
        let language = match languages::language_for_extension(&ext) {
            Some(l) => l,
            None => return, // not an error, just unsupported
        };

        // Read file bytes
        let source = match std::fs::read(path) {
            Ok(s) => s,
            Err(_) => {
                files_errored.fetch_add(1, Ordering::Relaxed);
                return;
            }
        };

        // Create a thread-local parser (Parser is !Send, so we cannot share one)
        let mut parser = Parser::new();
        if parser.set_language(&language).is_err() {
            files_errored.fetch_add(1, Ordering::Relaxed);
            return;
        }

        let tree = match parser.parse(&source, None) {
            Some(t) => t,
            None => {
                files_errored.fetch_add(1, Ordering::Relaxed);
                return;
            }
        };

        let root = OwnedNode::build_from_ts_node(tree.root_node(), &source);
        let file_str = path.to_string_lossy().to_string();

        let mut local_findings = Vec::new();
        evaluate_file(&root, evaluators, &file_str, &mut local_findings);

        files_parsed.fetch_add(1, Ordering::Relaxed);

        if !local_findings.is_empty() {
            let mut lock = all_findings.lock().unwrap();
            lock.extend(local_findings);
        }
    });

    let parse_scan_ms = start.elapsed().as_millis();

    ScanResult {
        findings: all_findings.into_inner().unwrap(),
        files_parsed: files_parsed.load(Ordering::Relaxed),
        files_errored: files_errored.load(Ordering::Relaxed),
        parse_scan_ms,
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
}
