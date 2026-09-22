//! The AC4 binder: turns extracted `LocalIndex` records plus a repo-wide
//! name index into confidence-scored candidate sets (Story #1787, S2,
//! AC4).
//!
//! **Full-rebuild-only, structurally**: `bind()` takes ownership of a
//! fresh `Vec<FileForBind>` and produces a brand-new `CodeGraph`. There is
//! no update/patch entry point anywhere in this module -- that absence is
//! what makes "never an incremental patch of an existing graph" true by
//! construction rather than merely documented.
//!
//! **No AST anywhere in this module**: every function here (and in
//! `scope`, `name_index`, `resolve`) reads only the compact `LocalIndex`
//! records a `LanguageExtractor` already produced -- see
//! `crate::graph::extract`. Bind is the TAIL of extraction, exactly as
//! AC4 requires.
//!
//! Every reference gets a candidate SET, never a single resolved target:
//! `resolve::resolve_reference` always returns a `Vec`, even when it
//! contains exactly one entry (the unique-name-in-repo shortcut). An
//! unresolved reference (out-of-repo definition, or a name this repo
//! declares nowhere) returns an empty `Vec`, which `bind()` passes
//! straight through to `CodeGraphBuilder::add_reference` as a zero-length
//! candidate window -- never a guessed target.

mod admission;
mod budget_bind;
pub mod depth;
mod families;
mod name_index;
mod narrowing;
mod receiver_mismatch;
mod receiver;
mod resolve;
mod scope;

pub use admission::{
    bind_with_admission_gate, finish_bind, prepare_bind, BindOutcome, BindTimeFacts, PreBindStats,
    PreparedBind,
};

use crate::graph::budget::IndexBudget;
use crate::graph::csr::CodeGraph;
use crate::graph::extract::local_index::{ArgShape, LocalIndex};
use crate::graph::identity::SymbolId;
use crate::graph::reasons;
use depth::{
    BinderDepth, LEVEL_1_ARITY, LEVEL_2_IMPORT_CONTEXT, LEVEL_3_INHERITANCE_FAMILY,
    LEVEL_4_OVERLOAD_DISCRIMINATION, LEVEL_5_UNIQUE_NAME, LEVEL_6_RECEIVER_TYPE,
    LEVEL_7_SAME_CLASS_OR_SUPER,
};
use name_index::{DeclInfo, RepoNameIndex};
pub(crate) use resolve::enclosing_symbol;
use resolve::{resolve_reference, target_kind_for_ref};
use scope::build_file_scope;

pub use budget_bind::{bind_with_budget, bind_with_budget_and_completeness};

/// A method call site. See the two siblings below for the other reference
/// kinds this binder resolves. Stored verbatim in `csr::Reference.kind`.
pub const REF_KIND_INVOCATION: u8 = 0;
/// A bare type reference (e.g. a local variable's declared type).
pub const REF_KIND_TYPE_REFERENCE: u8 = 1;
/// An object-construction site (`new Foo()`).
pub const REF_KIND_CONSTRUCTION: u8 = 2;

/// One file's extracted records, ready to be bound. `bind()` consumes a
/// `Vec<FileForBind>` by value -- there is no way to add one file to an
/// already-built graph, which is what makes bind full-rebuild-only.
pub struct FileForBind {
    pub file_id: u32,
    pub language: String,
    pub index: LocalIndex,
}

/// One not-yet-built reference, resolved but not yet placed into the CSR
/// arena (which needs the exact total candidate count up front).
struct PendingReference {
    from: SymbolId,
    file: u32,
    line: u32,
    kind: u8,
    language: String,
    candidates: Vec<(DeclInfo, u16)>,
}

