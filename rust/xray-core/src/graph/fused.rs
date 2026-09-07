//! The fused Extract+Collect per-file pipeline (Story #1787, S2, AC2):
//! exactly TWO SEQUENTIAL walks over one parsed tree -- extraction first
//! (fully populating a `LocalIndex`), THEN `collect_facts` once, on the
//! SAME tree, with a read-only view of the now-complete index. NEVER one
//! interleaved walk: interleaving would let `collect_facts` observe a
//! partially-built `LocalIndex` that has not yet seen later declarations,
//! silently breaking forward references and nested classes.
//!
//! `root: OwnedNode` is taken BY VALUE in both functions below and never
//! retained past the call -- it drops when the function returns, which is
//! what guarantees "OwnedNode trees are NEVER retained across files".
//!
//! Both extraction and `collect_facts` are wrapped in `catch_unwind`: a
//! panic in either is CONTAINED and recorded explicitly via the status
//! enums below (Rule 13, anti-silent-failure) -- never silently downgraded
//! to an empty-looking success, and never allowed to abort the process.

use super::extract::local_index::LocalIndex;
use super::extract::{extractor_for_language, ExtractorLookup, LanguageExtractor};
use super::user_facts::{FactCollector, UserFact};
use crate::owned_node::OwnedNode;
use crate::scanner;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;

/// Outcome of the extraction walk. A distinct enum (not `Option`) so
/// "extraction ran and panicked" is never confused with "no extractor
/// exists yet for this language" -- both produce `index: None`, but only
/// this enum tells a caller which happened.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExtractionStatus {
    Completed,
    Panicked,
    LanguageNotSupported,
}

/// Outcome of the `collect_facts` walk.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CollectFactsStatus {
    Ran,
    Panicked,
    /// `collect_facts` was never invoked because extraction did not
    /// `Complete` -- calling it against a `None` index would be undefined
    /// behavior AC2 does not define, so this is a distinct, explicit
    /// status rather than silently returning an empty `Vec<UserFact>` that
    /// looks identical to a real collector finding nothing.
    SkippedNoIndex,
}

/// Outcome of fused extract+collect for one file.
pub struct FusedFileResult {
    pub file: String,
    pub index: Option<LocalIndex>,
    pub extraction_status: ExtractionStatus,
    pub facts: Vec<UserFact>,
    pub collect_facts_status: CollectFactsStatus,
    /// Story #1787 AC10: true when tree-sitter's root node carried a
    /// syntax error (`has_error`) for this file. Always `false` when the
    /// file could not be parsed at all (`process_file_fused` returns
    /// `None` in that case, never a `FusedFileResult`) -- this field
    /// exists to distinguish "parsed, but with a syntax error inside" from
    /// that unreadable/unsupported case, which a repo-wide orchestrator
    /// must count separately.
    pub has_syntax_error: bool,
}

fn run_extraction(
    root: &OwnedNode,
    file_id: u32,
    extractor: &dyn LanguageExtractor,
) -> (Option<LocalIndex>, ExtractionStatus) {
    match catch_unwind(AssertUnwindSafe(|| extractor.extract(root, file_id))) {
        Ok(index) => (Some(index), ExtractionStatus::Completed),
        Err(_) => (None, ExtractionStatus::Panicked),
    }
}

fn run_collect_facts(
    root: &OwnedNode,
    file: &str,
    index: &LocalIndex,
    fact_collector: &dyn FactCollector,
) -> (Vec<UserFact>, CollectFactsStatus) {
    match catch_unwind(AssertUnwindSafe(|| fact_collector.collect_facts(root, file, index))) {
        Ok(facts) => (facts, CollectFactsStatus::Ran),
        Err(_) => (Vec::new(), CollectFactsStatus::Panicked),
    }
}

/// Runs extraction then `collect_facts`, in that mandatory order, on
/// `root` -- AC2's two-sequential-walks sequencing. `root` is taken BY
/// VALUE so it is guaranteed dropped when this function returns.
#[allow(clippy::too_many_arguments)]
pub fn process_parsed_file(
    root: OwnedNode,
    file: &str,
    file_id: u32,
    ext: &str,
    fact_collector: &dyn FactCollector,
    has_syntax_error: bool,
) -> FusedFileResult {
    let (index, extraction_status) = match extractor_for_language(ext) {
        ExtractorLookup::Supported(extractor) => run_extraction(&root, file_id, extractor.as_ref()),
        ExtractorLookup::Unsupported => (None, ExtractionStatus::LanguageNotSupported),
    };

    let (facts, collect_facts_status) = match &index {
        Some(built_index) => run_collect_facts(&root, file, built_index, fact_collector),
        None => (Vec::new(), CollectFactsStatus::SkippedNoIndex),
    };

    FusedFileResult {
        file: file.to_string(),
        index,
        extraction_status,
        facts,
        collect_facts_status,
        has_syntax_error,
    }
    // `root` is dropped here -- never retained past this call.
}

