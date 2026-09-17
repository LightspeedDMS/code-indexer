//! AC4 Levels 0/1/2/5 reference resolution (Story #1787, S2). Called for
//! real by `super::resolve_site` (`bind()`'s per-reference-site helper) for
//! every invocation/type-reference/construction site in the repository.
//!
//! F5 (#1873/#1875 rework): the individual narrowing/filtering/expansion
//! passes (AC4 Levels 1/2/4, AC1/AC3 receiver-type/same-class/super-class
//! narrowing, D2 visibility, AC1 inheritance-family expansion) live in
//! `narrowing.rs` -- this file keeps only the orchestrator
//! (`resolve_reference`) and the small reasons-bit/enclosing-symbol
//! helpers specific to it, to stay under the project's line-count limit.

use super::name_index::{DeclInfo, RepoNameIndex};
use super::narrowing::{
    apply_arity_narrowing, apply_import_context_narrowing, apply_inheritance_family_expansion,
    apply_overload_shape_narrowing, apply_private_visibility_filter,
    apply_receiver_type_narrowing, apply_same_class_or_super_narrowing,
    apply_super_class_narrowing,
};
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

/// D3/AC4 Level 5: the unique-name shortcut. `Some(result)` when it
/// applies at all (a genuine `super` call, an ambiguous pool, or an
/// incomplete index all make it inapplicable, returning `None` so the
/// caller falls through to the full narrowing pipeline instead); within
/// `Some`, an empty `Vec` means the sole candidate was excluded by D2
/// (private, known cross-top-level owner), otherwise the singleton is
/// returned tagged `UNIQUE_NAME_IN_REPO`.
///
/// D3: a genuine `super` call must never take this shortcut, even when
/// the name happens to be globally unique -- the sole candidate could
/// easily be the enclosing type's OWN declaration (the exact self-loop
/// the D3 fix exists to prevent), which `apply_super_class_narrowing`
/// (narrowing.rs) is what actually verifies against the ancestor chain.
///
/// F6 (#1873/#1875 rework, LOW): reuses `apply_private_visibility_filter`
/// (D2) instead of a hand-rolled copy of its exact "private + known
/// cross-top-level owner" exclusion rule -- a fresh 0-bits singleton is
/// filtered in place, and only its survival is checked.
fn try_unique_name_shortcut(
    pool: &[&DeclInfo],
    super_class_context: Option<&str>,
    caller_top_level: Option<&str>,
    type_index: &super::families::TypeIndex,
    index_is_complete: bool,
) -> Option<Vec<(DeclInfo, u16)>> {
    if super_class_context.is_some() || pool.len() != 1 || !index_is_complete {
        return None;
    }
    let only = pool[0];
    let mut singleton = vec![(only.clone(), 0u16)];
    apply_private_visibility_filter(&mut singleton, caller_top_level, type_index);
    if singleton.is_empty() {
        return Some(Vec::new());
    }
    Some(vec![(only.clone(), reasons::UNIQUE_NAME_IN_REPO)])
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
///
/// F3 (#1873/#1875 rework, MEDIUM): D2 runs FIRST in the pipeline below,
/// before arity/overload-shape narrowing -- a cross-top-level private
/// candidate must never be allowed to win overload-shape's named-type
/// preference (knocking the real, accessible target out of the set) only
/// to be removed itself afterwards, leaving BOTH candidates unreferenced.
/// Reviewer-validated experiment (`r3_EXPERIMENT_filter_first_probes.log`):
/// moving this call here turned P4 from a false double-dead into the
/// correct single live target with the rest of the corpus unchanged.
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
    super_class_context: Option<&str>,
    caller_top_level: Option<&str>,
    index_is_complete: bool,
) -> Vec<(DeclInfo, u16)> {
    let pool = name_index.lookup(name, target_kind_for_ref(ref_kind));
    if pool.is_empty() {
        return Vec::new();
    }
    if let Some(result) = try_unique_name_shortcut(
        &pool,
        super_class_context,
        caller_top_level,
        type_index,
        index_is_complete,
    ) {
        return result;
    }

    let full_pool: Vec<DeclInfo> = pool.iter().map(|d| (*d).clone()).collect();
    let mut with_reasons: Vec<(DeclInfo, u16)> = pool
        .into_iter()
        .map(|d| (d.clone(), context_reasons(name, d, ref_file_id, ref_scope)))
        .collect();
    apply_private_visibility_filter(&mut with_reasons, caller_top_level, type_index);
    apply_arity_narrowing(&mut with_reasons, arg_count);
    apply_overload_shape_narrowing(&mut with_reasons, arg_shapes);
    apply_receiver_type_narrowing(&mut with_reasons, receiver_type, type_index);
    apply_same_class_or_super_narrowing(&mut with_reasons, same_class_context, type_index);
    apply_super_class_narrowing(&mut with_reasons, super_class_context, type_index);
    apply_import_context_narrowing(&mut with_reasons);
    apply_inheritance_family_expansion(&mut with_reasons, ref_kind, &full_pool, type_index);
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
#[path = "resolve_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "resolve_tests_family.rs"]
mod tests_family;