/// Resolves one reference site. Shared by all three per-file reference
/// loops in `resolve_all_references` below.
#[allow(clippy::too_many_arguments)]
fn resolve_site(
    name: &str,
    ref_kind: u8,
    line: usize,
    file: &FileForBind,
    scope: &scope::FileScope,
    arg_count: Option<usize>,
    arg_shapes: &[ArgShape],
    arg_known_types: &[Option<String>],
    name_index: &RepoNameIndex,
    type_index: &families::TypeIndex,
    receiver_type: Option<&str>,
    receiver_type_is_positive: bool,
    same_class_context: Option<&str>,
    super_class_context: Option<&str>,
    caller_top_level: Option<&str>,
    index_is_complete: bool,
    receiver_is_type_qualifier: bool,
    receiver_is_direct_parameter: bool,
    receiver_type_is_qualified_non_java_lang: bool,
    file_has_unresolved_external_supertype: bool,
) -> PendingReference {
    let candidates = resolve_reference(
        name,
        ref_kind,
        file.file_id,
        scope,
        arg_count,
        arg_shapes,
        arg_known_types,
        name_index,
        type_index,
        receiver_type,
        receiver_type_is_positive,
        same_class_context,
        super_class_context,
        caller_top_level,
        index_is_complete,
        receiver_is_type_qualifier,
        receiver_is_direct_parameter,
        receiver_type_is_qualified_non_java_lang,
        file_has_unresolved_external_supertype,
    );
    PendingReference {
        from: enclosing_symbol(&file.index, file.file_id, line),
        file: file.file_id,
        line: line as u32,
        kind: ref_kind,
        language: file.language.clone(),
        candidates,
    }
}

/// Marks every AC4 level a candidate's `reasons_bits` demonstrates
/// evidence for. Called once per produced candidate -- see module docs on
/// `depth::BinderDepth` for why "reached" means "evidence was actually
/// produced", never "the code path executed".
fn mark_depth_for_reasons(depth: &mut BinderDepth, reasons_bits: u16) {
    const CONTEXT_MASK: u16 = reasons::SAME_FILE
        | reasons::SAME_PACKAGE
        | reasons::IMPORTED
        | reasons::STATIC_IMPORT
        | reasons::WILDCARD_IMPORT;
    if reasons_bits & reasons::ARITY_MATCH != 0 {
        depth.mark(LEVEL_1_ARITY);
    }
    if reasons_bits & CONTEXT_MASK != 0 {
        depth.mark(LEVEL_2_IMPORT_CONTEXT);
    }
    if reasons_bits & reasons::INHERITANCE_FAMILY != 0 {
        depth.mark(LEVEL_3_INHERITANCE_FAMILY);
    }
    if reasons_bits & reasons::OVERLOAD_ARG_TYPE_MATCH != 0 {
        depth.mark(LEVEL_4_OVERLOAD_DISCRIMINATION);
    }
    if reasons_bits & reasons::UNIQUE_NAME_IN_REPO != 0 {
        depth.mark(LEVEL_5_UNIQUE_NAME);
    }
    if reasons_bits & reasons::RECEIVER_TYPE_MATCH != 0 {
        depth.mark(LEVEL_6_RECEIVER_TYPE);
    }
    if reasons_bits & reasons::SAME_CLASS_OR_SUPER != 0 {
        depth.mark(LEVEL_7_SAME_CLASS_OR_SUPER);
    }
}

/// True when any candidate in `candidates` carries
/// `reasons::FAMILY_TRUNCATED` -- i.e. this reference's inheritance-family
/// expansion (if any) hit the `families::MAX_FAMILY_SIZE` cap. Bounded
/// loop: iterates exactly `candidates.len()` times (finite, fixed by an
/// already-produced candidate list).
fn any_family_truncated(candidates: &[(DeclInfo, u16)]) -> bool {
    candidates
        .iter()
        .any(|(_, bits)| bits & reasons::FAMILY_TRUNCATED != 0)
}

/// #1898 round 4 (epic #1906, mandate item 3) + #1910 round 6 (finding 5,
/// the mis-narrow counter): `name`'s exact bare-name pool SIZE (every
/// same-named declaration of `ref_kind`'s target kind, repo-wide, BEFORE
/// any narrowing pass runs). Reused by every one of `resolve_all_
/// references`'s three per-site loops to compute BOTH the "narrowed to
/// zero candidates" counter (pool non-empty, final candidate count zero)
/// AND the NEW "narrowed to a non-empty strict subset" counter (pool
/// non-empty, final count non-empty but strictly SMALLER than the pool) --
/// Rule 4, anti-duplication, rather than two copies of the same lookup.
fn bare_name_pool_size(name_index: &RepoNameIndex, ref_kind: u8, name: &str) -> usize {
    name_index.lookup(name, target_kind_for_ref(ref_kind)).len()
}

