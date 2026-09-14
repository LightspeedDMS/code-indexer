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
//! `fact_graph_complete` is `false` whenever ANY repo-level indexing gap
//! degraded this build: `max_files` truncation, a parsed file whose tree
//! carried a real syntax error (`has_error`, surfaced via
//! `crate::graph::fused::FusedFileResult::has_syntax_error` -- measured
//! SEPARATELY from files that could not be read/parsed at all), an
//! extractor panic, an unreadable source file with a RECOGNIZED extension
//! (`files_with_read_errors`, distinct from a genuinely unsupported
//! extension), or an index-budget trip (the bound graph's
//! `AnalysisCompleteness` being anything other than `Complete`). Files
//! with a genuinely unsupported/no extension, or a path that escapes
//! `repo_root`, are counted too (`unreadable_or_unsupported_files`), but do
//! NOT by themselves flip `fact_graph_complete`: a `.txt` file in an
//! otherwise fully-indexed, in-budget repo is not a degraded build (dual-
//! review defect D5's secondary point -- that distinction is exactly why
//! `files_with_read_errors` exists as a SEPARATE counter). A collector
//! (fact-collection) panic is tracked separately still
//! (`files_with_collector_panics`) and does NOT flip `fact_graph_complete`:
//! it affects only the auxiliary `facts` FactIndex, never the reference
//! graph's own completeness.
//!
//! `index_is_complete` (dual-review defect D3) is the STRICTER subset of
//! these triggers that actually matters to the binder's `RepoNameIndex`:
//! whether every file's DECLARATIONS made it into the name index at all.
//! Files with a parse error (`has_error`) still get "extraction ran on
//! whatever it could parse" and are variantly present in the index. Only
//! truncation, extractor panics, and I/O read errors (files with a
//! recognized extension) can HIDE declarations entirely, so `bind_with_
//! budget_and_completeness` is fed exactly this narrower set -- see the
//! computation at the end of `build_repo_graph`.

use super::bind::{bind_with_budget_and_completeness, enclosing_symbol, FileForBind};
use super::budget::{AnalysisCompleteness, IndexBudget};
use super::csr::CodeGraph;
use super::fused::{process_file_fused, CollectFactsStatus, ExtractionStatus, FusedFileResult};
use super::identity::{file_id, FileIdCollisionError, FileIdRegistry};
use super::user_facts::{FactCollector, FactIndex, FactKey};
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
    /// False whenever any repo-level indexing gap or an index-budget trip
    /// degraded this build. See module docs.
    pub fact_graph_complete: bool,
    /// Files that parsed but whose tree carried a syntax error
    /// (`has_error`) -- measured SEPARATELY from
    /// `unreadable_or_unsupported_files`.
    pub files_with_parse_errors: usize,
    /// Files that could not be read or parsed at all because their
    /// extension is genuinely unsupported (or absent), OR a path that
    /// escapes `repo_root` (see `path_is_contained`). Never flips
    /// `fact_graph_complete` -- a `.txt` file was never source this repo
    /// claims to index.
    pub unreadable_or_unsupported_files: usize,
    /// Dual-review defect D5: files with a RECOGNIZED source-language
    /// extension that still could not be read/parsed (I/O error, total
    /// tree-sitter failure) -- a real source file this build silently
    /// dropped. DOES flip `fact_graph_complete`, unlike the merely-
    /// unsupported-extension case above.
    pub files_with_read_errors: usize,
    /// Dual-review defect D5: files whose extraction PANICKED
    /// (`ExtractionStatus::Panicked`) -- contributes zero declarations to
    /// this build. DOES flip `fact_graph_complete`.
    pub files_with_extractor_panics: usize,
    /// Dual-review defects D1/H2: files whose `collect_facts` PANICKED
    /// (`CollectFactsStatus::Panicked`). Tracked for observability but
    /// deliberately does NOT flip `fact_graph_complete` -- facts feed the
    /// separate `facts` FactIndex, never the reference graph itself.
    pub files_with_collector_panics: usize,
    /// Consolidated review finding C2 (Issue #1811/Bug #1812): files whose
    /// `ExtractionStatus` came back `LanguageNotSupported` -- a recognized
    /// source-language extension (distinct from
    /// `unreadable_or_unsupported_files`) for which the engine simply has
    /// no `LanguageExtractor` implemented yet (Java is currently the only
    /// language with one; see `extract::extractor_for_language`). The
    /// file's declarations are as invisible to the graph as an extractor
    /// panic's would be, so this DOES flip `fact_graph_complete` -- see
    /// `index_is_complete` below. Without this counter, an analysis over a
    /// non-Java repo silently reported `fact_graph_complete: true` with
    /// zero degradation signals: a false "verified clean" reading.
    pub files_with_unsupported_language: usize,
    pub truncated_by_max_files: bool,
    /// Dual-review defect H2: every `UserFact` collected across the whole
    /// repository, keyed by the fact's enclosing declaration
    /// (`bind::enclosing_symbol`) so a real `analyze_graph` evaluator can
    /// look facts up via `FactsHandle::for_symbol` -- never discarded.
    pub facts: FactIndex,
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

