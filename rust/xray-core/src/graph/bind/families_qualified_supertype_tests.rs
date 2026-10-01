//! Issue #1956: unit tests for `TypeIndex::supertypes_of_qualified`, the
//! QUALIFIED counterpart of `supertypes_of` (see `families.rs`'s own
//! `qualified_direct_parents` field doc for why this is a separate,
//! ADDITIVE substrate rather than a replacement of the existing bare-
//! name-keyed `direct_parents`/`direct_children` every pre-existing
//! narrowing consumer still relies on).
//!
//! Governing invariant these tests hold the implementation to: ambiguity
//! resolves to UNRESOLVED, never a guess -- the same rule Attempt 1 (see
//! issue #1956's own history) violated by trusting a same-bare-named
//! analyzed type as "the resolved ancestor".

use super::*;
use crate::graph::extract::local_index::{
    Declaration, DeclarationKind, ImportKind, ImportRecord, InheritanceKind, InheritanceRecord,
    LocalIndex,
};
use crate::graph::identity::make_symbol_id;

fn package_declaration(file_id: u32, name: &str) -> Declaration {
    Declaration {
        kind: DeclarationKind::Package,
        name: name.to_string(),
        line: 1,
        symbol: make_symbol_id(file_id, 0),
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    }
}

fn edge(subtype: &str, supertype: &str) -> InheritanceRecord {
    InheritanceRecord {
        kind: InheritanceKind::Extends,
        subtype_name: subtype.to_string(),
        supertype_name: supertype.to_string(),
        line: 1,
    }
}

fn file_with_package(
    file_id: u32,
    package: Option<&str>,
    imports: Vec<ImportRecord>,
    inheritance: Vec<InheritanceRecord>,
) -> FileForBind {
    let mut index = LocalIndex::new();
    if let Some(package) = package {
        index.declarations.push(package_declaration(file_id, package));
    }
    index.imports = imports;
    index.inheritance = inheritance;
    FileForBind {
        file_id,
        language: "java".to_string(),
        index,
    }
}

fn ordinary_import(path: &str) -> ImportRecord {
    ImportRecord {
        kind: ImportKind::Ordinary,
        path: path.to_string(),
        line: 1,
    }
}

fn wildcard_import(path: &str) -> ImportRecord {
    ImportRecord {
        kind: ImportKind::Wildcard,
        path: path.to_string(),
        line: 1,
    }
}

/// Issue #1956's own acceptance test: `Sub` (package `com.example.a`)
/// extends bare `Base`. An UNRELATED, same-bare-named `Base` also exists
/// in a DIFFERENT package (`com.example.b`) -- a decoy, exactly the shape
/// the bare-name-keyed substrate cannot distinguish. The qualified
/// supertype must be `com.example.a.Base` alone; the decoy must never
/// appear, no matter how many same-bare-named types exist elsewhere in
/// the repo.
#[test]
fn qualified_supertypes_of_keeps_same_bare_named_types_in_different_packages_distinct() {
    let files = vec![
        file_with_package(1, Some("com.example.a"), Vec::new(), Vec::new()), // Base (real)
        file_with_package(2, Some("com.example.b"), Vec::new(), Vec::new()), // Base (decoy)
        file_with_package(
            3,
            Some("com.example.a"),
            Vec::new(),
            vec![edge("Sub", "Base")],
        ),
    ];
    let type_index = TypeIndex::build(&files);

    let supertypes = type_index.supertypes_of_qualified("com.example.a.Sub");
    assert!(
        supertypes.contains("com.example.a.Base"),
        "Sub's own same-package Base must be its qualified supertype -- got {supertypes:?}"
    );
    assert!(
        !supertypes.contains("com.example.b.Base"),
        "an unrelated, same-bare-named Base in a DIFFERENT package must never be treated \
         as Sub's supertype just because they share the bare name 'Base' -- got {supertypes:?}"
    );
    assert_eq!(
        supertypes.len(),
        1,
        "exactly one qualified supertype expected -- got {supertypes:?}"
    );
}

/// An ORDINARY (single-type) import naming the real, cross-package
/// supertype must win over the same-package assumption: `Sub` is
/// declared in `com.example.b` but explicitly imports `com.example.a.
/// Base`, so its qualified supertype is `com.example.a.Base`, never the
/// same-package guess `com.example.b.Base`.
#[test]
fn qualified_supertypes_of_resolves_an_ordinary_import_over_the_same_package_assumption() {
    let files = vec![file_with_package(
        1,
        Some("com.example.b"),
        vec![ordinary_import("com.example.a.Base")],
        vec![edge("Sub", "Base")],
    )];
    let type_index = TypeIndex::build(&files);

    let supertypes = type_index.supertypes_of_qualified("com.example.b.Sub");
    assert_eq!(
        supertypes,
        HashSet::from(["com.example.a.Base".to_string()]),
        "an ordinary import naming the real supertype must be resolved over the \
         same-package assumption -- got {supertypes:?}"
    );
}

/// A WILDCARD import (`import pkg.*;`) makes "same package, or this
/// wildcard-imported package" genuinely ambiguous -- this substrate must
/// treat that as UNRESOLVED, never guess same-package. No edge is
/// recorded at all, so the qualified supertype set stays empty.
#[test]
fn qualified_supertypes_of_treats_a_wildcard_import_as_unresolved_never_a_guess() {
    let files = vec![file_with_package(
        1,
        Some("com.example.b"),
        vec![wildcard_import("com.example.other")],
        vec![edge("Sub", "Base")],
    )];
    let type_index = TypeIndex::build(&files);

    let supertypes = type_index.supertypes_of_qualified("com.example.b.Sub");
    assert!(
        supertypes.is_empty(),
        "a wildcard import competing with the same-package assumption must resolve to \
         UNRESOLVED (no edge), never a guessed qualified name -- got {supertypes:?}"
    );
}

/// A file with NO `package` statement (the Java default package) must
/// preserve a bare-name fallback: both `Sub` and `Base` qualify to their
/// own bare names, unprefixed.
#[test]
fn qualified_supertypes_of_preserves_a_bare_name_fallback_when_the_file_has_no_package() {
    let files = vec![file_with_package(1, None, Vec::new(), vec![edge("Sub", "Base")])];
    let type_index = TypeIndex::build(&files);

    let supertypes = type_index.supertypes_of_qualified("Sub");
    assert_eq!(
        supertypes,
        HashSet::from(["Base".to_string()]),
        "a file declaring no package must qualify its own types to their bare names \
         unprefixed -- got {supertypes:?}"
    );
}
