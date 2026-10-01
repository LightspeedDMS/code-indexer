//! `receiver_type_qualifier.rs`'s unit tests -- combines the
//! `is_definite_type_qualifier` tests (moved from `receiver_tests.rs`,
//! Messi Rule 6, anti-file-bloat split) with the `resolve_dotted_
//! qualifier_type` tests (moved from the now-removed `receiver_tests_
//! dotted_qualifier.rs`, itself a prior split of the same original
//! file), since both functions now live in the SAME production module.
//! Wired via `#[cfg(test)] #[path = "receiver_type_qualifier_tests.rs"]
//! mod tests;` in `receiver_type_qualifier.rs`.

use super::*;
use crate::graph::bind::FileForBind;
use crate::graph::extract::local_index::{
    Declaration, DeclarationKind as DK, ImportKind, LocalIndex, NameScope, TypedNameRecord,
};
use crate::graph::identity::make_symbol_id;

/// #1922: an uppercase identifier with NO local/parameter/field
/// evidence anywhere this binder can see is DEFINITELY a type
/// qualifier -- the discriminating positive case
/// `narrowing::apply_type_qualifier_narrowing` depends on.
#[test]
fn is_definite_type_qualifier_is_true_for_an_uppercase_identifier_with_no_local_or_field_evidence(
) {
    let typed_names = FileTypedNames::build(&[]);
    let type_index = TypeIndex::build(&[]);
    assert!(is_definite_type_qualifier(
        "Facade",
        Some("Caller"),
        None,
        &typed_names,
        &type_index,
        &[],
    ));
}

/// #1922: a lowercase identifier (`helper.m()`'s receiver) must NEVER
/// be treated as a type qualifier, regardless of local/field evidence
/// -- the issue's own "keep today's behaviour" requirement for
/// variable/field-shaped qualifiers, and the guard that keeps this
/// fix from becoming local-variable scope analysis (#1919).
#[test]
fn is_definite_type_qualifier_is_false_for_a_lowercase_identifier() {
    let typed_names = FileTypedNames::build(&[]);
    let type_index = TypeIndex::build(&[]);
    assert!(!is_definite_type_qualifier(
        "helper",
        Some("Caller"),
        None,
        &typed_names,
        &type_index,
        &[],
    ));
}

/// #1922: an uppercase identifier that IS a real local/parameter
/// binding in this exact scope (an unusual but legal Java style, e.g.
/// `Facade Facade = ...;`) must NOT be treated as a type qualifier --
/// the local binding shadows it, exactly as ordinary Java scoping
/// requires.
#[test]
fn is_definite_type_qualifier_is_false_when_a_local_binding_shadows_the_name() {
    let method_symbol = make_symbol_id(1, 0);
    let typed_names = FileTypedNames::build(&[TypedNameRecord {
        name: "Facade".to_string(),
        declared_type: "Facade".to_string(),
        scope: NameScope::Local {
            enclosing_method: method_symbol,
        },
    }]);
    let type_index = TypeIndex::build(&[]);
    assert!(!is_definite_type_qualifier(
        "Facade",
        Some("Caller"),
        Some(method_symbol),
        &typed_names,
        &type_index,
        &[],
    ));
}

/// #1922: an uppercase identifier that is a known FIELD name anywhere
/// in the repo (this file's own `typed_names` substrate misses it --
/// e.g. an inherited field from a different file) must NOT be treated
/// as a type qualifier -- reuses the SAME repo-wide guard
/// `resolve_identifier_receiver`'s static-type-name fallback already
/// trusts (Rule 4, anti-duplication), never a fresh local-scope check.
#[test]
fn is_definite_type_qualifier_is_false_when_the_name_is_a_known_field_name_anywhere_in_the_repo(
) {
    let mut field_file = LocalIndex::new();
    field_file.typed_names.push(TypedNameRecord {
        name: "Handle".to_string(),
        declared_type: "int".to_string(),
        scope: NameScope::Field {
            enclosing_type: "Outer".to_string(),
        },
    });
    let files = vec![FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index: field_file,
    }];
    let type_index = TypeIndex::build(&files);
    // The CALL site's own file carries no evidence for "Handle" at
    // all (mirrors the real gap: the field lives in a different file).
    let typed_names = FileTypedNames::build(&[]);
    assert!(!is_definite_type_qualifier(
        "Handle",
        Some("Inner"),
        None,
        &typed_names,
        &type_index,
        &[],
    ));
}

