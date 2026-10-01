//! Issue #1956: proves `apply_same_class_or_super_narrowing`'s wiring to
//! the qualified inheritance graph (`same_class_qualified::
//! QualifiedAncestry`) changes a REAL, observable narrowing outcome --
//! never merely that `TypeIndex::supertypes_of_qualified` itself returns a
//! different value in isolation.

use super::*;
use crate::graph::bind::families::TypeIndex;
use crate::graph::bind::name_index::DeclInfo;
use crate::graph::bind::FileForBind;
use crate::graph::extract::local_index::{
    Declaration, DeclarationKind, ImportKind, ImportRecord, InheritanceKind, InheritanceRecord,
    LocalIndex, Visibility,
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

fn wildcard_import(path: &str) -> ImportRecord {
    ImportRecord {
        kind: ImportKind::Wildcard,
        path: path.to_string(),
        line: 1,
    }
}

fn method_decl_info(file_id: u32, local: u32, package: Option<&str>, enclosing_type: &str) -> DeclInfo {
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

/// Issue #1956's own required proof: `Sub` (package `com.example.a`)
/// extends bare `Base`, resolved (no import needed) to the real ancestor
/// `com.example.a.Base`. Two same-named-method candidates exist: one truly
/// declared on the real ancestor (`com.example.a.Base`), one an unrelated
/// decoy sharing the exact same bare enclosing-type name `Base` in a
/// DIFFERENT package (`com.example.b`) with no real inheritance relation
/// to `Sub` at all. Under the PRE-#1956 bare-only check both would earn
/// `SAME_CLASS_OR_SUPER` (the bare graph cannot distinguish them) -- this
/// proves the qualified wiring changes that: the decoy loses the tag, the
/// real ancestor keeps it.
#[test]
fn same_class_or_super_narrowing_withdraws_the_tag_for_a_proven_cross_package_decoy() {
    let files = vec![file_with_package(
        1,
        Some("com.example.a"),
        Vec::new(),
        vec![edge("Sub", "Base")],
    )];
    let type_index = TypeIndex::build(&files);

    let real_ancestor = method_decl_info(2, 0, Some("com.example.a"), "Base");
    let cross_package_decoy = method_decl_info(3, 0, Some("com.example.b"), "Base");
    let mut candidates = vec![(real_ancestor, 0u16), (cross_package_decoy, 0u16)];

    apply_same_class_or_super_narrowing(
        &mut candidates,
        Some("Sub"),
        Some("com.example.a"),
        &type_index,
    );

    assert!(
        candidates[0].1 & reasons::SAME_CLASS_OR_SUPER != 0,
        "the REAL, same-package ancestor must still be tagged SAME_CLASS_OR_SUPER"
    );
    assert_eq!(
        candidates[1].1 & reasons::SAME_CLASS_OR_SUPER,
        0,
        "an unrelated Base in a DIFFERENT package must NOT be tagged SAME_CLASS_OR_SUPER just \
         because the bare graph conflates it with Sub's real, same-package ancestor"
    );
}

/// Companion / fallback proof: when the qualified graph has NO resolved
/// opinion at all about the caller's ancestor bare name (here, a wildcard
/// import makes the same-package assumption ambiguous, so `TypeIndex::
/// build` records no qualified edge for `Sub` -> `Base`), the ORIGINAL
/// bare-only tag decision must survive UNCHANGED for every candidate --
/// including the cross-package decoy, which keeps the tag exactly as the
/// pre-#1956 implementation always gave it. Ambiguity resolves to the
/// EXISTING behaviour, never to a guess and never to a stricter
/// exclusion.
#[test]
fn same_class_or_super_narrowing_falls_back_to_the_bare_tag_when_the_qualified_graph_has_no_opinion(
) {
    let files = vec![file_with_package(
        1,
        Some("com.example.a"),
        vec![wildcard_import("com.example.other")],
        vec![edge("Sub", "Base")],
    )];
    let type_index = TypeIndex::build(&files);

    let same_package_candidate = method_decl_info(2, 0, Some("com.example.a"), "Base");
    let cross_package_candidate = method_decl_info(3, 0, Some("com.example.b"), "Base");
    let mut candidates = vec![
        (same_package_candidate, 0u16),
        (cross_package_candidate, 0u16),
    ];

    apply_same_class_or_super_narrowing(
        &mut candidates,
        Some("Sub"),
        Some("com.example.a"),
        &type_index,
    );

    assert!(
        candidates[0].1 & reasons::SAME_CLASS_OR_SUPER != 0,
        "the same-package candidate must keep the bare tag when the qualified graph has no \
         opinion at all"
    );
    assert!(
        candidates[1].1 & reasons::SAME_CLASS_OR_SUPER != 0,
        "the cross-package candidate must ALSO keep the bare tag here -- with no qualified \
         edge resolved for this bare name (wildcard-import ambiguity upstream), there is no \
         positive proof of a decoy, so this falls back to the ORIGINAL, unmodified bare-match \
         behaviour rather than a guess"
    );
}
