//! Second half of `families.rs`'s relocated unit test suite (Messi
//! Rule 6, anti-file-bloat split, continued from `families_tests.rs`):
//! the remaining original `supertypes_of_*`/`is_*`/`unambiguous_field_
//! type_*` tests, plus the NEW `has_unresolved_external_supertype_
//! transitively_*` tests added for the #1931 rework's Codex P1 fix
//! (`family_transitively_unresolved_supertype_tests.rs`'s own small
//! `edge`/`file_with`/`file_with_known_types` helpers are duplicated
//! here rather than cross-referenced from the sibling `families_tests.rs`
//! module, matching this codebase's established per-file test-helper
//! convention).

use super::*;
use crate::graph::extract::local_index::{
    InheritanceKind, InheritanceRecord, LocalIndex, TypeNestingRecord,
};
use crate::graph::identity::make_symbol_id;

fn edge(subtype: &str, supertype: &str, kind: InheritanceKind) -> InheritanceRecord {
    InheritanceRecord {
        kind,
        subtype_name: subtype.to_string(),
        supertype_name: supertype.to_string(),
        line: 1,
    }
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

/// AC1/AC3 (Story #1806, S2b): a class that `implements` an interface
/// has that interface as a direct supertype; a class that `extends`
/// that implementor has the interface as a TRANSITIVE supertype --
/// both must be found. Mirrors `implementors_of_finds_direct_and_
/// transitive_implementors` exactly, walking the OPPOSITE direction.
#[test]
fn supertypes_of_finds_direct_and_transitive_supertypes() {
    let files = vec![file_with(
        1,
        vec![
            edge("C", "I", InheritanceKind::Implements),
            edge("D", "C", InheritanceKind::Extends),
        ],
        vec!["I".to_string()],
    )];
    let index = TypeIndex::build(&files);
    let supertypes = index.supertypes_of("D");
    assert!(supertypes.contains("C"));
    assert!(supertypes.contains("I"));
    assert_eq!(supertypes.len(), 2);
}

/// AC1/AC3: a type with NO declared supertypes anywhere in the repo
/// returns an EMPTY set, never a fabricated guess.
#[test]
fn supertypes_of_returns_empty_for_a_type_with_no_declared_supertypes() {
    let files = vec![file_with(1, Vec::new(), vec!["Lonely".to_string()])];
    let index = TypeIndex::build(&files);
    assert!(index.supertypes_of("Lonely").is_empty());
}

/// AC1/AC3 + Rule 14: a malformed/adversarial CYCLIC inheritance edge
/// set must still terminate -- this test itself times out (fails to
/// return) rather than failing an assertion if the implementation
/// loops forever.
#[test]
fn supertypes_of_terminates_on_a_cyclic_edge_set() {
    let files = vec![file_with(
        1,
        vec![
            edge("B", "A", InheritanceKind::Extends),
            edge("A", "B", InheritanceKind::Extends),
        ],
        Vec::new(),
    )];
    let index = TypeIndex::build(&files);
    let supertypes = index.supertypes_of("B");
    assert_eq!(supertypes, ["A"].into_iter().map(String::from).collect());
}

#[test]
fn is_interface_reports_known_interfaces_and_false_for_unknown_names() {
    let files = vec![file_with(1, Vec::new(), vec!["Shape".to_string()])];
    let index = TypeIndex::build(&files);
    assert!(index.is_interface("Shape"));
    assert!(!index.is_interface("NotAnInterface"));
}

/// P1-4 (#1898 code review): the substrate AC3's static-type receiver
/// resolution needs -- "is this bare identifier the name of a type
/// declared ANYWHERE in the repo", built from the SAME `type_nesting`
/// records `top_level_of` already reads, so no third parallel index of
/// declared type names is introduced. Two SEPARATE files, each
/// declaring a DIFFERENT type, prove this is a genuinely repo-wide
/// (not single-file) query.
#[test]
fn is_known_type_name_reports_every_declared_type_and_false_for_unknown_names() {
    let mut index_a = LocalIndex::new();
    index_a.type_nesting.push(TypeNestingRecord {
        type_name: "TimeUtil".to_string(),
        top_level_type: "TimeUtil".to_string(),
    });
    let mut index_b = LocalIndex::new();
    index_b.type_nesting.push(TypeNestingRecord {
        type_name: "ParserA".to_string(),
        top_level_type: "ParserA".to_string(),
    });
    let files = vec![
        FileForBind {
            file_id: 1,
            language: "java".to_string(),
            index: index_a,
        },
        FileForBind {
            file_id: 2,
            language: "java".to_string(),
            index: index_b,
        },
    ];
    let type_index = TypeIndex::build(&files);
    assert!(type_index.is_known_type_name("TimeUtil"));
    assert!(type_index.is_known_type_name("ParserA"));
    assert!(!type_index.is_known_type_name("NeverDeclared"));
}

/// P1-B (#1898 code review round 2, epic #1906): `is_known_field_name`
/// must see a field declared in ANY file, repo-wide -- the exact
/// substrate an inner-class field access or an inherited field (both
/// P1-B's own regression fixtures) needs, since the field's OWN
/// `field_declaration` may live in a different file from the call
/// site that reads it.
#[test]
fn is_known_field_name_reports_every_declared_field_and_false_for_unknown_names() {
    use crate::graph::extract::local_index::{NameScope, TypedNameRecord};

    let mut index_a = LocalIndex::new();
    index_a.typed_names.push(TypedNameRecord {
        name: "outerField".to_string(),
        declared_type: "int".to_string(),
        scope: NameScope::Field {
            enclosing_type: "Outer".to_string(),
        },
    });
    let mut index_b = LocalIndex::new();
    index_b.typed_names.push(TypedNameRecord {
        name: "count".to_string(),
        declared_type: "int".to_string(),
        scope: NameScope::Local {
            enclosing_method: make_symbol_id(2, 0),
        },
    });
    let files = vec![
        FileForBind {
            file_id: 1,
            language: "java".to_string(),
            index: index_a,
        },
        FileForBind {
            file_id: 2,
            language: "java".to_string(),
            index: index_b,
        },
    ];
    let type_index = TypeIndex::build(&files);
    assert!(type_index.is_known_field_name("outerField"));
    assert!(
        !type_index.is_known_field_name("count"),
        "a LOCAL-scoped typed name must never be reported as a known field"
    );
    assert!(!type_index.is_known_field_name("neverDeclared"));
}

/// P1-A (#1898 code review round 2, epic #1906): `is_known_type_
/// parameter_name` must see a type parameter declared in ANY file,
/// repo-wide -- mirrors `is_known_field_name`'s own repo-wide test
/// exactly.
#[test]
fn is_known_type_parameter_name_reports_every_declared_type_parameter_and_false_for_unknown_names(
) {
    let mut index_a = LocalIndex::new();
    index_a.type_parameter_names.push("T".to_string());
    let index_b = LocalIndex::new();
    let files = vec![
        FileForBind {
            file_id: 1,
            language: "java".to_string(),
            index: index_a,
        },
        FileForBind {
            file_id: 2,
            language: "java".to_string(),
            index: index_b,
        },
    ];
    let type_index = TypeIndex::build(&files);
    assert!(type_index.is_known_type_parameter_name("T"));
    assert!(!type_index.is_known_type_parameter_name("NeverDeclared"));
}

/// #1931: `is_nested_type_of` proves membership in the FULL set of
/// recorded nesting edges -- never merely "the" unambiguous top-level
/// owner of a bare name -- so a genuine `(Inner, OuterA)` edge is
/// found even when a DIFFERENT, unrelated `(Inner, OuterB)` edge is
/// ALSO recorded elsewhere in the repo (two distinct nested classes
/// sharing the bare name `Inner` under two different outers), and an
/// unrelated pair that was never recorded reports `false`.
#[test]
fn is_nested_type_of_reports_a_real_nesting_edge_and_false_for_an_unrelated_pair() {
    let mut index_a = LocalIndex::new();
    index_a.type_nesting.push(TypeNestingRecord {
        type_name: "Inner".to_string(),
        top_level_type: "OuterA".to_string(),
    });
    let mut index_b = LocalIndex::new();
    index_b.type_nesting.push(TypeNestingRecord {
        type_name: "Inner".to_string(),
        top_level_type: "OuterB".to_string(),
    });
    let files = vec![
        FileForBind {
            file_id: 1,
            language: "java".to_string(),
            index: index_a,
        },
        FileForBind {
            file_id: 2,
            language: "java".to_string(),
            index: index_b,
        },
    ];
    let type_index = TypeIndex::build(&files);

    assert!(
        type_index.is_nested_type_of("Inner", "OuterA"),
        "a real (Inner, OuterA) nesting edge must be found even though Inner is ALSO \
         nested under an unrelated OuterB elsewhere"
    );
    assert!(
        type_index.is_nested_type_of("Inner", "OuterB"),
        "the sibling (Inner, OuterB) edge must ALSO be found -- both are genuine, \
         independent facts"
    );
    assert!(
        !type_index.is_nested_type_of("Inner", "NeverAnOuter"),
        "a pair that was never recorded must never be reported as a real nesting edge"
    );
}

/// P1-B (#1898 code review round 2, epic #1906): `unambiguous_field_
/// type` must resolve a field declared in a DIFFERENT file from the
/// caller (the "inherited field" P1-B shape) when every record for
/// that bare name agrees, and return `None` when two unrelated
/// records disagree on the type (never guessed) or the name is
/// simply unknown.
#[test]
fn unambiguous_field_type_resolves_a_field_declared_in_a_different_file_and_returns_none_on_conflict(
) {
    use crate::graph::extract::local_index::{NameScope, TypedNameRecord};

    let field_file = |id: u32, owner: &str, name: &str, ty: &str| {
        let mut index = LocalIndex::new();
        index.typed_names.push(TypedNameRecord {
            name: name.to_string(),
            declared_type: ty.to_string(),
            scope: NameScope::Field {
                enclosing_type: owner.to_string(),
            },
        });
        FileForBind { file_id: id, language: "java".to_string(), index }
    };
    let files = vec![
        field_file(1, "Base", "inheritedField", "Svc"),
        field_file(2, "A", "conflicting", "int"),
        field_file(3, "B", "conflicting", "String"),
    ];
    let type_index = TypeIndex::build(&files);
    assert_eq!(
        type_index.unambiguous_field_type("inheritedField"),
        Some("Svc")
    );
    assert_eq!(
        type_index.unambiguous_field_type("conflicting"),
        None,
        "two unrelated fields sharing a bare name but disagreeing on type must never \
         resolve to either guessed type"
    );
    assert_eq!(type_index.unambiguous_field_type("neverDeclared"), None);
}

/// Builds a `FileForBind` whose `LocalIndex` records BOTH a real
/// self-referential `TypeNestingRecord` for every name in `known_
/// types` (so `is_known_type_name` reports them true, matching real
/// extraction's own shape for a top-level type) AND `inheritance`.
/// Any name used ONLY as a `supertype_name` in `inheritance` and
/// never listed in `known_types` stays genuinely UNKNOWN -- the
/// substrate `has_unresolved_external_supertype_transitively`'s own
/// tests below need to leave `ExternalBase` unregistered this way.
fn file_with_known_types(
    file_id: u32,
    known_types: &[&str],
    inheritance: Vec<InheritanceRecord>,
) -> FileForBind {
    let mut index = LocalIndex::new();
    for name in known_types {
        index.type_nesting.push(TypeNestingRecord {
            type_name: name.to_string(),
            top_level_type: name.to_string(),
        });
    }
    index.inheritance = inheritance;
    FileForBind {
        file_id,
        language: "java".to_string(),
        index,
    }
}

/// #1931 rework (Codex P1, third round): the DIRECT-only predicate
/// `has_unresolved_external_supertype` must NOT flag `Outer` here --
/// its own direct parent, `IndexedBase`, IS a known repo type. But
/// `IndexedBase` ITSELF extends `ExternalBase`, which is NOT a known
/// repo type (an external/unindexed grandparent) -- the TRANSITIVE
/// predicate must walk past `IndexedBase` to find this, since an
/// inherited field on `ExternalBase` is otherwise invisible no
/// matter how many DIRECT-only checks are performed.
#[test]
fn has_unresolved_external_supertype_transitively_catches_an_external_grandparent() {
    let files = vec![file_with_known_types(
        1,
        &["Outer", "IndexedBase"],
        vec![
            edge("Outer", "IndexedBase", InheritanceKind::Extends),
            edge("IndexedBase", "ExternalBase", InheritanceKind::Extends),
        ],
    )];
    let type_index = TypeIndex::build(&files);

    assert!(
        !type_index.has_unresolved_external_supertype("Outer"),
        "the DIRECT-only predicate must NOT flag Outer -- its own direct parent, \
         IndexedBase, IS a known repo type"
    );
    assert!(
        type_index.has_unresolved_external_supertype_transitively("Outer"),
        "the TRANSITIVE predicate must flag Outer -- its GRANDPARENT, ExternalBase, is \
         NOT a known repo type"
    );
}

/// Companion: a fully-resolved chain (every ancestor at every depth
/// is a known repo type) must never be flagged by either predicate.
#[test]
fn has_unresolved_external_supertype_transitively_is_false_for_a_fully_resolved_chain() {
    let files = vec![file_with_known_types(
        1,
        &["Outer", "IndexedBase", "IndexedRoot"],
        vec![
            edge("Outer", "IndexedBase", InheritanceKind::Extends),
            edge("IndexedBase", "IndexedRoot", InheritanceKind::Extends),
        ],
    )];
    let type_index = TypeIndex::build(&files);

    assert!(!type_index.has_unresolved_external_supertype_transitively("Outer"));
}

/// #1931 rework: cycle-safety (Rule 14) -- a malformed/adversarial
/// CYCLIC inheritance edge set (never valid real Java) must still
/// terminate, mirroring `supertypes_of_terminates_on_a_cyclic_edge_
/// set`'s own proof shape exactly.
#[test]
fn has_unresolved_external_supertype_transitively_terminates_on_a_cyclic_edge_set() {
    let files = vec![file_with_known_types(
        1,
        &["A", "B"],
        vec![
            edge("B", "A", InheritanceKind::Extends),
            edge("A", "B", InheritanceKind::Extends),
        ],
    )];
    let type_index = TypeIndex::build(&files);

    assert!(
        !type_index.has_unresolved_external_supertype_transitively("A"),
        "A and B are both known repo types cycling into each other -- neither is \
         external, so this must terminate and report false, never hang"
    );
}

/// #1931 round 4 (Codex P2, perf): proves `has_unresolved_external_
/// supertype_transitively` is a lookup into a map POPULATED ONCE by
/// `TypeIndex::build()`, never re-walked per query. If a future edit
/// reverted to lazy per-call computation (e.g. re-running the BFS inside
/// the query method itself, or only populating the map for the
/// first-queried name), this test would catch it: every known type name
/// already has an entry in the precomputed map immediately after
/// `build()` returns -- BEFORE `has_unresolved_external_supertype_
/// transitively` is ever called even once -- and querying it repeatedly
/// (including a name never seen at build time) never changes the map's
/// size, since the query method takes only `&self` with no interior
/// mutability anywhere on `TypeIndex` to grow it lazily.
#[test]
fn has_unresolved_external_supertype_transitively_is_precomputed_once_per_build_not_per_query() {
    let files = vec![file_with_known_types(
        1,
        &["Outer", "IndexedBase", "IndexedRoot"],
        vec![
            edge("Outer", "IndexedBase", InheritanceKind::Extends),
            edge("IndexedBase", "IndexedRoot", InheritanceKind::Extends),
        ],
    )];
    let type_index = TypeIndex::build(&files);

    assert_eq!(
        type_index.unresolved_external_supertype_transitively.len(),
        3,
        "every known type (Outer, IndexedBase, IndexedRoot) must already have a \
         precomputed answer immediately after build(), before any query is made"
    );

    let _ = type_index.has_unresolved_external_supertype_transitively("Outer");
    let _ = type_index.has_unresolved_external_supertype_transitively("Outer");
    let _ = type_index.has_unresolved_external_supertype_transitively("IndexedRoot");
    let _ = type_index.has_unresolved_external_supertype_transitively("NeverSeenAtBuildTime");

    assert_eq!(
        type_index.unresolved_external_supertype_transitively.len(),
        3,
        "repeated queries, including one for a name never seen at build time, must not \
         grow the precomputed map -- its answers are fixed once, at build() time"
    );
}
