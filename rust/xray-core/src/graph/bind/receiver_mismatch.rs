//! `RECEIVER_TYPE_MISMATCH` tagging, split out of `narrowing.rs` (Messi
//! Rule 6, anti-file-bloat -- that file was already at 978 lines before
//! this logic grew more complex).
//!
//! **Binding design (#1924/#1925)**: no deletion of candidates on
//! receiver-type evidence, ever -- see `narrowing::apply_receiver_type_
//! narrowing`'s own doc comment for why deletion is unsound on this
//! binder's bare-name, bare-scope substrate. `RECEIVER_TYPE_MISMATCH` is a
//! TAG-ONLY exception, safe only when ALL FOUR of the following hold:
//!
//! **(a) the receiver's declared-type simple name is not shadowed.** Not a
//! repo-declared type anywhere in the repo, not named by an explicit
//! import (an ORDINARY single-type import, or a single-member STATIC
//! import -- either legally brings an external type into scope under a
//! bare name that would otherwise mean `java.lang.*`), and not a known
//! generic type parameter.
//!
//! **(b) the receiver binding is a genuine method/constructor PARAMETER.**
//! `FileTypedNames.locals` is keyed by `(enclosing_method, name)` -- it
//! cannot distinguish a formal parameter from an ordinary local variable
//! declared later in the SAME method (#1919, "locals are keyed per
//! METHOD, not per BLOCK"). A field read outside any block can resolve to
//! an UNRELATED, block-scoped local's declared type under that same
//! lookup key.
//!
//! **(c) the parameter's declared type was written UNQUALIFIED, or
//! qualified exactly as `java.lang.*`.** This binder's type model only
//! ever records a declared type's BARE simple name -- a parameter written
//! `com.lib.String s` and one written `String s` both record `"String"`,
//! indistinguishable by name alone. An explicit qualifier naming any
//! OTHER package means the real type is NOT provably `java.lang.String`
//! (or any other JDK closed-world type): its own closed-world status is
//! unknown, so a repo type may legally extend it.
//!
//! **(d) EVERY type declared in the call site's own FILE has fully
//! repo-resolved supertype evidence.** A type extending an external/
//! unindexed supertype (`class Mid extends com.lib.Base`) may have a
//! NESTED type privately shadowing a closed-world name from further
//! outside than this binder can see into -- and that supertype can sit
//! on ANY nesting level, not just the call's immediate enclosing type or
//! the file's top-level type (an earlier, narrower version of this
//! condition checked only those two levels and missed an INTERMEDIATE
//! one). Checked once per file, never per call site.
//!
//! Condition (d) is itself judged by SIMPLE NAME only (`TypeIndex::
//! is_known_type_name`, like every other bare-name lookup in this
//! binder): a repo-declared type sharing the SAME bare name as the real
//! external supertype makes that supertype look "resolved" even though
//! it is not actually the same type -- a known, accepted imprecision
//! shared with every other bare-name substrate in this binder, never a
//! soundness gap this condition alone claims to close.
//!
//! All four conditions are independently necessary: each one alone still
//! leaves a real, javac-valid edge tagged mismatched under some fixture
//! shape.

use super::name_index::DeclInfo;
use crate::graph::extract::local_index::{ImportKind, ImportRecord, TypeNestingRecord};
use crate::graph::reasons;

/// The BASE ELEMENT name of `type_name` with every trailing `[]` array
/// dimension stripped -- `"String[][]"` -> `"String"`, `"Target"` ->
/// `"Target"` (a non-array name is its own base). Bounded loop (Rule 14):
/// each iteration strips two characters, so it terminates in at most
/// `type_name.len() / 2` steps. Used so condition (a)'s shadowing checks
/// below inspect the ELEMENT type a repo/import/type-parameter might
/// actually declare, never the synthetic `"Foo[]"` string those substrates
/// never contain a literal entry for.
fn base_element_name(type_name: &str) -> &str {
    let mut base = type_name;
    while let Some(stripped) = base.strip_suffix("[]") {
        base = stripped;
    }
    base
}

