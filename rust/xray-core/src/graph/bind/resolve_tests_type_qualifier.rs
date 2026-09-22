//! Issue #1922: discriminating regression tests for `narrowing::apply_
//! type_qualifier_narrowing`, exercised end-to-end through `resolve_
//! reference` exactly like `resolve_tests.rs`/`resolve_tests_family.rs`
//! do -- split into its own file (Rule 6, anti-file-bloat) rather than
//! growing either already-large sibling.
//!
//! `apply_type_qualifier_narrowing`'s rule: hard-narrow ONLY when the
//! qualifier POSITIVELY resolves to a declared in-repo type AND at least
//! one candidate already matches it; every other combination -- an
//! unresolved qualifier, or a resolved qualifier matching zero
//! candidates -- is a no-op, exactly preserving the pre-#1922 tag-only
//! pipeline (neither is proof the real target is external). A few tests
//! below assert this no-op behavior directly, with an explanatory note
//! each; the real end-to-end shapes that motivate it (an interface
//! constant field, several Java-calling-Kotlin shapes, a
//! static-imported external constant) are covered by dedicated
//! integration tests under `rust/xray-core/tests/`.
//!
//! Every fixture below uses neutral, public-repository-safe naming
//! (`A`/`B`/`Caller`/`Target`-style class names, `com.example`-style
//! packages where a package matters) -- no third-party library
//! identifiers, per this repository's Disclosure Discipline.

use super::tests::{file, method_decl};
use super::*;
use crate::graph::bind::FileForBind;
use crate::graph::extract::local_index::MethodOwnerRecord;

fn type_index_for(files: &[FileForBind]) -> super::super::families::TypeIndex {
    super::super::families::TypeIndex::build(files)
}

/// THE core discriminating case: a static facade `class A { R m(x) {
/// return B.m(x); } }` alongside `class B { R m(x) {...} }` -- both
/// declare a same-named, same-arity `m`, and the CALLER is `A` itself
/// (same file as its own `m`). Pre-#1922, `apply_import_context_
/// narrowing` collapsed this to the SELF candidate (`A.m`, the only one
/// carrying `SAME_FILE`) and silently dropped `B.m` (which carries no
/// same-file/same-package/import evidence at all) -- exactly the false
/// self-loop + dropped real edge the issue reports. Fails on pre-#1922
/// `resolve_reference` (returns `[A.m]`, `file_id == 10`).
#[test]
fn type_qualified_call_binds_the_qualifier_type_not_the_callers_own_same_named_method() {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("m", 10, 0, Some(1)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(10, 0),
        enclosing_type: "A".to_string(),
    });
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("m", 11, 0, Some(1)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(11, 0),
        enclosing_type: "B".to_string(),
    });
    let files = vec![file(10, "java", file_a), file(11, "java", file_b)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = type_index_for(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    // The call site lives in file 10 (A's own file) -- reproducing the
    // real repro's "caller and self-candidate share a file" shape.
    let candidates = resolve_reference(
        "m",
        REF_KIND_INVOCATION,
        10,
        &scope,
        Some(1),
        &[],
        &name_index,
        &type_index,
        Some("B"),
        false,
        None,
        None,
        None,
        true,
        true,
    );

    assert_eq!(
        candidates.len(),
        1,
        "exactly one edge -- neither zero nor an ambiguous multi-candidate set -- got {} \
         candidate(s)",
        candidates.len()
    );
    assert_eq!(
        candidates[0].0.file_id, 11,
        "the edge must target B.m (the qualifier type), never A.m (the caller's own \
         same-named self-candidate)"
    );
    assert!(
        candidates.iter().all(|(d, _)| d.file_id != 10),
        "no self-edge (A.m -> A.m) may survive"
    );
    assert_ne!(
        candidates[0].1 & reasons::RECEIVER_TYPE_MATCH,
        0,
        "the surviving candidate must be RECEIVER_TYPE_MATCH-tagged, confirming it was \
         selected because it matches the qualifier, not by accident"
    );
}

/// Overloaded variant: `A` declares SEVERAL `m` overloads (one matching
/// the call's arity, one not) -- the qualified call must still bind
/// exactly `B.m`, never any `A.m` overload, regardless of which of A's
/// overloads happens to match arity.
#[test]
fn type_qualified_call_with_overloaded_caller_still_binds_only_the_qualifier_type() {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("m", 10, 0, Some(1)));
    file_a.declarations.push(method_decl("m", 10, 1, Some(2)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(10, 0),
        enclosing_type: "A".to_string(),
    });
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(10, 1),
        enclosing_type: "A".to_string(),
    });
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("m", 11, 0, Some(1)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(11, 0),
        enclosing_type: "B".to_string(),
    });
    let files = vec![file(10, "java", file_a), file(11, "java", file_b)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = type_index_for(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "m",
        REF_KIND_INVOCATION,
        10,
        &scope,
        Some(1),
        &[],
        &name_index,
        &type_index,
        Some("B"),
        false,
        None,
        None,
        None,
        true,
        true,
    );

    assert_eq!(candidates.len(), 1, "only B.m must survive");
    assert_eq!(candidates[0].0.file_id, 11);
}

