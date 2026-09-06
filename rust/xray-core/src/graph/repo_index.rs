//! Whole-repository indexing orchestrator (Story #1787, S2, AC10):
//! "indexing scope vs finding scope".
//!
//! **Indexing covers the WHOLE repository; findings are restricted to
//! driver-regex-matched files.** `build_repo_graph` below is the ONE place
//! that decides which files feed extraction+bind (`crate::graph::bind`) --
//! it always processes every file `repo_relative_paths` names, never a
//! subset pre-filtered down to whatever a driver regex matched for
//! reporting purposes. Restricting an already-bound graph's REFERENCES to a
//! matched subset is a separate, later concern -- conflating the two
//! (indexing only what will be reported) is exactly the bug this module
//! exists to prevent: a regex matching a call site but not the file
//! declaring its target would silently produce an empty candidate set,
//! misreporting a real in-repo symbol as "external to the repo".
//!
//! `fact_graph_complete` is `false` whenever ANY of the three triggers the
//! story names fired on this build: `max_files` truncation, a parsed file
//! whose tree carried a real syntax error (`has_error`, surfaced via
//! `crate::graph::fused::FusedFileResult::has_syntax_error` -- measured
//! SEPARATELY from files that could not be read/parsed at all), or an
//! index-budget trip (the bound graph's `AnalysisCompleteness` being
//! anything other than `Complete`). Unreadable/unsupported files are
//! counted too (`unreadable_or_unsupported_files`), but -- per the story's
//! literal wording, which names exactly three triggers -- do NOT by
//! themselves flip `fact_graph_complete`: a file with an unsupported
//! extension in an otherwise fully-indexed, in-budget repo is not a
//! degraded build.

use super::bind::{bind_with_budget, FileForBind};
use super::budget::{AnalysisCompleteness, IndexBudget};
use super::csr::CodeGraph;
use super::fused::process_file_fused;
use super::identity::file_id;
use super::user_facts::FactCollector;
use std::path::Path;

/// Configuration for one `build_repo_graph` call.
pub struct RepoIndexOptions {
    pub budget: IndexBudget,
    /// `None` means unlimited (no truncation possible).
    pub max_files: Option<usize>,
}

/// Outcome of indexing a whole repository (AC10).
pub struct RepoIndexResult {
    pub graph: CodeGraph,
    /// False whenever `max_files` truncation, a real parse error, or an
    /// index-budget trip degraded this build. See module docs.
    pub fact_graph_complete: bool,
    /// Files that parsed but whose tree carried a syntax error
    /// (`has_error`) -- measured SEPARATELY from
    /// `unreadable_or_unsupported_files`.
    pub files_with_parse_errors: usize,
    /// Files that could not be read or parsed at all (unsupported
    /// extension, I/O error, total tree-sitter failure, OR a path that
    /// escapes `repo_root` -- see `path_is_contained` below).
    pub unreadable_or_unsupported_files: usize,
    pub truncated_by_max_files: bool,
}

/// Reuses the exact same containment technique
/// `identity::repo_snapshot_identity` already established (Rule 4,
/// anti-duplication): canonicalize (resolving `..` and symlinks) and check
/// `starts_with` against the canonicalized root. A `relative_path` that
/// cannot be canonicalized (does not exist, dangling symlink, etc.) is
/// treated as not contained -- the caller folds that into "unreadable",
/// which is accurate: this function could not safely read it either way.
fn path_is_contained(canonical_repo_root: &Path, repo_root: &Path, relative_path: &str) -> bool {
    match repo_root.join(relative_path).canonicalize() {
        Ok(canonical_candidate) => canonical_candidate.starts_with(canonical_repo_root),
        Err(_) => false,
    }
}