/// Condition (a): is `type_name` a CLOSED-WORLD receiver type (`narrowing::
/// is_closed_world_value_type` -- String, a primitive, or a boxed wrapper --
/// OR any array type `T[]`, at any element type/depth: arrays are never
/// user-subclassable regardless of their element type) whose BASE ELEMENT
/// name this repo does NOT also plausibly mean by the same bare simple
/// name -- not a repo-declared type ANYWHERE in the repo (`TypeIndex::
/// is_known_type_name`, which already covers every file's own declared
/// types, including nested and private ones, repo-wide -- a name declared
/// in THIS file is trivially a subset of "declared in the repo"), not
/// shadowed by an explicit import (ordinary OR single-member static), and
/// not a known generic type parameter (`TypeIndex::is_known_type_
/// parameter_name` -- normally already screened out upstream by
/// `receiver::is_pseudo_type`, re-checked here as defense in depth).
fn is_provably_closed_world_and_unshadowed(
    type_name: &str,
    type_index: &super::families::TypeIndex,
    imports: &[ImportRecord],
) -> bool {
    let is_closed_world = type_name.ends_with("[]") || super::narrowing::is_closed_world_value_type(type_name);
    if !is_closed_world {
        return false;
    }
    let base = base_element_name(type_name);
    if type_index.is_known_type_name(base) {
        return false;
    }
    if type_index.is_known_type_parameter_name(base) {
        return false;
    }
    if is_shadowed_by_an_explicit_import(base, imports) {
        return false;
    }
    true
}

/// True when `name` is explicitly named by an ORDINARY single-type import
/// OR a single-member STATIC import anywhere in this file -- either form
/// legally brings an external declaration into scope under a bare name
/// that would otherwise resolve to `java.lang.*` (`import com.other.
/// Integer;` and `import static com.other.Outer.Integer;` both make the
/// bare name `Integer` in this file refer to THAT declaration). Wildcard
/// forms (`import com.other.*;`, `import static com.other.Outer.*;`) name
/// no specific identifier and so cannot positively confirm a shadow --
/// deliberately excluded, same as every other narrowing pass in this
/// binder that consults imports. Bounded loop (Rule 14): iterates at most
/// `imports.len()` times, this file's own already-extracted list.
fn is_shadowed_by_an_explicit_import(name: &str, imports: &[ImportRecord]) -> bool {
    imports.iter().any(|import| {
        matches!(import.kind, ImportKind::Ordinary | ImportKind::Static) && import.path.rsplit('.').next() == Some(name)
    })
}

/// Condition (d): true when ANY type declared in this file (`LocalIndex::
/// type_nesting`, one record per declared type regardless of nesting
/// depth) has unresolved external supertype evidence (`TypeIndex::has_
/// unresolved_external_supertype`). Whole-FILE rather than "the call's
/// immediate enclosing type and its top-level ancestor only" -- that
/// narrower check missed an unresolved supertype on an INTERMEDIATE
/// nesting level (neither the immediate enclosing type nor the top-level
/// one), a real javac-valid false positive. Bounded loop (Rule 14):
/// iterates at most `type_nesting.len()` times, this file's own
/// already-extracted list; short-circuits on the first match.
pub(super) fn file_has_unresolved_external_supertype(
    type_nesting: &[TypeNestingRecord],
    type_index: &super::families::TypeIndex,
) -> bool {
    type_nesting
        .iter()
        .any(|record| type_index.has_unresolved_external_supertype(&record.type_name))
}

/// TAGS every candidate whose `enclosing_type` names a REPO-declared type
/// with `RECEIVER_TYPE_MISMATCH`, ONLY when ALL FOUR conditions in this
/// module's own doc comment hold. TAG-ONLY, exactly like every sibling
/// narrowing pass in `narrowing.rs`: this NEVER removes a candidate, on an
/// empty match or otherwise. An evaluator drops these fabricated edges
/// from its own analysis via `GraphHandle`'s evidence-filtered traversal
/// primitives (`callees_of_filtered`/`callers_of_filtered`/`reachable_to_
/// filtered`/`reachable_from_filtered`/`strongly_connected_components_
/// filtered`) instead.
#[allow(clippy::too_many_arguments)]
pub(super) fn apply_receiver_type_mismatch_tagging(
    candidates: &mut [(DeclInfo, u16)],
    receiver_type: Option<&str>,
    receiver_type_is_positive: bool,
    receiver_is_direct_parameter: bool,
    receiver_type_is_qualified_non_java_lang: bool,
    file_has_unresolved_external_supertype: bool,
    type_index: &super::families::TypeIndex,
    imports: &[ImportRecord],
) {
    if !receiver_type_is_positive || !receiver_is_direct_parameter {
        return;
    }
    if receiver_type_is_qualified_non_java_lang {
        return;
    }
    if file_has_unresolved_external_supertype {
        return;
    }
    let Some(receiver_type) = receiver_type else {
        return;
    };
    if !is_provably_closed_world_and_unshadowed(receiver_type, type_index, imports) {
        return;
    }
    for (decl, bits) in candidates.iter_mut() {
        if decl.enclosing_type.is_some() {
            *bits |= reasons::RECEIVER_TYPE_MISMATCH;
        }
    }
}
