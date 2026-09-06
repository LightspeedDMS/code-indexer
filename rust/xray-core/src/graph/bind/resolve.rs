//! AC4 Levels 0/1/2/5 reference resolution (Story #1787, S2). Called for
//! real by `super::resolve_site` (`bind()`'s per-reference-site helper) for
//! every invocation/type-reference/construction site in the repository.

use super::name_index::{DeclInfo, RepoNameIndex};
use super::scope::FileScope;
use super::REF_KIND_INVOCATION;
use crate::graph::extract::local_index::{DeclarationKind, ImportKind, LocalIndex};
use crate::graph::identity::{make_symbol_id, SymbolId};
use crate::graph::reasons;

/// Which `DeclarationKind` a reference resolves against: methods for
/// calls, types for type references and constructions.
fn target_kind_for_ref(ref_kind: u8) -> DeclarationKind {
    match ref_kind {
        REF_KIND_INVOCATION => DeclarationKind::Method,
        _ => DeclarationKind::Type,
    }
}

/// Trailing dot-segment of a dotted path stripped off, e.g.
/// `"com.foo.Bar"` -> `Some("com.foo")`. `None` for an unqualified path.
fn package_prefix(path: &str) -> Option<&str> {
    path.rfind('.').map(|idx| &path[..idx])
}

/// AC4 reasons contributed by `ref_scope`'s import list toward `decl`
/// (whose bare name is already known to equal `name`, since `decl` only
/// ever reaches here via `RepoNameIndex::lookup(name, ..)`).
///
/// `STATIC_IMPORT` is deliberately coarser than the other two: a static
/// import path's second-to-last segment names the DECLARING CLASS (e.g.
/// `java.lang.Math` in `import static java.lang.Math.max;`), not a
/// package, and `LocalIndex` records a method's own package but not its
/// enclosing class -- there is no substrate here to compare against. So
/// this reason is name-only: any static import whose last segment matches
/// `name` counts, regardless of `decl`'s package. This is a documented
/// scope limitation, not a bug.
fn import_reasons(name: &str, decl: &DeclInfo, ref_scope: &FileScope) -> u16 {
    let mut bits = 0u16;
    for import in &ref_scope.imports {
        match import.kind {
            ImportKind::Ordinary => {
                if import.path.rsplit('.').next() == Some(name)
                    && decl.package.as_deref() == package_prefix(&import.path)
                {
                    bits |= reasons::IMPORTED;
                }
            }
            ImportKind::Static => {
                if import.path.rsplit('.').next() == Some(name) {
                    bits |= reasons::STATIC_IMPORT;
                }
            }
            ImportKind::Wildcard => {
                if decl.package.as_deref() == Some(import.path.as_str()) {
                    bits |= reasons::WILDCARD_IMPORT;
                }
            }
        }
    }
    bits
}

/// Every context reasons bit for `decl` relative to the reference at
/// `ref_file_id`/`ref_scope`, computed independently of any narrowing:
/// `SAME_FILE`, `SAME_PACKAGE`, and whichever import reasons apply.
fn context_reasons(name: &str, decl: &DeclInfo, ref_file_id: u32, ref_scope: &FileScope) -> u16 {
    let mut bits = 0u16;
    if decl.file_id == ref_file_id {
        bits |= reasons::SAME_FILE;
    }
    if decl.package.is_some() && decl.package == ref_scope.package {
        bits |= reasons::SAME_PACKAGE;
    }
    bits |= import_reasons(name, decl, ref_scope);
    bits
}