/// Bounded loop: iterates at most `repo_relative_paths.len()` times (finite,
/// fixed at call time), further capped by `options.max_files` via
/// `Iterator::take` -- terminates the moment the shorter of the two limits
/// is reached (Rule 14, anti-unbounded-loop).
///
/// Builds a `FileForBind` for EVERY file that parses (indexing scope is the
/// WHOLE repository, per this module's docs) and binds them ALL TOGETHER --
/// there is no filtering by "will this file's references be reported" here.
pub fn build_repo_graph(
    repo_root: &Path,
    repo_relative_paths: &[String],
    options: &RepoIndexOptions,
    fact_collector: &dyn FactCollector,
) -> RepoIndexResult {
    let canonical_repo_root =
        repo_root.canonicalize().expect("repo_root must exist and be canonicalizable");
    let limit = options.max_files.unwrap_or(usize::MAX);
    let truncated_by_max_files = repo_relative_paths.len() > limit;

    let mut files_for_bind: Vec<FileForBind> = Vec::new();
    let mut files_with_parse_errors = 0usize;
    let mut unreadable_or_unsupported_files = 0usize;

    for relative_path in repo_relative_paths.iter().take(limit) {
        if !path_is_contained(&canonical_repo_root, repo_root, relative_path) {
            unreadable_or_unsupported_files += 1;
            continue;
        }
        let full_path = repo_root.join(relative_path);
        match process_file_fused(&full_path, relative_path, fact_collector) {
            None => unreadable_or_unsupported_files += 1,
            Some(fused_result) => {
                if fused_result.has_syntax_error {
                    files_with_parse_errors += 1;
                }
                if let Some(index) = fused_result.index {
                    let language = full_path
                        .extension()
                        .and_then(|e| e.to_str())
                        .unwrap_or_default()
                        .to_string();
                    files_for_bind.push(FileForBind {
                        file_id: file_id(relative_path),
                        language,
                        index,
                    });
                }
            }
        }
    }

    let graph = bind_with_budget(files_for_bind, &options.budget);
    let budget_exceeded = graph.completeness() != AnalysisCompleteness::Complete;
    let fact_graph_complete = !truncated_by_max_files && files_with_parse_errors == 0 && !budget_exceeded;

    RepoIndexResult {
        graph,
        fact_graph_complete,
        files_with_parse_errors,
        unreadable_or_unsupported_files,
        truncated_by_max_files,
    }
}

/// AC10 "finding scope": restricts which references are surfaced as
/// findings to those whose call/reference SITE lives in one of
/// `matched_file_ids` -- the driver-regex-matched file set -- while the
/// graph itself (built by `build_repo_graph` over the WHOLE repo) still
/// carries candidates resolved against declarations in every indexed file,
/// matched or not. `Reference` is `Copy`, so this returns owned values, not
/// borrows into `graph`.
///
/// Bounded loop: iterates exactly `graph.references().len()` times (finite,
/// fixed by the already-built graph) -- Rule 14, anti-unbounded-loop.
pub fn references_in_matched_files(
    graph: &CodeGraph,
    matched_file_ids: &std::collections::HashSet<u32>,
) -> Vec<super::csr::Reference> {
    graph.references().iter().filter(|r| matched_file_ids.contains(&r.file)).copied().collect()
}

#[cfg(test)]
mod tests {
    use crate::graph::budget::{AnalysisCompleteness, IndexBudget};
    use crate::graph::extract::local_index::LocalIndex;
    use crate::graph::repo_index::{build_repo_graph, RepoIndexOptions};
    use crate::graph::user_facts::{FactCollector, UserFact};
    use crate::owned_node::OwnedNode;

