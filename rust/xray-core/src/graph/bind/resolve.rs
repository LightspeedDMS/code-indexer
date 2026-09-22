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
    apply_super_class_narrowing, apply_type_qualifier_narrowing, param_count_matches_arity,
};
use super::scope::FileScope;
use super::REF_KIND_INVOCATION;
use crate::graph::extract::local_index::{DeclarationKind, ImportKind, LocalIndex};
use crate::graph::identity::{make_symbol_id, SymbolId};
use crate::graph::reasons;

/// Which `DeclarationKind` a reference resolves against: methods for
/// calls, types for type references and constructions. `pub(super)`
/// (#1898 round 4, epic #1906): `bind/mod.rs`'s `resolve_all_references`
/// reuses this exact mapping (Rule 4, anti-duplication) to re-derive a
/// reference's pre-narrowing bare-name pool for the "narrowed to zero"
/// observability counter.
pub(super) fn target_kind_for_ref(ref_kind: u8) -> DeclarationKind {
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
/// `STATIC_IMPORT` (the `ImportKind::Static` arm below) is deliberately
/// coarser than the other two: a SINGLE-MEMBER static import path's
/// second-to-last segment names the DECLARING CLASS (e.g. `java.lang.Math`
/// in `import static java.lang.Math.max;`), not a package, and `DeclInfo`
/// carries a method's own package but not its enclosing class -- there is
/// no substrate here to compare against for THAT kind. So that reason is
/// name-only: any single-member static import whose last segment matches
/// `name` counts, regardless of `decl`'s package. This is a documented
/// scope limitation, not a bug.
///
/// `ImportKind::StaticWildcard` (issue #1915) does NOT share that
/// limitation and gets a PRECISE check instead: its `path` (e.g.
/// `"pkg.Util"` for `import static pkg.Util.*;`) is the declaring class's
/// own dotted path, and `DeclInfo` carries `enclosing_type`, so both the
/// class (`import.path`'s last segment) and the package (`import.path`'s
/// prefix) can be compared against `decl` directly -- a real fix, not
/// merely a reclassification, since before issue #1915 a static-on-demand
/// import was misclassified as `Wildcard` (whose own check compares
/// `decl.package` against the WHOLE `import.path`, i.e. `"pkg"` against
/// `"pkg.Util"`, which never matches) and so contributed ZERO reason bits
/// at all -- not even the coarse name-only signal `Static` gets.
///
/// #1915 follow-up (found during dual review, real repro): the FIRST
/// `StaticWildcard` check alone still misses a static-on-demand import of
/// a member of a NESTED declaring class (`import static pkg.Outer.Util.*;`)
/// -- the import path's prefix (`"pkg.Outer"`) mixes the real package with
/// a NESTED-CLASS segment, which `decl.package` (always the true Java
/// package, `"pkg"`, never a package+class compound) can never equal
/// directly, so the real target earned zero bits while a same-package
/// decoy could earn `SAME_PACKAGE` and win `apply_import_context_
/// narrowing`'s hard-narrow outright -- the exact annihilation shape
/// #1915 was filed for. The second condition below closes the ONE-LEVEL
/// nesting case using the SAME unambiguous-only `TypeIndex::top_level_of`
/// substrate `apply_private_visibility_filter` (D2) already trusts for
/// this exact reason: when the import prefix equals `{decl.package}.
/// {top_level_of(decl.enclosing_type)}`, the import is naming a member of
/// a type nested exactly one level under that top-level type. **This does
/// NOT cover two-or-more levels of nesting** (`pkg.A.B.Util`): `top_level_
/// of` only ever returns the ROOT ancestor, never an intermediate one, and
/// this binder deliberately carries no qualified-name/lexical-chain
/// substrate that could walk the remaining levels (see `docs/
/// xray-architecture.md`'s "What a future round 8 would need" -- the same
/// `(enclosing_method, name)`-shaped scope-key gap, here at the type-nesting
/// level instead of the local-binding level). For that uncovered depth,
/// this function correctly contributes NO bit at all -- under-tagging,
/// never a fabricated one, is the safe direction this whole contract
/// leans toward everywhere else.
fn import_reasons(
    name: &str,
    decl: &DeclInfo,
    ref_scope: &FileScope,
    type_index: &super::families::TypeIndex,
) -> u16 {
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
            ImportKind::StaticWildcard => {
                if decl.enclosing_type.as_deref() != import.path.rsplit('.').next() {
                    continue;
                }
                let Some(prefix) = package_prefix(&import.path) else {
                    continue;
                };
                let matches_top_level_class = decl.package.as_deref() == Some(prefix);
                let matches_one_level_nested_class = decl
                    .package
                    .as_deref()
                    .zip(
                        decl.enclosing_type
                            .as_deref()
                            .and_then(|owner| type_index.top_level_of(owner)),
                    )
                    .is_some_and(|(pkg, top_level)| prefix == format!("{pkg}.{top_level}"));
                if matches_top_level_class || matches_one_level_nested_class {
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
fn context_reasons(
    name: &str,
    decl: &DeclInfo,
    ref_file_id: u32,
    ref_scope: &FileScope,
    type_index: &super::families::TypeIndex,
) -> u16 {
    let mut bits = 0u16;
    if decl.file_id == ref_file_id {
        bits |= reasons::SAME_FILE;
    }
    if decl.package.is_some() && decl.package == ref_scope.package {
        bits |= reasons::SAME_PACKAGE;
    }
    bits |= import_reasons(name, decl, ref_scope, type_index);
    bits
}

/// D3/AC4 Level 5: the unique-name shortcut. `Some(result)` when it
/// applies at all (a genuine `super` call, an ambiguous pool, an
/// incomplete index, or an EVIDENCE MISMATCH all make it inapplicable,
/// returning `None` so the caller falls through to the full narrowing
/// pipeline instead); within `Some`, an empty `Vec` means the sole
/// candidate was excluded by D2 (private, known cross-top-level owner),
/// otherwise the singleton is returned tagged `UNIQUE_NAME_IN_REPO`.
///
/// D3: a genuine `super` call must never take this shortcut, even when
/// the name happens to be globally unique -- the sole candidate could
/// easily be the enclosing type's OWN declaration (the exact self-loop
/// the D3 fix exists to prevent), which `apply_super_class_narrowing`
/// (narrowing.rs) is what actually verifies against the ancestor chain.
///
/// Bug #1898 (P1-3 of the code review, epic #1906): this shortcut runs
/// BEFORE arity/receiver-type narrowing and, pre-fix, consulted neither --
/// a call whose real target is EXTERNAL to the repo (e.g. `connection.
/// close()`, 0 args) but whose bare name happens to be globally unique in
/// the repo (`Something.close(int code)`, 1 param, on some unrelated
/// class) was admitted unconditionally as `UNIQUE_NAME_IN_REPO` /
/// `Confidence::Exact`, the exact AC2 repro #1898 names. Now gated: when
/// `arg_count` is known and the sole candidate's OWN declared arity is
/// known and does not accept it (`param_count_matches_arity`), or when
/// `receiver_type` is known (and its supertype evidence is COMPLETE) and
/// the sole candidate's `enclosing_type` is not in
/// `{receiver_type} U supertypes_of(receiver_type)`, the shortcut is
/// inapplicable and falls through -- the full pipeline's own hard arity/
/// receiver-type narrowing then correctly empties the set. Missing
/// evidence (`param_count: None`, an unresolved `receiver_type`, or
/// receiver-type evidence recorded incomplete) never invalidates the
/// shortcut on its own -- mirrors this same file's/narrowing.rs's
/// "missing evidence retains, never excludes" doctrine (P2-1).
///
/// Round 4 (#1898 epic #1906): `receiver_type` here is ONLY ever the
/// POSITIVE-tier value -- `resolve_reference` (below) passes `None`
/// instead whenever the caller's evidence is Advisory, so this shortcut
/// never invalidates itself on a coincidentally-wrong GUESS the way the
/// full pipeline's hard filter used to (see `super::receiver::
/// ReceiverEvidence` and `apply_receiver_type_narrowing`'s own doc
/// comments for the full rationale).
///
/// #1898 scope split (epic #1906, round-4 review): `apply_receiver_type_
/// narrowing` itself is now TAG-ONLY and never deletes a candidate --
/// this shortcut's own receiver-type gate is UNCHANGED and deliberately
/// kept, because it governs a different decision. Declining the shortcut
/// here is an ADMISSION choice (whether to bypass the rest of the
/// pipeline for a singleton pool), not a DELETION of an already-built
/// candidate set -- falling through simply hands the singleton to the
/// (now tag-only) full pipeline below, which keeps it regardless. A
/// positive, definitional mismatch (the sole candidate's enclosing type
/// provably outside `{receiver_type} U supertypes_of(receiver_type)`)
/// remains a sound reason to skip a SHORTCUT that would otherwise claim
/// `UNIQUE_NAME_IN_REPO`/`Confidence::Exact` for it.
///
/// Bug #1912 (purely additive, no candidate-selection change): when the
/// closure check above CONFIRMS the match instead of rejecting it, the
/// accepted singleton is now also tagged `RECEIVER_TYPE_MATCH`, not just
/// `UNIQUE_NAME_IN_REPO` -- the closure was already computed to decide
/// ACCEPT-vs-DECLINE and used to be discarded on the accept branch,
/// leaving a qualified unique-name hop with confirmed receiver evidence
/// byte-identical to one with none. Unqualified calls (`receiver_type:
/// None`) and calls whose receiver evidence is only INCOMPLETE (the
/// `has_incomplete_supertype_evidence` branch, where the closure is never
/// consulted at all) still tag `UNIQUE_NAME_IN_REPO` alone -- there is no
/// receiver corroboration to report in either case.
///
/// F6 (#1873/#1875 rework, LOW): reuses `apply_private_visibility_filter`
/// (D2) instead of a hand-rolled copy of its exact "private + known
/// cross-top-level owner" exclusion rule -- a fresh 0-bits singleton is
/// filtered in place, and only its survival is checked.
#[allow(clippy::too_many_arguments)]
fn try_unique_name_shortcut(
    pool: &[&DeclInfo],
    arg_count: Option<usize>,
    receiver_type: Option<&str>,
    super_class_context: Option<&str>,
    caller_top_level: Option<&str>,
    type_index: &super::families::TypeIndex,
    index_is_complete: bool,
) -> Option<Vec<(DeclInfo, u16)>> {
    if super_class_context.is_some() || pool.len() != 1 || !index_is_complete {
        return None;
    }
    let only = pool[0];
    if let Some(arg_count) = arg_count {
        if only.param_count.is_some() && !param_count_matches_arity(only, arg_count) {
            return None;
        }
    }
    // Bug #1912: the closure below already exists solely to decide
    // whether to DECLINE the shortcut on a definitional receiver-type
    // mismatch -- `receiver_type_confirmed` captures its answer on the
    // ACCEPT path too, purely additive to that same decision, so the
    // caller can tag `RECEIVER_TYPE_MATCH` alongside `UNIQUE_NAME_IN_REPO`
    // instead of discarding a check it already performed. Only the
    // COMPLETE-evidence branch counts as confirmation: when the
    // receiver's own supertype evidence is incomplete, the closure below
    // is never even consulted (the shortcut still accepts, per the
    // "missing evidence retains, never excludes" doctrine), so there is
    // no real corroboration to tag.
    let mut receiver_type_confirmed = false;
    if let Some(receiver_type) = receiver_type {
        if !type_index.has_incomplete_supertype_evidence(receiver_type) {
            let mut allowed = type_index.supertypes_of(receiver_type);
            allowed.insert(receiver_type.to_string());
            if !only
                .enclosing_type
                .as_deref()
                .is_some_and(|t| allowed.contains(t))
            {
                return None;
            }
            receiver_type_confirmed = true;
        }
    }
    let mut singleton = vec![(only.clone(), 0u16)];
    apply_private_visibility_filter(&mut singleton, caller_top_level, type_index);
    if singleton.is_empty() {
        return Some(Vec::new());
    }
    let mut bits = reasons::UNIQUE_NAME_IN_REPO;
    if receiver_type_confirmed {
        bits |= reasons::RECEIVER_TYPE_MATCH;
    }
    Some(vec![(only.clone(), bits)])
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
    arg_known_types: &[Option<String>],
    name_index: &RepoNameIndex,
    type_index: &super::families::TypeIndex,
    receiver_type: Option<&str>,
    receiver_type_is_positive: bool,
    same_class_context: Option<&str>,
    super_class_context: Option<&str>,
    caller_top_level: Option<&str>,
    index_is_complete: bool,
    receiver_is_type_qualifier: bool,
) -> Vec<(DeclInfo, u16)> {
    let pool = name_index.lookup(name, target_kind_for_ref(ref_kind));
    if pool.is_empty() {
        return Vec::new();
    }
    // Round 4 (#1898 epic #1906): the shortcut only ever sees POSITIVE
    // receiver-type evidence -- an Advisory guess must never invalidate
    // it (see this function's own doc comment above `try_unique_name_
    // shortcut` for why).
    let shortcut_receiver_type = if receiver_type_is_positive {
        receiver_type
    } else {
        None
    };
    if let Some(result) = try_unique_name_shortcut(
        &pool,
        arg_count,
        shortcut_receiver_type,
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
        .map(|d| (d.clone(), context_reasons(name, d, ref_file_id, ref_scope, type_index)))
        .collect();
    apply_private_visibility_filter(&mut with_reasons, caller_top_level, type_index);
    apply_arity_narrowing(&mut with_reasons, arg_count);
    apply_overload_shape_narrowing(&mut with_reasons, arg_shapes, arg_known_types, type_index);
    apply_receiver_type_narrowing(&mut with_reasons, receiver_type, type_index);
    // #1922: MUST run immediately after `apply_receiver_type_narrowing`
    // (consumes the `RECEIVER_TYPE_MATCH` tag it just set) and before
    // `apply_import_context_narrowing` (which otherwise wrongly discards
    // a qualifier-confirmed candidate that happens to carry no same-file/
    // same-package/import evidence, letting an unrelated same-file/
    // same-package decoy -- often the caller's own self-loop -- win
    // instead; see a static-facade shape, `class A { static R m(X x) {
    // return B.m(x); } }` alongside `class B { static R m(X x) {...} }`,
    // called from inside `A` itself).
    apply_type_qualifier_narrowing(&mut with_reasons, receiver_is_type_qualifier, receiver_type);
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

#[cfg(test)]
#[path = "resolve_tests_type_qualifier.rs"]
mod tests_type_qualifier;

#[cfg(test)]
#[path = "resolve_tests_unique_shortcut.rs"]
mod tests_unique_shortcut;
