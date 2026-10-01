//! Issue #1956 (the safe-exclusion half of #1952): a SEPARATE, narrower
//! hard-exclusion pass for a type-QUALIFIED call's receiver
//! (`Util.normalize(x)`), independent of -- and NEVER gated behind --
//! `receiver_type_qualifier::file_is_safe_for_type_qualifier_narrowing`'s
//! whole-file safety flag (`narrowing::apply_type_qualifier_narrowing`'s
//! own gate, which stays exactly as conservative as it always was).
//!
//! **Why this can be sound where a whole-file-gated hard-narrow is not.**
//! `apply_type_qualifier_narrowing`'s own exclusive `retain` is unsafe on a
//! file with ANY supertype evidence because the receiver identifier could
//! be an INHERITED FIELD shadowing what looks like a type reference (see
//! that function's own doc comment) -- in which case the resolved
//! `receiver_type` is simply WRONG, and blindly keeping only candidates
//! matching that wrong guess can delete the real target outright (a
//! same-bare-name field's declared type, sharing none of the qualifier's
//! own bare name at all -- see `bug_1922_bare_name_supertype_regressions.
//! rs`'s own `Svc`/`Worker` shape). This module closes that exact gap
//! with THREE conditions, all independently necessary, before it will ever
//! remove a single candidate:
//!
//! 1. **The receiver's qualified identity must be EXACT, never guessed.**
//!    `families::resolve_via_ordinary_import` only ever returns `Some`
//!    when an ORDINARY (single-type) import in the CALLING file's own
//!    import list names the receiver's bare type by its exact last dotted
//!    segment -- there is no same-package fallback here (unlike
//!    `families::resolve_supertype_to_qualified_name`, which the
//!    inheritance-graph substrate uses and which this deliberately does
//!    NOT reuse for that reason): a same-package guess is an accepted
//!    imprecision for TAG adjustments elsewhere in this binder, never
//!    sound enough to justify removing a candidate from the graph.
//! 2. **No field anywhere in the ANALYZED repo shares the receiver's bare
//!    name** (`TypeIndex::is_known_field_name`, the exact same repo-wide
//!    guard `receiver::resolve_identifier_receiver`'s own static-type-name
//!    fallback and `receiver_type_qualifier::is_definite_type_qualifier`
//!    already trust -- Rule 4, anti-duplication).
//! 3. **The calling site's own enclosing type has a FULLY-RESOLVED
//!    ancestor chain** (`TypeIndex::has_unresolved_external_supertype_
//!    transitively` is `false`) -- every ancestor, at every depth, is
//!    itself a repo-declared, ANALYZED type. Combined with condition 2,
//!    this is what actually closes Attempt 1's own gap (see issue #1956's
//!    own history): condition 2 alone only proves "no field named X is
//!    declared in any ANALYZED file"; it says nothing about a field
//!    hiding in an EXCLUDED/external ancestor this binder never saw.
//!    Condition 3 proves there IS no such excluded ancestor anywhere in
//!    this call's own chain -- every hop was actually analyzed, so
//!    condition 2's repo-wide field census is, for THIS call site,
//!    complete, not merely "complete among what we happened to see".
//!    Attempt 1 (#1956's own history) had no equivalent to this condition
//!    and, worse, judged its analogous check by BARE name against a
//!    bare-keyed `TypeIndex` -- indistinguishable from a same-bare-named
//!    decoy standing in for a genuinely excluded ancestor. This module
//!    accepts the SAME pre-existing bare-name imprecision `has_unresolved_
//!    external_supertype_transitively` already documents (shared with
//!    `receiver_mismatch.rs`'s own condition (d) for an analogous
//!    purpose) rather than inventing a new one -- but even that shared
//!    imprecision only matters when this module's OWN condition 1 (an
//!    ordinary import) also happens to be present, a combination none of
//!    the existing regression corpus constructs.
//!
//! JAVA ONLY, deliberately, mirroring `receiver_is_type_qualifier`'s own
//! gate in `mod.rs`: Kotlin's extractor never populates `typed_names`, so
//! an uppercase Kotlin property/object receiver is structurally
//! indistinguishable from a genuine type reference on this substrate.
//!
//! **What this pass removes.** Once all three conditions hold, EVERY
//! candidate whose bare `enclosing_type` is neither (a) the receiver's own
//! bare type name WITH a matching qualified identity, nor (b) one of that
//! type's BARE transitive supertypes (the same, pre-existing, unqualified
//! `TypeIndex::supertypes_of` substrate `apply_receiver_type_narrowing`
//! already tags on) is excluded. A same-bare-name candidate declared under
//! a DIFFERENT, provably non-matching package is the one new thing this
//! removes relative to today (the actual jsoup shape #1956 reports); an
//! unrelated, differently-bare-named candidate surviving only via
//! same-file/same-package import-context evidence is _also_ removed here
//! -- exactly the `Leaf.normalize`/`TextNode.normaliseWhitespace`
//! over-binding #1956 exists to close, now that conditions 1-3 make doing
//! so provably safe for this specific call site.
//!
//! Never fires (a no-op, exactly today's behaviour) when no candidate
//! ALREADY exactly matches the receiver's own proven qualified identity --
//! mirroring `apply_type_qualifier_narrowing`'s own `has_positive_match`
//! guard: missing/inconclusive evidence never narrows to empty.

