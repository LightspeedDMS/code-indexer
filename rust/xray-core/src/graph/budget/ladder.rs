//! AC6 ladder step 2: "cap candidate sets per reference, top-N by
//! confidence" (Story #1787, S2).
//!
//! Deliberately the ONLY ladder step that touches the CSR `candidates`
//! arena's contents -- step 1 (drop snippets) and step 3 (the decoupled
//! referenced-bit) live in `super::super::bind::bind_with_budget` and
//! `super::referenced_bits` respectively, since neither needs to reorder
//! or truncate a candidate list.
//!
//! **This never removes a candidate on the grounds of low confidence
//! alone in the sense AC6 forbids**: it keeps the top N candidates FOR ONE
//! AMBIGUOUS REFERENCE by confidence, which only ever fires when a
//! reference already proposed MORE than N candidates (i.e. it was already
//! highly ambiguous) -- it is a cap on ambiguity fan-out, not a blanket
//! "drop anything below confidence X" filter across the whole graph. The
//! candidates it discards are still recorded in `ReferencedBits` before
//! this runs (see `bind_with_budget`), so no symbol's referenced status is
//! ever affected by this truncation.

use crate::graph::confidence::Confidence;

/// Truncates `candidates` (each a `(dense_symbol_id, reasons_bits)` pair)
/// to its `max_per_reference` highest-confidence entries, in place. A
/// no-op when `candidates.len() <= max_per_reference` -- capping only ever
/// narrows an already-ambiguous reference, never pads or reorders one that
/// was already within budget.
///
/// Sorts by DESCENDING confidence with a stable sort, so candidates tied
/// on confidence keep their original (extraction) relative order rather
/// than an arbitrary one. Called once per reference, over that
/// reference's own (bounded by that one reference's ambiguity) candidate
/// list -- terminates after exactly one sort plus one truncate.
pub fn cap_top_n_by_confidence(candidates: &mut Vec<(u32, u16)>, max_per_reference: usize) {
    if candidates.len() <= max_per_reference {
        return;
    }
    candidates
        .sort_by(|(_, a_bits), (_, b_bits)| Confidence::derive(*b_bits).cmp(&Confidence::derive(*a_bits)));
    candidates.truncate(max_per_reference);
}

#[cfg(test)]
mod tests {
    use super::super::ladder::cap_top_n_by_confidence;
    use crate::graph::reasons;

    #[test]
    fn leaves_a_set_within_budget_untouched() {
        let mut candidates = vec![(1u32, reasons::SAME_FILE), (2u32, reasons::IMPORTED)];
        cap_top_n_by_confidence(&mut candidates, 5);
        assert_eq!(candidates.len(), 2);
    }

    /// The central discriminating case for the cap itself (separate from
    /// the referenced-bit guarantee, which is tested at the
    /// `bind_with_budget` level): given three candidates of strictly
    /// descending confidence, capping to 1 must keep the STRONGEST, not
    /// the first- or last-inserted one.
    #[test]
    fn keeps_the_highest_confidence_candidates_when_capped() {
        let mut candidates = vec![
            (1u32, reasons::SAME_FILE),
            (2u32, reasons::UNIQUE_NAME_IN_REPO),
            (3u32, reasons::SAME_PACKAGE),
        ];
        cap_top_n_by_confidence(&mut candidates, 1);
        assert_eq!(candidates, vec![(2u32, reasons::UNIQUE_NAME_IN_REPO)]);
    }

    #[test]
    fn ties_on_confidence_keep_original_relative_order() {
        let mut candidates =
            vec![(1u32, reasons::SAME_FILE), (2u32, reasons::SAME_FILE), (3u32, reasons::SAME_FILE)];
        cap_top_n_by_confidence(&mut candidates, 2);
        assert_eq!(candidates, vec![(1u32, reasons::SAME_FILE), (2u32, reasons::SAME_FILE)]);
    }
}
