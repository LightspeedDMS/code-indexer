//! `families.rs`'s unit tests, split into their own file (Messi Rule 6,
//! anti-file-bloat -- that file reached 1037 lines once the #1931 rework's
//! transitive-supertype tests were added), wired via `#[cfg(test)]
//! #[path = "families_tests.rs"] mod tests;`, mirroring the pre-existing
//! `receiver.rs`/`receiver_tests.rs` split pattern. This file holds the
//! ORIGINAL relocated test suite; the NEW transitive-supertype tests plus
//! the remaining original `supertypes_of_*`/`is_*`/`unambiguous_field_
//! type_*` tests live in the sibling file `families_transitive_
//! supertype_tests.rs`, keeping each test module under the 500-line
//! guideline.

use super::super::name_index::DeclInfo;
use super::*;
use crate::graph::extract::local_index::{
    DeclarationKind, InheritanceKind, InheritanceRecord, LocalIndex, Visibility,
};
use crate::graph::identity::make_symbol_id;

fn method_decl_info(file_id: u32, local: u32, enclosing_type: &str) -> DeclInfo {
    DeclInfo {
        symbol: make_symbol_id(file_id, local),
        file_id,
        package: None,
        kind: DeclarationKind::Method,
        param_count: Some(0),
        enclosing_type: Some(enclosing_type.to_string()),
        param_types: Vec::new(),
        is_varargs: false,
        language: "java".to_string(),
        return_type: None,
        visibility: Visibility::Unknown,
    }
}

fn edge(subtype: &str, supertype: &str, kind: InheritanceKind) -> InheritanceRecord {
    InheritanceRecord {
        kind,
        subtype_name: subtype.to_string(),
        supertype_name: supertype.to_string(),
        line: 1,
    }
}

/// AC1: `overrides_of` filters an EXISTING same-named-method pool down
/// to declarations whose enclosing type is a known implementor --
/// excluding both the interface's own declaration and any unrelated
/// same-named method in a type with no inheritance relation at all.
#[test]
fn overrides_of_filters_the_pool_to_known_implementors_only() {
    const SHARED_FILE_ID: u32 = 1;
    // Three distinct `DeclInfo`s (distinct `local` symbol indices),
    // all named "save" by fixture convention, in three different
    // enclosing types: the interface itself, its real implementor,
    // and an unrelated type with NO inheritance relation to "Repo".
    const REPO_METHOD_LOCAL: u32 = 0;
    const IMPL_METHOD_LOCAL: u32 = 1;
    const UNRELATED_METHOD_LOCAL: u32 = 2;

    let files = vec![file_with(
        SHARED_FILE_ID,
        vec![edge("Impl", "Repo", InheritanceKind::Implements)],
        vec!["Repo".to_string()],
    )];
    let index = TypeIndex::build(&files);

    let pool = vec![
        method_decl_info(SHARED_FILE_ID, REPO_METHOD_LOCAL, "Repo"),
        method_decl_info(SHARED_FILE_ID, IMPL_METHOD_LOCAL, "Impl"),
        method_decl_info(SHARED_FILE_ID, UNRELATED_METHOD_LOCAL, "UnrelatedType"),
    ];
    let (overrides, truncated) = index.overrides_of("Repo", &pool);
    assert_eq!(overrides.len(), 1);
    assert_eq!(overrides[0].enclosing_type.as_deref(), Some("Impl"));
    assert!(
        !truncated,
        "a family well under the cap must never report truncation"
    );
}

/// Shared fixture for the `MAX_FAMILY_SIZE` boundary tests below:
/// `count` distinct types, each implementing interface `"Repo"` and
/// each contributing exactly one same-named pool entry, so the pool's
/// match count against `"Repo"` is exactly `count`.
fn single_interface_family_fixture(count: usize) -> (TypeIndex, Vec<DeclInfo>) {
    const SHARED_FILE_ID: u32 = 1;
    let implementor_names: Vec<String> = (0..count).map(|i| format!("Impl{i}")).collect();
    let edges: Vec<InheritanceRecord> = implementor_names
        .iter()
        .map(|name| edge(name, "Repo", InheritanceKind::Implements))
        .collect();
    let files = vec![file_with(SHARED_FILE_ID, edges, vec!["Repo".to_string()])];
    let index = TypeIndex::build(&files);
    let pool: Vec<DeclInfo> = implementor_names
        .iter()
        .enumerate()
        .map(|(i, name)| method_decl_info(SHARED_FILE_ID, i as u32, name))
        .collect();
    (index, pool)
}

