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

/// AC2 (Story #1793, S4): does `decl`'s declared arity accept a call
/// passing `arg_count` arguments? A varargs declaration (`Foo... x` as
/// its last formal parameter) accepts any `arg_count >= param_count - 1`
/// (the fixed leading parameters, plus zero or more trailing varargs) --
/// `saturating_sub` avoids an unsigned underflow if `param_count` were
/// ever 0 (never true for a genuine varargs method, which always has at
/// least its one varargs parameter, but this keeps the arithmetic total
/// rather than trusting that invariant). A non-varargs declaration keeps
/// the pre-existing exact-equality check.
fn param_count_matches_arity(decl: &DeclInfo, arg_count: usize) -> bool {
    let Some(param_count) = decl.param_count else { return false };
    if decl.is_varargs {
        arg_count >= param_count.saturating_sub(1)
    } else {
        param_count == arg_count
    }
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
        .filter(|(_, (d, _))| param_count_matches_arity(d, arg_count))
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

/// AC2 (Story #1793, S4): `decl`'s declared parameter TYPE at call-site
/// argument `position`, or `None` if `decl` carries no param-type
/// evidence at all (non-Java, or an extraction gap -- never fabricated).
/// For a varargs declaration, every position from `param_types.len() - 1`
/// onward maps to the SAME (last, element) declared type -- the varargs
/// parameter itself.
fn declared_type_at(decl: &DeclInfo, position: usize) -> Option<&str> {
    if decl.param_types.is_empty() {
        return None;
    }
    let index = if decl.is_varargs { position.min(decl.param_types.len() - 1) } else { position };
    decl.param_types.get(index).map(|s| s.as_str())
}

/// AC2: is `shape` DEFINITELY incompatible with `declared_type`? Only the
/// closed, fixed set of Java primitive/String/boxed-numeric/boolean/char
/// type NAMES is used here -- this is closed-world-safe (a
/// `StringLiteral` genuinely cannot bind to `int` in Java, full stop),
/// unlike a named class/interface type (open-world: this repo's
/// heuristic inheritance index can never prove "these two named types are
/// definitely unrelated"). `Cast`/`Constructor`/`Lambda`/`MethodReference`/
/// `Other` therefore never report incompatibility here -- see
/// `apply_overload_shape_narrowing`'s named-type PREFERENCE step for how
/// those contribute positive (not exclusionary) evidence instead.
fn literal_shape_is_incompatible(shape: &crate::graph::extract::local_index::ArgShape, declared_type: &str) -> bool {
    use crate::graph::extract::local_index::ArgShape;
    let is_numeric = matches!(
        declared_type,
        "int" | "long" | "double" | "float" | "short" | "byte" | "Integer" | "Long" | "Double" | "Float" | "Short" | "Byte"
    );
    let is_boolean = matches!(declared_type, "boolean" | "Boolean");
    let is_char = matches!(declared_type, "char" | "Character");
    let is_primitive = matches!(declared_type, "int" | "long" | "double" | "float" | "short" | "byte" | "boolean" | "char");
    match shape {
        ArgShape::StringLiteral => is_numeric || is_boolean || is_char,
        ArgShape::NumericLiteral => declared_type == "String" || is_boolean || is_char,
        ArgShape::BooleanLiteral => declared_type == "String" || is_numeric || is_char,
        ArgShape::NullLiteral => is_primitive,
        ArgShape::Cast(_) | ArgShape::Constructor(_) | ArgShape::Lambda | ArgShape::MethodReference | ArgShape::Other => false,
    }
}

/// AC2: true when ANY call-site argument position hits a DEFINITE
/// literal-shape mismatch against `decl`'s declared parameter type at
/// that position (positions with no declared-type evidence, or a
/// non-discriminating shape, never count).
fn candidate_has_definite_mismatch(decl: &DeclInfo, arg_shapes: &[crate::graph::extract::local_index::ArgShape]) -> bool {
    arg_shapes
        .iter()
        .enumerate()
        .any(|(i, shape)| declared_type_at(decl, i).is_some_and(|t| literal_shape_is_incompatible(shape, t)))
}

/// AC2: how many argument positions carry a `Cast`/`Constructor` shape
/// whose named type EXACTLY equals `decl`'s declared type at that
/// position -- positive, open-world-safe evidence (see
/// `literal_shape_is_incompatible`'s docs on why a NAME MISMATCH here is
/// never treated as exclusionary).
fn named_type_match_count(decl: &DeclInfo, arg_shapes: &[crate::graph::extract::local_index::ArgShape]) -> usize {
    use crate::graph::extract::local_index::ArgShape;
    arg_shapes
        .iter()
        .enumerate()
        .filter(|(i, shape)| {
            let target = match shape {
                ArgShape::Cast(t) | ArgShape::Constructor(t) => Some(t.as_str()),
                _ => None,
            };
            target.is_some() && declared_type_at(decl, *i) == target
        })
        .count()
}

/// AC2 (Story #1793, S4) Level 4 "overload discrimination": candidate-set
/// REDUCTION beyond arity, never exact resolution. Two independent
/// passes, each following the SAME "narrow only if safe" pattern as
/// `apply_arity_narrowing`/`apply_import_context_narrowing` (never empty
/// the set, never a no-op "narrow" to the same set already there):
/// (1) exclude candidates with a definite literal-shape mismatch;
/// (2) among survivors, prefer the highest cast/constructor named-type
/// match count. `OVERLOAD_ARG_TYPE_MATCH` is marked on every surviving
/// candidate that carried genuine `param_types` evidence to check against
/// -- never on a candidate with no such evidence at all.
fn apply_overload_shape_narrowing(
    candidates: &mut Vec<(DeclInfo, u16)>,
    arg_shapes: &[crate::graph::extract::local_index::ArgShape],
) {
    if arg_shapes.is_empty() {
        return;
    }
    let surviving: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (d, _))| !candidate_has_definite_mismatch(d, arg_shapes))
        .map(|(i, _)| i)
        .collect();
    if !surviving.is_empty() && surviving.len() < candidates.len() {
        *candidates = surviving.into_iter().map(|i| candidates[i].clone()).collect();
    }

    let max_score = candidates.iter().map(|(d, _)| named_type_match_count(d, arg_shapes)).max().unwrap_or(0);
    if max_score > 0 {
        let preferred: Vec<usize> = candidates
            .iter()
            .enumerate()
            .filter(|(_, (d, _))| named_type_match_count(d, arg_shapes) == max_score)
            .map(|(i, _)| i)
            .collect();
        if preferred.len() < candidates.len() {
            *candidates = preferred.into_iter().map(|i| candidates[i].clone()).collect();
        }
    }

    for (decl, bits) in candidates.iter_mut() {
        if !decl.param_types.is_empty() {
            *bits |= reasons::OVERLOAD_ARG_TYPE_MATCH;
        }
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

/// AC1 (Story #1806, S2b -- FINDING 3's missing narrowing): tags/narrows
/// candidates whose `enclosing_type` matches the resolved RECEIVER type
/// (`receiver_type`, computed by the caller via
/// `super::receiver::resolve_receiver_type` from the call's own
/// `ReceiverExpr`) or one of that type's transitive supertypes -- e.g.
/// `obj.doSomething()` where `obj`'s declared type is `Foo` narrows to
/// declarations of `doSomething` on `Foo` or an ancestor of `Foo`. `None`
/// means the receiver's type could not be resolved (unknown variable,
/// unsupported receiver shape, ambiguous chained return type) -- never a
/// guessed narrowing. Same "narrow only if safe" pattern as every other
/// pass here.
fn apply_receiver_type_narrowing(candidates: &mut Vec<(DeclInfo, u16)>, receiver_type: Option<&str>, type_index: &super::families::TypeIndex) {
    let Some(receiver_type) = receiver_type else { return };
    let mut allowed = type_index.supertypes_of(receiver_type);
    allowed.insert(receiver_type.to_string());
    let matching: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (d, _))| d.enclosing_type.as_deref().is_some_and(|t| allowed.contains(t)))
        .map(|(i, _)| i)
        .collect();
    if matching.is_empty() {
        return;
    }
    for &i in &matching {
        candidates[i].1 |= reasons::RECEIVER_TYPE_MATCH;
    }
    if matching.len() < candidates.len() {
        *candidates = matching.into_iter().map(|i| candidates[i].clone()).collect();
    }
}

