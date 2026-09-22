//! AC4 Level 5 unique-name-shortcut tests, split out of
//! `resolve_tests_family.rs` (Messi Rule 6, anti-file-bloat -- that file
//! crossed the project's 1000-line limit) -- see `resolve_tests.rs` for
//! the overall split rationale. Wired the same way, as a sibling
//! `#[cfg(test)]` module declared directly under `resolve.rs`.

use super::tests::{file, method_decl};
use super::*;

/// AC4 Level 5: a name unique across the whole repo reaches
/// `Confidence::Exact` via `UNIQUE_NAME_IN_REPO` -- but ONLY when the
/// caller confirms the index is complete.
#[test]
fn unique_name_in_repo_resolves_to_a_single_exact_confidence_candidate() {
    use crate::graph::confidence::Confidence;

    let mut index = LocalIndex::new();
    index
        .declarations
        .push(method_decl("uniqueMethod", 1, 0, None));
    let name_index = RepoNameIndex::build(&[file(1, "java", index)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "uniqueMethod",
        REF_KIND_INVOCATION,
        1,
        &scope,
        None,
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
        false,
    );
    assert_eq!(candidates.len(), 1);
    let reasons_bits = candidates[0].1;
    assert_ne!(reasons_bits & reasons::UNIQUE_NAME_IN_REPO, 0);
    assert_eq!(Confidence::derive(reasons_bits), Confidence::Exact);
}

/// Dual-review defect D3 (Critical): `UNIQUE_NAME_IN_REPO` must NEVER
/// be claimed when the caller reports the index is PARTIAL (e.g. this
/// exact same fixture, but a sibling file elsewhere in the real repo
/// was dropped by `max_files` truncation and never made it into
/// `RepoNameIndex`). A wrong implementation that ignored
/// `index_is_complete` would pass the test right above this one and
/// still fail here -- the discriminating input is the SAME single
/// declaration, only the completeness flag differs.
#[test]
fn a_name_unique_only_in_a_partial_index_does_not_get_exact_confidence() {
    use crate::graph::confidence::Confidence;

    let mut index = LocalIndex::new();
    index
        .declarations
        .push(method_decl("uniqueMethod", 1, 0, None));
    let name_index = RepoNameIndex::build(&[file(1, "java", index)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "uniqueMethod",
        REF_KIND_INVOCATION,
        1,
        &scope,
        None,
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        false,
        false,
    );
    assert_eq!(
        candidates.len(),
        1,
        "the sole indexed declaration is still a candidate -- never dropped"
    );
    let reasons_bits = candidates[0].1;
    assert_eq!(
        reasons_bits & reasons::UNIQUE_NAME_IN_REPO,
        0,
        "UNIQUE_NAME_IN_REPO must not be claimed from a partial index"
    );
    assert_ne!(
        Confidence::derive(reasons_bits),
        Confidence::Exact,
        "a partial-index match must never reach Exact confidence"
    );
}

/// Bug #1912: the unique-name shortcut (AC4 Level 5) must tag
/// `RECEIVER_TYPE_MATCH` too when POSITIVE receiver-type evidence exists
/// and the sole candidate's `enclosing_type` is in the receiver's own
/// supertype closure (here, the receiver type itself) -- the shortcut
/// already computes that exact closure at `resolve.rs`'s `try_unique_
/// name_shortcut` to decide whether to DECLINE; before this fix, the
/// answer was used only to reject and discarded once the shortcut
/// accepted, leaving a qualified call to a unique-name method
/// byte-identical (`0x0040 [UNIQUE_NAME_IN_REPO]` only) to a hop with
/// zero receiver corroboration. Discriminating: on unfixed code this
/// assertion fails because `RECEIVER_TYPE_MATCH` is never set on this
/// path (see the `bare_call_marks_only_same_class_or_super_never_
/// receiver_type_match` guard in `bind/mod.rs`, which this fix must not
/// touch -- that guard's fixture has a pool of 2 and never reaches the
/// shortcut at all).
#[test]
fn unique_name_shortcut_tags_receiver_type_match_when_receiver_evidence_confirms_the_candidate() {
    use crate::graph::confidence::Confidence;
    use crate::graph::extract::local_index::MethodOwnerRecord;

    let mut index = LocalIndex::new();
    index
        .declarations
        .push(method_decl("uniqueHelper", 90, 0, Some(0)));
    index.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(90, 0),
        enclosing_type: "MatchType".to_string(),
    });
    let files = vec![file(90, "java", index)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::super::families::TypeIndex::build(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "uniqueHelper",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        Some("MatchType"),
        true,
        None,
        None,
        None,
        true,
        false,
    );

    assert_eq!(candidates.len(), 1, "the sole candidate must survive unchanged");
    let reasons_bits = candidates[0].1;
    assert_ne!(
        reasons_bits & reasons::UNIQUE_NAME_IN_REPO,
        0,
        "the shortcut must still fire and tag UNIQUE_NAME_IN_REPO"
    );
    assert_ne!(
        reasons_bits & reasons::RECEIVER_TYPE_MATCH,
        0,
        "receiver-type evidence confirmed the sole candidate -- the shortcut must tag \
         RECEIVER_TYPE_MATCH too, not discard the closure check it already performed"
    );
    assert_eq!(
        Confidence::derive(reasons_bits),
        Confidence::Exact,
        "both bits set must still derive Exact (UNIQUE_NAME_IN_REPO dominates)"
    );
}

/// Sibling negative case of the test above: an UNQUALIFIED call (no
/// receiver-type evidence at all, `receiver_type: None`) reaching the
/// SAME unique-name shortcut must gain ONLY `UNIQUE_NAME_IN_REPO` --
/// never `RECEIVER_TYPE_MATCH`, since there is no receiver to confirm
/// against. Without this guard, a naive fix could tag every shortcut hit
/// unconditionally, which would make the evidence meaningless in the
/// opposite direction (fabricating corroboration for an unqualified
/// call). This also doubles as the exact shape `bind/mod.rs`'s "bare
/// call must NEVER also carry RECEIVER_TYPE_MATCH" assertion protects,
/// now proven directly through the shortcut path that assertion's own
/// fixture (pool of 2) never reaches.
#[test]
fn unique_name_shortcut_does_not_tag_receiver_type_match_without_receiver_corroboration() {
    use crate::graph::extract::local_index::MethodOwnerRecord;

    let mut index = LocalIndex::new();
    index
        .declarations
        .push(method_decl("uniqueHelper", 91, 0, Some(0)));
    index.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(91, 0),
        enclosing_type: "MatchType".to_string(),
    });
    let files = vec![file(91, "java", index)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::super::families::TypeIndex::build(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "uniqueHelper",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        None,
        false,
        None,
        None,
        None,
        true,
        false,
    );

    assert_eq!(candidates.len(), 1);
    let reasons_bits = candidates[0].1;
    assert_ne!(reasons_bits & reasons::UNIQUE_NAME_IN_REPO, 0);
    assert_eq!(
        reasons_bits & reasons::RECEIVER_TYPE_MATCH,
        0,
        "an unqualified call has no receiver to corroborate against -- must never gain \
         RECEIVER_TYPE_MATCH"
    );
}