/// AC4 Level 1 ("+arity"): tags every candidate whose declared
/// `param_count` matches `arg_count` with `ARITY_MATCH`, and NARROWS the
/// set to just those matches -- but only when that is safe: `arg_count`
/// must be known, at least one candidate must match, and the match must
/// be a PROPER subset (never silently wipe every candidate, and never
/// "narrow" to the same set that was already there).
fn apply_arity_narrowing(candidates: &mut Vec<(DeclInfo, u16)>, arg_count: Option<usize>) {
    let Some(arg_count) = arg_count else { return };
    let matching: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (d, _))| d.param_count == Some(arg_count))
        .map(|(i, _)| i)
        .collect();
    if matching.is_empty() {
        return;
    }
    for &i in &matching {
        candidates[i].1 |= reasons::ARITY_MATCH;
    }
    if matching.len() < candidates.len() {
        *candidates = matching.into_iter().map(|i| candidates[i].clone()).collect();
    }
}

/// AC4 Level 2 ("+import context"): narrows to candidates reachable via
/// `SAME_FILE`/`SAME_PACKAGE`/`IMPORTED`/`STATIC_IMPORT`/`WILDCARD_IMPORT`
/// evidence, when that is a genuine, non-degenerate narrowing (at least
/// one reachable candidate, and not all of them already were).
fn apply_import_context_narrowing(candidates: &mut Vec<(DeclInfo, u16)>) {
    const CONTEXT_MASK: u16 = reasons::SAME_FILE
        | reasons::SAME_PACKAGE
        | reasons::IMPORTED
        | reasons::STATIC_IMPORT
        | reasons::WILDCARD_IMPORT;
    let reachable: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (_, bits))| bits & CONTEXT_MASK != 0)
        .map(|(i, _)| i)
        .collect();
    if reachable.is_empty() || reachable.len() == candidates.len() {
        return;
    }
    *candidates = reachable.into_iter().map(|i| candidates[i].clone()).collect();
}

/// Resolves ONE reference (bare `name`, of kind `ref_kind`) into its
/// candidate set. Always a `Vec`: empty means unresolved/out-of-repo,
/// length 1 can mean either "genuinely only one declaration anywhere with
/// this name" (tagged `UNIQUE_NAME_IN_REPO`, AC4 Level 5, short-circuiting
/// all further narrowing) or "several existed but every level of
/// narrowing this binder applies converged on one"; length > 1 means
/// ambiguous.
///
/// `index_is_complete` (dual-review defect D3): `UNIQUE_NAME_IN_REPO`
/// asserts "no OTHER declaration anywhere in the repo shares this name" --
/// a claim `RepoNameIndex` can only back up when it was built from EVERY
/// file in the repo. When the caller's indexing pass dropped some files
/// (`max_files` truncation, an extractor panic, an unreadable source
/// file), `pool.len() == 1` only proves "unique in the files we managed to
/// index", a strictly weaker claim. Passing `false` disables the
/// short-circuit so a same-named declaration hiding in a dropped file can
/// never be silently ignored -- the reference instead flows through the
/// same context/arity/import narrowing an ambiguous (`pool.len() > 1`)
/// reference already uses, which can never produce `Confidence::Exact`.
pub(crate) fn resolve_reference(
    name: &str,
    ref_kind: u8,
    ref_file_id: u32,
    ref_scope: &FileScope,
    arg_count: Option<usize>,
    name_index: &RepoNameIndex,
    index_is_complete: bool,
) -> Vec<(DeclInfo, u16)> {
    let pool = name_index.lookup(name, target_kind_for_ref(ref_kind));
    if pool.is_empty() {
        return Vec::new();
    }
    if pool.len() == 1 && index_is_complete {
        return vec![(pool[0].clone(), reasons::UNIQUE_NAME_IN_REPO)];
    }

    let mut with_reasons: Vec<(DeclInfo, u16)> = pool
        .into_iter()
        .map(|d| (d.clone(), context_reasons(name, d, ref_file_id, ref_scope)))
        .collect();
    apply_arity_narrowing(&mut with_reasons, arg_count);
    apply_import_context_narrowing(&mut with_reasons);
    with_reasons
}