/// AC3 (Story #1806, S2b): "unqualified calls resolve against the
/// enclosing class and its supertypes first". `same_class_context` is
/// `Some(enclosing_type)` ONLY when the caller (`super::resolve_site`)
/// determined this reference is an unqualified/`this`/`super` call whose
/// enclosing type is known -- `None` for a qualified call (a receiver-type
/// match is a DIFFERENT evidence path, AC1) or a reference kind AC3 does
/// not apply to (type references/constructions have no "calling class").
/// Follows the same "narrow only if safe" pattern as every other
/// narrowing pass here: never empties the set, never no-ops onto the same
/// set already there.
fn apply_same_class_or_super_narrowing(
    candidates: &mut Vec<(DeclInfo, u16)>,
    same_class_context: Option<&str>,
    type_index: &super::families::TypeIndex,
) {
    let Some(enclosing_type) = same_class_context else { return };
    let mut allowed = type_index.supertypes_of(enclosing_type);
    allowed.insert(enclosing_type.to_string());
    let matching: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (d, _))| d.enclosing_type.as_deref().is_some_and(|t| allowed.contains(t)))
        .map(|(i, _)| i)
        .collect();
    if matching.is_empty() {
        return;
    }
    for &i in &matching {
        candidates[i].1 |= reasons::SAME_CLASS_OR_SUPER;
    }
    if matching.len() < candidates.len() {
        *candidates = matching.into_iter().map(|i| candidates[i].clone()).collect();
    }
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
#[allow(clippy::too_many_arguments)]
pub(crate) fn resolve_reference(
    name: &str,
    ref_kind: u8,
    ref_file_id: u32,
    ref_scope: &FileScope,
    arg_count: Option<usize>,
    arg_shapes: &[crate::graph::extract::local_index::ArgShape],
    name_index: &RepoNameIndex,
    type_index: &super::families::TypeIndex,
    receiver_type: Option<&str>,
    same_class_context: Option<&str>,
    index_is_complete: bool,
) -> Vec<(DeclInfo, u16)> {
    let pool = name_index.lookup(name, target_kind_for_ref(ref_kind));
    if pool.is_empty() {
        return Vec::new();
    }
    if pool.len() == 1 && index_is_complete {
        return vec![(pool[0].clone(), reasons::UNIQUE_NAME_IN_REPO)];
    }

    let full_pool: Vec<DeclInfo> = pool.iter().map(|d| (*d).clone()).collect();
    let mut with_reasons: Vec<(DeclInfo, u16)> = pool
        .into_iter()
        .map(|d| (d.clone(), context_reasons(name, d, ref_file_id, ref_scope)))
        .collect();
    apply_arity_narrowing(&mut with_reasons, arg_count);
    apply_overload_shape_narrowing(&mut with_reasons, arg_shapes);
    apply_import_context_narrowing(&mut with_reasons);
    apply_receiver_type_narrowing(&mut with_reasons, receiver_type, type_index);
    apply_same_class_or_super_narrowing(&mut with_reasons, same_class_context, type_index);
    apply_inheritance_family_expansion(&mut with_reasons, ref_kind, &full_pool, type_index);
    with_reasons
}