/// Parses `path` exactly ONCE (via `crate::scanner::parse_file_with_error_flag`,
/// the SAME primitive every other scan path's error-tolerant parsing shares
/// -- Rule 4, anti-duplication) and runs the fused pipeline on the result.
/// Returns `None` if the file cannot be parsed at all (unsupported
/// extension, unreadable, or a tree-sitter parse failure) -- mirrors
/// `scanner::parse_file`'s own `Option` convention for that case, which is
/// a DIFFERENT, upstream failure from `ExtractionStatus`/`CollectFactsStatus`
/// above, and from AC10's `has_syntax_error` (a file that DID parse but
/// whose tree contains a real ERROR node).
pub fn process_file_fused(
    path: &Path,
    repo_relative_path: &str,
    fact_collector: &dyn FactCollector,
) -> Option<FusedFileResult> {
    let ext = path.extension()?.to_str()?.to_string();
    let (root, has_syntax_error) = scanner::parse_file_with_error_flag(path)?;
    let file_id = crate::graph::identity::file_id(repo_relative_path);
    Some(process_parsed_file(root, repo_relative_path, file_id, &ext, fact_collector, has_syntax_error))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::extract::local_index::DeclarationKind;
    use std::path::Path as StdPath;

    struct PanickingCollector;

    impl FactCollector for PanickingCollector {
        fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
            panic!("PanickingCollector always panics");
        }
    }

    struct NoOpCollector;

    impl FactCollector for NoOpCollector {
        fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
            Vec::new()
        }
    }

    fn write_and_process(source: &str, ext: &str, fact_collector: &dyn FactCollector) -> Option<FusedFileResult> {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(format!("Sample.{ext}"));
        std::fs::write(&path, source).unwrap();
        process_file_fused(&path, &format!("Sample.{ext}"), fact_collector)
    }

    #[test]
    fn a_panic_inside_collect_facts_is_contained_and_reported_never_aborts() {
        let result = write_and_process("class Foo {}\n", "java", &PanickingCollector).unwrap();
        assert_eq!(result.collect_facts_status, CollectFactsStatus::Panicked);
        assert!(result.facts.is_empty());
        // Extraction itself must be UNAFFECTED by the later panic --
        // proving the two walks are genuinely independent, not one
        // combined try block that loses extraction's results too.
        assert_eq!(result.extraction_status, ExtractionStatus::Completed);
        assert!(result.index.is_some());
    }

    #[test]
    fn an_unsupported_language_is_explicit_never_a_silent_empty_success() {
        let result = write_and_process("print('hi')\n", "py", &NoOpCollector).unwrap();
        assert_eq!(result.extraction_status, ExtractionStatus::LanguageNotSupported);
        assert!(result.index.is_none());
        assert_eq!(result.collect_facts_status, CollectFactsStatus::SkippedNoIndex);
    }

    /// Story #1787 AC10: a whole-repo orchestrator needs to count "files
    /// with parse errors" separately from unreadable files. This is the
    /// signal it will read: `has_syntax_error` must be false for
    /// well-formed source and true for a real tree-sitter ERROR node,
    /// surfaced right on the per-file fused result.
    #[test]
    fn has_syntax_error_is_true_for_malformed_source_and_false_for_valid_source() {
        let valid = write_and_process("class Foo { void run() {} }\n", "java", &NoOpCollector).unwrap();
        assert!(!valid.has_syntax_error, "well-formed Java must not report a syntax error");

        let malformed = write_and_process("class Broken { void run( {\n", "java", &NoOpCollector).unwrap();
        assert!(malformed.has_syntax_error, "malformed Java must report a syntax error");
    }

    #[test]
    fn a_supported_language_completes_extraction_and_runs_collect_facts() {
        let result = write_and_process("class Foo {}\n", "java", &NoOpCollector).unwrap();
        assert_eq!(result.extraction_status, ExtractionStatus::Completed);
        assert_eq!(result.collect_facts_status, CollectFactsStatus::Ran);
        let index = result.index.unwrap();
        let decl = index.declaration_named("Foo").unwrap();
        assert_eq!(decl.kind, DeclarationKind::Type);
    }

    #[test]
    fn an_unparseable_path_returns_none() {
        assert!(process_file_fused(StdPath::new("/does/not/exist.java"), "exist.java", &NoOpCollector).is_none());
    }
}
