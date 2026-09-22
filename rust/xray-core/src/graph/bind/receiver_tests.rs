//! `receiver.rs`'s unit tests, split into their own file (Messi Rule 6,
//! anti-file-bloat) so `receiver.rs` itself stays under the project's
//! 1000-line limit -- mirrors the split `resolve.rs` already uses
//! (`resolve_tests.rs`/`resolve_tests_family.rs`/etc.), wired the same way
//! via `#[cfg(test)] #[path = "receiver_tests.rs"] mod tests;`.

use super::*;
use crate::graph::bind::FileForBind;
use crate::graph::extract::local_index::{
    Declaration, DeclarationKind as DK, ImportKind, LocalIndex, MethodOwnerRecord,
    MethodReturnTypeRecord,
};
use crate::graph::identity::make_symbol_id;

/// #1910 prerequisite 1 (round4-findings.md finding 1): `FileTypedNames`
/// keys locals by `(enclosing_method, name)`, but Java scopes a local
/// by BLOCK. Two legal same-named locals in SIBLING blocks of the SAME
/// method are never simultaneously in scope under real javac, but this
/// per-method key cannot tell them apart -- last-write-wins would let
/// an ARBITRARY one of the two declared types answer a lookup for
/// EITHER call site, exactly the failure that let a live private
/// method be reported `is_definitely_dead_code() == Some(true)` in
/// round 4. The safe fix (named explicitly in the issue as an
/// acceptable minimal alternative to full block-scoping): once a
/// `(method, name)` key has seen two DIFFERENT declared types, it is
/// permanently ambiguous and `lookup` must return `LocalLookup::
/// Ambiguous` -- a DISTINCT, TERMINAL outcome from a genuine `Missing`
/// lookup (round 6, finding 1's remediation; see `LocalLookup`'s own
/// doc comment) -- rather than guess.
#[test]
fn file_typed_names_refuses_to_pick_a_type_when_sibling_blocks_declare_the_same_name_with_different_types(
) {
    let method_symbol = make_symbol_id(1, 0);
    let records = vec![
        TypedNameRecord {
            name: "x".to_string(),
            declared_type: "Foo".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        },
        TypedNameRecord {
            name: "x".to_string(),
            declared_type: "Bar".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        },
    ];
    let typed_names = FileTypedNames::build(&records);
    assert_eq!(
        typed_names.lookup(Some(method_symbol), None, "x"),
        LocalLookup::Ambiguous,
        "two sibling-block locals sharing a bare name but disagreeing on declared type \
         must resolve to the DISTINCT, terminal Ambiguous outcome -- never a guessed type \
         AND never collapsed into an ordinary Missing lookup (#1910 round 6, finding 1)"
    );
}

/// Companion to the ambiguity test above: the SAME name repeating with
/// the SAME declared type (e.g. two independent `for (String s : ...)`
/// loops in sibling blocks of one method -- extremely common, legal
/// Java) must NOT be treated as ambiguous -- there is no genuine
/// disagreement to be conservative about.
#[test]
fn file_typed_names_still_resolves_when_sibling_blocks_repeat_the_same_name_and_type() {
    let method_symbol = make_symbol_id(1, 0);
    let records = vec![
        TypedNameRecord {
            name: "x".to_string(),
            declared_type: "Foo".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        },
        TypedNameRecord {
            name: "x".to_string(),
            declared_type: "Foo".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        },
    ];
    let typed_names = FileTypedNames::build(&records);
    assert_eq!(
        typed_names.lookup(Some(method_symbol), None, "x"),
        LocalLookup::Found("Foo".to_string())
    );
}

/// AC1: a local/parameter binding must be preferred over a
/// same-named field -- the discriminating case ordinary Java scoping
/// requires (shadowing).
#[test]
fn file_typed_names_prefers_a_local_binding_over_a_field_of_the_same_name() {
    let method_symbol = make_symbol_id(1, 0);
    let records = vec![
        TypedNameRecord {
            name: "x".to_string(),
            declared_type: "Local".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        },
        TypedNameRecord {
            name: "x".to_string(),
            declared_type: "Field".to_string(),
            scope: NameScope::Field {
                enclosing_type: "Owner".to_string(),
            },
        },
    ];
    let typed_names = FileTypedNames::build(&records);
    assert_eq!(
        typed_names.lookup(Some(method_symbol), Some("Owner"), "x"),
        LocalLookup::Found("Local".to_string())
    );
    // Without a matching local (different method), falls back to the field.
    assert_eq!(
        typed_names.lookup(Some(make_symbol_id(1, 99)), Some("Owner"), "x"),
        LocalLookup::Found("Field".to_string())
    );
    assert_eq!(
        typed_names.lookup(None, None, "neverDeclared"),
        LocalLookup::Missing
    );
}