/// #1922 regression guard (caught by `bug_1910_narrowing_liveness_
/// guards.rs`'s A3/A4/A5 fixtures via `rust-automation.sh`, before
/// this guard existed): a CAPTURED local declared in an OUTER,
/// lexically-enclosing method (`make_symbol_id(1, 0)` below) must
/// never be promoted to a type qualifier just because a lookup keyed
/// on a DIFFERENT (inner/anonymous-class) enclosing method
/// (`make_symbol_id(1, 1)`) reports `Missing` -- exactly the #1910
/// false-`Missing` shape `has_any_local_binding` exists to
/// close. Discriminating: without the `has_any_local_binding` guard
/// in `is_definite_type_qualifier`, this returns `true` (the same
/// defect that flipped a live `Target.helper()` to a false dead
/// verdict).
#[test]
fn is_definite_type_qualifier_is_false_for_a_captured_local_looked_up_under_a_different_enclosing_method(
) {
    let outer_method = make_symbol_id(1, 0);
    let inner_method = make_symbol_id(1, 1);
    let typed_names = FileTypedNames::build(&[TypedNameRecord {
        name: "Helper".to_string(),
        declared_type: "Target".to_string(),
        scope: NameScope::Local {
            enclosing_method: outer_method,
        },
    }]);
    let type_index = TypeIndex::build(&[]);
    assert!(!is_definite_type_qualifier(
        "Helper",
        Some("Target"),
        Some(inner_method),
        &typed_names,
        &type_index,
        &[],
    ));
}

/// #1922: a bare identifier
/// EXPLICITLY declared, by a single-member static import
/// (`import static ext.Holder.CONSTANT;`), to name an external member --
/// must NEVER be treated as a type qualifier, even though it clears
/// every other guard (uppercase, no local binding anywhere, not a known
/// field in THIS repo -- the import names an EXTERNAL class this repo
/// never indexes at all).
#[test]
fn is_definite_type_qualifier_is_false_for_a_statically_imported_member_name() {
    let typed_names = FileTypedNames::build(&[]);
    let type_index = TypeIndex::build(&[]);
    let imports = [crate::graph::extract::local_index::ImportRecord {
        kind: ImportKind::Static,
        path: "ext.Holder.CONSTANT".to_string(),
        line: 1,
    }];
    assert!(!is_definite_type_qualifier(
        "CONSTANT",
        Some("Caller"),
        None,
        &typed_names,
        &type_index,
        &imports,
    ));
}

/// Negative control for the static-import guard: an UNRELATED static
/// import (different last path segment) must never block a genuine type
/// qualifier -- the guard matches on the imported member's bare name
/// exactly, never merely "a static import exists somewhere in this file".
#[test]
fn is_definite_type_qualifier_is_true_when_a_static_import_names_a_different_member() {
    let typed_names = FileTypedNames::build(&[]);
    let type_index = TypeIndex::build(&[]);
    let imports = [crate::graph::extract::local_index::ImportRecord {
        kind: ImportKind::Static,
        path: "ext.Holder.OTHER_MEMBER".to_string(),
        line: 1,
    }];
    assert!(is_definite_type_qualifier(
        "Facade",
        Some("Caller"),
        None,
        &typed_names,
        &type_index,
        &imports,
    ));
}

// =====================================================================
// `resolve_dotted_qualifier_type` -- the dotted-qualifier counterpart to
// `is_definite_type_qualifier` above, resolving a `ReceiverExpr::
// DottedQualifier` chain's final segment to a concrete in-repo type,
// under the identical shadowing guards.
// =====================================================================

