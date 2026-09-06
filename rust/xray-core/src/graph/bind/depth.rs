//! `BinderDepth` -- per-language report of which AC4 narrowing levels the
//! binder actually EXERCISED (produced real evidence for) for that
//! language (Story #1787, S2, AC4).
//!
//! Without this, `Confidence::SamePackage` (say) on a language whose
//! binder only ever reached level 0/2 would be silently indistinguishable
//! from the same `Confidence` value on Java, which also has levels 3-4
//! (S4, out of scope here) available to it. `BinderDepth` makes that
//! difference visible instead of leaving it as an unstated assumption.
//!
//! "Reached" is computed from ACTUAL evidence produced, never from "the
//! code path for this level executed": a level only counts as reached once
//! at least one candidate for that language actually carries the reasons
//! bit(s) that level is responsible for (see `super::mod` where this is
//! populated by scanning the real candidates a bind produced). This is
//! what keeps a language with no extractor (zero declarations, zero
//! references, therefore zero candidates ever produced) truthfully at
//! `levels_reached == 0` -- there is no code path that could ever ascribe
//! it a level as a side effect of merely being invoked.

/// Level 0: bare-name candidate lookup against the repo-wide name index,
/// with no additional narrowing evidence.
pub const LEVEL_0_BARE_NAME: u8 = 1 << 0;
/// Level 1: `+arity` -- `ARITY_MATCH` evidence was attached to at least one
/// candidate for this language.
pub const LEVEL_1_ARITY: u8 = 1 << 1;
/// Level 2: `+import context` -- one of `SAME_FILE`/`SAME_PACKAGE`/
/// `IMPORTED`/`STATIC_IMPORT`/`WILDCARD_IMPORT` was attached to at least
/// one candidate for this language.
pub const LEVEL_2_IMPORT_CONTEXT: u8 = 1 << 2;
/// Level 5: `unique-name-in-repo -> Exact` -- `UNIQUE_NAME_IN_REPO`
/// evidence was attached to at least one candidate for this language.
///
/// The gap between `1 << 2` and `1 << 5` is deliberate, not a packing bug:
/// AC4 explicitly numbers this level 5 (levels 3-4 are Java-only receiver-
/// type inference, S4, out of scope here), so this constant's shift keeps
/// the bit position matching the story's own level number exactly -- a
/// reader diffing this file against AC4 never has to mentally remap a
/// compacted 0..3 range back onto 0/1/2/5.
pub const LEVEL_5_UNIQUE_NAME: u8 = 1 << 5;

/// Per-language narrowing-depth report. One instance per DISTINCT language
/// seen across a `bind()` call's input files -- see `super::bind` for how
/// these are aggregated.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BinderDepth {
    pub language: String,
    pub levels_reached: u8,
}

impl BinderDepth {
    /// A fresh depth report for `language`, claiming NO levels reached yet.
    pub fn new(language: impl Into<String>) -> Self {
        BinderDepth { language: language.into(), levels_reached: 0 }
    }

    /// Records that `level_bit` was genuinely exercised (real evidence was
    /// produced) for this language. Idempotent: marking the same bit twice
    /// leaves `levels_reached` unchanged the second time.
    pub fn mark(&mut self, level_bit: u8) {
        self.levels_reached |= level_bit;
    }

    /// True once `mark(level_bit)` has been called at least once.
    pub fn reached(&self, level_bit: u8) -> bool {
        self.levels_reached & level_bit != 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_depth_starts_with_no_levels_reached() {
        let depth = BinderDepth::new("java");
        assert_eq!(depth.language, "java");
        assert_eq!(depth.levels_reached, 0);
        assert!(!depth.reached(LEVEL_0_BARE_NAME));
        assert!(!depth.reached(LEVEL_1_ARITY));
        assert!(!depth.reached(LEVEL_2_IMPORT_CONTEXT));
        assert!(!depth.reached(LEVEL_5_UNIQUE_NAME));
    }

    /// The discriminating case: marking ONE level must not falsely report
    /// any OTHER level as reached -- a wrong implementation that set all
    /// bits (or compared `levels_reached != 0` instead of masking the
    /// specific bit) would pass a less careful test but fail this one.
    #[test]
    fn marking_a_level_makes_it_reached_and_leaves_others_unreached() {
        let mut depth = BinderDepth::new("java");
        depth.mark(LEVEL_1_ARITY);

        assert!(depth.reached(LEVEL_1_ARITY));
        assert!(!depth.reached(LEVEL_0_BARE_NAME));
        assert!(!depth.reached(LEVEL_2_IMPORT_CONTEXT));
        assert!(!depth.reached(LEVEL_5_UNIQUE_NAME));
    }

    #[test]
    fn marking_the_same_level_twice_is_idempotent() {
        let mut depth = BinderDepth::new("java");
        depth.mark(LEVEL_5_UNIQUE_NAME);
        depth.mark(LEVEL_5_UNIQUE_NAME);
        assert_eq!(depth.levels_reached, LEVEL_5_UNIQUE_NAME);
    }

    #[test]
    fn multiple_marked_levels_accumulate_independently() {
        let mut depth = BinderDepth::new("java");
        depth.mark(LEVEL_0_BARE_NAME);
        depth.mark(LEVEL_2_IMPORT_CONTEXT);
        assert!(depth.reached(LEVEL_0_BARE_NAME));
        assert!(depth.reached(LEVEL_2_IMPORT_CONTEXT));
        assert!(!depth.reached(LEVEL_1_ARITY));
        assert!(!depth.reached(LEVEL_5_UNIQUE_NAME));
    }
}