fn method_decl(name: &str, file_id: u32, local: u32) -> Declaration {
    Declaration {
        kind: DK::Method,
        name: name.to_string(),
        line: 1,
        symbol: make_symbol_id(file_id, local),
        param_count: Some(0),
        param_types: Vec::new(),
        is_varargs: false,
    }
}

/// AC1: `obj.doSomething()` where `obj` is a locally-declared `Foo`
/// resolves to `"Foo"`.
#[test]
fn resolve_receiver_type_resolves_a_simple_identifier_via_typed_names() {
    let method_symbol = make_symbol_id(1, 0);
    let typed_names = FileTypedNames::build(&[TypedNameRecord {
        name: "obj".to_string(),
        declared_type: "Foo".to_string(),
        scope: NameScope::Local {
            enclosing_method: method_symbol,
        },
    }]);
    let name_index = RepoNameIndex::build(&[]);
    let type_index = TypeIndex::build(&[]);

    let resolved = resolve_receiver_type(
        &ReceiverExpr::Identifier("obj".to_string()),
        Some("Caller"),
        Some(method_symbol),
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(resolved, ReceiverEvidence::Positive("Foo".to_string()));
}

/// AC2: THE central discriminating case named in the story --
/// `auth.realm().requireX()`'s receiver (as seen from `requireX`) is
/// `Chained { method_name: "realm", receiver: Identifier("auth") }`.
/// `auth`'s declared type is `Auth`; `Auth.realm()` declares return
/// type `Realm` -- the resolved receiver type must be `"Realm"`,
/// proving the chain was followed, not just the base.
#[test]
fn resolve_receiver_type_follows_a_chained_call_through_a_declared_return_type() {
    const AUTH_FILE_ID: u32 = 5;
    let mut auth_file = LocalIndex::new();
    auth_file
        .declarations
        .push(method_decl("realm", AUTH_FILE_ID, 0));
    auth_file.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
        enclosing_type: "Auth".to_string(),
    });
    auth_file.method_return_types.push(MethodReturnTypeRecord {
        method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
        return_type: "Realm".to_string(),
    });
    let files = vec![FileForBind {
        file_id: AUTH_FILE_ID,
        language: "java".to_string(),
        index: auth_file,
    }];
    let name_index = RepoNameIndex::build(&files);
    let type_index = TypeIndex::build(&files);

    let method_symbol = make_symbol_id(9, 0);
    let typed_names = FileTypedNames::build(&[TypedNameRecord {
        name: "auth".to_string(),
        declared_type: "Auth".to_string(),
        scope: NameScope::Local {
            enclosing_method: method_symbol,
        },
    }]);

    let receiver = ReceiverExpr::Chained {
        method_name: "realm".to_string(),
        receiver: Box::new(ReceiverExpr::Identifier("auth".to_string())),
    };
    let resolved = resolve_receiver_type(
        &receiver,
        Some("Caller"),
        Some(method_symbol),
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(resolved, ReceiverEvidence::Positive("Realm".to_string()));
}

