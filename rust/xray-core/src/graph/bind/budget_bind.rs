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
///
/// Bug #1929 item 1: `language` (the file extension string `FileForBind`
/// already carries, e.g. `"java"`/`"kt"`/`"kts"`) selects the REAL
/// per-language spelling for a varargs parameter -- see
/// `format_param_types` below. Without this, a varargs method's cached
/// signature rendered its variadic parameter as a bare type name
/// (`consumeToAny(char)`), textually indistinguishable from a genuine
/// one-arg overload and breaking the documented substring-match recipe on
/// `signature_for`.
///
/// Bug #1929 rework item 2 (Codex P2, closes #1939): reads
/// `declaration.vararg_index` -- a REAL typed field on `Declaration`
/// (`local_index.rs`), populated at extraction time -- directly, never
/// assumed to be the last parameter. Kotlin allows a `vararg` parameter
/// at ANY position, unlike Java (JLS 8.4.1's "always last" rule is
/// Java-specific); `java_methods::extract_method_declaration` computes
/// the Java case as `param_types.len() - 1` (always valid, since Java
/// guarantees "last"), while Kotlin's extractor
/// (`kotlin::extract_param_types_and_varargs`) tracks the REAL position
/// as it scans parameters. This superseded an earlier, since-deleted
/// in-band `\u{0}vararg_index=N` text marker that had to smuggle this
/// same fact through the existing cached-signature string because
/// `Declaration` had no field for it yet (`local_index.rs` was owned by
/// a concurrent story at the time) -- now that the field exists, no
/// marker or cached-signature parsing is needed at all.
fn widen_method_signature(declaration: &Declaration, owner: Option<&str>, language: &str) -> Option<String> {
    if declaration.kind != DeclarationKind::Method {
        return None;
    }
    let params_text = if declaration.param_types.len() == declaration.param_count.unwrap_or(usize::MAX) {
        format_param_types(&declaration.param_types, declaration.vararg_index, language)
    } else {
        format!("{} params", declaration.param_count.unwrap_or(0))
    };
    Some(match owner {
        Some(owner) => format!("{owner}.{}({params_text})", declaration.name),
        None => format!("{}({params_text})", declaration.name),
    })
}

/// Bug #1929 item 1 (rework item 2, closes #1939): renders `param_types`
/// as the signature's parameter list, spelling the entry at
/// `vararg_index` with its REAL per-language varargs syntax when `Some`
/// -- at whatever position it ACTUALLY occupies, never assumed to be
/// last (see `widen_method_signature`'s own doc comment for why Kotlin
/// needs this and Java does not). Java spells it with a trailing
/// ellipsis (`char...`, the real `char... chars` source syntax); Kotlin
/// spells it with a leading `vararg` keyword (`vararg Int`, the real
/// `vararg xs: Int` source syntax) -- rendering the Java spelling for a
/// Kotlin declaration (or vice versa) would itself be a fabricated,
/// non-real-source spelling (Rule 10). `language` is matched against
/// `"kt"`/`"kts"` (the literal file-extension strings
/// `FileForBind::language` carries, per `graph::bind::mod`'s own
/// `file.language == "java"` convention) rather than a normalized
/// language name, since no normalization step exists anywhere in this
/// pipeline. `vararg_index` is `None` for the (safe, expected) case of a
/// non-varargs declaration or an extractor that could not determine the
/// position -- that renders the plain, unmarked parameter list. A
/// `Some(idx)` that points OUTSIDE `param_types`, however, is no longer
/// a benign case to silently swallow: every real extractor site
/// populates `vararg_index` from the SAME `param_types` it hands to this
/// function, so an out-of-bounds index can only mean the extractor
/// itself violated that invariant -- a compilation-infrastructure bug,
/// not a data-format mismatch, so this fails loudly instead of guessing
/// "no vararg" and silently mis-rendering the signature.
fn format_param_types(param_types: &[String], vararg_index: Option<usize>, language: &str) -> String {
    let Some(idx) = vararg_index else {
        return param_types.join(", ");
    };
    assert!(
        idx < param_types.len(),
        "vararg_index ({idx}) is outside param_types (len {}) -- this is an extractor \
         invariant violation, never a benign 'no vararg' case",
        param_types.len()
    );
    param_types
        .iter()
        .enumerate()
        .map(|(i, t)| {
            if i == idx {
                if matches!(language, "kt" | "kts") {
                    format!("vararg {t}")
                } else {
                    format!("{t}...")
                }
            } else {
                t.clone()
            }
        })
        .collect::<Vec<_>>()
        .join(", ")
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
                let widened =
                    widen_method_signature(declaration, owner, file.language.as_str()).unwrap_or_else(|| signature.clone());
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
#[path = "budget_bind_tests.rs"]
mod tests;

/// Second half of `budget_bind.rs`'s relocated unit tests -- see
/// `budget_bind_tests.rs` for the first half (widening/varargs) and the
/// split rationale (Messi Rule 6, anti-file-bloat; mirrors `resolve.rs`'s
/// own established `mod tests;`/`mod tests_family;` multi-file test
/// split). This half covers the AC6 budget-ladder tests (ambiguous
/// candidate capping, visibility/kind/signature retention under budget
/// pressure, and the `index_is_complete` unique-name shortcut).
#[cfg(test)]
#[path = "budget_bind_tests_ac6.rs"]
mod tests_ac6;
