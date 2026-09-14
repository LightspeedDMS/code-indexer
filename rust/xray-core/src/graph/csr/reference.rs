//! `Reference` -- one call/type/construction site recorded in the CSR
//! arena (Story #1787, S2, AC5). Field types and names match the layout
//! mandated by the story text exactly (`from`, `file`, `line`, `kind`,
//! `cand_start`, `cand_len`), since those sizes are what keep one
//! `Reference` compact enough to pack millions of them in a `Vec`.
//!
//! `Ambiguous` and `Unresolved` (AC4) are NOT separate types: per AC4 they
//! are properties of the candidate SET a `Reference` points at --
//! `cand_len > 1` and `cand_len == 0` respectively, read directly off this
//! struct, never a distinct enum variant anywhere in this crate.

/// One reference (call site, type reference, or construction site) in the
/// CSR arena. `cand_start`/`cand_len` window into the graph's single shared
/// `candidates: Vec<Candidate>` arena -- this struct owns no candidate
/// storage of its own.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Reference {
    pub from: u32,
    pub file: u32,
    pub line: u32,
    pub kind: u8,
    pub cand_start: u32,
    pub cand_len: u16,
}

impl Reference {
    /// AC4: ambiguity is a property of the candidate SET, never a
    /// `Confidence` value: true when more than one candidate was proposed.
    pub fn is_ambiguous(&self) -> bool {
        self.cand_len > 1
    }

    /// AC4: unresolved is a property of the candidate SET, never a
    /// `Confidence` value: true when zero candidates were proposed --
    /// including the "definition is outside the repository" case.
    pub fn is_unresolved(&self) -> bool {
        self.cand_len == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reference_with_cand_len(cand_len: u16) -> Reference {
        Reference { from: 0, file: 0, line: 1, kind: 0, cand_start: 0, cand_len }
    }

    /// AC4: "Ambiguous ... is candidates.len() > 1". A wrong implementation
    /// that treated `cand_len >= 1` as ambiguous (i.e. any resolution at
    /// all) would wrongly flag a single, unambiguous candidate as
    /// ambiguous -- this table exercises the exact boundary (0, 1, 2).
    #[test]
    fn is_ambiguous_is_true_only_when_more_than_one_candidate() {
        assert!(!reference_with_cand_len(0).is_ambiguous());
        assert!(!reference_with_cand_len(1).is_ambiguous());
        assert!(reference_with_cand_len(2).is_ambiguous());
    }

    /// AC4: "unresolved is candidates.is_empty()". A wrong implementation
    /// that also treated `cand_len == 1` (say, testing `cand_len <= 1`) as
    /// unresolved would misreport a perfectly-resolved single candidate as
    /// unresolved -- this table exercises that exact boundary too.
    #[test]
    fn is_unresolved_is_true_only_when_zero_candidates() {
        assert!(reference_with_cand_len(0).is_unresolved());
        assert!(!reference_with_cand_len(1).is_unresolved());
        assert!(!reference_with_cand_len(2).is_unresolved());
    }

    /// AC4: "a reference whose definition is outside the repository carries
    /// an EMPTY candidate set (cand_len == 0)". That is exactly the
    /// unresolved case above -- named here as its own test so the
    /// out-of-repo scenario has a test that names it explicitly, not just a
    /// boundary-value coincidence.
    #[test]
    fn out_of_repo_reference_is_modeled_as_zero_candidates_and_is_unresolved() {
        let out_of_repo_call = reference_with_cand_len(0);
        assert_eq!(out_of_repo_call.cand_len, 0);
        assert!(out_of_repo_call.is_unresolved());
        assert!(!out_of_repo_call.is_ambiguous());
    }
}