/// Approximates the enclosing declaration for a reference at `line` in
/// `file_id`: the declaration in the SAME file with the largest `line`
/// that is still `<= line` (the innermost declaration known to have
/// started before this reference). `LocalIndex` records no true
/// parent/enclosing-declaration link, so this is a documented, purely
/// binder-side heuristic over already-extracted declaration lines -- it
/// touches no AST. Falls back to a stable per-file sentinel symbol
/// (`local_index == u32::MAX`, never assigned by real extraction, which
/// always starts at 0 and increments) when the file declares nothing at
/// or before `line`.
pub(crate) fn enclosing_symbol(index: &LocalIndex, file_id: u32, line: usize) -> SymbolId {
    index
        .declarations
        .iter()
        .filter(|d| d.line <= line)
        .max_by_key(|d| d.line)
        .map(|d| d.symbol)
        .unwrap_or_else(|| make_symbol_id(file_id, u32::MAX))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::bind::FileForBind;

    fn file(file_id: u32, language: &str, index: LocalIndex) -> FileForBind {
        FileForBind { file_id, language: language.to_string(), index }
    }

    fn method_decl(
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
        }
    }

    fn package_decl(file_id: u32, name: &str) -> crate::graph::extract::local_index::Declaration {
        crate::graph::extract::local_index::Declaration {
            kind: DeclarationKind::Package,
            name: name.to_string(),
            line: 1,
            symbol: make_symbol_id(file_id, 999),
            param_count: None,
        }
    }

    /// AC4: a reference to a name the repo declares nowhere gets an EMPTY
    /// candidate set -- never a guessed target.
    #[test]
    fn out_of_repo_reference_resolves_to_an_empty_candidate_set() {
        let name_index = RepoNameIndex::build(&[file(1, "java", LocalIndex::new())]);
        let scope = FileScope { package: None, imports: Vec::new() };

        let candidates =
            resolve_reference("neverDeclared", REF_KIND_INVOCATION, 1, &scope, None, &name_index, true);
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
        let scope = FileScope { package: None, imports: Vec::new() };

        let candidates = resolve_reference("getId", REF_KIND_INVOCATION, 1, &scope, None, &name_index, true);
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
        let scope = FileScope { package: None, imports: Vec::new() };

        let level0 = resolve_reference("run", REF_KIND_INVOCATION, 1, &scope, None, &name_index, true);
        assert_eq!(level0.len(), 2, "level 0 (no arity known) keeps both");

        let narrowed = resolve_reference("run", REF_KIND_INVOCATION, 1, &scope, Some(2), &name_index, true);
        assert_eq!(narrowed.len(), 1);
        assert_eq!(narrowed[0].0.file_id, 11);
        assert_ne!(narrowed[0].1 & reasons::ARITY_MATCH, 0);
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

        let scope_no_import = FileScope { package: Some("pkg.ref".to_string()), imports: Vec::new() };
        let arity_only =
            resolve_reference("run", REF_KIND_INVOCATION, 1, &scope_no_import, Some(0), &name_index, true);
        assert_eq!(arity_only.len(), 3, "arity alone cannot narrow when every candidate matches");

        let scope_with_import = FileScope {
            package: Some("pkg.ref".to_string()),
            imports: vec![crate::graph::extract::local_index::ImportRecord {
                kind: ImportKind::Ordinary,
                path: "pkg.b.run".to_string(),
                line: 1,
            }],
        };
        let narrowed =
            resolve_reference("run", REF_KIND_INVOCATION, 1, &scope_with_import, Some(0), &name_index, true);
        assert_eq!(narrowed.len(), 1);
        assert_eq!(narrowed[0].0.file_id, 11);
    }

    /// AC4 Level 5: a name unique across the whole repo reaches
    /// `Confidence::Exact` via `UNIQUE_NAME_IN_REPO` -- but ONLY when the
    /// caller confirms the index is complete.
    #[test]
    fn unique_name_in_repo_resolves_to_a_single_exact_confidence_candidate() {
        use crate::graph::confidence::Confidence;

        let mut index = LocalIndex::new();
        index.declarations.push(method_decl("uniqueMethod", 1, 0, None));
        let name_index = RepoNameIndex::build(&[file(1, "java", index)]);
        let scope = FileScope { package: None, imports: Vec::new() };

        let candidates =
            resolve_reference("uniqueMethod", REF_KIND_INVOCATION, 1, &scope, None, &name_index, true);
        assert_eq!(candidates.len(), 1);
        let reasons_bits = candidates[0].1;
        assert_ne!(reasons_bits & reasons::UNIQUE_NAME_IN_REPO, 0);
        assert_eq!(Confidence::derive(reasons_bits), Confidence::Exact);
    }

    /// Dual-review defect D3 (Critical): `UNIQUE_NAME_IN_REPO` must NEVER
    /// be claimed when the caller reports the index is PARTIAL (e.g. this
    /// exact same fixture, but a sibling file elsewhere in the real repo
    /// was dropped by `max_files` truncation and never made it into
    /// `RepoNameIndex`). A wrong implementation that ignored
    /// `index_is_complete` would pass the test right above this one and
    /// still fail here -- the discriminating input is the SAME single
    /// declaration, only the completeness flag differs.
    #[test]
    fn a_name_unique_only_in_a_partial_index_does_not_get_exact_confidence() {
        use crate::graph::confidence::Confidence;

        let mut index = LocalIndex::new();
        index.declarations.push(method_decl("uniqueMethod", 1, 0, None));
        let name_index = RepoNameIndex::build(&[file(1, "java", index)]);
        let scope = FileScope { package: None, imports: Vec::new() };

        let candidates =
            resolve_reference("uniqueMethod", REF_KIND_INVOCATION, 1, &scope, None, &name_index, false);
        assert_eq!(candidates.len(), 1, "the sole indexed declaration is still a candidate -- never dropped");
        let reasons_bits = candidates[0].1;
        assert_eq!(
            reasons_bits & reasons::UNIQUE_NAME_IN_REPO,
            0,
            "UNIQUE_NAME_IN_REPO must not be claimed from a partial index"
        );
        assert_ne!(
            Confidence::derive(reasons_bits),
            Confidence::Exact,
            "a partial-index match must never reach Exact confidence"
        );
    }

    /// `enclosing_symbol` only ever receives ONE file's `LocalIndex` (each
    /// file has its own, in `bind()`'s real pipeline) -- there is no
    /// cross-file data for it to confuse, so the real discriminating axis
    /// is LINE proximity, not file identity. A wrong implementation that
    /// picked the LAST declaration regardless of line (rather than the
    /// nearest one AT OR BEFORE the query line) would pass a naive test
    /// but fail this one: querying line 10 (between the two declarations)
    /// must still return the EARLIER one, not the later one at line 20.
    #[test]
    fn enclosing_symbol_picks_the_nearest_preceding_declaration_by_line() {
        let mut index = LocalIndex::new();
        let mut first = method_decl("first", 1, 0, None);
        first.line = 5;
        let mut second = method_decl("second", 1, 1, None);
        second.line = 20;
        index.declarations.push(first);
        index.declarations.push(second);

        assert_eq!(enclosing_symbol(&index, 1, 25), make_symbol_id(1, 1));
        assert_eq!(enclosing_symbol(&index, 1, 10), make_symbol_id(1, 0));
    }

    /// A declaration existing in the index is NOT enough to avoid the
    /// sentinel fallback -- it must actually PRECEDE the query line. A
    /// wrong implementation that fell back to the sentinel only on a
    /// truly empty index (never checking `d.line <= line`) would wrongly
    /// pick this later-only declaration instead of falling back.
    #[test]
    fn enclosing_symbol_falls_back_to_a_sentinel_when_nothing_precedes_the_line() {
        let mut index = LocalIndex::new();
        let mut later = method_decl("later", 3, 0, None);
        later.line = 50;
        index.declarations.push(later);

        assert_eq!(enclosing_symbol(&index, 3, 10), make_symbol_id(3, u32::MAX));
    }
}
