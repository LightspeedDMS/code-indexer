//! AC6 budgeted binder entry point (Story #1787, S2): the fail-closed
//! degradation ladder wired against a real `bind`. Split out of
//! `super::mod` to keep that module under the project's per-module line
//! budget, mirroring how `resolve`/`name_index`/`scope`/`depth` each carry
//! their own focused logic and tests.
//!
//! See `crate::graph::budget` for the ladder's individual pieces
//! (`IndexBudget`, `ReferencedBits`, `cap_top_n_by_confidence`,
//! `AnalysisCompleteness`) and their own unit tests; this module is where
//! they are actually composed against a real multi-file bind.

use super::{FileForBind, PendingReference};
use crate::graph::budget::IndexBudget;
use crate::graph::csr::{CodeGraph, CodeGraphBuilder};
use crate::graph::extract::local_index::{Declaration, DeclarationKind};
use crate::graph::identity::SymbolId;

/// AC6: the exact final CSR candidate-arena size once the ladder's step-2
/// cap is (or is not) applied -- must be computed BEFORE
/// `CodeGraphBuilder::with_candidate_capacity`, which needs the exact
/// final count up front. Terminates in one pass over `pending` (finite:
/// one entry per reference `resolve_all_references` produced, itself
/// bounded by the finite invocation/type-reference/construction counts
/// extracted per file).
pub(super) fn capped_candidate_total(
    pending: &[PendingReference],
    exceeded: bool,
    max_per_reference: usize,
) -> usize {
    if !exceeded {
        return pending.iter().map(|r| r.candidates.len()).sum();
    }
    pending
        .iter()
        .map(|r| r.candidates.len().min(max_per_reference))
        .sum()
}

/// Bug #1904: widens a `Method` declaration's cached signature to carry
/// its declaring type's bare name and its real, per-parameter bare type
/// names (e.g. `"TimeUtil.parse(XMLGregorianCalendar)"`) instead of the
/// arity-only `"name(N params)"` shape the shipped signature-matching
/// template (`docs/xray-templates/callers-of-symbols-matching-signature-
/// text.rs`) cannot match against by name or declaring type at all.
/// Fully LANGUAGE-AGNOSTIC: it reads only `Declaration`/
/// `MethodOwnerRecord`, both already populated by every extractor (Java
/// AND Kotlin, #1908) at extraction time -- this is the ONE place this
/// widening needs to happen for every current and future language,
/// never a per-extractor duplicate (Rule 4, anti-duplication).
///
/// `None` for every non-`Method` `DeclarationKind` (`Type`/`Field`/
/// `Constant`/`Package`) -- those already name the one thing they
/// declare via their own `"{keyword} {name}"`-shaped cached signature,
/// and there is no separate "declaring type" for them at this layer
/// without inventing data the extractor does not actually capture here
/// (Rule 10, fact-verification). The caller falls back to the original
/// cached string in that case.
///
/// `owner` is `None` when the extractor could not determine the
/// immediately enclosing type (see `MethodOwnerRecord`'s own doc
/// comment) -- the plain `"name(Type, Type)"` form is used then, never a
/// fabricated declaring type.
///
/// `declaration.param_types` is BARE, generic-stripped text (never
/// fully-qualified with a package -- that resolution does not exist at
/// this layer, see `Declaration::param_types`'s own doc comment) and can
/// be SHORTER than `declaration.param_count` when the extractor could
/// not read every parameter's type. Presenting a shorter list as if it
/// were the complete signature would be fabrication, so this falls back
/// to the original arity-only `"(N params)"` form (still prefixed with
/// the declaring type when known) whenever the two counts disagree --
/// under-reporting is the safe direction here, never a guessed param list.
fn widen_method_signature(declaration: &Declaration, owner: Option<&str>) -> Option<String> {
    if declaration.kind != DeclarationKind::Method {
        return None;
    }
    let params_text = if declaration.param_types.len() == declaration.param_count.unwrap_or(usize::MAX) {
        declaration.param_types.join(", ")
    } else {
        format!("{} params", declaration.param_count.unwrap_or(0))
    };
    Some(match owner {
        Some(owner) => format!("{owner}.{}({params_text})", declaration.name),
        None => format!("{}({params_text})", declaration.name),
    })
}

