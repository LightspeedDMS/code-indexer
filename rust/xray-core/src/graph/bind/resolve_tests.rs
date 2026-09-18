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
        None,
        None,
        None,
        true,
    );
    assert_eq!(narrowed.len(), 1);
    assert_eq!(narrowed[0].0.file_id, 11);
}