/// Companion regression guard (bug #1912's own AC2, "a qualified call
/// whose receiver type does NOT match still takes no edge (unchanged)"):
/// a POSITIVE receiver type that shares no relation with the sole
/// candidate's `enclosing_type` must make the shortcut DECLINE (falling
/// through to the full, tag-only pipeline) exactly as before this fix --
/// the sole candidate survives (never deleted) but earns neither
/// `UNIQUE_NAME_IN_REPO` nor `RECEIVER_TYPE_MATCH`.
#[test]
fn unique_name_shortcut_declines_and_tags_nothing_when_receiver_type_does_not_match() {
    use crate::graph::extract::local_index::MethodOwnerRecord;

    let mut index = LocalIndex::new();
    index
        .declarations
        .push(method_decl("uniqueHelper", 92, 0, Some(0)));
    index.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(92, 0),
        enclosing_type: "MatchType".to_string(),
    });
    let files = vec![file(92, "java", index)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::super::families::TypeIndex::build(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "uniqueHelper",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        Some("UnrelatedType"),
        true,
        None,
        None,
        None,
        true,
        false,
    );

    assert_eq!(
        candidates.len(),
        1,
        "the sole candidate must survive even when the shortcut declines"
    );
    let reasons_bits = candidates[0].1;
    assert_eq!(
        reasons_bits & reasons::UNIQUE_NAME_IN_REPO,
        0,
        "the shortcut must decline on a receiver-type mismatch, exactly as before this fix"
    );
    assert_eq!(
        reasons_bits & reasons::RECEIVER_TYPE_MATCH,
        0,
        "no receiver-type match exists, so the full pipeline must not tag it either"
    );
}