/// Interns EVERY declared symbol across `files` -- not just symbols that
/// happen to appear as a reference's `from` or as some candidate's target
/// -- and, unless `exceeded` (AC6 ladder step 1, "drop snippets first"),
/// attaches its cached AC2 signature line too. Interning every declared
/// symbol MUST run unconditionally regardless of budget pressure: a
/// symbol nobody ever calls (the exact case `is_definitely_dead_code`
/// exists to report on) would otherwise never be interned at all, making
/// it unqueryable rather than correctly "unreferenced".
pub(super) fn intern_declarations_and_attach_signatures(
    files: &[FileForBind],
    builder: &mut CodeGraphBuilder,
    exceeded: bool,
    file_paths: &std::collections::HashMap<u32, String>,
) {
    for file in files {
        // Bug #1900 (epic #1906 P5): interned ONCE per file (not per
        // declaration) -- `intern_string` dedups internally too, but there
        // is no reason to pay even a HashMap lookup per declaration when
        // one file may declare hundreds of symbols. Absent from
        // `file_paths` (every caller except `repo_index::build_repo_graph`
        // today) means no location is ever recorded for this file's
        // declarations -- the same safe "unknown" default `location_for`
        // already returns for a dense id it never saw.
        let file_string_id = file_paths.get(&file.file_id).map(|path| builder.intern_string(path));
        // Bug #1904: per-file lookup from a method-shaped symbol to its
        // immediately enclosing type's bare name, built ONCE per file
        // (O(declared methods in this file)) rather than re-scanning
        // `method_owners` per declaration.
        let owners_by_symbol: std::collections::HashMap<SymbolId, &str> = file
            .index
            .method_owners
            .iter()
            .map(|owner| (owner.method_symbol, owner.enclosing_type.as_str()))
            .collect();
        // Bug #1926: built ONCE per file, same rationale as `owners_by_
        // symbol` above.
        let non_instantiable: std::collections::HashSet<SymbolId> =
            file.index.non_instantiable_constructors.iter().copied().collect();
        for declaration in &file.index.declarations {
            let dense = builder.intern_symbol(declaration.symbol);
            // Bug #1926: a SEPARATE analytical fact from `visibility`
            // (never folded into it) -- see `CodeGraphBuilder`'s own field
            // doc comment for why. Sparse (like `visibilities`/`kinds`),
            // but the ATTACH ITSELF is unconditional w.r.t. budget
            // pressure -- this runs before the `exceeded` early return
            // below, so a qualifying symbol is never dropped under
            // pressure.
            if non_instantiable.contains(&declaration.symbol) {
                builder.add_non_instantiable_constructor(dense);
            }
            // Story #1835: visibility is ANALYTICAL data `is_definitely_
            // dead_code` depends on directly, not presentation-only like
            // `signatures` -- it MUST survive budget pressure, so this
            // runs unconditionally, before the `exceeded` early return
            // below (which gates only the signature cache).
            if let Some(&visibility) = file.index.visibilities.get(&declaration.symbol) {
                builder.add_visibility(dense, visibility);
            }
            // Bug #1858: declaration kind is likewise ANALYTICAL data
            // `is_definitely_dead_code` depends on directly (it is what
            // lets the predicate tell a Field/Constant, whose reads never
            // become graph edges, apart from a Method/Type, whose
            // references ARE tracked) -- it MUST survive budget pressure,
            // so this too runs unconditionally, before the `exceeded`
            // early return. Unlike `visibilities`, `declaration.kind` is a
            // mandatory field on every `Declaration` (never a sparse,
            // possibly-absent map lookup), so there is no `Option` to
            // unwrap here.
            builder.add_kind(dense, declaration.kind);
            // Bug #1900: DECLARATION location, like visibility/kind, is
            // analytical data an evaluator needs to audit a finding -- it
            // MUST survive budget pressure, so this too runs
            // unconditionally, before the `exceeded` early return below
            // (which gates only the signature cache).
            if let Some(file_string_id) = file_string_id {
                builder.add_location(dense, file_string_id, declaration.line as u32);
            }
            if exceeded {
                continue;
            }
            if let Some(signature) = file.index.signatures.get(&declaration.symbol) {
                // Bug #1904: for a Method declaration, replace the cached
                // arity-only text with the widened declaring-type +
                // real-param-types form; every other DeclarationKind keeps
                // its existing cached signature UNCHANGED (`widen_method_
                // signature` returns None for them).
                let owner = owners_by_symbol.get(&declaration.symbol).copied();
                let widened = widen_method_signature(declaration, owner).unwrap_or_else(|| signature.clone());
                builder.add_signature(dense, widened);
            }
        }
        // Bug #1926 (final round): each target here was already resolved
        // at extraction time DIRECTLY against its own annotated method's
        // owning type (`java_methods::resolve_method_source_edges`) --
        // marking it referenced here bypasses `resolve_reference`/
        // `RepoNameIndex` entirely, so no outer-class, sibling-nested-
        // class, or other-file same-named decoy can ever be considered.
        // Every symbol here is already a real declaration in THIS file
        // (interned by the loop above), so `intern_symbol` here is a
        // pure dense-id lookup, never a fresh allocation.
        //
        // SCOPE: `mark_referenced` only ever sets the target's AC6
        // referenced-bit (`ReferencedBits`) -- it never adds a `Reference`/
        // `Candidate` to the CSR arena. This is enough to suppress the
        // target's OWN `is_definitely_dead_code` verdict (`Some(true)` ->
        // `Some(false)`), but the reflection-invoked reference this
        // represents is INVISIBLE to `callers_of`/`callees_of`/
        // `reachable_to`/`reachable_from` and any other query that walks
        // real graph edges -- a `@MethodSource` provider never appears as
        // a "caller" of the annotated test method or vice versa.
        for &target in &file.index.method_source_edges {
            let dense = builder.intern_symbol(target);
            builder.mark_referenced(dense);
        }
    }
}