/// `Type::m` method reference: no argument list at all (`arg_count:
/// None`), so `apply_arity_narrowing` cannot discriminate by arity the
/// way an ordinary call can -- both `A.m` and `B.m` remain viable
/// candidates through every arity-based pass. The type-qualifier
/// hard-narrow must still isolate `B.m` alone.
#[test]
fn type_qualified_method_reference_with_no_arity_evidence_still_binds_only_the_qualifier_type() {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("m", 20, 0, Some(1)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(20, 0),
        enclosing_type: "A".to_string(),
    });
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("m", 21, 0, Some(1)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(21, 0),
        enclosing_type: "B".to_string(),
    });
    let files = vec![file(20, "java", file_a), file(21, "java", file_b)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = type_index_for(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    // arg_count: None, arg_shapes: [] -- exactly what a `B::m` method
    // reference extracts (`java.rs::extract_method_reference`).
    let candidates = resolve_reference(
        "m",
        REF_KIND_INVOCATION,
        20,
        &scope,
        None,
        &[],
        &name_index,
        &type_index,
        Some("B"),
        false,
        None,
        None,
        None,
        true,
        true,
    );

    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].0.file_id, 21);
}

/// A definitely-type-qualified call to an UNRESOLVED (external)
/// qualifier must never bind NOTHING, even when the bare method name is
/// the caller's own and globally unique in the repo -- `import static
/// ext.Holder.CONSTANT; ... CONSTANT.m();` with `m` declared on some
/// OTHER indexed type must keep its real edge. This binder never clears
/// on an unresolved qualifier -- the AC4 Level 5 unique-name shortcut
/// fires exactly as it always did (external-receiver over-binding is a
/// separately tracked scope, not this guard's).
#[test]
fn type_qualified_call_to_an_unresolved_qualifier_still_takes_the_unique_name_shortcut() {
    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(method_decl("emptyList", 30, 0, Some(0)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(30, 0),
        enclosing_type: "Caller".to_string(),
    });
    let files = vec![file(30, "java", file_a)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = type_index_for(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "emptyList",
        REF_KIND_INVOCATION,
        30,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        // The qualifier resolves to no known in-repo type --
        // `receiver_type: None` -- but IS definitely type-shaped
        // (uppercase, no local/field evidence anywhere).
        None,
        false,
        None,
        None,
        None,
        true,
        true,
    );

    assert_eq!(
        candidates.len(),
        1,
        "a definitely-type-qualified call to an unresolved qualifier must keep taking the \
         unique-name shortcut exactly as before #1922 -- got {} candidate(s)",
        candidates.len()
    );
    assert_ne!(candidates[0].1 & reasons::UNIQUE_NAME_IN_REPO, 0);
}

/// Companion negative control: the identical single-candidate/unresolved-
/// receiver-type fixture, but with `receiver_is_type_qualifier: false`
/// (an ordinary unqualified call, or one whose receiver could not be
/// proven type-shaped) -- proves the shortcut's behaviour does not
/// depend on the flag at all post-#1922 (both `true` and `false` take
/// it identically now that the "external -> zero" branch is gone).
#[test]
fn unqualified_reference_with_unresolved_receiver_type_still_takes_the_unique_name_shortcut() {
    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(method_decl("emptyList", 31, 0, Some(0)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(31, 0),
        enclosing_type: "Caller".to_string(),
    });
    let files = vec![file(31, "java", file_a)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = type_index_for(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "emptyList",
        REF_KIND_INVOCATION,
        31,
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

    assert_eq!(
        candidates.len(),
        1,
        "without a definite type qualifier, the pre-#1922 unique-name-shortcut behaviour \
         (admit the sole candidate) must be unchanged"
    );
    assert_ne!(candidates[0].1 & reasons::UNIQUE_NAME_IN_REPO, 0);
}

/// Lowercase qualifier (variable/field, e.g. `helper.m()`) must keep
/// TODAY'S behaviour: `receiver_is_type_qualifier: false` leaves
/// `apply_type_qualifier_narrowing` a no-op, so an unrelated same-named
/// sibling stays accepted noise (tag-only), exactly like every receiver-
/// type-adjacent pass in `narrowing.rs` already documents. No local-
/// variable SCOPE analysis is introduced anywhere in this path (#1919).
#[test]
fn lowercase_variable_qualified_call_keeps_the_pre_1922_tag_only_behaviour() {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("m", 40, 0, Some(1)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(40, 0),
        enclosing_type: "B".to_string(),
    });
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("m", 41, 0, Some(1)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(41, 0),
        enclosing_type: "Other".to_string(),
    });
    let files = vec![file(40, "java", file_a), file(41, "java", file_b)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = type_index_for(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "m",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(1),
        &[],
        &name_index,
        &type_index,
        Some("B"),
        false,
        None,
        None,
        None,
        true,
        // receiver_is_type_qualifier: false -- e.g. `helper.m()`, a
        // lowercase variable receiver.
        false,
    );

    assert_eq!(
        candidates.len(),
        2,
        "a variable/field-qualified call must never hard-narrow -- both B.m and Other.m stay \
         accepted noise, exactly as before #1922"
    );
}

/// Guard the safe direction (mandatory per the issue): when the
/// qualifier resolves to a known in-repo type but ITS OWN supertype
/// evidence is recorded INCOMPLETE, `apply_type_qualifier_narrowing`
/// must stay soft (never fabricate an empty set on unproven absence) --
/// mirrors the identical guard every sibling receiver-type-adjacent pass
/// already documents (`apply_receiver_type_narrowing`,
/// `apply_super_class_narrowing`). Indirect: `apply_receiver_type_
/// narrowing` itself skips tagging entirely under incomplete evidence,
/// so the tagged subset this pass sees is trivially empty -- proving the
/// no-op path, not a bespoke incomplete-evidence check of its own.
#[test]
fn type_qualifier_narrowing_skips_when_the_qualifier_types_own_supertype_evidence_is_incomplete()
{
    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(method_decl("helper", 50, 0, Some(0)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(50, 0),
        enclosing_type: "Other".to_string(),
    });
    file_a.incomplete_supertypes.push("B".to_string());
    let mut file_b = LocalIndex::new();
    file_b
        .declarations
        .push(method_decl("helper", 51, 0, Some(0)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(51, 0),
        enclosing_type: "AlsoUnrelated".to_string(),
    });
    let files = vec![file(50, "java", file_a), file(51, "java", file_b)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = type_index_for(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "helper",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        Some("B"),
        false,
        None,
        None,
        None,
        true,
        true,
    );

    assert_eq!(
        candidates.len(),
        2,
        "incomplete supertype evidence on the qualifier's own type must fall back to keeping \
         the pool, never narrow to empty on unproven absence -- got {} candidate(s)",
        candidates.len()
    );
}

/// A HARD EMPTY when the qualifier resolves to a known in-repo type
/// with COMPLETE supertype evidence but zero candidates match it would
/// wrongly treat that as "proof every same-named candidate elsewhere is
/// a coincidental decoy". It is not: an interface constant field, a
/// Kotlin companion `@JvmStatic` member attributed to a different
/// enclosing-type string than its outer class, and a Kotlin top-level
/// function's synthetic facade name can all produce a real, positively-
/// resolved `receiver_type` with a genuinely EMPTY tagged subset for
/// reasons that have nothing to do with the call site being wrong. This
/// guard treats an empty tagged subset as a no-op in every case, not
/// just the incomplete-supertype-evidence one.
#[test]
fn type_qualifier_narrowing_never_hard_empties_on_a_zero_match_even_with_complete_evidence() {
    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(method_decl("parse", 60, 0, Some(1)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(60, 0),
        enclosing_type: "UnrelatedA".to_string(),
    });
    let mut file_b = LocalIndex::new();
    file_b
        .declarations
        .push(method_decl("parse", 61, 0, Some(1)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(61, 0),
        enclosing_type: "UnrelatedB".to_string(),
    });
    let files = vec![file(60, "java", file_a), file(61, "java", file_b)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = type_index_for(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "parse",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(1),
        &[],
        &name_index,
        &type_index,
        // "TargetType" is a real, complete-evidence qualifier (no
        // `incomplete_supertypes` entry anywhere), but declares/inherits
        // no `parse` method anywhere in THIS pool.
        Some("TargetType"),
        false,
        None,
        None,
        None,
        true,
        true,
    );

    assert_eq!(
        candidates.len(),
        2,
        "a zero-match tagged subset must never be hard-emptied, even with complete supertype \
         evidence -- both UnrelatedA.parse and UnrelatedB.parse must survive as accepted \
         noise, exactly as the pre-#1922 tag-only pipeline already kept them -- got {} \
         candidate(s)",
        candidates.len()
    );
}
