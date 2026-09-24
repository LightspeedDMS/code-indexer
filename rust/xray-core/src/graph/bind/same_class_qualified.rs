//! Issue #1956: the qualified cross-package-decoy check `narrowing::
//! apply_same_class_or_super_narrowing` uses, split out into its own file
//! (Rule 6, anti-file-bloat -- `narrowing.rs` crossed the project's
//! 1000-line cap once this logic was written inline).
//!
//! The one query value in this whole binder whose OWN package is always
//! exactly known: `same_class_context` (an unqualified/`this`/bare call's
//! enclosing type) is always declared IN the calling file itself, so it is
//! always exactly qualifiable via that file's own `package` -- never a
//! guess (see `families::qualify_bare_type_name`'s own doc comment).
//! `receiver_type`, used by the sibling `apply_receiver_type_narrowing`,
//! has no such attribution (see `families.rs`'s own `qualified_direct_
//! parents` field doc) -- this is why only THIS one narrowing pass is
//! migrated to the qualified graph.
//!
//! Governing invariant: withdrawal of a bare match requires POSITIVE proof
//! (the qualified graph resolved SOME node sharing the candidate's exact
//! bare name, and this specific candidate's own qualified identity is not
//! among the resolved set) -- never a guess, and never on missing/
//! ambiguous evidence (an unresolved import, a no-package file, or simply
//! no qualified edge for this bare name at all), which falls back to
//! keeping the ORIGINAL bare tag unconditionally. This mirrors the exact
//! rule Attempt 1 (see issue #1956's own history) violated by trusting an
//! unproven same-bare-named candidate as "the resolved ancestor".

use super::families::{qualify_bare_type_name, TypeIndex};
use std::collections::HashSet;

/// The calling site's own qualified ancestor evidence, computed once per
/// `apply_same_class_or_super_narrowing` call and reused across every
/// candidate in `matching`.
pub(super) struct QualifiedAncestry {
    /// `{qualified_enclosing_type} U supertypes_of_qualified(qualified_
    /// enclosing_type)` -- every qualified identity this call's own
    /// ancestry (self included) resolves to.
    allowed_qualified: HashSet<String>,
    /// The bare (unqualified, last-dotted-segment) name of every entry in
    /// `allowed_qualified` -- i.e. every bare name the qualified graph has
    /// "an opinion about" for this call. A bare name ABSENT from this set
    /// means the qualified graph could not resolve anything for it (no
    /// edge, an unresolved import, ...) -- genuinely no opinion, never
    /// grounds for withdrawal.
    qualified_bare_ancestor_names: HashSet<String>,
}

impl QualifiedAncestry {
    /// `enclosing_type` is the caller's own bare enclosing type
    /// (`same_class_context`); `caller_package` is the calling file's own
    /// recorded package (`None` for the Java default package) -- both
    /// exact, never guessed, since `enclosing_type` is declared IN that
    /// same file.
    pub(super) fn compute(
        enclosing_type: &str,
        caller_package: Option<&str>,
        type_index: &TypeIndex,
    ) -> Self {
        let qualified_enclosing_type = qualify_bare_type_name(enclosing_type, caller_package);
        let mut allowed_qualified = type_index.supertypes_of_qualified(&qualified_enclosing_type);
        allowed_qualified.insert(qualified_enclosing_type);
        let qualified_bare_ancestor_names = allowed_qualified
            .iter()
            .map(|qualified| qualified.rsplit('.').next().unwrap_or(qualified).to_string())
            .collect();
        QualifiedAncestry {
            allowed_qualified,
            qualified_bare_ancestor_names,
        }
    }

    /// True ONLY when the qualified graph has POSITIVE, resolved evidence
    /// that `(candidate_bare_name, candidate_package)` is a DIFFERENT type
    /// from every ancestor/self this call's own qualified chain resolved --
    /// i.e. this bare name IS one the qualified graph has an opinion about
    /// (`qualified_bare_ancestor_names.contains`), yet this exact candidate's
    /// own qualified identity is not among the resolved set. `false`
    /// whenever the qualified graph has NO opinion on this bare name at all
    /// (an unresolved import upstream, a no-package file, or simply no
    /// recorded edge) -- the caller must then fall back to its own,
    /// unmodified bare-match result.
    pub(super) fn is_proven_cross_package_decoy(
        &self,
        candidate_bare_name: &str,
        candidate_package: Option<&str>,
    ) -> bool {
        self.qualified_bare_ancestor_names.contains(candidate_bare_name) && {
            let candidate_qualified = qualify_bare_type_name(candidate_bare_name, candidate_package);
            !self.allowed_qualified.contains(&candidate_qualified)
        }
    }
}