/// AC6 budgeted binder entry point. `super::bind()` delegates here with
/// `IndexBudget::unlimited()` -- a budget that can never be exceeded, so
/// the ladder never engages and `completeness()` always reports
/// `Complete`, matching `bind()`'s pre-AC6 behavior byte-for-byte.
///
/// Ladder, applied only when `budget.is_exceeded_by(total raw
/// candidates)`: (1) `intern_declarations_and_attach_signatures` simply
/// skips `add_signature` -- the cheapest step, dropped first; (2)
/// `cap_top_n_by_confidence` narrows each reference's own candidate
/// window; (3) `builder.mark_referenced` is called, per candidate, from
/// the RAW list BEFORE step 2 ever truncates anything -- decoupled by
/// construction, not by a check; (4) completeness is set to
/// `IndexBudgetExceeded`, which `completeness()` reports to other
/// consumers (e.g. `repo_index`'s budget-exceeded reporting). Bug #1833:
/// this no longer affects `CodeGraph::is_definitely_dead_code`, which
/// suppresses the strongest dead-code tier (`Some(true)`)
/// unconditionally now, regardless of completeness -- see that method's
/// doc comment in `code_graph.rs`.
pub fn bind_with_budget(files: Vec<FileForBind>, budget: &IndexBudget) -> CodeGraph {
    // Story #1787 AC12: re-expressed in terms of the two-step admission
    // split (`super::admission`) so there is exactly ONE copy of the
    // ladder logic. `_stats` is discarded here -- `bind_with_budget`
    // itself never gates on anything; `admission::bind_with_admission_gate`
    // is the entry point an external caller uses when it wants to.
    //
    // `index_is_complete = true`: this convenience entry point has no way
    // to know whether `files` represents the WHOLE repository (that
    // knowledge lives one layer up, in whatever assembled `files`) -- it
    // preserves this function's existing, byte-for-byte-unchanged contract
    // for its many current callers/tests. `bind_with_budget_and_completeness`
    // below is the real entry point a caller with that knowledge (e.g.
    // `repo_index::build_repo_graph`) must use instead (dual-review D3).
    bind_with_budget_and_completeness(files, budget, true)
}

