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

pub mod depth;
mod admission;
mod budget_bind;
mod families;
mod name_index;
mod receiver;
mod resolve;
mod scope;

pub use admission::{bind_with_admission_gate, finish_bind, prepare_bind, BindOutcome, PreBindStats, PreparedBind};

use crate::graph::budget::IndexBudget;
use crate::graph::csr::CodeGraph;
use crate::graph::extract::local_index::LocalIndex;
use crate::graph::identity::SymbolId;
use crate::graph::reasons;
use depth::{
    BinderDepth, LEVEL_1_ARITY, LEVEL_2_IMPORT_CONTEXT, LEVEL_3_INHERITANCE_FAMILY,
    LEVEL_4_OVERLOAD_DISCRIMINATION, LEVEL_5_UNIQUE_NAME, LEVEL_6_RECEIVER_TYPE, LEVEL_7_SAME_CLASS_OR_SUPER,
};
use name_index::{DeclInfo, RepoNameIndex};
pub(crate) use resolve::enclosing_symbol;
use resolve::resolve_reference;
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
    arg_shapes: &[crate::graph::extract::local_index::ArgShape],
    name_index: &RepoNameIndex,
    type_index: &families::TypeIndex,
    receiver_type: Option<&str>,
    same_class_context: Option<&str>,
    index_is_complete: bool,
) -> PendingReference {
    let candidates = resolve_reference(
        name,
        ref_kind,
        file.file_id,
        scope,
        arg_count,
        arg_shapes,
        name_index,
        type_index,
        receiver_type,
        same_class_context,
        index_is_complete,
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
    candidates.iter().any(|(_, bits)| bits & reasons::FAMILY_TRUNCATED != 0)
}

/// Resolves every invocation/type-reference/construction site across
/// EVERY file in `files` into `PendingReference`s, and returns them
/// alongside the total candidate count (needed to reserve the CSR arena's
/// single allocation up front, AC5) and whether ANY reference's
/// inheritance-family expansion was truncated by `families::
/// MAX_FAMILY_SIZE` -- the signal `admission::prepare_bind`/`finish_bind`
/// use to set `AnalysisCompleteness::ResolutionAmbiguous` at the
/// whole-graph level.
fn resolve_all_references(
    files: &[FileForBind],
    name_index: &RepoNameIndex,
    type_index: &families::TypeIndex,
    index_is_complete: bool,
) -> (Vec<PendingReference>, usize, bool) {
    let mut pending = Vec::new();
    let mut total_candidates = 0usize;
    let mut family_truncated_anywhere = false;
    for file in files {
        let scope = build_file_scope(&file.index);
        // AC1/AC2 (Story #1806, S2b): the file's declared-type substrate
        // (locals/fields/params) -- built once per file, mirroring
        // `scope`'s own per-file construction, since every invocation in
        // this file's receiver-type resolution reads from it.
        let typed_names = receiver::FileTypedNames::build(&file.index.typed_names);
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
            let receiver_type = match &site.receiver {
                crate::graph::extract::local_index::ReceiverExpr::Identifier(_)
                | crate::graph::extract::local_index::ReceiverExpr::Chained { .. } => receiver::resolve_receiver_type(
                    &site.receiver,
                    site.enclosing_type.as_deref(),
                    site.enclosing_method,
                    &typed_names,
                    name_index,
                    type_index,
                ),
                crate::graph::extract::local_index::ReceiverExpr::None
                | crate::graph::extract::local_index::ReceiverExpr::SelfOrSuper
                | crate::graph::extract::local_index::ReceiverExpr::Other => None,
            };
            // AC3 (Story #1806, S2b): "unqualified calls resolve against
            // the enclosing class and its supertypes first" -- applies
            // ONLY to a bare/`this`/`super` receiver, never a qualified
            // call on some OTHER object (that is AC1's receiver-type
            // path instead, a separate evidence bit).
            let same_class_context = match &site.receiver {
                crate::graph::extract::local_index::ReceiverExpr::None
                | crate::graph::extract::local_index::ReceiverExpr::SelfOrSuper => site.enclosing_type.as_deref(),
                _ => None,
            };
            let r = resolve_site(
                &site.callee_name,
                REF_KIND_INVOCATION,
                site.line,
                file,
                &scope,
                site.arg_count,
                &site.arg_shapes,
                name_index,
                type_index,
                receiver_type.as_deref(),
                same_class_context,
                index_is_complete,
            );
            total_candidates += r.candidates.len();
            family_truncated_anywhere |= any_family_truncated(&r.candidates);
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
                name_index,
                type_index,
                None,
                None,
                index_is_complete,
            );
            total_candidates += r.candidates.len();
            family_truncated_anywhere |= any_family_truncated(&r.candidates);
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
                name_index,
                type_index,
                None,
                None,
                index_is_complete,
            );
            total_candidates += r.candidates.len();
            family_truncated_anywhere |= any_family_truncated(&r.candidates);
            pending.push(r);
        }
    }
    (pending, total_candidates, family_truncated_anywhere)
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
mod tests {
    use super::depth::LEVEL_0_BARE_NAME;
    use super::*;
    use crate::graph::confidence::Confidence;
    use crate::graph::extract::local_index::{Declaration, DeclarationKind, InvocationSite};