/// Dual-review defect D5: distinguishes "genuinely unsupported/no
/// extension" from "a recognized source-language extension that merely
/// failed to read/parse" -- the two cases `unreadable_or_unsupported_files`
/// and `files_with_read_errors` must never be conflated.
fn extension_is_recognized(path: &Path) -> bool {
    path.extension().and_then(|e| e.to_str()).and_then(crate::languages::language_for_extension).is_some()
}

/// Per-repository mutable state threaded through `process_one_file`. Kept
/// as one struct (rather than five separate `&mut usize` parameters) so
/// adding a future counter never grows any function's parameter list.
#[derive(Default)]
struct IndexAccumulator {
    files_for_bind: Vec<FileForBind>,
    facts: FactIndex,
    files_with_parse_errors: usize,
    unreadable_or_unsupported_files: usize,
    files_with_read_errors: usize,
    files_with_extractor_panics: usize,
    files_with_collector_panics: usize,
    files_with_unsupported_language: usize,
}

/// Handles one `Some(fused_result)` outcome from `process_file_fused`:
/// updates every counter D1/D5 need, aggregates collected facts into
/// `acc.facts` (H2), and pushes a `FileForBind` when extraction produced a
/// usable `LocalIndex` -- exactly the pre-existing behavior, plus the
/// panic/fact bookkeeping the dual review found missing.
fn record_fused_result(full_path: &Path, relative_path: &str, fused_result: FusedFileResult, acc: &mut IndexAccumulator) {
    if fused_result.has_syntax_error {
        acc.files_with_parse_errors += 1;
    }
    if fused_result.extraction_status == ExtractionStatus::Panicked {
        acc.files_with_extractor_panics += 1;
    }
    if fused_result.collect_facts_status == CollectFactsStatus::Panicked {
        acc.files_with_collector_panics += 1;
    }
    if fused_result.extraction_status == ExtractionStatus::LanguageNotSupported {
        acc.files_with_unsupported_language += 1;
    }
    if let Some(index) = fused_result.index {
        let file_id_val = file_id(relative_path);
        for fact in &fused_result.facts {
            // Story #1785 / ADR-001: a fact naming a `custom_key` is a
            // genuinely non-symbol value (config key, event topic,
            // structural hash) and is attributed to that custom key ONLY --
            // never ALSO to `enclosing_symbol`, which would reintroduce the
            // exact false identity ADR-001's closed `FactKey::Symbol(
            // SymbolId) | FactKey::Custom(InternedStr)` sum type exists to
            // prevent. A fact with no `custom_key` keeps the pre-#1785
            // behavior unchanged.
            match &fact.custom_key {
                Some(name) => acc.facts.insert_custom(name, fact.clone()),
                None => {
                    let symbol = enclosing_symbol(&index, file_id_val, fact.line);
                    acc.facts.insert(FactKey::Symbol(symbol), fact.clone());
                }
            }
        }
        let language = full_path.extension().and_then(|e| e.to_str()).unwrap_or_default().to_string();
        acc.files_for_bind.push(FileForBind { file_id: file_id_val, language, index });
    }
}