/// Dual-review defect D3 fix: the real AC6 entry point for a caller that
/// KNOWS whether its `files` list represents the entire repository (e.g.
/// `repo_index::build_repo_graph`, which tracks `max_files` truncation,
/// extractor panics, and unreadable files). `index_is_complete = false`
/// disables the binder's `UNIQUE_NAME_IN_REPO` shortcut end-to-end (see
/// `bind::resolve::resolve_reference`'s doc comment) so a same-named
/// declaration hiding in a file this run never saw can never be silently
/// promoted to `Confidence::Exact`.
pub fn bind_with_budget_and_completeness(
    files: Vec<FileForBind>,
    budget: &IndexBudget,
    index_is_complete: bool,
) -> CodeGraph {
    let (prepared, _stats) = super::admission::prepare_bind(files, index_is_complete);
    // Bug #1900: this generic entry point has no path-tracking caller --
    // only `repo_index::build_repo_graph` knows real repo-relative paths
    // and threads them through `finish_bind` directly. An empty map here
    // preserves this function's pre-existing "no location data" behavior
    // exactly (every `location_for` call on graphs built through this path
    // returns `None`, unchanged).
    //
    // Bug #1897 P1 fix: `finish_bind` now also returns `BindTimeFacts` --
    // discarded here (`.0`) since this convenience entry point's contract
    // is "return a `CodeGraph`" byte-for-byte, unchanged. The lossless
    // reasons only `repo_index::build_repo_graph` needs are read directly
    // from `finish_bind`'s return value at that call site instead.
    super::admission::finish_bind(prepared, budget, &std::collections::HashMap::new()).0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::budget::AnalysisCompleteness;
    use crate::graph::extract::local_index::{
        Declaration, DeclarationKind, InvocationSite, LocalIndex, MethodOwnerRecord,
    };
    use crate::graph::identity::{make_symbol_id, SymbolId};

    fn method_decl(
        name: &str,
        file_id: u32,
        local: u32,
        param_count: Option<usize>,
    ) -> Declaration {
        Declaration {
            kind: DeclarationKind::Method,
            name: name.to_string(),
            line: 1,
            symbol: make_symbol_id(file_id, local),
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
        FileForBind {
            file_id,
            language: language.to_string(),
            index,
        }
    }

    /// Bug #1904: a single-file fixture with one Method declaration
    /// carrying real `param_types` and (optionally) a `MethodOwnerRecord`
    /// linking it to its declaring type -- mirrors what a real extractor
    /// (Java or Kotlin, #1908) actually populates at extraction time, but
    /// stays independent of BOTH concrete extractors: this exercises
    /// `budget_bind`'s own language-agnostic widening logic, never one
    /// language's extraction code. `index.signatures` is seeded with the
    /// OLD arity-only text every real extractor still inserts (java.rs/
    /// kotlin.rs `format!("{name}({param_count} params)")`) -- the text
    /// `widen_method_signature` must REPLACE for a Method declaration,
    /// never merely copy through.
    fn single_method_fixture(
        name: &str,
        owner: Option<&str>,
        param_count: Option<usize>,
        param_types: Vec<String>,
    ) -> (Vec<FileForBind>, SymbolId) {
        let symbol = make_symbol_id(1, 1);
        let mut index = LocalIndex::new();
        index.declarations.push(Declaration {
            kind: DeclarationKind::Method,
            name: name.to_string(),
            line: 5,
            symbol,
            param_count,
            param_types,
            is_varargs: false,
        });
        index
            .signatures
            .insert(symbol, format!("{name}({} params)", param_count.unwrap_or(0)));
        if let Some(owner) = owner {
            index.method_owners.push(MethodOwnerRecord {
                method_symbol: symbol,
                enclosing_type: owner.to_string(),
            });
        }
        (vec![file(1, "java", index)], symbol)
    }

    /// Bug #1904 discriminating RED/GREEN: a Method's cached signature
    /// must carry its declaring type and real parameter types, never just
    /// arity -- this is what lets the shipped signature-matching template
    /// (`docs/xray-templates/callers-of-symbols-matching-signature-text.rs`)
    /// match on a real declaring-class name instead of matching nothing.
    #[test]
    fn method_declaration_with_real_param_types_and_a_known_owner_produces_the_widened_signature() {
        let (files, symbol) =
            single_method_fixture("parse", Some("TimeUtil"), Some(1), vec!["XMLGregorianCalendar".to_string()]);
        let graph = bind_with_budget(files, &IndexBudget::unlimited());
        let dense = graph.dense_id_for(symbol).expect("method must be interned");
        assert_eq!(graph.signature_for(dense), Some("TimeUtil.parse(XMLGregorianCalendar)"));
    }

    /// Bug #1904: with no `MethodOwnerRecord` at all (the extractor could
    /// not determine the enclosing type), the plain `name(Types)` form is
    /// used -- never a fabricated declaring type.
    #[test]
    fn method_declaration_with_no_known_owner_omits_the_prefix_but_still_lists_real_param_types() {
        let (files, symbol) = single_method_fixture("parse", None, Some(1), vec!["String".to_string()]);
        let graph = bind_with_budget(files, &IndexBudget::unlimited());
        let dense = graph.dense_id_for(symbol).expect("method must be interned");
        assert_eq!(graph.signature_for(dense), Some("parse(String)"));
    }

    /// Bug #1904: `param_count` says 2 parameters but only 1 type was
    /// successfully read -- presenting that ONE type as if it were the
    /// complete list would be fabrication (Rule 10). Must fall back to
    /// the arity-only `"N params"` form, still prefixed with the known
    /// owner -- under-reporting is the safe direction, never a guessed
    /// partial param list.
    #[test]
    fn method_declaration_with_incomplete_param_types_falls_back_to_the_arity_only_form_without_fabricating() {
        let (files, symbol) = single_method_fixture("connect", Some("Client"), Some(2), vec!["String".to_string()]);
        let graph = bind_with_budget(files, &IndexBudget::unlimited());
        let dense = graph.dense_id_for(symbol).expect("method must be interned");
        assert_eq!(graph.signature_for(dense), Some("Client.connect(2 params)"));
    }

    /// Bug #1904: `MethodOwnerRecord`'s own doc comment says a constructor
    /// gets an owner record too (constructors use `DeclarationKind::
    /// Method` so invocation sites resolve by the ordinary method path) --
    /// widening must behave sensibly for one, not just an ordinary method.
    #[test]
    fn constructor_declaration_widens_using_its_owner_exactly_like_an_ordinary_method() {
        let (files, symbol) = single_method_fixture("OrderService", Some("OrderService"), Some(0), Vec::new());
        let graph = bind_with_budget(files, &IndexBudget::unlimited());
        let dense = graph.dense_id_for(symbol).expect("constructor must be interned");
        assert_eq!(graph.signature_for(dense), Some("OrderService.OrderService()"));
    }

    /// Bug #1904 constraint: the widening must alter Method signature
    /// CONTENT only -- every other `DeclarationKind` keeps its existing
    /// cached signature byte-for-byte, since `widen_method_signature`
    /// returns `None` for them and the caller falls back to the original
    /// cached string unchanged.
    #[test]
    fn non_method_declarations_keep_their_existing_cached_signature_unchanged() {
        let symbol = make_symbol_id(1, 0);
        let mut index = LocalIndex::new();
        index.declarations.push(Declaration {
            kind: DeclarationKind::Type,
            name: "OrderService".to_string(),
            line: 1,
            symbol,
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
        });
        index.signatures.insert(symbol, "class OrderService".to_string());
        let files = vec![file(1, "java", index)];
        let graph = bind_with_budget(files, &IndexBudget::unlimited());
        let dense = graph.dense_id_for(symbol).expect("type must be interned");
        assert_eq!(graph.signature_for(dense), Some("class OrderService"));
    }

    /// Three same-named `run` declarations, none sharing a file (or
    /// package/import) with the call site: zero distinguishing evidence,
    /// so all three stay equally NameOnly-confidence and genuinely
    /// ambiguous -- nothing narrows the set before the AC6 cap. Plus a
    /// unique, non-ambiguous declaration invoked separately, to push the
    /// repo-wide RAW candidate total (4) past a tight ceiling (3).
    fn ambiguous_run_fixture() -> (Vec<FileForBind>, IndexBudget, Vec<SymbolId>) {
        let mut file_a = LocalIndex::new();
        file_a.declarations.push(method_decl("run", 1, 0, None));
        let mut file_b = LocalIndex::new();
        file_b.declarations.push(method_decl("run", 2, 0, None));
        let mut file_c = LocalIndex::new();
        file_c.declarations.push(method_decl("run", 3, 0, None));
        let mut caller = LocalIndex::new();
        caller.invocations.push(invocation("run", None));
        caller.invocations.push(invocation("uniqueOne", Some(0)));
        let mut file_unique = LocalIndex::new();
        file_unique
            .declarations
            .push(method_decl("uniqueOne", 5, 0, None));

        let files = vec![
            file(1, "java", file_a),
            file(2, "java", file_b),
            file(3, "java", file_c),
            file(4, "java", caller),
            file(5, "java", file_unique),
        ];
        let run_symbols: Vec<SymbolId> = [1u32, 2u32, 3u32]
            .iter()
            .map(|&f| make_symbol_id(f, 0))
            .collect();
        (files, IndexBudget::new(3, 1), run_symbols)
    }

    /// AC6's central discriminating test (named explicitly in the story):
    /// a symbol referenced ONLY by a LOW-CONFIDENCE, ambiguous edge must
    /// still be reported as referenced after the budget forces a
    /// per-reference top-N cap that truncates that exact candidate out of
    /// the CSR arena. A wrong implementation that marked `ReferencedBits`
    /// from the CAPPED (post-truncation) candidate list -- rather than the
    /// raw, pre-cap list -- would report the truncated-away symbols as
    /// unreferenced: the false "this code is dead" verdict AC6 exists to
    /// prevent.
    #[test]
    fn symbol_referenced_only_by_a_low_confidence_edge_still_reports_referenced_under_budget() {
        let (files, budget, run_symbols) = ambiguous_run_fixture();
        let graph = bind_with_budget(files, &budget);

        assert_eq!(
            graph.completeness(),
            AnalysisCompleteness::IndexBudgetExceeded
        );

        let run_reference = graph
            .references()
            .iter()
            .find(|r| {
                r.kind == super::super::REF_KIND_INVOCATION
                    && graph
                        .candidates_for(r)
                        .iter()
                        .any(|c| run_symbols.contains(&graph.resolve_symbol(c.symbol())))
            })
            .expect("expected to find the 'run' reference by one of its surviving candidates");
        assert_eq!(
            graph.candidates_for(run_reference).len(),
            1,
            "cap must narrow the 3-way ambiguous set to 1"
        );

        // All three "run" declarations are NameOnly confidence (no
        // distinguishing evidence whatsoever) -- every one of them,
        // including the two capped away, must still report referenced.
        for &symbol in &run_symbols {
            let dense = graph
                .dense_id_for(symbol)
                .expect("declared symbol must be interned");
            assert!(
                graph.is_symbol_referenced(dense),
                "symbol {symbol:#x} lost its referenced bit after cap truncation"
            );
            assert_eq!(
                graph.is_definitely_dead_code(dense),
                Some(false),
                "a referenced symbol must never be reported as (even possibly) dead"
            );
        }
    }

    /// Story #1835 AC2 (RED against unmodified code --
    /// `intern_declarations_and_attach_signatures` never calls
    /// `add_visibility`, so the `LocalIndex.visibilities` entry set here
    /// never reaches the built graph and this stays `None`): proves
    /// visibility survives the FULL extraction-to-CSR pipeline, not just
    /// the isolated `CodeGraphBuilder` unit tested in `code_graph.rs`.
    #[test]
    fn unreferenced_private_declaration_reports_definitely_dead_after_binding_end_to_end() {
        use crate::graph::extract::local_index::Visibility;

        let mut index = LocalIndex::new();
        let symbol = make_symbol_id(1, 0);
        index.declarations.push(method_decl("hidden", 1, 0, None));
        index.visibilities.insert(symbol, Visibility::Private);

        let files = vec![file(1, "java", index)];
        let graph = bind_with_budget(files, &IndexBudget::unlimited());

        let dense = graph
            .dense_id_for(symbol)
            .expect("declared symbol must be interned");
        assert!(
            !graph.is_symbol_referenced(dense),
            "fixture sanity: hidden() must have zero callers"
        );
        assert_eq!(
            graph.is_definitely_dead_code(dense),
            Some(true),
            "an unreferenced PRIVATE declaration's visibility must survive end-to-end from \
             LocalIndex.visibilities through the builder into a real dead-code verdict"
        );
    }

    /// Bug #1858: proves `DeclarationKind` survives the FULL
    /// extraction-to-CSR pipeline (not just the isolated
    /// `CodeGraphBuilder` unit tests in `code_graph.rs`), on BOTH a normal
    /// and a genuinely budget-exceeded real `bind_with_budget` call --
    /// mirroring `unreferenced_private_declaration_reports_definitely_dead_after_binding_end_to_end`
    /// (visibility's end-to-end proof) and reusing `dup_pair`'s
    /// budget-overage shape from
    /// `signatures_are_present_when_budget_is_not_exceeded_and_dropped_when_it_is`.
    /// Unlike that signature test, kind must NOT be dropped under budget
    /// pressure -- it must behave like visibility, not like signatures.
    #[test]
    fn declaration_kind_is_retained_end_to_end_through_the_real_extraction_to_csr_pipeline_regardless_of_budget_pressure(
    ) {
        use crate::graph::extract::local_index::{Declaration, DeclarationKind};

        fn field_decl(name: &str, file_id: u32, local: u32) -> Declaration {
            Declaration {
                kind: DeclarationKind::Field,
                name: name.to_string(),
                line: 1,
                symbol: make_symbol_id(file_id, local),
                param_count: None,
                param_types: Vec::new(),
                is_varargs: false,
            }
        }

        let mut index = LocalIndex::new();
        let field_symbol = make_symbol_id(1, 0);
        let method_symbol = make_symbol_id(1, 1);
        index.declarations.push(field_decl("count", 1, 0));
        index.declarations.push(method_decl("hidden", 1, 1, None));

        let unlimited_graph =
            bind_with_budget(vec![file(1, "java", index)], &IndexBudget::unlimited());
        assert_eq!(
            unlimited_graph.completeness(),
            AnalysisCompleteness::Complete
        );
        let field_dense = unlimited_graph
            .dense_id_for(field_symbol)
            .expect("field must be interned");
        let method_dense = unlimited_graph
            .dense_id_for(method_symbol)
            .expect("method must be interned");
        assert_eq!(
            unlimited_graph.kind_for(field_dense),
            Some(DeclarationKind::Field),
            "a Field declaration's kind must survive end-to-end from LocalIndex.declarations \
             through the builder into a real CodeGraph"
        );
        assert_eq!(
            unlimited_graph.kind_for(method_dense),
            Some(DeclarationKind::Method)
        );

        let mut index_for_exceeded = LocalIndex::new();
        index_for_exceeded
            .declarations
            .push(field_decl("count", 1, 0));
        let (dup_a, dup_b) = dup_pair();
        let exceeded_graph = bind_with_budget(
            vec![
                file(1, "java", index_for_exceeded),
                file(2, "java", dup_a),
                file(3, "java", dup_b),
            ],
            &IndexBudget::new(0, 5),
        );
        assert_eq!(
            exceeded_graph.completeness(),
            AnalysisCompleteness::IndexBudgetExceeded
        );
        let field_dense_exceeded = exceeded_graph
            .dense_id_for(field_symbol)
            .expect("field must be interned");
        assert_eq!(
            exceeded_graph.kind_for(field_dense_exceeded),
            Some(DeclarationKind::Field),
            "declaration kind must survive a genuinely budget-exceeded real bind, mirroring \
             visibility retention -- it must never be dropped like signatures are"
        );
    }

    fn solo_declared_symbol_with_signature() -> LocalIndex {
        let mut index = LocalIndex::new();
        index.declarations.push(method_decl("solo", 1, 0, None));
        index
            .signatures
            .insert(make_symbol_id(1, 0), "solo()".to_string());
        index
    }

    fn dup_pair() -> (LocalIndex, LocalIndex) {
        let mut dup_a = LocalIndex::new();
        dup_a.declarations.push(method_decl("dup", 2, 0, None));
        let mut dup_b = LocalIndex::new();
        dup_b.declarations.push(method_decl("dup", 3, 0, None));
        dup_b.invocations.push(invocation("dup", None));
        (dup_a, dup_b)
    }

    /// AC6 ladder step 1 ("drop snippets first"): unconditional on ANY
    /// repo-wide overage, independent of whether THIS particular symbol's
    /// own reference ever needed capping -- demonstrated here with an
    /// unrelated, unambiguous "dup" pair that alone pushes the raw total
    /// over budget while "solo" itself is never even referenced.
    #[test]
    fn signatures_are_present_when_budget_is_not_exceeded_and_dropped_when_it_is() {
        let unlimited_graph = bind_with_budget(
            vec![file(1, "java", solo_declared_symbol_with_signature())],
            &IndexBudget::unlimited(),
        );
        let dense = unlimited_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
        // Bug #1904: `method_decl`'s fixture shape uses `param_count: None`
        // (arity untracked in this minimal test helper -- a real extractor
        // always sets `Some`, see `widen_method_signature`'s doc comment),
        // so `param_types.len() (0) != param_count.unwrap_or(usize::MAX)`
        // and the widening falls back to the arity-only form; no
        // `MethodOwnerRecord` is set here either, so there is no declaring-
        // type prefix. This test's own purpose (presence-vs-dropped under
        // budget pressure) is unaffected by the CONTENT of the string.
        assert_eq!(unlimited_graph.signature_for(dense), Some("solo(0 params)"));
        assert_eq!(
            unlimited_graph.completeness(),
            AnalysisCompleteness::Complete
        );

        let (dup_a, dup_b) = dup_pair();
        let exceeded_graph = bind_with_budget(
            vec![
                file(1, "java", solo_declared_symbol_with_signature()),
                file(2, "java", dup_a),
                file(3, "java", dup_b),
            ],
            &IndexBudget::new(0, 5),
        );
        assert_eq!(
            exceeded_graph.completeness(),
            AnalysisCompleteness::IndexBudgetExceeded
        );
        let dense = exceeded_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
        assert_eq!(
            exceeded_graph.signature_for(dense),
            None,
            "snippets must be dropped once the budget is exceeded, even for an unrelated symbol"
        );
    }

    /// Bug #1833: the "no reference at all" finding tier is suppressed
    /// (`None`) for an unreferenced symbol REGARDLESS of completeness --
    /// including on a `Complete` build. Before the fix this test asserted
    /// `Some(true)` for the `Complete` case, which was exactly the false
    /// certainty Bug #1833 reported live on jsoup-global: a `Complete`
    /// graph proves every in-repo file parsed cleanly, not that no
    /// external caller exists, so an unreferenced method here is
    /// indistinguishable from a library's unreferenced-in-repo public API.
    #[test]
    fn strongest_dead_code_tier_is_suppressed_regardless_of_completeness() {
        fn never_called_file() -> LocalIndex {
            let mut index = LocalIndex::new();
            index
                .declarations
                .push(method_decl("neverCalled", 1, 0, None));
            index
        }

        let complete_graph = bind_with_budget(
            vec![file(1, "java", never_called_file())],
            &IndexBudget::unlimited(),
        );
        let dense = complete_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
        assert_eq!(
            complete_graph.is_definitely_dead_code(dense),
            None,
            "an unreferenced symbol on a Complete graph must be undecidable (None), never a \
             confident Some(true) -- see Bug #1833"
        );

        let (dup_a, dup_b) = dup_pair();
        let exceeded_graph = bind_with_budget(
            vec![
                file(1, "java", never_called_file()),
                file(2, "java", dup_a),
                file(3, "java", dup_b),
            ],
            &IndexBudget::new(0, 5),
        );
        let dense = exceeded_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
        assert_eq!(
            exceeded_graph.is_definitely_dead_code(dense),
            None,
            "the 'no reference at all' tier must also be suppressed under IndexBudgetExceeded"
        );
    }

    /// `AnalysisCompleteness` must report the graph's REAL state, not
    /// always `Complete` -- `Complete` for an unlimited budget, and the
    /// SPECIFIC `IndexBudgetExceeded` variant once the ladder engages.
    #[test]
    fn completeness_reports_the_real_build_state() {
        let (dup_a, dup_b) = dup_pair();
        let complete = bind_with_budget(
            vec![file(2, "java", dup_a), file(3, "java", dup_b)],
            &IndexBudget::unlimited(),
        );
        assert_eq!(complete.completeness(), AnalysisCompleteness::Complete);

        let (dup_a, dup_b) = dup_pair();
        let exceeded = bind_with_budget(
            vec![file(2, "java", dup_a), file(3, "java", dup_b)],
            &IndexBudget::new(0, 5),
        );
        assert_eq!(
            exceeded.completeness(),
            AnalysisCompleteness::IndexBudgetExceeded
        );
    }

    /// Dual-review defect D3: `bind_with_budget_and_completeness` is the
    /// real public entry point a caller with partial-index knowledge
    /// (`repo_index::build_repo_graph`) must use. `index_is_complete =
    /// false` must disable `UNIQUE_NAME_IN_REPO`/`Confidence::Exact`
    /// end-to-end through the public API, even though `bind_with_budget`
    /// (the `index_is_complete = true` convenience wrapper) would still
    /// grant it for the identical input.
    #[test]
    fn bind_with_budget_and_completeness_disables_unique_name_shortcut_when_index_is_partial() {
        use crate::graph::confidence::Confidence;

        let files = || {
            let mut solo = LocalIndex::new();
            solo.declarations.push(method_decl("onlyOne", 1, 0, None));
            let mut caller = LocalIndex::new();
            caller.invocations.push(invocation("onlyOne", None));
            vec![file(1, "java", solo), file(2, "java", caller)]
        };

        let complete_graph =
            bind_with_budget_and_completeness(files(), &IndexBudget::unlimited(), true);
        let complete_ref = complete_graph
            .references()
            .iter()
            .find(|r| !r.is_unresolved())
            .expect("the call must resolve to something");
        let complete_candidate = &complete_graph.candidates_for(complete_ref)[0];
        assert_eq!(complete_candidate.confidence(), Confidence::Exact);

        let partial_graph =
            bind_with_budget_and_completeness(files(), &IndexBudget::unlimited(), false);
        let partial_ref = partial_graph
            .references()
            .iter()
            .find(|r| !r.is_unresolved())
            .expect("the call must still resolve to the sole indexed declaration");
        let partial_candidate = &partial_graph.candidates_for(partial_ref)[0];
        assert_ne!(
            partial_candidate.confidence(),
            Confidence::Exact,
            "a partial index must never grant Exact confidence through the public bind API"
        );
    }
}