    fn method_decl(name: &str, file_id: u32, local: u32, param_count: Option<usize>) -> Declaration {
        Declaration {
            kind: DeclarationKind::Method,
            name: name.to_string(),
            line: 1,
            symbol: crate::graph::identity::make_symbol_id(file_id, local),
            param_count,
            param_types: Vec::new(),
            is_varargs: false,
        }
    }

    fn invocation(name: &str, arg_count: Option<usize>) -> InvocationSite {
        InvocationSite {
            callee_name: name.to_string(),
            line: 10,
            arg_count,
            arg_shapes: Vec::new(),
            receiver: crate::graph::extract::local_index::ReceiverExpr::None,
            enclosing_type: None,
            enclosing_method: None,
        }
    }

    fn file(file_id: u32, language: &str, index: LocalIndex) -> FileForBind {
        FileForBind { file_id, language: language.to_string(), index }
    }

    /// Whole-pipeline regression guard: every candidate `bind()` ever
    /// produces must have `confidence()` exactly equal to
    /// `Confidence::derive(reasons())` -- never set independently.
    #[test]
    fn every_candidate_bind_produces_has_confidence_matching_derive_of_its_reasons() {
        let mut file_a = LocalIndex::new();
        file_a.declarations.push(method_decl("uniqueOne", 1, 0, None));
        file_a.declarations.push(method_decl("shared", 1, 1, Some(1)));
        file_a.invocations.push(invocation("uniqueOne", Some(0)));
        file_a.invocations.push(invocation("shared", Some(1)));
        file_a.invocations.push(invocation("neverDeclared", None));

        let mut file_b = LocalIndex::new();
        file_b.declarations.push(method_decl("shared", 2, 0, Some(2)));

        let graph = bind(vec![file(1, "java", file_a), file(2, "java", file_b)]);

        let mut checked_any = false;
        for reference in graph.references() {
            for candidate in graph.candidates_for(reference) {
                checked_any = true;
                assert_eq!(candidate.confidence(), Confidence::derive(candidate.reasons()));
            }
        }
        assert!(checked_any, "test fixture produced no candidates to check");
    }

    /// AC4: `BinderDepth` must report Java's REAL levels and must NOT
    /// claim any depth for a language with no declarations/references.
    #[test]
    fn binder_depth_reports_real_levels_for_java_and_claims_none_for_an_empty_language() {
        let mut file_a = LocalIndex::new();
        file_a.declarations.push(method_decl("uniqueOne", 1, 0, None));
        file_a.invocations.push(invocation("uniqueOne", Some(0)));

        let graph = bind(vec![file(1, "java", file_a), file(2, "text", LocalIndex::new())]);

        let java_depth = graph.binder_depths().iter().find(|d| d.language == "java").unwrap();
        assert!(java_depth.reached(LEVEL_0_BARE_NAME));
        assert!(java_depth.reached(LEVEL_5_UNIQUE_NAME));

        let text_depth = graph.binder_depths().iter().find(|d| d.language == "text").unwrap();
        assert_eq!(
            text_depth.levels_reached, 0,
            "a language with no declarations/references must claim no depth"
        );
    }

    /// A sentinel local-symbol index for a file's package declaration --
    /// mirrors the pattern `resolve.rs`'s own `package_decl` helper uses.
    const PACKAGE_DECL_LOCAL_ID: u32 = 999;

    fn package_decl(file_id: u32, name: &str) -> Declaration {
        Declaration {
            kind: DeclarationKind::Package,
            name: name.to_string(),
            line: 1,
            symbol: crate::graph::identity::make_symbol_id(file_id, PACKAGE_DECL_LOCAL_ID),
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
        }
    }

