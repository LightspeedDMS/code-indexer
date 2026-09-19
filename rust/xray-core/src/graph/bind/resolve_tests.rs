//! F5 (#1873/#1875 rework): `resolve.rs`'s unit tests, relocated verbatim
//! out of its `#[cfg(test)] mod tests { ... }` body to keep both files
//! under the project's line limit. Split in two by subject: this half
//! covers the dispatcher basics (empty/ambiguous pools), arity, overload
//! shape, and import-context narrowing. `resolve_tests_family.rs` covers
//! same-class/receiver-type narrowing, inheritance-family expansion, the
//! unique-name shortcut, and `enclosing_symbol`.

use super::*;
use crate::graph::bind::FileForBind;

/// N6 (#1873/#1875 second-review rework): `pub(super)` so the sibling test
/// module `resolve_tests_family.rs` (also declared directly under
/// `resolve.rs`, hence visible via `super::tests::*`) can reuse these
/// instead of keeping a verbatim duplicate copy.
pub(super) fn file(file_id: u32, language: &str, index: LocalIndex) -> FileForBind {
    FileForBind {
        file_id,
        language: language.to_string(),
        index,
    }
}

pub(super) fn method_decl(
    name: &str,
    file_id: u32,
    local: u32,
    param_count: Option<usize>,
) -> crate::graph::extract::local_index::Declaration {
    crate::graph::extract::local_index::Declaration {
        kind: DeclarationKind::Method,
        name: name.to_string(),
        line: 1,
        symbol: make_symbol_id(file_id, local),
        param_count,
        param_types: Vec::new(),
        is_varargs: false,
    }
}

pub(super) fn package_decl(
    file_id: u32,
    name: &str,
) -> crate::graph::extract::local_index::Declaration {
    crate::graph::extract::local_index::Declaration {
        kind: DeclarationKind::Package,
        name: name.to_string(),
        line: 1,
        symbol: make_symbol_id(file_id, 999),
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
    }
}

/// AC4: a reference to a name the repo declares nowhere gets an EMPTY
/// candidate set -- never a guessed target.
#[test]
fn out_of_repo_reference_resolves_to_an_empty_candidate_set() {
    let name_index = RepoNameIndex::build(&[file(1, "java", LocalIndex::new())]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "neverDeclared",
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
    );
    assert!(candidates.is_empty());
}

/// AC4's central discriminating case: several same-named declarations
/// (e.g. several `getId` methods, none reachable via file/package/
/// import/arity evidence) resolve to a candidate SET with len > 1 --
/// never a picked "winner".
#[test]
fn ambiguous_same_name_declarations_yield_a_multi_candidate_set_not_a_picked_winner() {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("getId", 10, 0, None));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("getId", 11, 0, None));
    let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "getId",
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
    );
    assert_eq!(candidates.len(), 2);
}

/// AC4 Level 1: arity narrows a set a bare-name match (Level 0) would
/// have kept intact.
#[test]
fn arity_narrowing_removes_candidates_a_bare_name_match_would_have_kept() {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("run", 10, 0, Some(1)));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("run", 11, 0, Some(2)));
    let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let level0 = resolve_reference(
        "run",
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
    );
    assert_eq!(level0.len(), 2, "level 0 (no arity known) keeps both");

    let narrowed = resolve_reference(
        "run",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(2),
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert_eq!(narrowed.len(), 1);
    assert_eq!(narrowed[0].0.file_id, 11);
    assert_ne!(narrowed[0].1 & reasons::ARITY_MATCH, 0);
}

fn varargs_method_decl(
    name: &str,
    file_id: u32,
    local: u32,
    param_count: usize,
) -> crate::graph::extract::local_index::Declaration {
    crate::graph::extract::local_index::Declaration {
        kind: DeclarationKind::Method,
        name: name.to_string(),
        line: 1,
        symbol: make_symbol_id(file_id, local),
        param_count: Some(param_count),
        param_types: Vec::new(),
        is_varargs: true,
    }
}

/// AC2 (Story #1793, S4): a VARARGS declaration's arity match is
/// `arg_count >= param_count - 1` (any call passing zero or more
/// trailing varargs), never plain equality -- the pre-existing
/// `apply_arity_narrowing` equality check would wrongly exclude a
/// varargs candidate from every call whose arg_count differs from its
/// formal parameter count. A sibling NON-varargs candidate with a
/// different declared param_count must still be excluded by ordinary
/// equality, proving this is a widened match for varargs only, not a
/// blanket relaxation.
#[test]
fn varargs_declaration_matches_any_arg_count_at_or_above_its_minimum() {
    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(varargs_method_decl("run", 10, 0, 1));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("run", 11, 0, Some(3)));
    let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let narrowed = resolve_reference(
        "run",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(5),
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert_eq!(
        narrowed.len(),
        1,
        "only the varargs candidate accepts 5 args"
    );
    assert_eq!(narrowed[0].0.file_id, 10);
    assert_ne!(narrowed[0].1 & reasons::ARITY_MATCH, 0);
}