fn nested_type_index(type_name: &str, top_level_type: &str) -> TypeIndex {
    let mut index = LocalIndex::new();
    index.type_nesting.push(crate::graph::extract::local_index::TypeNestingRecord {
        type_name: type_name.to_string(),
        top_level_type: top_level_type.to_string(),
    });
    TypeIndex::build(&[FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }])
}

fn fqn_name_index(package: &str, type_name: &str) -> RepoNameIndex {
    let mut index = LocalIndex::new();
    index.declarations.push(Declaration {
        kind: DK::Package,
        name: package.to_string(),
        line: 1,
        symbol: make_symbol_id(1, 0),
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    });
    index.declarations.push(Declaration {
        kind: DK::Type,
        name: type_name.to_string(),
        line: 2,
        symbol: make_symbol_id(1, 1),
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    });
    RepoNameIndex::build(&[FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }])
}

/// #1931 issue repro shape: `["Outer", "Inner"]` positively resolves to
/// `"Inner"` when the repo recorded a real `(Inner, Outer)` nesting edge,
/// and the first segment carries no shadowing evidence at all.
#[test]
fn resolve_dotted_qualifier_type_resolves_a_real_two_segment_nested_type() {
    let typed_names = FileTypedNames::build(&[]);
    let type_index = nested_type_index("Inner", "Outer");
    let name_index = RepoNameIndex::build(&[]);
    let evidence = resolve_dotted_qualifier_type(
        &["Outer".to_string(), "Inner".to_string()],
        &typed_names,
        &type_index,
        &name_index,
        &[],
    );
    assert_eq!(evidence, ReceiverEvidence::Positive("Inner".to_string()));
}

/// #1931: `["com", "example", "Target"]` positively resolves to
/// `"Target"` when a repo type named `Target` is declared in a file whose
/// package is exactly `com.example`.
#[test]
fn resolve_dotted_qualifier_type_resolves_a_real_fully_qualified_type() {
    let typed_names = FileTypedNames::build(&[]);
    let type_index = TypeIndex::build(&[]);
    let name_index = fqn_name_index("com.example", "Target");
    let evidence = resolve_dotted_qualifier_type(
        &["com".to_string(), "example".to_string(), "Target".to_string()],
        &typed_names,
        &type_index,
        &name_index,
        &[],
    );
    assert_eq!(evidence, ReceiverEvidence::Positive("Target".to_string()));
}

/// #1931: a chain whose first segment is shadowed by a LOCAL binding
/// anywhere in the file must never resolve, even though the nesting edge
/// itself is real -- mirrors `is_definite_type_qualifier`'s own captured-
/// local guard.
#[test]
fn resolve_dotted_qualifier_type_is_none_when_the_first_segment_is_a_local_binding() {
    let records = vec![TypedNameRecord {
        name: "Outer".to_string(),
        declared_type: "Something".to_string(),
        scope: NameScope::Local {
            enclosing_method: make_symbol_id(1, 0),
        },
    }];
    let typed_names = FileTypedNames::build(&records).with_all_local_binding_names(&["Outer".to_string()]);
    let type_index = nested_type_index("Inner", "Outer");
    let name_index = RepoNameIndex::build(&[]);
    let evidence = resolve_dotted_qualifier_type(
        &["Outer".to_string(), "Inner".to_string()],
        &typed_names,
        &type_index,
        &name_index,
        &[],
    );
    assert_eq!(
        evidence,
        ReceiverEvidence::None,
        "a shadowed first segment must never resolve, even when the nesting edge is real"
    );
}