/// #1910 salvage (P3, Rule 4 anti-duplication): the SAME "did this
/// reference get mis-narrowed" accounting was copy-pasted once per
/// resolution loop in `resolve_all_references` below (invocations, type
/// references, constructions) -- pure counting, no behavior change from
/// extracting it into one function. `pool_size == 0` means an ordinary
/// out-of-repo reference, never counted by either counter (see
/// `resolve_all_references`'s own doc comment for the full rationale).
fn record_narrowing_outcome(
    candidate_count: usize,
    pool_size: usize,
    narrowed_to_zero_count: &mut usize,
    narrowed_to_nonempty_strict_subset_count: &mut usize,
) {
    if pool_size == 0 {
        return;
    }
    if candidate_count == 0 {
        *narrowed_to_zero_count += 1;
    } else if candidate_count < pool_size {
        *narrowed_to_nonempty_strict_subset_count += 1;
    }
}

/// #1910 salvage (P3): `bare_name_pool_size` performs a `RepoNameIndex::
/// lookup` (allocates a `Vec`) for every single reference across all three
/// resolution loops, even though its result can only ever CHANGE the
/// narrowing-outcome counters when the reference did NOT take the AC4
/// Level 5 unique-name-in-repo shortcut. A shortcut-admitted candidate
/// set is tagged `reasons::UNIQUE_NAME_IN_REPO` and is BY DEFINITION a
/// singleton drawn from a pool of exactly 1 (`try_unique_name_shortcut`'s
/// own `pool.len() != 1` precondition) -- so its pool size and final
/// candidate count are always equal (1 == 1), and neither counter can
/// ever fire for it. Skipping the lookup entirely for that case is a
/// pure performance short-circuit, not a behavior change: a
/// shortcut-admitted reference never contributed to either counter
/// before this change either.
fn took_unique_name_shortcut(candidates: &[(DeclInfo, u16)]) -> bool {
    candidates
        .iter()
        .any(|(_, bits)| bits & reasons::UNIQUE_NAME_IN_REPO != 0)
}