    struct NoOpCollector;
    impl FactCollector for NoOpCollector {
        fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
            Vec::new()
        }
    }

    fn write_java(dir: &tempfile::TempDir, name: &str, source: &str) {
        std::fs::write(dir.path().join(name), source).unwrap();
    }

    /// Happy path: two well-formed files, no `max_files` cap, an unlimited
    /// budget -- `fact_graph_complete` must be true and both parse-error /
    /// unreadable counters must be zero.
    #[test]
    fn build_repo_graph_reports_complete_when_within_budget_and_untruncated_and_error_free() {
        let dir = tempfile::tempdir().unwrap();
        write_java(&dir, "A.java", "class A { void run() { helper(); } }\n");
        write_java(&dir, "B.java", "class B { void helper() {} }\n");

        let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
        let result = build_repo_graph(
            dir.path(),
            &["A.java".to_string(), "B.java".to_string()],
            &options,
            &NoOpCollector,
        );

        assert!(result.fact_graph_complete);
        assert_eq!(result.files_with_parse_errors, 0);
        assert_eq!(result.unreadable_or_unsupported_files, 0);
        assert!(!result.truncated_by_max_files);
        assert_eq!(result.graph.completeness(), AnalysisCompleteness::Complete);
    }

    /// AC10: `max_files` truncation must set BOTH `truncated_by_max_files`
    /// and `fact_graph_complete = false`, even when every processed file is
    /// perfectly well-formed and the index budget is unlimited.
    #[test]
    fn max_files_truncation_sets_truncated_flag_and_fact_graph_complete_false() {
        let dir = tempfile::tempdir().unwrap();
        write_java(&dir, "A.java", "class A {}\n");
        write_java(&dir, "B.java", "class B {}\n");
        write_java(&dir, "C.java", "class C {}\n");

        let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: Some(2) };
        let result = build_repo_graph(
            dir.path(),
            &["A.java".to_string(), "B.java".to_string(), "C.java".to_string()],
            &options,
            &NoOpCollector,
        );

        assert!(result.truncated_by_max_files);
        assert!(!result.fact_graph_complete, "max_files truncation must flip fact_graph_complete to false");
    }

    /// AC10: "parse errors (root.has_error), measured SEPARATELY from
    /// unreadable files". One file has a genuine tree-sitter syntax error
    /// (parses, has_error=true); a second is unreadable (unsupported
    /// extension, never even attempted to parse). The two counters must
    /// disagree -- a wrong implementation that conflated them into one
    /// counter would fail this test's exact-value assertions.
    #[test]
    fn parse_errors_are_counted_separately_from_unreadable_or_unsupported_files() {
        let dir = tempfile::tempdir().unwrap();
        write_java(&dir, "Malformed.java", "class Broken { void run( {\n");
        std::fs::write(dir.path().join("notes.txt"), "not code").unwrap();

        let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
        let result = build_repo_graph(
            dir.path(),
            &["Malformed.java".to_string(), "notes.txt".to_string()],
            &options,
            &NoOpCollector,
        );

        assert_eq!(result.files_with_parse_errors, 1, "the malformed Java file must count as a parse error");
        assert_eq!(result.unreadable_or_unsupported_files, 1, "the unsupported .txt file must count separately");
        assert!(!result.fact_graph_complete, "a real parse error must flip fact_graph_complete to false");
    }

    /// AC10: a tripped index budget must ALSO set `fact_graph_complete =
    /// false`, on top of AC6's own `AnalysisCompleteness::IndexBudgetExceeded`.
    #[test]
    fn index_budget_trip_sets_fact_graph_complete_false() {
        let dir = tempfile::tempdir().unwrap();
        // Three same-named ambiguous declarations plus one caller: pushes
        // the repo-wide raw candidate total past a deliberately tight
        // ceiling, exactly as `bind::budget_bind`'s own fixture does.
        write_java(&dir, "R1.java", "class R1 { void run() {} }\n");
        write_java(&dir, "R2.java", "class R2 { void run() {} }\n");
        write_java(&dir, "R3.java", "class R3 { void run() {} }\n");
        write_java(&dir, "Caller.java", "class Caller { void go() { run(); } }\n");

        let options = RepoIndexOptions { budget: IndexBudget::new(0, 1), max_files: None };
        let result = build_repo_graph(
            dir.path(),
            &["R1.java".to_string(), "R2.java".to_string(), "R3.java".to_string(), "Caller.java".to_string()],
            &options,
            &NoOpCollector,
        );

        assert_eq!(result.graph.completeness(), AnalysisCompleteness::IndexBudgetExceeded);
        assert!(!result.fact_graph_complete, "a tripped index budget must flip fact_graph_complete to false");
    }

    /// THE central AC10 discriminating test, named explicitly in the story:
    /// "a driver regex matching ONE file still resolves a call into a
    /// declaration in an UNMATCHED file". `A.java` (matched) calls
    /// `helper()`; only `B.java` (deliberately NOT matched) declares it.
    /// Indexing must cover BOTH files regardless of the matched set, so the
    /// call in `A.java` still resolves into `B.java`'s declaration once
    /// `references_in_matched_files` restricts REPORTING (not indexing) to
    /// the matched set.
    ///
    /// A wrong implementation that indexed only the matched files (excluding
    /// `B.java` from extraction+bind entirely) would leave the candidate set
    /// for this call EMPTY -- the `RepoNameIndex` would never have seen
    /// `helper` declared anywhere -- which is exactly the failure this test
    /// exists to catch.
    #[test]
    fn a_driver_regex_matching_one_file_still_resolves_a_call_into_a_declaration_in_an_unmatched_file() {
        use crate::graph::identity::file_id as fid;
        use std::collections::HashSet;

        let dir = tempfile::tempdir().unwrap();
        write_java(&dir, "A.java", "class A { void run() { helper(); } }\n");
        write_java(&dir, "B.java", "class B { void helper() {} }\n");

        let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
        let result = build_repo_graph(
            dir.path(),
            &["A.java".to_string(), "B.java".to_string()],
            &options,
            &NoOpCollector,
        );
        assert!(result.fact_graph_complete);

        // Finding scope: the driver regex matched ONLY A.java.
        let matched_file_ids: HashSet<u32> = [fid("A.java")].into_iter().collect();
        let matched_references =
            crate::graph::repo_index::references_in_matched_files(&result.graph, &matched_file_ids);

        let call_site = matched_references
            .iter()
            .find(|r| !r.is_unresolved())
            .expect("the call in A.java must be reported and must have resolved to something");
        assert_eq!(call_site.file, fid("A.java"), "a matched reference must come from the matched file");

        let candidates = result.graph.candidates_for(call_site);
        assert!(!candidates.is_empty(), "indexing must not have been restricted to the matched file set");
        let resolved_file_ids: Vec<u32> =
            candidates.iter().map(|c| (result.graph.resolve_symbol(c.symbol()) >> 32) as u32).collect();
        assert!(
            resolved_file_ids.contains(&fid("B.java")),
            "the call site's candidate must resolve into B.java, an INDEXED but UNMATCHED file"
        );
    }
}