/// AC2: a `Cast`/`Constructor` argument's named type is OPEN-WORLD
/// evidence -- it never EXCLUDES a candidate on name mismatch alone
/// (this repo's heuristic inheritance index cannot prove two named
/// types are unrelated), but it DOES preferentially narrow to the
/// candidate whose declared type EXACTLY matches, when a genuine
/// match exists among the candidates.
#[test]
fn named_type_preference_narrows_between_two_unrelated_named_types() {
    use crate::graph::extract::local_index::ArgShape;

    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl_with_types(
        "save",
        10,
        0,
        vec!["Foo".to_string()],
    ));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl_with_types(
        "save",
        11,
        0,
        vec!["Bar".to_string()],
    ));
    let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let arg_shapes = [ArgShape::Cast("Foo".to_string())];
    let narrowed = resolve_reference(
        "save",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(1),
        &arg_shapes,
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert_eq!(
        narrowed.len(),
        1,
        "the exactly-matching Foo-typed candidate must be preferred"
    );
    assert_eq!(narrowed[0].0.file_id, 10);
    assert_ne!(narrowed[0].1 & reasons::OVERLOAD_ARG_TYPE_MATCH, 0);
}

fn method_decl_with_types(
    name: &str,
    file_id: u32,
    local: u32,
    param_types: Vec<String>,
) -> crate::graph::extract::local_index::Declaration {
    crate::graph::extract::local_index::Declaration {
        kind: DeclarationKind::Method,
        name: name.to_string(),
        line: 1,
        symbol: make_symbol_id(file_id, local),
        param_count: Some(param_types.len()),
        param_types,
        is_varargs: false,
    }
}

/// AC2 (Story #1793, S4): a `StringLiteral` argument DEFINITELY
/// cannot bind to a numeric or boolean declared parameter type --
/// candidate-set REDUCTION beyond arity (both candidates here already
/// match arity: one parameter each). The `String`-typed sibling must
/// survive; the `int`-typed one must be excluded.
#[test]
fn literal_shape_excludes_a_candidate_with_a_definitely_incompatible_declared_type() {
    use crate::graph::extract::local_index::ArgShape;

    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl_with_types(
        "save",
        10,
        0,
        vec!["String".to_string()],
    ));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl_with_types(
        "save",
        11,
        0,
        vec!["int".to_string()],
    ));
    let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let arg_shapes = [ArgShape::StringLiteral];
    let narrowed = resolve_reference(
        "save",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(1),
        &arg_shapes,
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert_eq!(
        narrowed.len(),
        1,
        "the int-typed candidate must be excluded by a String literal argument"
    );
    assert_eq!(narrowed[0].0.file_id, 10);
    assert_ne!(narrowed[0].1 & reasons::OVERLOAD_ARG_TYPE_MATCH, 0);
}

/// AC4 Level 2: import context narrows further than arity alone. Every
/// candidate here matches arity (0 args), so Level 1 makes no
/// progress on its own -- only adding import-context evidence narrows.
#[test]
fn import_context_narrows_further_than_arity_alone() {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(package_decl(10, "pkg.a"));
    file_a.declarations.push(method_decl("run", 10, 0, Some(0)));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(package_decl(11, "pkg.b"));
    file_b.declarations.push(method_decl("run", 11, 0, Some(0)));
    let mut file_c = LocalIndex::new();
    file_c.declarations.push(package_decl(12, "pkg.c"));
    file_c.declarations.push(method_decl("run", 12, 0, Some(0)));
    let name_index = RepoNameIndex::build(&[
        file(10, "java", file_a),
        file(11, "java", file_b),
        file(12, "java", file_c),
    ]);

    let scope_no_import = FileScope {
        package: Some("pkg.ref".to_string()),
        imports: Vec::new(),
    };
    let arity_only = resolve_reference(
        "run",
        REF_KIND_INVOCATION,
        1,
        &scope_no_import,
        Some(0),
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert_eq!(
        arity_only.len(),
        3,
        "arity alone cannot narrow when every candidate matches"
    );

    let scope_with_import = FileScope {
        package: Some("pkg.ref".to_string()),
        imports: vec![crate::graph::extract::local_index::ImportRecord {
            kind: ImportKind::Ordinary,
            path: "pkg.b.run".to_string(),
            line: 1,
        }],
    };
    let narrowed = resolve_reference(
        "run",
        REF_KIND_INVOCATION,
        1,
        &scope_with_import,
        Some(0),
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert_eq!(narrowed.len(), 1);
    assert_eq!(narrowed[0].0.file_id, 11);
}

/// Bug #1898 (P1 of epic #1906) -- the canonical failure shape: a 0-arg
/// call whose real target is EXTERNAL to the repo (e.g. a JDK
/// `close()`), resolved against a repo that only declares 1-param
/// `close` methods. When NO candidate in the same-named pool matches the
/// call site's arity, the pre-fix `apply_arity_narrowing` silently kept
/// the ENTIRE bare-name pool rather than narrowing to empty -- fabricating
/// an edge to a wrong-arity in-repo candidate. `pool.len() == 2` here
/// (two unrelated 1-param `close` declarations) so the AC4 Level 5
/// unique-name shortcut -- which bypasses arity checking entirely --
/// never applies; this exercises `apply_arity_narrowing` itself.
#[test]
fn arity_mismatch_with_no_matching_candidate_yields_zero_not_the_wrong_arity_pool() {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("close", 10, 0, Some(1)));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("close", 11, 0, Some(1)));
    let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "close",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert!(
        candidates.is_empty(),
        "a 0-arg call must not fall back onto the repo's wrong-arity `close(1 params)` \
         declarations -- got {} candidate(s)",
        candidates.len()
    );
}