    /// AC1 fixture: interface Repo (pkg.a) + implementor Impl (pkg.b);
    /// the caller shares pkg.a, so import-context narrowing alone would
    /// collapse the call to Repo.save alone -- family expansion must add
    /// Impl.save back. Mirrors `resolve.rs`'s own family-expansion test
    /// fixture, assembled as real `FileForBind`s for the full `bind()`
    /// pipeline.
    fn family_expansion_fixture_files() -> Vec<FileForBind> {
        use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord, MethodOwnerRecord};
        use crate::graph::identity::make_symbol_id;
        const INTERFACE_FILE_ID: u32 = 10;
        const IMPL_FILE_ID: u32 = 11;
        const CALLER_FILE_ID: u32 = 12;
        const SAVE_METHOD_LOCAL: u32 = 1;

        let mut interface_file = LocalIndex::new();
        interface_file.declarations.push(package_decl(INTERFACE_FILE_ID, "pkg.a"));
        interface_file.declarations.push(method_decl("save", INTERFACE_FILE_ID, SAVE_METHOD_LOCAL, Some(0)));
        interface_file.interface_names.push("Repo".to_string());
        interface_file.method_owners.push(MethodOwnerRecord {
            method_symbol: make_symbol_id(INTERFACE_FILE_ID, SAVE_METHOD_LOCAL),
            enclosing_type: "Repo".to_string(),
        });

        let mut impl_file = LocalIndex::new();
        impl_file.declarations.push(package_decl(IMPL_FILE_ID, "pkg.b"));
        impl_file.declarations.push(method_decl("save", IMPL_FILE_ID, SAVE_METHOD_LOCAL, Some(0)));
        impl_file.method_owners.push(MethodOwnerRecord {
            method_symbol: make_symbol_id(IMPL_FILE_ID, SAVE_METHOD_LOCAL),
            enclosing_type: "Impl".to_string(),
        });
        impl_file.inheritance.push(InheritanceRecord {
            kind: InheritanceKind::Implements,
            subtype_name: "Impl".to_string(),
            supertype_name: "Repo".to_string(),
            line: 1,
        });

        let mut caller = LocalIndex::new();
        caller.declarations.push(package_decl(CALLER_FILE_ID, "pkg.a"));
        caller.invocations.push(invocation("save", Some(0)));

