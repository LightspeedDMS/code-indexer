//! `AnalysisCompleteness` -- the whole-graph completeness state a bind
//! produced (Story #1787, S2, AC6).
//!
//! Surfaced as a VALUE on `CodeGraph` (`CodeGraph::completeness`), never
//! only logged: a caller (a future `analyze_graph`, AC7) MUST be able to
//! ask "was this graph complete?" and change its own verdicts accordingly
//! -- see `CodeGraph::is_definitely_dead_code`, which suppresses the
//! strongest "no reference at all" dead-code tier whenever this is
//! anything other than `Complete`.

/// Seven-way completeness state a graph build reports about itself. Every
/// variant but `Complete` names a SPECIFIC way the build degraded, so a
/// caller can react differently (e.g. "budget" vs "parse errors") rather
/// than collapsing every imperfection into one boolean. `Complete`,
/// `IndexBudgetExceeded`, and `RepoIndexIncomplete` are PRODUCED today; the
/// other four are declared now (per the story's exact enum text) so later
/// slices (S3 derivation, S4 binder depth, fact-collection budgets) extend
/// an already-stable surface instead of inventing a new one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum AnalysisCompleteness {
    #[default]
    Complete,
    FactBudgetExceeded,
    IndexBudgetExceeded,
    DerivationTruncated,
    ResolutionAmbiguous,
    ParseErrorsPresent,
    /// Dual-review defect D1 fix: `repo_index::build_repo_graph` dropped
    /// one or more files from the whole-repository index -- `max_files`
    /// truncation, a real tree-sitter parse error, an extractor panic, or
    /// an unreadable source file with a recognized extension. Distinct
    /// from `IndexBudgetExceeded` (the LATER binder-ladder cap on an
    /// already-complete file set): this variant means the file set itself
    /// was incomplete BEFORE binding ever started.
    RepoIndexIncomplete,
}

#[cfg(test)]
mod tests {
    use super::super::AnalysisCompleteness;

    #[test]
    fn default_completeness_is_complete() {
        assert_eq!(AnalysisCompleteness::default(), AnalysisCompleteness::Complete);
    }
}