/// Memory-safety amendment (real 21.8GB-RSS incident on Elasticsearch,
/// 31,929 files, killed before it exhausted the host -- see the
/// story's own remediation notes): `overrides_of` MUST hard-cap its
/// result at `MAX_FAMILY_SIZE` and report `truncated = true` whenever
/// the true match count exceeds it -- never silently return a
/// truncated set indistinguishable from "the family really only has
/// this many members" (that would be the confidently-wrong outcome
/// AC1 exists to prevent). `MAX_FAMILY_SIZE + 5` distinct implementors
/// is the minimal discriminating fixture: strictly more matches than
/// the cap allows, so a wrong implementation that never capped at all
/// (returning all `MAX_FAMILY_SIZE + 5`) fails this test just as
/// loudly as one that capped but forgot to report `truncated`.
#[test]
fn overrides_of_caps_family_size_and_reports_truncation() {
    let (index, pool) = single_interface_family_fixture(MAX_FAMILY_SIZE + 5);
    let (overrides, truncated) = index.overrides_of("Repo", &pool);
    assert_eq!(
        overrides.len(),
        MAX_FAMILY_SIZE,
        "result must be capped at exactly MAX_FAMILY_SIZE"
    );
    assert!(
        truncated,
        "exceeding the cap must be reported, never silently swallowed"
    );
}

/// Companion to the cap test: exactly `MAX_FAMILY_SIZE` matches (not
/// one more) must NOT report truncation -- the boundary is "strictly
/// more than the cap", not "at or above it".
#[test]
fn overrides_of_does_not_report_truncation_when_exactly_at_the_cap() {
    let (index, pool) = single_interface_family_fixture(MAX_FAMILY_SIZE);
    let (overrides, truncated) = index.overrides_of("Repo", &pool);
    assert_eq!(overrides.len(), MAX_FAMILY_SIZE);
    assert!(
        !truncated,
        "exactly-at-cap must not be reported as truncated"
    );
}

fn file_with(
    file_id: u32,
    inheritance: Vec<InheritanceRecord>,
    interface_names: Vec<String>,
) -> FileForBind {
    let mut index = LocalIndex::new();
    index.inheritance = inheritance;
    index.interface_names = interface_names;
    FileForBind {
        file_id,
        language: "java".to_string(),
        index,
    }
}

/// AC1: a class that `implements` an interface is a direct
/// implementor; a class that `extends` that implementor is a
/// TRANSITIVE implementor -- both must be found.
#[test]
fn implementors_of_finds_direct_and_transitive_implementors() {
    let files = vec![file_with(
        1,
        vec![
            edge("C", "I", InheritanceKind::Implements),
            edge("D", "C", InheritanceKind::Extends),
        ],
        vec!["I".to_string()],
    )];
    let index = TypeIndex::build(&files);
    let implementors = index.implementors_of("I");
    assert!(implementors.contains("C"));
    assert!(implementors.contains("D"));
    assert_eq!(implementors.len(), 2);
}

/// AC1: a name with NO implementors anywhere in the repo returns an
/// EMPTY set, never a fabricated guess.
#[test]
fn implementors_of_returns_empty_for_a_name_with_no_implementors() {
    let files = vec![file_with(1, Vec::new(), vec!["Lonely".to_string()])];
    let index = TypeIndex::build(&files);
    assert!(index.implementors_of("Lonely").is_empty());
}

/// AC1 + Rule 14 (anti-unbounded-loop): diamond inheritance (two
/// sub-interfaces of `I`, both implemented by the SAME class `C`)
/// must converge on `C` exactly once, not loop or double-count.
#[test]
fn implementors_of_converges_on_diamond_inheritance() {
    let files = vec![file_with(
        1,
        vec![
            edge("J", "I", InheritanceKind::Extends),
            edge("K", "I", InheritanceKind::Extends),
            edge("C", "J", InheritanceKind::Implements),
            edge("C", "K", InheritanceKind::Implements),
        ],
        vec!["I".to_string(), "J".to_string(), "K".to_string()],
    )];
    let index = TypeIndex::build(&files);
    let implementors = index.implementors_of("I");
    assert_eq!(
        implementors,
        ["J", "K", "C"].into_iter().map(String::from).collect()
    );
}

/// AC1 + Rule 14: a malformed/adversarial CYCLIC inheritance edge set
/// (never valid real Java, but the binder must not trust its own
/// heuristic extraction to always be well-formed) must still
/// terminate -- this test itself times out (fails to return) rather
/// than failing an assertion if the implementation loops forever.
#[test]
fn implementors_of_terminates_on_a_cyclic_edge_set() {
    let files = vec![file_with(
        1,
        vec![
            edge("B", "A", InheritanceKind::Extends),
            edge("A", "B", InheritanceKind::Extends),
        ],
        Vec::new(),
    )];
    let index = TypeIndex::build(&files);
    let implementors = index.implementors_of("A");
    assert_eq!(implementors, ["B"].into_iter().map(String::from).collect());
}
