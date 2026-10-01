//! AC6: the fail-closed degradation ladder + `AnalysisCompleteness` (Story
//! #1787, S2). See `super::bind::bind_with_budget` for where these pieces
//! are actually wired together against a real bind.

pub mod completeness;
pub mod index_budget;
pub mod ladder;
pub mod referenced_bits;

pub use completeness::AnalysisCompleteness;
pub use index_budget::IndexBudget;
pub use referenced_bits::ReferencedBits;