/// AC1 (Story #1793, S4) Level 3 "inheritance families": a call resolving
/// to an interface method binds to the FAMILY of implementations, never
/// silently collapsed to one -- this EXPANDS the (possibly already
/// narrowed) candidate set, it never removes anything. Only
/// `REF_KIND_INVOCATION` is in scope (type references/constructions have
/// no "override" concept). `full_pool` is the ORIGINAL, un-narrowed
/// same-named pool: an implementor's own override may have already been
/// narrowed away by import-context evidence (the exact scenario this
/// exists to fix), so `overrides_of` must search the full pool, never the
/// already-narrowed `candidates`. Deduplicated by symbol so a candidate
/// already present (e.g. still surviving narrowing) is never added twice.
///
/// Memory-safety amendment: `overrides_of` itself hard-caps each
/// `interface_name`'s expansion at `families::MAX_FAMILY_SIZE` (see its
/// doc comment for the real 21.8GB-RSS incident this fixes). When ANY
/// interface processed for this reference was truncated, every surviving
/// `INHERITANCE_FAMILY` candidate on this reference (not just the ones
/// added by the truncated interface) is additionally marked
/// `reasons::FAMILY_TRUNCATED` -- the family this candidate set represents
/// is known-INCOMPLETE, and that must be visible on the result rather than
/// silently dropped.
fn apply_inheritance_family_expansion(
    candidates: &mut Vec<(DeclInfo, u16)>,
    ref_kind: u8,
    full_pool: &[DeclInfo],
    type_index: &super::families::TypeIndex,
) {
    if ref_kind != REF_KIND_INVOCATION {
        return;
    }
    let mut existing_symbols: std::collections::HashSet<SymbolId> =
        candidates.iter().map(|(d, _)| d.symbol).collect();
    let interface_names: Vec<String> = candidates
        .iter()
        .filter_map(|(d, _)| d.enclosing_type.as_deref())
        .filter(|t| type_index.is_interface(t))
        .map(|t| t.to_string())
        .collect();
    let mut any_truncated = false;
    for interface_name in interface_names {
        let (overrides, truncated) = type_index.overrides_of(&interface_name, full_pool);
        any_truncated |= truncated;
        for over in overrides {
            if existing_symbols.insert(over.symbol) {
                candidates.push((over.clone(), reasons::INHERITANCE_FAMILY));
            }
        }
    }
    if any_truncated {
        for (_, bits) in candidates.iter_mut() {
            if *bits & reasons::INHERITANCE_FAMILY != 0 {
                *bits |= reasons::FAMILY_TRUNCATED;
            }
        }
    }
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
            param_types: Vec::new(),
            is_varargs: false,
        }
    }

    fn package_decl(file_id: u32, name: &str) -> crate::graph::extract::local_index::Declaration {
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
        let scope = FileScope { package: None, imports: Vec::new() };

        let candidates =
            resolve_reference("neverDeclared", REF_KIND_INVOCATION, 1, &scope, None, &[], &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
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

        let candidates = resolve_reference("getId", REF_KIND_INVOCATION, 1, &scope, None, &[], &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
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

        let level0 = resolve_reference("run", REF_KIND_INVOCATION, 1, &scope, None, &[], &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
        assert_eq!(level0.len(), 2, "level 0 (no arity known) keeps both");

        let narrowed = resolve_reference("run", REF_KIND_INVOCATION, 1, &scope, Some(2), &[], &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
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
        file_a.declarations.push(varargs_method_decl("run", 10, 0, 1));
        let mut file_b = LocalIndex::new();
        file_b.declarations.push(method_decl("run", 11, 0, Some(3)));
        let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
        let scope = FileScope { package: None, imports: Vec::new() };

        let narrowed = resolve_reference("run", REF_KIND_INVOCATION, 1, &scope, Some(5), &[], &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
        assert_eq!(narrowed.len(), 1, "only the varargs candidate accepts 5 args");
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
        file_a.declarations.push(method_decl_with_types("save", 10, 0, vec!["Foo".to_string()]));
        let mut file_b = LocalIndex::new();
        file_b.declarations.push(method_decl_with_types("save", 11, 0, vec!["Bar".to_string()]));
        let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
        let scope = FileScope { package: None, imports: Vec::new() };

        let arg_shapes = [ArgShape::Cast("Foo".to_string())];
        let narrowed =
            resolve_reference("save", REF_KIND_INVOCATION, 1, &scope, Some(1), &arg_shapes, &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
        assert_eq!(narrowed.len(), 1, "the exactly-matching Foo-typed candidate must be preferred");
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
        file_a.declarations.push(method_decl_with_types("save", 10, 0, vec!["String".to_string()]));
        let mut file_b = LocalIndex::new();
        file_b.declarations.push(method_decl_with_types("save", 11, 0, vec!["int".to_string()]));
        let name_index = RepoNameIndex::build(&[file(10, "java", file_a), file(11, "java", file_b)]);
        let scope = FileScope { package: None, imports: Vec::new() };

        let arg_shapes = [ArgShape::StringLiteral];
        let narrowed =
            resolve_reference("save", REF_KIND_INVOCATION, 1, &scope, Some(1), &arg_shapes, &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
        assert_eq!(narrowed.len(), 1, "the int-typed candidate must be excluded by a String literal argument");
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

        let scope_no_import = FileScope { package: Some("pkg.ref".to_string()), imports: Vec::new() };
        let arity_only =
            resolve_reference("run", REF_KIND_INVOCATION, 1, &scope_no_import, Some(0), &[], &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
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
            resolve_reference("run", REF_KIND_INVOCATION, 1, &scope_with_import, Some(0), &[], &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
        assert_eq!(narrowed.len(), 1);
        assert_eq!(narrowed[0].0.file_id, 11);
    }

    /// AC3 (Story #1806, S2b): a bare call from `Sub` (which `extends
    /// Base`) to `helper()` must narrow to `Base.helper` -- reachable via
    /// the caller's own supertype chain -- excluding an unrelated
    /// `Other.helper` that shares only the name, with zero relation to
    /// `Sub`'s type hierarchy. Neither candidate carries any import/
    /// package/arity evidence, so `SAME_CLASS_OR_SUPER` must be the ONLY
    /// thing doing the narrowing here.
    #[test]
    fn same_class_or_super_narrows_an_unqualified_call_to_the_callers_own_type_hierarchy() {
        use crate::graph::confidence::Confidence;
        use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord, MethodOwnerRecord};

        let mut base_file = LocalIndex::new();
        base_file.declarations.push(method_decl("helper", 20, 0, Some(0)));
        base_file
            .method_owners
            .push(MethodOwnerRecord { method_symbol: make_symbol_id(20, 0), enclosing_type: "Base".to_string() });
        base_file.inheritance.push(InheritanceRecord {
            kind: InheritanceKind::Extends,
            subtype_name: "Sub".to_string(),
            supertype_name: "Base".to_string(),
            line: 1,
        });

        let mut other_file = LocalIndex::new();
        other_file.declarations.push(method_decl("helper", 21, 0, Some(0)));
        other_file
            .method_owners
            .push(MethodOwnerRecord { method_symbol: make_symbol_id(21, 0), enclosing_type: "Other".to_string() });

        let files = vec![file(20, "java", base_file), file(21, "java", other_file)];
        let name_index = RepoNameIndex::build(&files);
        let type_index = super::super::families::TypeIndex::build(&files);
        let scope = FileScope { package: None, imports: Vec::new() };

        let candidates = resolve_reference(
            "helper",
            REF_KIND_INVOCATION,
            1,
            &scope,
            Some(0),
            &[],
            &name_index,
            &type_index,
            None,
            Some("Sub"),
            true,
        );
        assert_eq!(candidates.len(), 1, "Other.helper must be excluded -- it has no relation to Sub's hierarchy");
        assert_eq!(candidates[0].0.file_id, 20);
        assert_ne!(candidates[0].1 & reasons::SAME_CLASS_OR_SUPER, 0);
        assert_eq!(Confidence::derive(candidates[0].1), Confidence::SameClassOrSuper);
    }

    /// AC1 (Story #1806, S2b -- FINDING 3's missing narrowing): a
    /// qualified call's receiver was resolved (by the caller, via
    /// `super::receiver::resolve_receiver_type`) to declared type `"Foo"`,
    /// which `extends Base` -- narrows to `Base.doSomething` (declared on
    /// a SUPERTYPE of the receiver's declared type, not just an
    /// exact-type match), excluding an unrelated `Other.doSomething` that
    /// shares only the name.
    #[test]
    fn receiver_type_match_narrows_a_qualified_call_to_the_receivers_declared_type() {
        use crate::graph::confidence::Confidence;
        use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord, MethodOwnerRecord};

        let mut base_file = LocalIndex::new();
        base_file.declarations.push(method_decl("doSomething", 30, 0, Some(0)));
        base_file
            .method_owners
            .push(MethodOwnerRecord { method_symbol: make_symbol_id(30, 0), enclosing_type: "Base".to_string() });
        base_file.inheritance.push(InheritanceRecord {
            kind: InheritanceKind::Extends,
            subtype_name: "Foo".to_string(),
            supertype_name: "Base".to_string(),
            line: 1,
        });

        let mut other_file = LocalIndex::new();
        other_file.declarations.push(method_decl("doSomething", 31, 0, Some(0)));
        other_file
            .method_owners
            .push(MethodOwnerRecord { method_symbol: make_symbol_id(31, 0), enclosing_type: "Other".to_string() });

        let files = vec![file(30, "java", base_file), file(31, "java", other_file)];
        let name_index = RepoNameIndex::build(&files);
        let type_index = super::super::families::TypeIndex::build(&files);
        let scope = FileScope { package: None, imports: Vec::new() };

        let candidates = resolve_reference(
            "doSomething",
            REF_KIND_INVOCATION,
            1,
            &scope,
            Some(0),
            &[],
            &name_index,
            &type_index,
            Some("Foo"),
            None,
            true,
        );
        assert_eq!(candidates.len(), 1, "Other.doSomething must be excluded -- it has no relation to Foo's hierarchy");
        assert_eq!(candidates[0].0.file_id, 30, "Base.doSomething must be reachable via Foo's supertype chain");
        assert_ne!(candidates[0].1 & reasons::RECEIVER_TYPE_MATCH, 0);
        assert_eq!(Confidence::derive(candidates[0].1), Confidence::ReceiverType);
    }

    /// AC1 (Story #1793, S4): THE central discriminating case named in
    /// the story -- import-context narrowing alone would collapse a call
    /// resolving to an interface method down to just that ONE
    /// declaration (the interface's own package matches the caller's;
    /// the real implementor lives in an unrelated package with zero
    /// import evidence). Family expansion must add the implementor's
    /// override BACK, marked `INHERITANCE_FAMILY` and `Confidence::High`
    /// -- never leaving the call silently collapsed to the interface
    /// declaration alone.
    #[test]
    fn interface_method_expands_to_its_family_after_narrowing_would_have_collapsed_it_to_one() {
        use crate::graph::confidence::Confidence;
        use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord, MethodOwnerRecord};
        use crate::graph::identity::make_symbol_id;

        let mut interface_file = LocalIndex::new();
        interface_file.declarations.push(package_decl(10, "pkg.a"));
        interface_file.declarations.push(method_decl("save", 10, 1, Some(0)));
        interface_file.interface_names.push("Repo".to_string());
        interface_file
            .method_owners
            .push(MethodOwnerRecord { method_symbol: make_symbol_id(10, 1), enclosing_type: "Repo".to_string() });

        let mut impl_file = LocalIndex::new();
        impl_file.declarations.push(package_decl(11, "pkg.b"));
        impl_file.declarations.push(method_decl("save", 11, 1, Some(0)));
        impl_file
            .method_owners
            .push(MethodOwnerRecord { method_symbol: make_symbol_id(11, 1), enclosing_type: "Impl".to_string() });
        impl_file.inheritance.push(InheritanceRecord {
            kind: InheritanceKind::Implements,
            subtype_name: "Impl".to_string(),
            supertype_name: "Repo".to_string(),
            line: 1,
        });

        let files = vec![file(10, "java", interface_file), file(11, "java", impl_file)];
        let name_index = RepoNameIndex::build(&files);
        let type_index = super::super::families::TypeIndex::build(&files);
        let scope = FileScope { package: Some("pkg.a".to_string()), imports: Vec::new() };

        let candidates =
            resolve_reference("save", REF_KIND_INVOCATION, 1, &scope, Some(0), &[], &name_index, &type_index, None, None, true);
        assert_eq!(
            candidates.len(),
            2,
            "the family (interface + its real implementor) must both be present, never collapsed to one"
        );

        let impl_candidate = candidates.iter().find(|(d, _)| d.file_id == 11).expect("Impl.save must be present");
        assert_ne!(impl_candidate.1 & reasons::INHERITANCE_FAMILY, 0);
        assert_eq!(Confidence::derive(impl_candidate.1), Confidence::High);
    }

    /// Shared fixture helper: a file declaring interface(s) `interface_names`
    /// plus one `save` method owned by `enclosing_type`, in `package`. Used
    /// by the `MAX_FAMILY_SIZE` cap and cyclic-hierarchy tests below.
    fn interface_decl_file(file_id: u32, package: &str, interface_names: &[&str], enclosing_type: &str) -> LocalIndex {
        use crate::graph::extract::local_index::MethodOwnerRecord;
        use crate::graph::identity::make_symbol_id;
        let mut f = LocalIndex::new();
        f.declarations.push(package_decl(file_id, package));
        f.declarations.push(method_decl("save", file_id, 1, Some(0)));
        for name in interface_names {
            f.interface_names.push(name.to_string());
        }
        f.method_owners
            .push(MethodOwnerRecord { method_symbol: make_symbol_id(file_id, 1), enclosing_type: enclosing_type.to_string() });
        f
    }

    /// Shared fixture helper: a file declaring type `type_name` (which
    /// `implements supertype`) plus its own `save` override, in `package`.
    fn implementor_file(file_id: u32, package: &str, type_name: &str, supertype: &str) -> LocalIndex {
        use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord, MethodOwnerRecord};
        use crate::graph::identity::make_symbol_id;
        let mut f = LocalIndex::new();
        f.declarations.push(package_decl(file_id, package));
        f.declarations.push(method_decl("save", file_id, 1, Some(0)));
        f.method_owners
            .push(MethodOwnerRecord { method_symbol: make_symbol_id(file_id, 1), enclosing_type: type_name.to_string() });
        f.inheritance.push(InheritanceRecord {
            kind: InheritanceKind::Implements,
            subtype_name: type_name.to_string(),
            supertype_name: supertype.to_string(),
            line: 1,
        });
        f
    }

    /// Shared fixture helper: resolves `"save"` (`REF_KIND_INVOCATION`,
    /// `arg_count = Some(0)`) from a caller scoped to `"pkg.a"` -- the
    /// package every interface-owning fixture file above declares itself
    /// in, so import-context narrowing alone always collapses to the
    /// interface's own candidate, forcing family expansion to do the real
    /// work (mirrors `interface_method_expands_to_its_family_...`'s own
    /// fixture shape).
    fn resolve_save_against(files: &[FileForBind]) -> Vec<(DeclInfo, u16)> {
        let name_index = RepoNameIndex::build(files);
        let type_index = super::super::families::TypeIndex::build(files);
        let scope = FileScope { package: Some("pkg.a".to_string()), imports: Vec::new() };
        resolve_reference("save", REF_KIND_INVOCATION, 1, &scope, Some(0), &[], &name_index, &type_index, None, None, true)
    }

    /// Memory-safety amendment (real 21.8GB-RSS incident on Elasticsearch,
    /// killed before it exhausted the host): family expansion through the
    /// FULL `resolve_reference` pipeline must cap the family at
    /// `MAX_FAMILY_SIZE` and mark every surviving family candidate
    /// `reasons::FAMILY_TRUNCATED` -- never silently return a partial
    /// family indistinguishable from a genuinely small one.
    #[test]
    fn family_expansion_caps_at_max_family_size_and_marks_family_truncated() {
        use super::super::families::MAX_FAMILY_SIZE;
        const INTERFACE_FILE_ID: u32 = 10;
        const IMPL_FILE_ID_BASE: u32 = 100;
        const IMPLEMENTOR_COUNT: usize = MAX_FAMILY_SIZE + 5;

        let mut files =
            vec![file(INTERFACE_FILE_ID, "java", interface_decl_file(INTERFACE_FILE_ID, "pkg.a", &["Repo"], "Repo"))];
        for i in 0..IMPLEMENTOR_COUNT {
            let file_id = IMPL_FILE_ID_BASE + i as u32;
            let impl_type_name = format!("Impl{i}");
            let impl_file = implementor_file(file_id, &format!("pkg.impl{i}"), &impl_type_name, "Repo");
            files.push(file(file_id, "java", impl_file));
        }

        let candidates = resolve_save_against(&files);

        assert_eq!(
            candidates.len(),
            MAX_FAMILY_SIZE + 1,
            "the interface's own candidate plus a family capped at MAX_FAMILY_SIZE"
        );
        let family_candidates: Vec<_> =
            candidates.iter().filter(|(_, bits)| bits & reasons::INHERITANCE_FAMILY != 0).collect();
        assert_eq!(family_candidates.len(), MAX_FAMILY_SIZE);
        assert!(
            family_candidates.iter().all(|(_, bits)| bits & reasons::FAMILY_TRUNCATED != 0),
            "every surviving family candidate must be marked FAMILY_TRUNCATED once the cap is hit"
        );
    }

    /// A class hierarchy can contain cycles through interfaces (malformed
    /// or adversarial extraction, never valid real Java) -- family
    /// expansion through the FULL `resolve_reference` pipeline must still
    /// terminate, not merely the lower-level `TypeIndex::implementors_of`
    /// BFS in isolation. `I` and `J` extend each other (a 2-cycle); `Impl`
    /// genuinely implements `I`. This test itself fails to return (times
    /// out the test run) rather than failing an assertion if
    /// `apply_inheritance_family_expansion` loops forever on the cycle.
    #[test]
    fn family_expansion_terminates_on_a_cyclic_interface_hierarchy_through_resolve_reference() {
        use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord};
        const INTERFACE_FILE_ID: u32 = 10;
        const IMPL_FILE_ID: u32 = 11;

        let mut interface_file = interface_decl_file(INTERFACE_FILE_ID, "pkg.a", &["I", "J"], "I");
        // The adversarial 2-cycle: I extends J, J extends I.
        interface_file.inheritance.push(InheritanceRecord {
            kind: InheritanceKind::Extends,
            subtype_name: "I".to_string(),
            supertype_name: "J".to_string(),
            line: 1,
        });
        interface_file.inheritance.push(InheritanceRecord {
            kind: InheritanceKind::Extends,
            subtype_name: "J".to_string(),
            supertype_name: "I".to_string(),
            line: 1,
        });

        let files = vec![
            file(INTERFACE_FILE_ID, "java", interface_file),
            file(IMPL_FILE_ID, "java", implementor_file(IMPL_FILE_ID, "pkg.b", "Impl", "I")),
        ];

        let candidates = resolve_save_against(&files);

        assert_eq!(
            candidates.len(),
            2,
            "must terminate and return exactly the interface's own candidate plus Impl.save -- \
             a cyclic hierarchy must never hang or fabricate extra candidates"
        );
        assert!(candidates.iter().any(|(d, _)| d.file_id == IMPL_FILE_ID));
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
            resolve_reference("uniqueMethod", REF_KIND_INVOCATION, 1, &scope, None, &[], &name_index, &super::super::families::TypeIndex::build(&[]), None, None, true);
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
            resolve_reference("uniqueMethod", REF_KIND_INVOCATION, 1, &scope, None, &[], &name_index, &super::super::families::TypeIndex::build(&[]), None, None, false);
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