        vec![
            file(INTERFACE_FILE_ID, "java", interface_file),
            file(IMPL_FILE_ID, "java", impl_file),
            file(CALLER_FILE_ID, "java", caller),
        ]
    }

    /// AC2 fixture: two `process` overloads distinguished only by
    /// declared parameter type, called with a discriminating literal.
    fn overload_discrimination_fixture_files() -> Vec<FileForBind> {
        use crate::graph::extract::local_index::ArgShape;
        use crate::graph::identity::make_symbol_id;
        const STRING_OVERLOAD_FILE_ID: u32 = 20;
        const INT_OVERLOAD_FILE_ID: u32 = 21;
        const CALLER_FILE_ID: u32 = 22;

        fn overload_decl(file_id: u32, param_type: &str) -> Declaration {
            Declaration {
                kind: DeclarationKind::Method,
                name: "process".to_string(),
                line: 1,
                symbol: make_symbol_id(file_id, 0),
                param_count: Some(1),
                param_types: vec![param_type.to_string()],
                is_varargs: false,
            }
        }

        let mut string_overload = LocalIndex::new();
        string_overload.declarations.push(overload_decl(STRING_OVERLOAD_FILE_ID, "String"));
        let mut int_overload = LocalIndex::new();
        int_overload.declarations.push(overload_decl(INT_OVERLOAD_FILE_ID, "int"));
        let mut caller = LocalIndex::new();
        caller.invocations.push(InvocationSite {
            callee_name: "process".to_string(),
            line: 10,
            arg_count: Some(1),
            arg_shapes: vec![ArgShape::StringLiteral],
            receiver: crate::graph::extract::local_index::ReceiverExpr::None,
            enclosing_type: None,
            enclosing_method: None,
        });

        vec![
            file(STRING_OVERLOAD_FILE_ID, "java", string_overload),
            file(INT_OVERLOAD_FILE_ID, "java", int_overload),
            file(CALLER_FILE_ID, "java", caller),
        ]
    }

    /// AC4 (Story #1793, S4): Java's `BinderDepth` must reach the NEW
    /// levels 3 (inheritance-family expansion) and 4 (overload
    /// discrimination) once real evidence for each is produced, exercised
    /// end-to-end through the real `bind()` pipeline.
    #[test]
    fn binder_depth_reaches_levels_3_and_4_for_java_via_family_expansion_and_overload_discrimination() {
        use super::depth::{LEVEL_3_INHERITANCE_FAMILY, LEVEL_4_OVERLOAD_DISCRIMINATION};

        let mut files = family_expansion_fixture_files();
        files.extend(overload_discrimination_fixture_files());
        let graph = bind(files);

        let java_depth = graph
            .binder_depths()
            .iter()
            .find(|d| d.language == "java")
            .expect("java depth must be present: both fixtures declare java files");
        assert!(java_depth.reached(LEVEL_3_INHERITANCE_FAMILY), "family expansion evidence must reach level 3");
        assert!(
            java_depth.reached(LEVEL_4_OVERLOAD_DISCRIMINATION),
            "overload-shape evidence must reach level 4"
        );
    }

    /// AC3 (Story #1806, S2b): Java's `BinderDepth` must reach the NEW
    /// level 7 (same-class-or-super resolution) once real evidence is
    /// produced, exercised end-to-end through the real `bind()` pipeline.
    #[test]
    fn binder_depth_reaches_level_7_for_java_via_same_class_or_super_resolution() {
        use super::depth::LEVEL_7_SAME_CLASS_OR_SUPER;
        use crate::graph::extract::local_index::{
            InheritanceKind, InheritanceRecord, MethodOwnerRecord, ReceiverExpr,
        };

        const BASE_FILE_ID: u32 = 30;
        const OTHER_FILE_ID: u32 = 31;
        const CALLER_FILE_ID: u32 = 32;

        let mut base_file = LocalIndex::new();
        base_file.declarations.push(method_decl("helper", BASE_FILE_ID, 0, Some(0)));
        base_file.method_owners.push(MethodOwnerRecord {
            method_symbol: crate::graph::identity::make_symbol_id(BASE_FILE_ID, 0),
            enclosing_type: "Base".to_string(),
        });
        base_file.inheritance.push(InheritanceRecord {
            kind: InheritanceKind::Extends,
            subtype_name: "Sub".to_string(),
            supertype_name: "Base".to_string(),
            line: 1,
        });

        let mut other_file = LocalIndex::new();
        other_file.declarations.push(method_decl("helper", OTHER_FILE_ID, 0, Some(0)));
        other_file.method_owners.push(MethodOwnerRecord {
            method_symbol: crate::graph::identity::make_symbol_id(OTHER_FILE_ID, 0),
            enclosing_type: "Other".to_string(),
        });

        let mut caller = LocalIndex::new();
        caller.invocations.push(InvocationSite {
            callee_name: "helper".to_string(),
            line: 10,
            arg_count: Some(0),
            arg_shapes: Vec::new(),
            receiver: ReceiverExpr::None,
            enclosing_type: Some("Sub".to_string()),
            enclosing_method: None,
        });

        let graph = bind(vec![
            file(BASE_FILE_ID, "java", base_file),
            file(OTHER_FILE_ID, "java", other_file),
            file(CALLER_FILE_ID, "java", caller),
        ]);

        let java_depth = graph.binder_depths().iter().find(|d| d.language == "java").unwrap();
        assert!(
            java_depth.reached(LEVEL_7_SAME_CLASS_OR_SUPER),
            "same-class-or-super evidence must reach level 7"
        );
    }

    /// AC5 (Story #1806, S2b) regression guard: a bare call's resolved
    /// candidate must carry `SAME_CLASS_OR_SUPER` but NEVER
    /// `RECEIVER_TYPE_MATCH` -- the two are distinct evidence PATHS, and
    /// `Confidence::derive` checks `RECEIVER_TYPE_MATCH` first, so a bare
    /// call incorrectly carrying both would misreport as `ReceiverType`
    /// confidence instead of the correct `SameClassOrSuper`.
    #[test]
    fn bare_call_marks_only_same_class_or_super_never_receiver_type_match() {
        use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord, MethodOwnerRecord, ReceiverExpr};
        use crate::graph::reasons;

        const BASE_FILE_ID: u32 = 60;
        const OTHER_FILE_ID: u32 = 61;
        const CALLER_FILE_ID: u32 = 62;

        let mut base_file = LocalIndex::new();
        base_file.declarations.push(method_decl("helper", BASE_FILE_ID, 0, Some(0)));
        base_file.method_owners.push(MethodOwnerRecord {
            method_symbol: crate::graph::identity::make_symbol_id(BASE_FILE_ID, 0),
            enclosing_type: "Base".to_string(),
        });
        base_file.inheritance.push(InheritanceRecord {
            kind: InheritanceKind::Extends,
            subtype_name: "Sub".to_string(),
            supertype_name: "Base".to_string(),
            line: 1,
        });

        let mut other_file = LocalIndex::new();
        other_file.declarations.push(method_decl("helper", OTHER_FILE_ID, 0, Some(0)));
        other_file.method_owners.push(MethodOwnerRecord {
            method_symbol: crate::graph::identity::make_symbol_id(OTHER_FILE_ID, 0),
            enclosing_type: "Other".to_string(),
        });

        let mut caller = LocalIndex::new();
        caller.invocations.push(InvocationSite {
            callee_name: "helper".to_string(),
            line: 10,
            arg_count: Some(0),
            arg_shapes: Vec::new(),
            receiver: ReceiverExpr::None,
            enclosing_type: Some("Sub".to_string()),
            enclosing_method: None,
        });

        let graph = bind(vec![
            file(BASE_FILE_ID, "java", base_file),
            file(OTHER_FILE_ID, "java", other_file),
            file(CALLER_FILE_ID, "java", caller),
        ]);

        let reference = graph
            .references()
            .iter()
            .find(|r| r.kind == REF_KIND_INVOCATION)
            .expect("the bare call must produce a reference");
        let candidates = graph.candidates_for(reference);
        assert_eq!(candidates.len(), 1, "Other.helper must be excluded");
        let reasons_bits = candidates[0].reasons();
        assert_ne!(reasons_bits & reasons::SAME_CLASS_OR_SUPER, 0, "bare call must carry SAME_CLASS_OR_SUPER");
        assert_eq!(
            reasons_bits & reasons::RECEIVER_TYPE_MATCH,
            0,
            "bare call must NEVER also carry RECEIVER_TYPE_MATCH"
        );
    }

    /// Shared fixture helper: a file declaring ONE method named
    /// `"doSomething"`, owned by `enclosing_type` -- factored out of
    /// `binder_depth_reaches_level_6_for_java_via_receiver_type_resolution`
    /// below, whose two candidate files (`Foo`, `Other`) both need exactly
    /// this shape.
    fn owned_do_something_file(file_id: u32, enclosing_type: &str) -> LocalIndex {
        use crate::graph::extract::local_index::MethodOwnerRecord;

        let mut index = LocalIndex::new();
        index.declarations.push(method_decl("doSomething", file_id, 0, Some(0)));
        index.method_owners.push(MethodOwnerRecord {
            method_symbol: crate::graph::identity::make_symbol_id(file_id, 0),
            enclosing_type: enclosing_type.to_string(),
        });
        index
    }

    /// AC1 (Story #1806, S2b): Java's `BinderDepth` must reach the NEW
    /// level 6 (receiver-type resolution) once real evidence is produced,
    /// exercised end-to-end through the real `bind()` pipeline: a caller
    /// method declares local `obj` of type `Foo`, then calls
    /// `obj.doSomething()` -- resolved via `obj`'s declared type.
    #[test]
    fn binder_depth_reaches_level_6_for_java_via_receiver_type_resolution() {
        use super::depth::LEVEL_6_RECEIVER_TYPE;
        use crate::graph::extract::local_index::{NameScope, ReceiverExpr, TypedNameRecord};

        const FOO_FILE_ID: u32 = 40;
        const OTHER_FILE_ID: u32 = 41;
        const CALLER_FILE_ID: u32 = 42;
        const CALLER_METHOD_LOCAL: u32 = 0;

        let foo_file = owned_do_something_file(FOO_FILE_ID, "Foo");
        let other_file = owned_do_something_file(OTHER_FILE_ID, "Other");

        let caller_method_symbol = crate::graph::identity::make_symbol_id(CALLER_FILE_ID, CALLER_METHOD_LOCAL);
        let mut caller = LocalIndex::new();
        caller.declarations.push(method_decl("run", CALLER_FILE_ID, CALLER_METHOD_LOCAL, Some(0)));
        caller.typed_names.push(TypedNameRecord {
            name: "obj".to_string(),
            declared_type: "Foo".to_string(),
            scope: NameScope::Local { enclosing_method: caller_method_symbol },
        });
        caller.invocations.push(InvocationSite {
            callee_name: "doSomething".to_string(),
            line: 10,
            arg_count: Some(0),
            arg_shapes: Vec::new(),
            receiver: ReceiverExpr::Identifier("obj".to_string()),
            enclosing_type: Some("Caller".to_string()),
            enclosing_method: Some(caller_method_symbol),
        });

        let graph = bind(vec![
            file(FOO_FILE_ID, "java", foo_file),
            file(OTHER_FILE_ID, "java", other_file),
            file(CALLER_FILE_ID, "java", caller),
        ]);

        let java_depth = graph.binder_depths().iter().find(|d| d.language == "java").unwrap();
        assert!(java_depth.reached(LEVEL_6_RECEIVER_TYPE), "receiver-type evidence must reach level 6");
    }
}
