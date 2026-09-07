//! `IndexBudget` -- the AC6 fail-closed budget configuration a bind runs
//! against (Story #1787, S2).
//!
//! This slice builds the MECHANISM only: `IndexBudget`'s two numbers are a
//! deliberately simple proxy for "the index memory budget" (a coarse
//! repo-wide raw-candidate-count ceiling, and a per-reference top-N cap).
//! Real, measured admission (bytes, cgroup pressure) is AC12-AC17's
//! memory-governor wiring, a LATER slice -- this type is what that future
//! wiring will construct and pass in, not what computes the numbers.

/// Fail-closed index-build budget. `IndexBudget::unlimited()` is what
/// `bind()` uses today (see `super::super::bind::bind`) -- the degradation
/// ladder never engages unless a caller explicitly supplies a finite
/// budget via `bind_with_budget`.
///
/// `Hash` (dual-review defect H3 fix): `IndexBudget` is itself a
/// RESULT-AFFECTING input to a graph build -- a tighter budget can produce
/// a materially less complete graph for the identical repo snapshot -- so
/// it must be embeddable in `graph_cache::GraphCacheKey`, a `HashMap` key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct IndexBudget {
    max_total_candidates: usize,
    max_candidates_per_reference: usize,
}

impl IndexBudget {
    /// No ceiling on either dimension -- the ladder can never trigger.
    pub fn unlimited() -> Self {
        IndexBudget { max_total_candidates: usize::MAX, max_candidates_per_reference: usize::MAX }
    }

    /// `max_total_candidates`: the repo-wide raw-candidate-count ceiling
    /// whose breach means "the index memory budget is exceeded" (AC6's
    /// Gherkin trigger). `max_candidates_per_reference`: the top-N cap
    /// applied to any one reference's candidate set once that ceiling is
    /// breached (AC6 ladder step 2) -- never applied otherwise.
    pub fn new(max_total_candidates: usize, max_candidates_per_reference: usize) -> Self {
        IndexBudget { max_total_candidates, max_candidates_per_reference }
    }

    /// True once `total_raw_candidates` breaches this budget's ceiling --
    /// the AC6 Gherkin's "When the budget is reached".
    pub fn is_exceeded_by(&self, total_raw_candidates: usize) -> bool {
        total_raw_candidates > self.max_total_candidates
    }

    pub fn max_candidates_per_reference(&self) -> usize {
        self.max_candidates_per_reference
    }
}

#[cfg(test)]
mod tests {
    use super::super::IndexBudget;

    #[test]
    fn unlimited_budget_is_never_exceeded() {
        let budget = IndexBudget::unlimited();
        assert!(!budget.is_exceeded_by(usize::MAX));
    }

    #[test]
    fn is_exceeded_by_is_true_only_strictly_above_the_ceiling() {
        let budget = IndexBudget::new(10, 3);
        assert!(!budget.is_exceeded_by(10));
        assert!(budget.is_exceeded_by(11));
    }

    #[test]
    fn max_candidates_per_reference_returns_the_configured_cap() {
        let budget = IndexBudget::new(10, 3);
        assert_eq!(budget.max_candidates_per_reference(), 3);
    }
}