/// P1-4 (#1898 code review, AC3): `TimeUtil.parse("a")`'s receiver is
/// `ReceiverExpr::Identifier("TimeUtil")` -- a CLASS NAME, not a
/// local/field/parameter, so `typed_names.lookup` has no evidence for
/// it at all. Before this fix, that meant `resolve_receiver_type`
/// returned `None`, so no receiver-type narrowing ever ran and the
/// call fanned out to every same-named method in the repo (#1898's
/// own bug report). `TimeUtil` IS a known in-repo type (recorded via
/// `type_nesting`), so the receiver must resolve to `"TimeUtil"`
/// itself.
///
/// #1910 SALVAGE: a round 6/7 attempt promoted this fallback to
/// `Positive` for a type declared/imported by the SAME calling file,
/// but round 7's review proved even that narrower substrate rests on
/// `FileTypedNames::lookup`'s `Missing` result, which is not always a
/// genuine absence (the captured-local scope-key problem) -- so this
/// fallback stays `Advisory` permanently, exactly as it shipped
/// before any #1910 attempt.
#[test]
fn resolve_receiver_type_resolves_a_static_type_identifier_via_known_type_names() {
    let files = vec![FileForBind {
        file_id: 20,
        language: "java".to_string(),
        index: {
            let mut index = LocalIndex::new();
            index.type_nesting.push(
                crate::graph::extract::local_index::TypeNestingRecord {
                    type_name: "TimeUtil".to_string(),
                    top_level_type: "TimeUtil".to_string(),
                },
            );
            index
        },
    }];
    let name_index = RepoNameIndex::build(&files);
    let type_index = TypeIndex::build(&files);
    let typed_names = FileTypedNames::build(&[]);

    let resolved = resolve_receiver_type(
        &ReceiverExpr::Identifier("TimeUtil".to_string()),
        Some("Caller"),
        None,
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(
        resolved,
        ReceiverEvidence::Advisory("TimeUtil".to_string()),
        "a bare identifier resolved only via the is_known_type_name fallback (no direct \
         typed-name evidence) must be tagged Advisory, never Positive"
    );
}

/// Negative control: an identifier that is neither a typed
/// local/field/parameter NOR a known in-repo type name must still
/// resolve to `None` -- never a guessed type (Rule 2, anti-fallback).
#[test]
fn resolve_receiver_type_returns_none_for_an_identifier_with_no_evidence_at_all() {
    let name_index = RepoNameIndex::build(&[]);
    let type_index = TypeIndex::build(&[]);
    let typed_names = FileTypedNames::build(&[]);

    let resolved = resolve_receiver_type(
        &ReceiverExpr::Identifier("neverDeclaredAnywhere".to_string()),
        Some("Caller"),
        None,
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(resolved, ReceiverEvidence::None);
}

/// #1910 prerequisite 2 (round4-findings.md finding 2): `is_pseudo_type`
/// was applied only to the BASE identifier's declared-type string, not
/// to a chain step's own resolved return type. `auth.realm()`'s
/// receiver chain here is `Chained { method_name: "realm", receiver:
/// Identifier("auth") }`; `auth`'s declared type is the real, positive
/// `"Auth"`, but `Auth.realm()`'s declared return type is a bare
/// GENERIC TYPE PARAMETER name (`"T"`, e.g. `<T> T realm()`) -- not a
/// real class this binder can narrow against. Before this fix, the
/// chain-following loop trusted `next_type` verbatim, promoting `"T"`
/// straight into `Positive("T")`; that fabricated type could then
/// coincidentally collide with an unrelated in-repo class literally
/// named `T` and hard-delete a real candidate. The fix: every chain
/// step's resolved type must clear the SAME `is_pseudo_type` guard the
/// base identifier already does.
#[test]
fn resolve_receiver_type_rejects_a_pseudo_type_returned_by_an_intermediate_chain_step() {
    const AUTH_FILE_ID: u32 = 5;
    let mut auth_file = LocalIndex::new();
    auth_file
        .declarations
        .push(method_decl("realm", AUTH_FILE_ID, 0));
    auth_file.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
        enclosing_type: "Auth".to_string(),
    });
    auth_file.method_return_types.push(MethodReturnTypeRecord {
        method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
        return_type: "T".to_string(),
    });
    auth_file.type_parameter_names.push("T".to_string());
    let files = vec![FileForBind {
        file_id: AUTH_FILE_ID,
        language: "java".to_string(),
        index: auth_file,
    }];
    let name_index = RepoNameIndex::build(&files);
    let type_index = TypeIndex::build(&files);

    let method_symbol = make_symbol_id(9, 0);
    let typed_names = FileTypedNames::build(&[TypedNameRecord {
        name: "auth".to_string(),
        declared_type: "Auth".to_string(),
        scope: NameScope::Local {
            enclosing_method: method_symbol,
        },
    }]);

    let receiver = ReceiverExpr::Chained {
        method_name: "realm".to_string(),
        receiver: Box::new(ReceiverExpr::Identifier("auth".to_string())),
    };
    let resolved = resolve_receiver_type(
        &receiver,
        Some("Caller"),
        Some(method_symbol),
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(
        resolved,
        ReceiverEvidence::None,
        "a chain step whose declared return type is a bare generic type parameter name \
         must never resolve to that pseudo-type, even though the chain's BASE was Positive"
    );
}

/// P1-A (#1898 code review round 2, epic #1906): `var x = new Svc();`
/// records `x`'s declared type as the LITERAL STRING `"var"` (the
/// extractor performs no type inference -- see `java_type_names::
/// resolve_type_node_base_name`). `"var"` is a reserved Java keyword,
/// never a legal class name, so it must never drive `apply_receiver_
/// type_narrowing`'s hard filter -- `resolve_receiver_type` must
/// return `None` (missing evidence, narrowing skipped) rather than
/// this pseudo-type string.
#[test]
fn resolve_receiver_type_rejects_the_var_pseudo_type_and_returns_none() {
    let method_symbol = make_symbol_id(1, 0);
    let typed_names = FileTypedNames::build(&[TypedNameRecord {
        name: "svc".to_string(),
        declared_type: "var".to_string(),
        scope: NameScope::Local {
            enclosing_method: method_symbol,
        },
    }]);
    let name_index = RepoNameIndex::build(&[]);
    let type_index = TypeIndex::build(&[]);

    let resolved = resolve_receiver_type(
        &ReceiverExpr::Identifier("svc".to_string()),
        Some("Caller"),
        Some(method_symbol),
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(
        resolved,
        ReceiverEvidence::None,
        "a var-typed receiver must never resolve to the literal string \"var\""
    );
}

/// P1-A: `<T extends Svc> void run(T t) { t.ping(); }` records `t`'s
/// declared type as `"T"` -- a generic TYPE PARAMETER name, never a
/// concrete class this binder can narrow against. `TypeIndex` knows
/// `"T"` was declared as a type parameter (via `type_parameter_names`,
/// populated from the method's own `<T extends Svc>` clause), so
/// `resolve_receiver_type` must reject it the same way it rejects
/// `"var"`.
#[test]
fn resolve_receiver_type_rejects_a_generic_type_parameter_name_and_returns_none() {
    let method_symbol = make_symbol_id(1, 0);
    let typed_names = FileTypedNames::build(&[TypedNameRecord {
        name: "t".to_string(),
        declared_type: "T".to_string(),
        scope: NameScope::Local {
            enclosing_method: method_symbol,
        },
    }]);
    let mut index = LocalIndex::new();
    index.type_parameter_names.push("T".to_string());
    let files = vec![FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }];
    let name_index = RepoNameIndex::build(&files);
    let type_index = TypeIndex::build(&files);

    let resolved = resolve_receiver_type(
        &ReceiverExpr::Identifier("t".to_string()),
        Some("Caller"),
        Some(method_symbol),
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(
        resolved,
        ReceiverEvidence::None,
        "a generic-type-parameter-typed receiver must never resolve to the type \
         parameter's own bare name"
    );
}

/// P1-B (#1898 code review round 2, epic #1906): an untyped lambda
/// parameter (`(x) -> x.foo()`, no explicit type annotation) records
/// an EMPTY-STRING declared-type sentinel (`java_receiver::lambda_
/// param_typed_names`'s own doc comment) -- the name IS a genuine
/// local binding (so `typed_names.lookup` correctly returns `Some`,
/// never falling through to the coincidental-type-name fallback), but
/// the empty string itself is exactly as much a pseudo-type as `"var"`
/// and must never drive a hard receiver-type filter either.
#[test]
fn resolve_receiver_type_rejects_the_empty_string_sentinel_for_an_untyped_local_binding_and_returns_none(
) {
    let method_symbol = make_symbol_id(1, 0);
    let typed_names = FileTypedNames::build(&[TypedNameRecord {
        name: "x".to_string(),
        declared_type: String::new(),
        scope: NameScope::Local {
            enclosing_method: method_symbol,
        },
    }]);
    let name_index = RepoNameIndex::build(&[]);
    let type_index = TypeIndex::build(&[]);

    let resolved = resolve_receiver_type(
        &ReceiverExpr::Identifier("x".to_string()),
        Some("Caller"),
        Some(method_symbol),
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(
        resolved,
        ReceiverEvidence::None,
        "an untyped local binding's empty-string sentinel must never resolve to a \
         (fabricated, empty-named) receiver type"
    );
}

/// P1-B (#1898 code review round 2, epic #1906): an identifier with NO
/// typed-name evidence in THIS file (e.g. an inner class reading its
/// outer class's field, or an inherited field declared in a different
/// file -- `typed_names.lookup` misses both) must resolve via the
/// REPO-WIDE `unambiguous_field_type` substrate to the field's REAL
/// declared type -- never fall back to `is_known_type_name` and guess
/// the coincidentally same-named type instead.
#[test]
fn resolve_receiver_type_resolves_a_type_name_fallback_to_the_unambiguous_field_type_when_the_identifier_is_also_a_known_field_name(
) {
    let mut type_decl_file = LocalIndex::new();
    type_decl_file.type_nesting.push(
        crate::graph::extract::local_index::TypeNestingRecord {
            type_name: "handle".to_string(),
            top_level_type: "handle".to_string(),
        },
    );
    let mut field_decl_file = LocalIndex::new();
    field_decl_file.typed_names.push(TypedNameRecord {
        name: "handle".to_string(),
        declared_type: "Caller".to_string(),
        scope: NameScope::Field {
            enclosing_type: "Outer".to_string(),
        },
    });
    let files = vec![
        FileForBind {
            file_id: 1,
            language: "java".to_string(),
            index: type_decl_file,
        },
        FileForBind {
            file_id: 2,
            language: "java".to_string(),
            index: field_decl_file,
        },
    ];
    let name_index = RepoNameIndex::build(&files);
    let type_index = TypeIndex::build(&files);
    // This file's OWN typed_names carries no evidence for "handle" at
    // all -- mirroring the real gap (the field lives on "Outer" in a
    // DIFFERENT file/scope than the caller's own).
    let typed_names = FileTypedNames::build(&[]);

    let resolved = resolve_receiver_type(
        &ReceiverExpr::Identifier("handle".to_string()),
        Some("Inner"),
        None,
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(
        resolved,
        ReceiverEvidence::Advisory("Caller".to_string()),
        "the field's own unambiguous declared type must win, never the coincidentally \
         same-named type -- but only as ADVISORY evidence, never Positive, since it came \
         from the unambiguous_field_type fallback rather than a direct typed-name hit"
    );
}

/// P1-B: when the SAME bare name is used as a field with genuinely
/// CONFLICTING declared types elsewhere in the repo (an unrelated
/// field, not the same one), `unambiguous_field_type` has no answer
/// and the static-type-name fallback must ALSO stay blocked (`name`
/// is still a known field name) -- `None`, never a guess either way.
#[test]
fn resolve_receiver_type_returns_none_when_the_identifier_is_a_field_name_with_conflicting_declared_types(
) {
    let mut field_decl_file_a = LocalIndex::new();
    field_decl_file_a.typed_names.push(TypedNameRecord {
        name: "handle".to_string(),
        declared_type: "Caller".to_string(),
        scope: NameScope::Field {
            enclosing_type: "Outer".to_string(),
        },
    });
    let mut field_decl_file_b = LocalIndex::new();
    field_decl_file_b.typed_names.push(TypedNameRecord {
        name: "handle".to_string(),
        declared_type: "SomethingElse".to_string(),
        scope: NameScope::Field {
            enclosing_type: "Unrelated".to_string(),
        },
    });
    let files = vec![
        FileForBind {
            file_id: 1,
            language: "java".to_string(),
            index: field_decl_file_a,
        },
        FileForBind {
            file_id: 2,
            language: "java".to_string(),
            index: field_decl_file_b,
        },
    ];
    let name_index = RepoNameIndex::build(&files);
    let type_index = TypeIndex::build(&files);
    let typed_names = FileTypedNames::build(&[]);

    let resolved = resolve_receiver_type(
        &ReceiverExpr::Identifier("handle".to_string()),
        Some("Inner"),
        None,
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(
        resolved,
        ReceiverEvidence::None,
        "conflicting declared types for the same field bare name must never resolve to \
         either guessed type"
    );
}

/// #1910 round 6, finding 1's remediation, at the `resolve_receiver_
/// type` level: an AMBIGUOUS local binding (two sibling-block locals
/// with different declared types) must resolve `ReceiverEvidence::
/// None` even when a coincidentally same-named class IS a known
/// in-repo type (so the `is_known_type_name` Advisory fallback would
/// otherwise fire) -- proving `Ambiguous` never falls through to
/// either open-world fallback.
#[test]
fn ambiguous_local_binding_never_falls_through_to_the_static_type_name_fallback() {
    let method_symbol = make_symbol_id(1, 0);
    let typed_names = FileTypedNames::build(&[
        TypedNameRecord {
            name: "handle".to_string(),
            declared_type: "Target".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        },
        TypedNameRecord {
            name: "handle".to_string(),
            declared_type: "String".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        },
    ]);
    let mut index = LocalIndex::new();
    index.type_nesting.push(
        crate::graph::extract::local_index::TypeNestingRecord {
            type_name: "handle".to_string(),
            top_level_type: "handle".to_string(),
        },
    );
    let files = vec![FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }];
    let name_index = RepoNameIndex::build(&files);
    let type_index = TypeIndex::build(&files);
    // The coincidental class "handle" is a known in-repo type name
    // (via `type_nesting`) -- the strongest possible case for the
    // `is_known_type_name` Advisory fallback to misfire on.

    let resolved = resolve_receiver_type(
        &ReceiverExpr::Identifier("handle".to_string()),
        Some("Target"),
        Some(method_symbol),
        &typed_names,
        &name_index,
        &type_index,
    );
    assert_eq!(
        resolved,
        ReceiverEvidence::None,
        "an AMBIGUOUS local binding must never fall through to the static-type-name \
         fallback, even though the coincidentally same-named class 'handle' is a known \
         in-repo type"
    );
}

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