/// #1931: a chain whose first segment is a known FIELD name anywhere in
/// the repo must never resolve -- mirrors `is_definite_type_qualifier`'s
/// own known-field guard.
#[test]
fn resolve_dotted_qualifier_type_is_none_when_the_first_segment_is_a_known_field_name() {
    use crate::graph::extract::local_index::NameScope as NS;

    let mut field_index = LocalIndex::new();
    field_index.typed_names.push(TypedNameRecord {
        name: "Outer".to_string(),
        declared_type: "Something".to_string(),
        scope: NS::Field {
            enclosing_type: "Caller".to_string(),
        },
    });
    let type_index = TypeIndex::build(&[FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index: field_index,
    }]);
    let typed_names = FileTypedNames::build(&[]);
    let name_index = RepoNameIndex::build(&[]);
    let evidence = resolve_dotted_qualifier_type(
        &["Outer".to_string(), "Inner".to_string()],
        &typed_names,
        &type_index,
        &name_index,
        &[],
    );
    assert_eq!(evidence, ReceiverEvidence::None);
}

/// #1931: a chain whose first segment is named by a SINGLE-MEMBER static
/// import must never resolve -- mirrors `is_definite_type_qualifier`'s
/// own static-import guard.
#[test]
fn resolve_dotted_qualifier_type_is_none_when_the_first_segment_is_statically_imported() {
    let typed_names = FileTypedNames::build(&[]);
    let type_index = nested_type_index("Inner", "Outer");
    let name_index = RepoNameIndex::build(&[]);
    let imports = [crate::graph::extract::local_index::ImportRecord {
        kind: ImportKind::Static,
        path: "ext.Holder.Outer".to_string(),
        line: 1,
    }];
    let evidence = resolve_dotted_qualifier_type(
        &["Outer".to_string(), "Inner".to_string()],
        &typed_names,
        &type_index,
        &name_index,
        &imports,
    );
    assert_eq!(evidence, ReceiverEvidence::None);
}

/// #1931: an ordinary field-access chain (`obj.field`) that resolves to
/// NEITHER a real nesting edge NOR a fully-qualified type must stay
/// completely unresolved -- the "otherwise tag-only" half of the
/// contract.
#[test]
fn resolve_dotted_qualifier_type_is_none_for_an_unresolved_chain() {
    let typed_names = FileTypedNames::build(&[]);
    let type_index = TypeIndex::build(&[]);
    let name_index = RepoNameIndex::build(&[]);
    let evidence = resolve_dotted_qualifier_type(
        &["obj".to_string(), "field".to_string()],
        &typed_names,
        &type_index,
        &name_index,
        &[],
    );
    assert_eq!(evidence, ReceiverEvidence::None);
}

/// Rework (reviewer-found root cause, Opus F16 / Codex `a.b.C`): the
/// shadowing guards must apply to EVERY segment, not just the first.
/// `["A", "B"]` where `B` is BOTH a real nested-type edge of `A` (the
/// nested-type rule alone would wrongly fire) AND a known FIELD declared
/// on `A` -- per JLS 6.5.2, `A.B` in expression position resolves to the
/// FIELD `B`, never the nested type `B`, whenever `A` has an accessible
/// field of that name. A guard that only inspects `segments[0]` ("A",
/// itself unshadowed) would wrongly resolve this to the nested type and
/// hard-narrow away the real target reached through the field.
#[test]
fn resolve_dotted_qualifier_type_is_none_when_a_non_first_segment_is_a_known_field_name() {
    use crate::graph::extract::local_index::NameScope as NS;

    let mut index = LocalIndex::new();
    index.type_nesting.push(crate::graph::extract::local_index::TypeNestingRecord {
        type_name: "B".to_string(),
        top_level_type: "A".to_string(),
    });
    index.typed_names.push(TypedNameRecord {
        name: "B".to_string(),
        declared_type: "Helper".to_string(),
        scope: NS::Field {
            enclosing_type: "A".to_string(),
        },
    });
    let type_index = TypeIndex::build(&[FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }]);
    let typed_names = FileTypedNames::build(&[]);
    let name_index = RepoNameIndex::build(&[]);
    let evidence = resolve_dotted_qualifier_type(
        &["A".to_string(), "B".to_string()],
        &typed_names,
        &type_index,
        &name_index,
        &[],
    );
    assert_eq!(
        evidence,
        ReceiverEvidence::None,
        "a real nesting edge on a LATER segment must never resolve when that same segment is \
         ALSO a known field name -- the field takes precedence (JLS 6.5.2), and a guard \
         limited to segments[0] misses this entirely"
    );
}