/// Resolves every invocation/type-reference/construction site across
/// EVERY file in `files` into `PendingReference`s, and returns them
/// alongside the total candidate count (needed to reserve the CSR arena's
/// single allocation up front, AC5), whether ANY reference's
/// inheritance-family expansion was truncated by `families::
/// MAX_FAMILY_SIZE` -- the signal `admission::prepare_bind`/`finish_bind`
/// use to set `AnalysisCompleteness::ResolutionAmbiguous` at the
/// whole-graph level -- and (#1898 round 4, epic #1906 mandate item 3)
/// how many references were genuinely NARROWED TO ZERO candidates: a
/// reference whose bare-name pool was non-empty (a real same-named
/// declaration exists somewhere in the repo) but whose FINAL candidate
/// set came back empty, i.e. every narrowing pass legitimately excluded
/// every same-named candidate (external receiver, wrong arity, wrong
/// enclosing type/hierarchy, ...). Deliberately distinct from an
/// out-of-repo reference (bare-name pool ALSO empty -- never counted
/// here, that is simply "this repo declares no such name at all", not a
/// narrowing outcome). This is the observability gap #1898's own report
/// named as having let three rounds of narrowing regressions survive a
/// green 531-test suite: #1897 (a sibling story in this same epic) wires
/// this number into `analyze_graph`'s completeness reporting; this
/// function's job is only to produce it.
///
/// #1910 round 6 (finding 5, round4-findings.md's own remediation item 5,
/// never built until now): ALSO returns how many references were
/// genuinely narrowed to a non-empty STRICT SUBSET of their bare-name
/// pool -- a DIFFERENT mis-narrow outcome `narrowed_to_zero_count` is
/// structurally blind to (it only ever increments on a final count of
/// EXACTLY zero). Both round-6 findings were wrong-non-empty-subset
/// deletions (a real edge deleted, a fabricated one kept, final count
/// staying non-zero throughout) that `narrowed_to_zero_count` reported as
/// zero for both. This counter makes that class of mis-narrow visible
/// without a reviewer hand-building fixtures every round.
fn resolve_all_references(
    files: &[FileForBind],
    name_index: &RepoNameIndex,
    type_index: &families::TypeIndex,
    index_is_complete: bool,
) -> (Vec<PendingReference>, usize, bool, usize, usize) {
    let mut pending = Vec::new();
    let mut total_candidates = 0usize;
    let mut family_truncated_anywhere = false;
    let mut narrowed_to_zero_count = 0usize;
    // #1910 round 6, finding 5: the mis-narrow counter `narrowed_to_zero_
    // count` is blind to -- see this function's own doc comment above.
    let mut narrowed_to_nonempty_strict_subset_count = 0usize;
    for file in files {
        let scope = build_file_scope(&file.index);
        // AC1/AC2 (Story #1806, S2b): the file's declared-type substrate
        // (locals/fields/params) -- built once per file, mirroring
        // `scope`'s own per-file construction, since every invocation in
        // this file's receiver-type resolution reads from it.
        let typed_names = receiver::FileTypedNames::build(&file.index.typed_names)
            .with_all_local_binding_names(&file.index.all_local_binding_names)
            .with_parameter_bindings(&file.index.parameter_typed_names)
            .with_qualified_non_java_lang_parameters(&file.index.qualified_non_java_lang_parameter_types);
        // #1922: computed ONCE per file, not per call site -- neither
        // check depends on which specific invocation is being resolved.
        // See `receiver::file_is_safe_for_type_qualifier_narrowing`'s own
        // doc comment for exactly what this guards against: an invisible
        // inherited field could be declared on ANY supertype anywhere in
        // this file (a same-file type sharing the real supertype's bare
        // name, a sibling nested class's own child, an anonymous class
        // body, or an excluded/Kotlin supertype), so hard-narrowing is
        // disabled for the whole file whenever any type in it carries any
        // extends/implements clause at all, or a static wildcard import
        // that could bring an unnamed field into scope.
        let file_safe_for_type_qualifier_narrowing = file.language == "java"
            && receiver::file_is_safe_for_type_qualifier_narrowing(&file.index, &scope.imports);
        // #1924 (p24): computed ONCE per file, not per call site -- true
        // when ANY type declared anywhere in this file has an unresolved
        // external supertype, regardless of nesting depth relative to a
        // given call site. See `receiver_mismatch::file_has_unresolved_
        // external_supertype`'s own doc comment for why this replaced a
        // narrower per-call-site check that only looked at the immediate
        // enclosing type and its top-level ancestor.
        let file_has_unresolved_external_supertype =
            receiver_mismatch::file_has_unresolved_external_supertype(&file.index.type_nesting, type_index);
        for site in &file.index.invocations {
            // AC1 (Story #1806, S2b): resolves the call's receiver to a
            // declared type, if the evidence exists -- `None` (never a
            // guess) when it doesn't. Deliberately gated to a genuinely
            // QUALIFIED receiver (`Identifier`/`Chained`): a bare/`this`/
            // `super` call's receiver ALSO "resolves" to the enclosing
            // type internally (needed so a chained call whose base is
            // `this`/`super` can still follow return types correctly),
            // but that value must never ALSO feed this AC1 evidence bit --
            // that would make `RECEIVER_TYPE_MATCH` fire redundantly
            // alongside AC3's `SAME_CLASS_OR_SUPER` on every bare call,
            // and since `Confidence::derive` checks `RECEIVER_TYPE_MATCH`
            // first, every bare call would misreport as `ReceiverType`
            // confidence instead of the correct `SameClassOrSuper` --
            // muddying AC5's distinct-evidence-path design even though it
            // would not change which candidates survive (both passes
            // compute the identical allowed-types set in that case).
            // Round 4 (#1898 epic #1906): the resolved type now carries
            // its own evidence TIER (`receiver::ReceiverEvidence`) --
            // `.type_name()`/`.is_positive()` below feed `resolve_site`'s
            // two separate params, so the full pipeline can tell a real
            // `TypedNameRecord` lookup hit apart from an open-world
            // fallback GUESS (see `ReceiverEvidence`'s own doc comment).
            let receiver_evidence = match &site.receiver {
                crate::graph::extract::local_index::ReceiverExpr::Identifier(_)
                | crate::graph::extract::local_index::ReceiverExpr::Chained { .. } => {
                    receiver::resolve_receiver_type(
                        &site.receiver,
                        site.enclosing_type.as_deref(),
                        site.enclosing_method,
                        &typed_names,
                        name_index,
                        type_index,
                    )
                }
                crate::graph::extract::local_index::ReceiverExpr::None
                | crate::graph::extract::local_index::ReceiverExpr::SelfOrSuper
                | crate::graph::extract::local_index::ReceiverExpr::Super
                | crate::graph::extract::local_index::ReceiverExpr::Other => {
                    receiver::ReceiverEvidence::None
                }
            };
            let receiver_type = receiver_evidence.type_name().map(|t| t.to_string());
            let receiver_type_is_positive = receiver_evidence.is_positive();
            // #1924/#1925: true ONLY for a DIRECT (non-chained) `Identifier`
            // receiver whose `(enclosing_method, name)` is a CONFIRMED
            // formal parameter -- never a block-scoped local, a field, or a
            // chained call's derived type. See `LocalIndex::parameter_
            // typed_names`'s own doc comment for why this is strictly
            // narrower than "receiver_type_is_positive" alone: a
            // `TypedNameRecord` lookup hit can be a genuine PARAMETER or an
            // ordinary LOCAL VARIABLE declared later in the same method,
            // indistinguishable by `(enclosing_method, name)` key alone
            // (#1919) -- `RECEIVER_TYPE_MISMATCH` tagging (`bind::receiver_
            // mismatch`) must see only the former.
            let receiver_is_direct_parameter = match &site.receiver {
                crate::graph::extract::local_index::ReceiverExpr::Identifier(name) => site
                    .enclosing_method
                    .is_some_and(|enclosing_method| typed_names.is_parameter_binding(enclosing_method, name)),
                _ => false,
            };
            // #1924 (p12): true ONLY for that SAME direct-parameter
            // receiver, when its declared type was written with an
            // explicit qualifier other than `java.lang` -- see
            // `LocalIndex::qualified_non_java_lang_parameter_types`'s own
            // doc comment. `RECEIVER_TYPE_MISMATCH` tagging must never fire
            // when this is true: the closed-world assumption behind it
            // only applies to the REAL `java.lang` type.
            let receiver_type_is_qualified_non_java_lang = match &site.receiver {
                crate::graph::extract::local_index::ReceiverExpr::Identifier(name) => site
                    .enclosing_method
                    .is_some_and(|enclosing_method| typed_names.is_disqualified_by_type_qualifier(enclosing_method, name)),
                _ => false,
            };
            // #1922: is this call DEFINITELY qualified by a type
            // reference -- see `receiver::is_definite_type_qualifier`'s
            // own doc comment for exactly what evidence this does and
            // does NOT rule out (same-file/captured local, parameter,
            // field including interface constants, single-member static
            // import; NOT wildcard static imports or multi-level nested
            // qualifiers). Only a direct `Identifier` receiver qualifies
            // (a `Chained` receiver's own type is inferred via return-type
            // chaining, not a bare qualifier the source itself wrote).
            // `narrowing::apply_type_qualifier_narrowing` never treats a
            // `false`/unresolved/unmatched result as proof of absence --
            // it only ever narrows on a POSITIVE, confirmed match, never
            // clears to empty -- so this flag being wrong in the
            // conservative direction (missing a real type qualifier) costs
            // evidence precision only, never an edge.
            //
            // JAVA ONLY, deliberately: `is_definite_type_qualifier`'s "no
            // local/field evidence anywhere" conclusion is only as good as
            // `typed_names`, and the Kotlin extractor NEVER populates
            // `typed_names` at all (`kotlin.rs`'s own module doc: "receiver-
            // type substrate ... explicitly OUT of scope -- this extractor
            // never populates typed_names"). For a Kotlin file, an
            // uppercase-named Kotlin property/`val`/object receiver would
            // be structurally indistinguishable from a genuine type
            // reference -- there is no evidence gap analogous to Java's
            // captured-local case (`has_any_local_binding`) to close it
            // with. Gating on `file.language == "java"` preserves this
            // tool's own documented guarantee (`analyze_graph.md`'s Kotlin
            // scope-limits paragraph): a Kotlin qualified call's evidence
            // quality is reduced, but it must never lose its edge outright.
            //
            // ALSO gated on `file_safe_for_type_qualifier_narrowing`
            // (#1922): even for a Java file, an uppercase
            // identifier clearing every OTHER guard can still be a real
            // field access INHERITED from a supertype this binder cannot
            // see into (outside the analyzed set, or Kotlin), or shadowed
            // by a static wildcard import -- see that flag's own
            // computation above for the full rationale.
            let receiver_is_type_qualifier = if file_safe_for_type_qualifier_narrowing {
                match &site.receiver {
                    crate::graph::extract::local_index::ReceiverExpr::Identifier(name) => {
                        receiver::is_definite_type_qualifier(
                            name,
                            site.enclosing_type.as_deref(),
                            site.enclosing_method,
                            &typed_names,
                            type_index,
                            &scope.imports,
                        )
                    }
                    _ => false,
                }
            } else {
                false
            };
            // AC3 (Story #1806, S2b): "unqualified calls resolve against
            // the enclosing class and its supertypes first" -- applies
            // ONLY to a bare/`this` receiver, never a qualified call on
            // some OTHER object (that is AC1's receiver-type path
            // instead, a separate evidence bit) and never a genuine
            // `super` call (D3 -- that is `super_class_context` below,
            // which must exclude the enclosing type itself).
            let same_class_context = match &site.receiver {
                crate::graph::extract::local_index::ReceiverExpr::None
                | crate::graph::extract::local_index::ReceiverExpr::SelfOrSuper => {
                    site.enclosing_type.as_deref()
                }
                _ => None,
            };
            // D3: a genuine `super.foo()`/`super::foo` call's enclosing
            // type, threaded to `apply_super_class_narrowing` -- resolved
            // ONLY against the enclosing type's supertypes, never the
            // enclosing type itself.
            let super_class_context = match &site.receiver {
                crate::graph::extract::local_index::ReceiverExpr::Super => {
                    site.enclosing_type.as_deref()
                }
                _ => None,
            };
            let caller_top_level = site
                .enclosing_type
                .as_deref()
                .and_then(|type_name| type_index.top_level_of(type_name));
            // Bug #1923 (AC2): resolves the KNOWN declared type of every
            // `ArgShape::Identifier`/`SelfReference` argument at this call
            // site, one entry per `site.arg_shapes` -- `None` for every
            // other shape (no evidence this pass can resolve at all).
            // `receiver::resolve_argument_identifier_type` restricts
            // itself to POSITIVE evidence only (see its own doc comment),
            // and `SelfReference`'s type is definitionally the call's own
            // `enclosing_type`, exactly like `ReceiverExpr::None`/
            // `SelfOrSuper` already resolve for a receiver.
            let arg_known_types: Vec<Option<String>> = site
                .arg_shapes
                .iter()
                .map(|shape| match shape {
                    ArgShape::Identifier(name) => receiver::resolve_argument_identifier_type(
                        name,
                        site.enclosing_type.as_deref(),
                        site.enclosing_method,
                        &typed_names,
                        type_index,
                    ),
                    ArgShape::SelfReference => site.enclosing_type.clone(),
                    // Bug #1923 (P1): a Cast/Constructor shape
                    // already carries its own known type name directly
                    // from extraction -- no bind-time lookup needed, and
                    // (unlike Identifier) no positive-vs-advisory
                    // evidence tier to choose between. Feeding it through
                    // the SAME tag-only mechanism as Identifier/`this`
                    // keeps a named-type cast's evidence out of any
                    // bare-name EXCLUSION path entirely, while still
                    // letting it earn the tag.
                    ArgShape::Cast(t) | ArgShape::Constructor(t) => Some(t.clone()),
                    _ => None,
                })
                .collect();
            let r = resolve_site(
                &site.callee_name,
                REF_KIND_INVOCATION,
                site.line,
                file,
                &scope,
                site.arg_count,
                &site.arg_shapes,
                &arg_known_types,
                name_index,
                type_index,
                receiver_type.as_deref(),
                receiver_type_is_positive,
                same_class_context,
                super_class_context,
                caller_top_level,
                index_is_complete,
                receiver_is_type_qualifier,
                receiver_is_direct_parameter,
                receiver_type_is_qualified_non_java_lang,
                file_has_unresolved_external_supertype,
            );
            total_candidates += r.candidates.len();
            family_truncated_anywhere |= any_family_truncated(&r.candidates);
            if !took_unique_name_shortcut(&r.candidates) {
                let pool_size =
                    bare_name_pool_size(name_index, REF_KIND_INVOCATION, &site.callee_name);
                record_narrowing_outcome(
                    r.candidates.len(),
                    pool_size,
                    &mut narrowed_to_zero_count,
                    &mut narrowed_to_nonempty_strict_subset_count,
                );
            }
            pending.push(r);
        }
        for site in &file.index.type_references {
            let r = resolve_site(
                &site.type_name,
                REF_KIND_TYPE_REFERENCE,
                site.line,
                file,
                &scope,
                None,
                &[],
                &[],
                name_index,
                type_index,
                None,
                false,
                None,
                None,
                None,
                index_is_complete,
                false,
                false,
                false,
                false,
            );
            total_candidates += r.candidates.len();
            family_truncated_anywhere |= any_family_truncated(&r.candidates);
            if !took_unique_name_shortcut(&r.candidates) {
                let pool_size =
                    bare_name_pool_size(name_index, REF_KIND_TYPE_REFERENCE, &site.type_name);
                record_narrowing_outcome(
                    r.candidates.len(),
                    pool_size,
                    &mut narrowed_to_zero_count,
                    &mut narrowed_to_nonempty_strict_subset_count,
                );
            }
            pending.push(r);
        }
        for site in &file.index.constructions {
            let r = resolve_site(
                &site.type_name,
                REF_KIND_CONSTRUCTION,
                site.line,
                file,
                &scope,
                None,
                &[],
                &[],
                name_index,
                type_index,
                None,
                false,
                None,
                None,
                None,
                index_is_complete,
                false,
                false,
                false,
                false,
            );
            total_candidates += r.candidates.len();
            family_truncated_anywhere |= any_family_truncated(&r.candidates);
            if !took_unique_name_shortcut(&r.candidates) {
                let pool_size =
                    bare_name_pool_size(name_index, REF_KIND_CONSTRUCTION, &site.type_name);
                record_narrowing_outcome(
                    r.candidates.len(),
                    pool_size,
                    &mut narrowed_to_zero_count,
                    &mut narrowed_to_nonempty_strict_subset_count,
                );
            }
            pending.push(r);
        }
    }
    (
        pending,
        total_candidates,
        family_truncated_anywhere,
        narrowed_to_zero_count,
        narrowed_to_nonempty_strict_subset_count,
    )
}

/// Full-rebuild-only binder entry point (AC4). Consumes `files` by value
/// and produces a brand-new `CodeGraph` with per-language `BinderDepth`
/// attached -- there is no other way to obtain a bound graph in this
/// crate, which is what makes bind structurally full-rebuild-only. Exactly
/// `bind_with_budget(files, &IndexBudget::unlimited())` (AC6): an
/// unlimited budget can never be exceeded, so this is byte-for-byte the
/// same graph `bind()` produced before AC6 existed.
pub fn bind(files: Vec<FileForBind>) -> CodeGraph {
    bind_with_budget(files, &IndexBudget::unlimited())
}

#[cfg(test)]
#[path = "mod_tests.rs"]
mod tests;