use super::families::{resolve_via_ordinary_import, TypeIndex};
use super::name_index::DeclInfo;
use crate::graph::extract::local_index::ImportRecord;

/// See this module's own doc comment for the full three-condition
/// soundness argument. `receiver_type` is the bare type name already
/// resolved by `receiver::resolve_receiver_type` (Positive or Advisory --
/// this pass does not care which tier, since its own soundness rests on
/// conditions 1-3 below, not on the receiver-evidence tier). `caller_
/// enclosing_type` is the call SITE's own immediately enclosing type
/// (`site.enclosing_type`, NOT `same_class_context`, which is `None` for
/// every qualified call this pass exists to handle).
pub(super) fn apply_receiver_qualified_type_narrowing(
    candidates: &mut Vec<(DeclInfo, u16)>,
    receiver_type: Option<&str>,
    caller_enclosing_type: Option<&str>,
    is_java_file: bool,
    imports: &[ImportRecord],
    type_index: &TypeIndex,
) {
    if !is_java_file {
        return;
    }
    let Some(receiver_type) = receiver_type else {
        return;
    };
    // Condition 2.
    if type_index.is_known_field_name(receiver_type) {
        return;
    }
    // Condition 3.
    if let Some(enclosing) = caller_enclosing_type {
        if type_index.has_unresolved_external_supertype_transitively(enclosing) {
            return;
        }
    }
    // Condition 1.
    let Some(qualified_receiver) = resolve_via_ordinary_import(receiver_type, imports) else {
        return;
    };
    let is_exact_qualified_match = |decl: &DeclInfo| -> bool {
        decl.package
            .as_deref()
            .is_some_and(|package| format!("{package}.{receiver_type}") == qualified_receiver)
    };
    let has_exact_match = candidates.iter().any(|(decl, _)| {
        decl.enclosing_type.as_deref() == Some(receiver_type) && is_exact_qualified_match(decl)
    });
    if !has_exact_match {
        return;
    }
    let allowed_supertypes = type_index.supertypes_of(receiver_type);
    candidates.retain(|(decl, _)| {
        let Some(owner) = decl.enclosing_type.as_deref() else {
            return false;
        };
        if owner == receiver_type {
            return is_exact_qualified_match(decl);
        }
        allowed_supertypes.contains(owner)
    });
}

#[cfg(test)]
#[path = "receiver_qualified_tests.rs"]
mod tests;