/// Rework (Codex P1, guard (b) isolated): a real `(Inner, Outer)` nesting
/// edge exists, but `Outer` itself has an UNRESOLVED EXTERNAL supertype
/// (`ExternalBase`, itself not a known repo type) -- an inherited field
/// on `Outer` could shadow `Inner` invisibly to guards (1)-(3), so
/// resolution must bail regardless of the nesting edge being genuinely
/// real.
#[test]
fn resolve_dotted_qualifier_type_is_none_when_the_first_segment_has_an_unresolved_external_supertype(
) {
    use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord};

    let mut index = LocalIndex::new();
    // Real extraction always ALSO records a top-level type's own
    // self-referential nesting record (`java.rs`'s `dispatch_type_
    // declaration`: `top_level_type = ctx.top_level_type.or_else(||
    // enclosing_type)`, so a top-level type's OWN record has
    // `top_level_type == type_name`) -- without it, `Outer` itself is
    // never registered as a known repo type at all, and guard (b) below
    // (`is_known_type_name(segment) && has_unresolved_external_
    // supertype(segment)`) short-circuits on the FIRST half before ever
    // reaching the supertype check.
    index.type_nesting.push(crate::graph::extract::local_index::TypeNestingRecord {
        type_name: "Outer".to_string(),
        top_level_type: "Outer".to_string(),
    });
    index.type_nesting.push(crate::graph::extract::local_index::TypeNestingRecord {
        type_name: "Inner".to_string(),
        top_level_type: "Outer".to_string(),
    });
    index.inheritance.push(InheritanceRecord {
        kind: InheritanceKind::Extends,
        subtype_name: "Outer".to_string(),
        supertype_name: "ExternalBase".to_string(),
        line: 1,
    });
    let type_index = TypeIndex::build(&[FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }]);
    let typed_names = FileTypedNames::build(&[]);
    let name_index = RepoNameIndex::build(&[]);
    let evidence = resolve_dotted_qualifier_type(
        &["Outer".to_string(), "Inner".to_string()],
        &typed_names,
        &type_index,
        &name_index,
        &[],
    );
    assert_eq!(
        evidence,
        ReceiverEvidence::None,
        "Outer's own unresolved external supertype (ExternalBase, not itself a known repo \
         type) means an inherited field on Outer could shadow Inner -- must bail even though \
         a real (Inner, Outer) nesting edge also exists"
    );
}

/// Rework (Codex P1, guard (a) isolated): a PREFIX segment (`Middle`) is
/// ALSO a known repo type, but carries NO supertype issue at all (guard
/// (b) would never fire here) -- proving guard (a) is independently
/// necessary, not merely a side effect of guard (b). Per the JLS, an
/// accessible type name is NEVER reinterpreted as a package fragment.
#[test]
fn resolve_dotted_qualifier_type_is_none_when_a_prefix_segment_is_a_known_type_even_without_a_supertype_issue(
) {
    let type_index = nested_type_index("Middle", "Middle");
    let typed_names = FileTypedNames::build(&[]);
    let name_index = fqn_name_index("Root.Middle", "Target");
    let evidence = resolve_dotted_qualifier_type(
        &["Root".to_string(), "Middle".to_string(), "Target".to_string()],
        &typed_names,
        &type_index,
        &name_index,
        &[],
    );
    assert_eq!(
        evidence,
        ReceiverEvidence::None,
        "a prefix segment (Middle) that is ALSO a known repo type can never be part of a \
         genuine package path -- Java always resolves an accessible type name as a type, \
         never falls back to reading it as a package fragment -- so the FQN rule must bail \
         regardless of whether Middle has any supertype issue at all"
    );
}