/// Symmetric case: a 1-arg call site against a repo whose only same-named
/// declarations take 0 params (e.g. a `map.get(key)` call whose real
/// target is an external collection type, resolved against an unrelated
/// in-repo `get()` overload that takes no arguments).
#[test]
fn arity_mismatch_one_arg_call_against_zero_param_declarations_yields_zero() {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("get", 10, 0, Some(0)));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("get", 11, 0, Some(0)));
    let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "get",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(1),
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert!(
        candidates.is_empty(),
        "a 1-arg call must not fall back onto the repo's wrong-arity `get(0 params)` \
         declarations -- got {} candidate(s)",
        candidates.len()
    );
}

/// Guard against over-correction (#1898 AC): unknown arity
/// (`arg_count: None`) must still admit the full candidate pool --
/// `apply_arity_narrowing`'s `None` early return is untouched by the
/// #1898 fix, only the KNOWN-arity/zero-match branch changed.
#[test]
fn unknown_arity_still_admits_the_full_candidate_pool() {
    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(method_decl("process", 10, 0, Some(1)));
    let mut file_b = LocalIndex::new();
    file_b
        .declarations
        .push(method_decl("process", 11, 0, Some(3)));
    let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "process",
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
    );
    assert_eq!(
        candidates.len(),
        2,
        "unknown arity must never narrow -- both candidates stay admitted"
    );
}

/// P2-1 (#1898 code review, Anti-Silent-Failure): a candidate whose OWN
/// `param_count` is `None` (missing arity evidence -- e.g. a non-Java
/// extractor gap; latent for Java today, but Kotlin is P4 of the same
/// epic #1906, and an extractor that omits param counts must never
/// silently zero every arity-known call) must be RETAINED by
/// `apply_arity_narrowing`, never treated as a definite mismatch --
/// mirroring the "missing/ambiguous evidence retains the candidate"
/// doctrine already documented on `apply_private_visibility_filter`
/// three functions later in narrowing.rs. `file_a`'s `param_count:
/// Some(5)` is a GENUINE mismatch against `arg_count: Some(2)` and must
/// still be deleted (the #1898 P1 fix stays intact) -- proving this test
/// discriminates "unknown evidence retains" from "narrowing stopped
/// deleting anything at all". `pool.len() == 2` keeps the AC4 Level 5
/// unique-name shortcut out of the way.
#[test]
fn arity_narrowing_retains_a_candidate_with_unknown_param_count_evidence() {
    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(method_decl("process", 10, 0, Some(5)));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("process", 11, 0, None));
    let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "process",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(2),
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert_eq!(
        candidates.len(),
        1,
        "a candidate with unknown param_count evidence must be retained even though a \
         DIFFERENT, genuinely wrong-arity candidate is correctly deleted -- got {} \
         candidate(s)",
        candidates.len()
    );
    assert_eq!(
        candidates[0].0.file_id, 11,
        "the surviving candidate must be the unknown-arity one (file 11), never the \
         genuinely mismatched file 10"
    );
    assert_eq!(
        candidates[0].1 & reasons::ARITY_MATCH,
        0,
        "a candidate with NO arity evidence must never be tagged ARITY_MATCH -- retained \
         does not mean confirmed"
    );
}
