//! Issue #1956: unit-level proofs for `apply_receiver_qualified_type_
//! narrowing`'s own three-condition soundness argument (see the parent
//! module's doc comment) -- each test isolates ONE condition at a time,
//! keeping every other input in its safe/no-op configuration so a failure
//! localizes to the exact condition it exercises.

use super::*;
use crate::graph::bind::families::TypeIndex;
use crate::graph::bind::name_index::DeclInfo;
use crate::graph::bind::FileForBind;
use crate::graph::extract::local_index::{
    Declaration, DeclarationKind, ImportKind, ImportRecord, InheritanceKind, InheritanceRecord,
    LocalIndex, NameScope, TypeNestingRecord, TypedNameRecord, Visibility,
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

fn ordinary_import(path: &str) -> ImportRecord {
    ImportRecord {
        kind: ImportKind::Ordinary,
        path: path.to_string(),
        line: 1,
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

fn empty_file(file_id: u32, package: Option<&str>) -> FileForBind {
    let mut index = LocalIndex::new();
    if let Some(package) = package {
        index.declarations.push(package_declaration(file_id, package));
    }
    FileForBind {
        file_id,
        language: "java".to_string(),
        index,
    }
}

fn file_with_inheritance(
    file_id: u32,
    package: Option<&str>,
    inheritance: Vec<InheritanceRecord>,
) -> FileForBind {
    let mut file = empty_file(file_id, package);
    file.index.inheritance = inheritance;
    file
}

fn decl_info(file_id: u32, local: u32, package: Option<&str>, enclosing_type: &str) -> DeclInfo {
    DeclInfo {
        symbol: make_symbol_id(file_id, local),
        file_id,
        package: package.map(str::to_string),
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

/// The baseline fixture every test starts from: `Util` (package
/// `com.example.util`) and `Leaf` (package `com.example.core`, no
/// supertype evidence of its own -- kept minimal since these unit tests
/// each isolate one condition at a time) both declare a same-named
/// method. `caller` imports `com.example.util.Util` ordinarily.
fn base_candidates() -> Vec<(DeclInfo, u16)> {
    vec![
        (decl_info(10, 0, Some("com.example.util"), "Util"), 0u16),
        (decl_info(11, 0, Some("com.example.core"), "Leaf"), 0u16),
    ]
}

fn imports() -> Vec<ImportRecord> {
    vec![ordinary_import("com.example.util.Util")]
}

#[test]
fn excludes_a_cross_package_decoy_sharing_the_receivers_exact_bare_name() {
    let files = vec![empty_file(1, Some("com.example.core"))];
    let type_index = TypeIndex::build(&files);
    let mut candidates = base_candidates();
    candidates.push((decl_info(12, 0, Some("com.example.other"), "Util"), 0u16));

    apply_receiver_qualified_type_narrowing(
        &mut candidates,
        Some("Util"),
        Some("Caller"),
        true,
        &imports(),
        &type_index,
    );

    assert_eq!(candidates.len(), 1, "only the real, correctly-qualified Util must survive");
    assert_eq!(candidates[0].0.package.as_deref(), Some("com.example.util"));
}

#[test]
fn excludes_an_unrelated_candidate_surviving_only_via_same_package_over_binding() {
    let files = vec![empty_file(1, Some("com.example.core"))];
    let type_index = TypeIndex::build(&files);
    let mut candidates = base_candidates();

    apply_receiver_qualified_type_narrowing(
        &mut candidates,
        Some("Util"),
        Some("Caller"),
        true,
        &imports(),
        &type_index,
    );

    assert_eq!(
        candidates.len(),
        1,
        "Leaf (an unrelated decoy kept alive only by same-package/import over-binding) must be \
         excluded once Util's own qualified identity is proven"
    );
    assert_eq!(candidates[0].0.package.as_deref(), Some("com.example.util"));
}

#[test]
fn never_fires_when_the_receiver_is_not_resolved_via_an_ordinary_import() {
    let files = vec![empty_file(1, Some("com.example.core"))];
    let type_index = TypeIndex::build(&files);
    let mut candidates = base_candidates();
    let before = candidates.len();

    // No import at all -- e.g. a same-package or locally-declared
    // qualifier -- must leave the pool completely untouched.
    apply_receiver_qualified_type_narrowing(
        &mut candidates,
        Some("Util"),
        Some("Caller"),
        true,
        &[],
        &type_index,
    );

    assert_eq!(candidates.len(), before, "no ordinary import resolved the receiver -- no-op");
}

#[test]
fn never_fires_when_the_receiver_bare_name_is_a_known_field_name_anywhere() {
    let mut field_file = empty_file(2, Some("com.example.other"));
    field_file.index.typed_names.push(TypedNameRecord {
        name: "Util".to_string(),
        declared_type: "SomeType".to_string(),
        scope: NameScope::Field {
            enclosing_type: "SomeOwner".to_string(),
        },
    });
    let files = vec![empty_file(1, Some("com.example.core")), field_file];
    let type_index = TypeIndex::build(&files);
    let mut candidates = base_candidates();
    let before = candidates.len();

    apply_receiver_qualified_type_narrowing(
        &mut candidates,
        Some("Util"),
        Some("Caller"),
        true,
        &imports(),
        &type_index,
    );

    assert_eq!(
        candidates.len(),
        before,
        "a repo-wide field literally named Util means the identifier might be a shadowing \
         field access, never provably a type reference -- must never exclude anything"
    );
}

#[test]
fn never_fires_when_the_callers_own_ancestor_chain_has_an_unresolved_external_supertype() {
    // `Caller extends ExternalBase`, and `ExternalBase` is never declared
    // anywhere in the analyzed set -- an unresolved external supertype.
    // `Caller` itself IS registered as a known (self-referential)
    // top-level type -- otherwise `has_unresolved_external_supertype_
    // transitively` has no entry for it at all and answers `false` for
    // the wrong reason (an absent key, not a proven-resolved chain).
    let mut file = file_with_inheritance(
        1,
        Some("com.example.core"),
        vec![edge("Caller", "ExternalBase")],
    );
    file.index.type_nesting.push(TypeNestingRecord {
        type_name: "Caller".to_string(),
        top_level_type: "Caller".to_string(),
    });
    let files = vec![file];
    let type_index = TypeIndex::build(&files);
    let mut candidates = base_candidates();
    let before = candidates.len();

    apply_receiver_qualified_type_narrowing(
        &mut candidates,
        Some("Util"),
        Some("Caller"),
        true,
        &imports(),
        &type_index,
    );

    assert_eq!(
        candidates.len(),
        before,
        "Caller's own ancestor chain has an unresolved external supertype -- a hidden field \
         could be declared there, so the repo-wide field census is incomplete for this call \
         site; must never exclude anything"
    );
}

#[test]
fn never_fires_for_a_non_java_file() {
    let files = vec![empty_file(1, Some("com.example.core"))];
    let type_index = TypeIndex::build(&files);
    let mut candidates = base_candidates();
    let before = candidates.len();

    apply_receiver_qualified_type_narrowing(
        &mut candidates,
        Some("Util"),
        Some("Caller"),
        false,
        &imports(),
        &type_index,
    );

    assert_eq!(candidates.len(), before, "Kotlin/non-Java files must never be narrowed here");
}

#[test]
fn never_narrows_when_no_candidate_matches_the_qualified_receiver_at_all() {
    let files = vec![empty_file(1, Some("com.example.core"))];
    let type_index = TypeIndex::build(&files);
    // Neither candidate is actually "Util" -- the real Util.normalize
    // simply is not in this reference's bare-name pool at all (e.g. an
    // external/JDK target).
    let mut candidates = vec![
        (decl_info(11, 0, Some("com.example.core"), "Leaf"), 0u16),
        (decl_info(13, 0, Some("com.example.core"), "Other"), 0u16),
    ];
    let before = candidates.len();

    apply_receiver_qualified_type_narrowing(
        &mut candidates,
        Some("Util"),
        Some("Caller"),
        true,
        &imports(),
        &type_index,
    );

    assert_eq!(
        candidates.len(),
        before,
        "no candidate confirms the qualified receiver at all -- missing evidence must never \
         narrow to empty"
    );
}

#[test]
fn keeps_a_candidate_declared_on_a_bare_supertype_of_the_receiver() {
    // `Util extends Base` (bare, unqualified substrate) -- a candidate
    // declared on `Base` (e.g. an inherited static method) must survive,
    // even without its own qualified proof (the same, pre-existing
    // limitation `apply_receiver_type_narrowing` already accepts).
    let files = vec![file_with_inheritance(
        1,
        Some("com.example.util"),
        vec![edge("Util", "Base")],
    )];
    let type_index = TypeIndex::build(&files);
    let mut candidates = base_candidates();
    candidates.push((decl_info(14, 0, Some("com.example.util"), "Base"), 0u16));

    apply_receiver_qualified_type_narrowing(
        &mut candidates,
        Some("Util"),
        Some("Caller"),
        true,
        &imports(),
        &type_index,
    );

    assert_eq!(candidates.len(), 2, "Util itself and its bare supertype Base must both survive");
    assert!(candidates.iter().any(|(d, _)| d.enclosing_type.as_deref() == Some("Base")));
    assert!(!candidates.iter().any(|(d, _)| d.enclosing_type.as_deref() == Some("Leaf")));
}