/// Processes exactly one candidate path: containment check, then fused
/// extract+collect, updating `acc` throughout. Factored out of
/// `build_repo_graph`'s loop body to keep that function under the
/// project's per-function line budget.
fn process_one_file(
    canonical_repo_root: &Path,
    repo_root: &Path,
    relative_path: &str,
    fact_collector: &dyn FactCollector,
    acc: &mut IndexAccumulator,
) {
    if !path_is_contained(canonical_repo_root, repo_root, relative_path) {
        acc.unreadable_or_unsupported_files += 1;
        return;
    }
    let full_path = repo_root.join(relative_path);
    match process_file_fused(&full_path, relative_path, fact_collector) {
        None => {
            if extension_is_recognized(&full_path) {
                acc.files_with_read_errors += 1;
            } else {
                acc.unreadable_or_unsupported_files += 1;
            }
        }
        Some(fused_result) => record_fused_result(&full_path, relative_path, fused_result, acc),
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
///
/// Dual-review defect D4: every considered path is registered in a
/// run-scoped `FileIdRegistry` BEFORE it is processed -- a real `file_id`
/// collision between two distinct paths fails the WHOLE build loudly
/// (`Err`) rather than silently merging their symbol namespaces.
pub fn build_repo_graph(
    repo_root: &Path,
    repo_relative_paths: &[String],
    options: &RepoIndexOptions,
    fact_collector: &dyn FactCollector,
) -> Result<RepoIndexResult, FileIdCollisionError> {
    let canonical_repo_root =
        repo_root.canonicalize().expect("repo_root must exist and be canonicalizable");
    let limit = options.max_files.unwrap_or(usize::MAX);
    let truncated_by_max_files = repo_relative_paths.len() > limit;

    let mut registry = FileIdRegistry::new();
    let mut acc = IndexAccumulator::default();
    for relative_path in repo_relative_paths.iter().take(limit) {
        registry.assign(relative_path)?;
        process_one_file(&canonical_repo_root, repo_root, relative_path, fact_collector, &mut acc);
    }

    // Dual-review defect D3: only truncation/extractor-panics/read-errors
    // can HIDE a file's declarations from the name index entirely -- a
    // parse error still extracts whatever it could parse (see module docs).
    let index_is_complete = !truncated_by_max_files
        && acc.files_with_extractor_panics == 0
        && acc.files_with_read_errors == 0
        && acc.files_with_unsupported_language == 0;

    let mut graph = bind_with_budget_and_completeness(acc.files_for_bind, &options.budget, index_is_complete);
    let budget_exceeded = graph.completeness() != AnalysisCompleteness::Complete;

    // Dual-review defect D1: propagate EVERY repo-level incompleteness
    // trigger onto the graph itself, not just onto `fact_graph_complete`
    // below -- a caller that queries `graph.is_definitely_dead_code(..)`
    // directly must see the same suppression `fact_graph_complete` would
    // have told it to apply.
    let repo_level_incomplete =
        !index_is_complete || acc.files_with_parse_errors > 0;
    if repo_level_incomplete {
        graph.downgrade_completeness(AnalysisCompleteness::RepoIndexIncomplete);
    }
    let fact_graph_complete = !repo_level_incomplete && !budget_exceeded;

    Ok(RepoIndexResult {
        graph,
        fact_graph_complete,
        files_with_parse_errors: acc.files_with_parse_errors,
        unreadable_or_unsupported_files: acc.unreadable_or_unsupported_files,
        files_with_read_errors: acc.files_with_read_errors,
        files_with_extractor_panics: acc.files_with_extractor_panics,
        files_with_collector_panics: acc.files_with_collector_panics,
        files_with_unsupported_language: acc.files_with_unsupported_language,
        truncated_by_max_files,
        facts: acc.facts,
    })
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
    use super::{record_fused_result, IndexAccumulator};
    use crate::graph::bind::bind_with_budget_and_completeness;
    use crate::graph::budget::{AnalysisCompleteness, IndexBudget};
    use crate::graph::extract::local_index::LocalIndex;
    use crate::graph::identity::{COLLIDING_PATH_A, COLLIDING_PATH_B};
    use crate::graph::repo_index::{build_repo_graph, RepoIndexOptions};
    use crate::graph::user_facts::{FactCollector, FactKey, UserFact};
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
        )
        .expect("no file_id collision in this fixture");

        assert!(result.fact_graph_complete);
        assert_eq!(result.files_with_parse_errors, 0);
        assert_eq!(result.unreadable_or_unsupported_files, 0);
        assert_eq!(result.files_with_read_errors, 0);
        assert_eq!(result.files_with_extractor_panics, 0);
        assert_eq!(result.files_with_collector_panics, 0);
        assert!(!result.truncated_by_max_files);
        assert_eq!(result.graph.completeness(), AnalysisCompleteness::Complete);
    }

    /// Consolidated review finding C2 (Issue #1811/Bug #1812): Java is the
    /// ONLY language with a graph extractor (`extract::extractor_for_
    /// language`) -- every other engine-supported language (e.g. Python)
    /// produces `ExtractionStatus::LanguageNotSupported`. A `.py` file that
    /// parses perfectly fine (recognized extension, no syntax error) must
    /// still count as a repo-level indexing gap distinct from BOTH
    /// `unreadable_or_unsupported_files` (genuinely unsupported/no
    /// extension) and `files_with_read_errors` (I/O failure) -- and must
    /// flip `fact_graph_complete` to `false`, exactly like an extractor
    /// panic or read error would, because the file's declarations are
    /// entirely invisible to the graph either way. Before the fix, NO
    /// counter tracks this case at all and `fact_graph_complete` stays
    /// `true` -- a confident, false "verified clean" reading for any
    /// non-Java repo.
    #[test]
    fn unsupported_language_file_flips_fact_graph_complete_and_counts_separately() {
        let dir = tempfile::tempdir().unwrap();
        write_java(&dir, "A.java", "class A { void run() {} }\n");
        std::fs::write(dir.path().join("script.py"), "def totally_unused():\n    pass\n").unwrap();

        let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
        let result = build_repo_graph(
            dir.path(),
            &["A.java".to_string(), "script.py".to_string()],
            &options,
            &NoOpCollector,
        )
        .expect("no file_id collision in this fixture");

        assert_eq!(
            result.files_with_unsupported_language, 1,
            "the .py file (recognized extension, no graph extractor) must be counted"
        );
        assert_eq!(
            result.unreadable_or_unsupported_files, 0,
            "an unsupported-LANGUAGE file (recognized extension) must never be conflated \
             with a genuinely unsupported/no-extension file"
        );
        assert_eq!(result.files_with_read_errors, 0);
        assert_eq!(result.files_with_parse_errors, 0);
        assert!(
            !result.fact_graph_complete,
            "a file whose language has no graph extractor must flip fact_graph_complete to \
             false -- its declarations are as invisible to the graph as a read error's"
        );
        assert_ne!(
            result.graph.completeness(),
            AnalysisCompleteness::Complete,
            "the missing-extractor gap must also downgrade the GRAPH's own completeness(), \
             mirroring the D1 propagation every other repo-level gap already gets"
        );
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
        )
        .expect("no file_id collision in this fixture");

        assert!(result.truncated_by_max_files);
        assert!(!result.fact_graph_complete, "max_files truncation must flip fact_graph_complete to false");
        assert_ne!(
            result.graph.completeness(),
            AnalysisCompleteness::Complete,
            "dual-review defect D1: max_files truncation must also downgrade the GRAPH's own completeness()"
        );
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
        )
        .expect("no file_id collision in this fixture");

        assert_eq!(result.files_with_parse_errors, 1, "the malformed Java file must count as a parse error");
        assert_eq!(result.unreadable_or_unsupported_files, 1, "the unsupported .txt file must count separately");
        assert!(!result.fact_graph_complete, "a real parse error must flip fact_graph_complete to false");
        assert_ne!(
            result.graph.completeness(),
            AnalysisCompleteness::Complete,
            "dual-review defect D1: a real parse error must also downgrade the GRAPH's own completeness()"
        );
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
        )
        .expect("no file_id collision in this fixture");

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
        )
        .expect("no file_id collision in this fixture");
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

    /// Dual-review defect D4 (High): `FileIdRegistry` (built specifically to
    /// detect `file_id` collisions and fail loud) had zero callers before
    /// this fix. Reuses the exact real-collision fixture `identity.rs`'s own
    /// `FileIdRegistry` test already established (Rule 4, anti-duplication).
    /// Neither path needs to exist on disk: the collision is detected by
    /// `FileIdRegistry::assign` BEFORE any filesystem access, so this fails
    /// loud even for two files that were never actually read.
    #[test]
    fn two_paths_that_collide_on_file_id_fail_the_build_loudly() {
        let dir = tempfile::tempdir().unwrap();
        let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };

        let result = build_repo_graph(
            dir.path(),
            &[COLLIDING_PATH_A.to_string(), COLLIDING_PATH_B.to_string()],
            &options,
            &NoOpCollector,
        );

        match result {
            Err(collision) => {
                assert_eq!(collision.existing_path, COLLIDING_PATH_A);
                assert_eq!(collision.new_path, COLLIDING_PATH_B);
            }
            Ok(_) => panic!(
                "a real file_id collision between two distinct repo paths must fail the whole build \
                 loudly, never silently merge their symbol namespaces"
            ),
        }
    }

    /// Dual-review defects D5 + D1: an I/O failure on a file with a
    /// RECOGNIZED source-language extension (here: a `.java` path that is
    /// never actually written to disk) must count under
    /// `files_with_read_errors` -- NEVER `unreadable_or_unsupported_files`,
    /// which is reserved for a genuinely unsupported/no extension -- and
    /// must flip `fact_graph_complete` AND downgrade `graph.completeness()`
    /// away from `Complete`, which in turn suppresses `is_definitely_dead_
    /// code` for a genuinely-dead symbol declared in an UNRELATED,
    /// successfully-indexed file.
    #[test]
    fn an_unreadable_source_file_with_a_recognized_extension_flips_fact_graph_complete_and_suppresses_dead_code() {
        use crate::graph::identity::make_symbol_id;

        let dir = tempfile::tempdir().unwrap();
        write_java(&dir, "NeverCalled.java", "class NeverCalled { void deadMethod() {} }\n");
        // "Unreadable.java" exists (so it PASSES the containment check --
        // `canonicalize()` needs a real path) but has its read permission
        // revoked, so `std::fs::read` genuinely fails even though its
        // extension IS a recognized language.
        write_java(&dir, "Unreadable.java", "class Unreadable {}\n");
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(dir.path().join("Unreadable.java"), std::fs::Permissions::from_mode(0o000))
                .expect("revoke read permission for the fixture");
        }

        let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
        let result = build_repo_graph(
            dir.path(),
            &["NeverCalled.java".to_string(), "Unreadable.java".to_string()],
            &options,
            &NoOpCollector,
        )
        .expect("no file_id collision in this fixture");

        assert_eq!(
            result.files_with_read_errors, 1,
            "a recognized-extension file that fails to read must count as a read error"
        );
        assert_eq!(
            result.unreadable_or_unsupported_files, 0,
            "a read error on a RECOGNIZED extension must never be folded into the unsupported-extension counter"
        );
        assert!(!result.fact_graph_complete, "a real read error must flip fact_graph_complete to false");
        assert_ne!(
            result.graph.completeness(),
            AnalysisCompleteness::Complete,
            "a real read error must also downgrade the GRAPH's own completeness()"
        );

        let dead_symbol = result
            .graph
            .dense_id_for(make_symbol_id(crate::graph::identity::file_id("NeverCalled.java"), 0))
            .expect("NeverCalled's declaration must still be interned despite the sibling read error");
        assert_eq!(
            result.graph.is_definitely_dead_code(dead_symbol),
            None,
            "a genuinely unreferenced symbol must be suppressed (None), never a confident Some(true), \
             once a sibling file's read error left this build incomplete"
        );
    }

    /// Builds a real `FusedFileResult` -- the exact shape `fused.rs`'s own
    /// fused pipeline produces for `extraction_status`. `facts`/
    /// `collect_facts_status`/`has_syntax_error` take the values a
    /// successful-but-uneventful (or panicked, per `process_parsed_file`'s
    /// own `None => (Vec::new(), SkippedNoIndex)` branch) real run would
    /// have -- never a stub of behavior under test, just the parts of the
    /// struct these tests don't vary.
    fn fused_result_fixture(
        file: &str,
        index: Option<LocalIndex>,
        extraction_status: crate::graph::fused::ExtractionStatus,
    ) -> crate::graph::fused::FusedFileResult {
        crate::graph::fused::FusedFileResult {
            file: file.to_string(),
            index,
            extraction_status,
            facts: Vec::new(),
            collect_facts_status: crate::graph::fused::CollectFactsStatus::SkippedNoIndex,
            has_syntax_error: false,
        }
    }

    /// Dual-review defect D5 (Critical): an extractor panic is a DISTINCT,
    /// explicit `ExtractionStatus::Panicked` outcome (proven for real by
    /// `fused.rs`'s own `run_extraction`/`catch_unwind` machinery). This
    /// test feeds the EXACT shape that machinery produces through
    /// `record_fused_result`, `repo_index`'s own reaction logic, proving
    /// it is counted -- never silently folded into an empty success -- and
    /// that carrying it through the SAME completeness pipeline
    /// `build_repo_graph` runs suppresses `is_definitely_dead_code` for an
    /// unrelated, genuinely dead symbol.
    #[test]
    fn an_extractor_panic_is_counted_and_suppresses_is_definitely_dead_code() {
        use crate::graph::extract::local_index::{Declaration, DeclarationKind};
        use crate::graph::fused::ExtractionStatus;
        use crate::graph::identity::make_symbol_id;

        let alive_file_id = crate::graph::identity::file_id("Alive.java");
        let repo_root = std::env::temp_dir().canonicalize().unwrap();
        let mut acc = IndexAccumulator::default();

        let mut alive_index = LocalIndex::new();
        alive_index.declarations.push(Declaration {
            kind: DeclarationKind::Method,
            name: "deadMethod".to_string(),
            line: 1,
            symbol: make_symbol_id(alive_file_id, 0),
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
        });
        let alive = fused_result_fixture("Alive.java", Some(alive_index), ExtractionStatus::Completed);
        record_fused_result(&repo_root.join("Alive.java"), "Alive.java", alive, &mut acc);

        let panicked = fused_result_fixture("Crashes.java", None, ExtractionStatus::Panicked);
        record_fused_result(&repo_root.join("Crashes.java"), "Crashes.java", panicked, &mut acc);

        assert_eq!(acc.files_with_extractor_panics, 1, "an extractor panic must be counted, never discarded");
        assert!(acc.files_for_bind.iter().any(|f| f.file_id == alive_file_id), "the alive file must still be bound");
        assert!(
            !acc.files_for_bind.iter().any(|f| f.file_id == crate::graph::identity::file_id("Crashes.java")),
            "a panicked extraction must contribute zero declarations"
        );

        // Run the SAME completeness pipeline `build_repo_graph` runs.
        let index_is_complete = acc.files_with_extractor_panics == 0 && acc.files_with_read_errors == 0;
        let mut graph =
            bind_with_budget_and_completeness(acc.files_for_bind, &IndexBudget::unlimited(), index_is_complete);
        if !index_is_complete || acc.files_with_parse_errors > 0 {
            graph.downgrade_completeness(AnalysisCompleteness::RepoIndexIncomplete);
        }

        let dead_symbol = graph.dense_id_for(make_symbol_id(alive_file_id, 0)).expect("alive declaration interned");
        assert_eq!(
            graph.is_definitely_dead_code(dead_symbol),
            None,
            "an extractor panic ELSEWHERE in the repo must suppress a confident dead-code verdict here"
        );
    }

    /// Dual-review defect H2: `build_repo_graph` pays for `collect_facts`
    /// on every file via `fact_collector` but must never discard the
    /// result -- every collected `UserFact` must reach `RepoIndexResult
    /// .facts`, keyed by the fact's enclosing declaration
    /// (`bind::enclosing_symbol`), so a real `analyze_graph` evaluator can
    /// retrieve it via `FactsHandle::for_symbol`.
    #[test]
    fn facts_collected_during_indexing_are_aggregated_into_the_repo_index_result() {
        use crate::graph::identity::make_symbol_id;

        struct DeprecatedAnnotationCollector;
        impl FactCollector for DeprecatedAnnotationCollector {
            fn collect_facts(&self, _root: &OwnedNode, file: &str, _index: &LocalIndex) -> Vec<UserFact> {
                vec![UserFact { kind: "deprecated".to_string(), line: 1, message: format!("{file}: old API"), custom_key: None }]
            }
        }

        let dir = tempfile::tempdir().unwrap();
        write_java(&dir, "Legacy.java", "class Legacy {\n    void run() {}\n}\n");

        let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
        let result =
            build_repo_graph(dir.path(), &["Legacy.java".to_string()], &options, &DeprecatedAnnotationCollector)
                .expect("no file_id collision in this fixture");

        // The fact is reported at line 1 (the class line); `run()` is
        // declared on line 2, strictly AFTER it, so `enclosing_symbol`'s
        // `d.line <= line` filter unambiguously resolves to `Legacy`'s own
        // class declaration (the file's first, local index 0) regardless
        // of the extractor's internal traversal/tie-break order.
        let legacy_file_id = crate::graph::identity::file_id("Legacy.java");
        let enclosing = FactKey::Symbol(make_symbol_id(legacy_file_id, 0));
        let facts = result.facts.get(&enclosing);
        assert!(
            !facts.is_empty(),
            "the fact collected during indexing must have reached RepoIndexResult.facts, never been discarded"
        );
        assert_eq!(facts[0].message, "Legacy.java: old API");
    }

    /// Story #1785 / ADR-001: a `UserFact` naming a `custom_key` (config
    /// key, event topic, structural hash) must be attributed to that
    /// custom key ONLY -- never ALSO to whichever symbol happens to
    /// enclose its reported line. Attributing it to both would reintroduce
    /// the exact false identity ADR-001's closed `FactKey::Symbol(SymbolId)
    /// | FactKey::Custom(InternedStr)` sum type exists to prevent: a config
    /// key has no real `SymbolId`, so `enclosing_symbol`'s "whichever
    /// declaration happens to precede this line" answer is meaningless for
    /// it and must never be recorded as if it were.
    #[test]
    fn a_fact_naming_a_custom_key_is_attributed_only_to_that_custom_key_never_also_to_its_enclosing_symbol() {
        use crate::graph::identity::make_symbol_id;

        struct ConfigKeyCollector;
        impl FactCollector for ConfigKeyCollector {
            fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
                vec![UserFact {
                    kind: "config_key".to_string(),
                    line: 1,
                    message: "db.host".to_string(),
                    custom_key: Some("db.host".to_string()),
                }]
            }
        }

        let dir = tempfile::tempdir().unwrap();
        write_java(&dir, "Legacy.java", "class Legacy {\n    void run() {}\n}\n");

        let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
        let result = build_repo_graph(dir.path(), &["Legacy.java".to_string()], &options, &ConfigKeyCollector)
            .expect("no file_id collision in this fixture");

        let custom_facts = result.facts.get_custom("db.host");
        assert_eq!(custom_facts.len(), 1, "the custom-keyed fact must be reachable via get_custom");
        assert_eq!(custom_facts[0].message, "db.host");

        // Line 1 is Legacy's own class declaration (local index 0) -- the
        // SAME symbol `enclosing_symbol` would have attributed this fact
        // to had it been treated as symbol-shaped. It must be EMPTY.
        let legacy_file_id = crate::graph::identity::file_id("Legacy.java");
        let would_be_enclosing = FactKey::Symbol(make_symbol_id(legacy_file_id, 0));
        assert!(
            result.facts.get(&would_be_enclosing).is_empty(),
            "a custom-keyed fact must NEVER also be attributed to its enclosing symbol -- \
             that would reintroduce the false identity ADR-001's closed sum type exists to prevent"
        );
    }
}
