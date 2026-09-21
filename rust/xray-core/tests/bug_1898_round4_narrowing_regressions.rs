//! Regression coverage for the #1898 code review ROUND 4 (epic #1906, P1).
//!
//! Round 3 (the working tree this round starts from) enumerated another
//! batch of binding forms (`var`, generic type parameters, six P1-B forms)
//! but hard-narrowed EVERY resolved receiver type identically regardless of
//! how it was obtained -- so round 3 itself introduced a NEW batch of the
//! exact same defect class: `instanceof` pattern variables and locals
//! declared inside a static initializer / instance initializer / record
//! compact constructor had NO typed-name extraction at all, so their
//! receiver identifier fell through to `resolve_receiver_type`'s two
//! OPEN-WORLD fallback substrates (`unambiguous_field_type`,
//! `is_known_type_name`), which round 3 then fed into the SAME hard filter
//! as genuine, positive evidence -- deleting the real candidate whenever
//! the fallback's guess coincidentally matched (or matched nothing but the
//! filter still emptied the set) an unrelated in-repo declaration.
//!
//! This round's fix is structural, not another enumerated case: `receiver::
//! ReceiverEvidence` now tiers every resolved receiver type as `Positive`
//! (an actual `TypedNameRecord` lookup hit, or the call's own enclosing
//! type) or `Advisory` (either fallback substrate) -- only `Positive`
//! evidence may ever hard-empty the candidate set. Independently, this
//! round ALSO adds real typed-name extraction for `instanceof` type
//! patterns and for locals declared inside an initializer block/compact
//! constructor (both previously extracted NOTHING at all), so the common
//! case narrows correctly rather than merely failing safe.
//!
//! Every test here drives real, javac-shaped Java source through the REAL
//! front door -- `build_repo_graph` (not `bind()` directly) -- over a real
//! temp-dir repo, exactly the path `analyze_graph` takes in production,
//! mirroring `bug_1898_round2_narrowing_regressions.rs`'s own established
//! pattern (`NoOpCollector`, `declaration_symbol_owned_by`, `extract_index`,
//! `build_graph_over`).
//!
//! **#1898 SCOPE SPLIT (epic #1906, round-4 review of THIS round's own
//! fix, `.analysis/1898-review-rounds/round4-findings.md`)**: the
//! `Positive`/`Advisory` evidence-tier design documented above was itself
//! found unsound two rounds later than this file's own module doc claims
//! (findings 1-2: `Positive` is not closed-world either -- `receiver::
//! FileTypedNames` keys locals by `(enclosing_method, name)` while Java
//! scopes by BLOCK) and a second, independently unguarded path (findings
//! 3-4: narrowing to a non-empty WRONG subset, never guarded by either
//! tier). `apply_receiver_type_narrowing` is consequently now TAG-ONLY --
//! it never removes a candidate, empty match or not, Positive evidence or
//! Advisory. Every test below whose ORIGINAL claim was "the coincidental
//! class/field must NEVER gain a fabricated caller edge" has been renamed
//! and re-scoped to assert the actual (regressed, accepted) outcome: the
//! coincidental candidate now KEEPS that edge, pending a follow-up issue
//! (named in `docs/xray-architecture.md`'s candidate-admission section)
//! that redesigns hard receiver-type narrowing on genuinely closed-world
//! evidence. The "real target keeps its own edge" half of each test is
//! UNCHANGED and still passes -- that guarantee never depended on
//! deletion.
//!
//! **#1910 SALVAGE note (Shape I below)**: a later issue (#1910) added
//! real `record_pattern_component` extraction to this crate (kept in the
//! salvage that reverted #1910's receiver-type deletion attempt) -- so
//! Shape I's ORIGINAL claim that nothing in this codebase has ever heard
//! of `record_pattern_component` is no longer true. The test's assertion
//! is unaffected either way (`Target.helper()` keeps its real edge
//! regardless of how `handle` resolves, since receiver-type narrowing is
//! PERMANENTLY tag-only and can never delete a candidate), so the test is
//! kept, with its doc comment corrected below.

use std::path::Path;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::extract::LanguageExtractor;
use xray_core::graph::identity::{file_id, SymbolId};
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, UserFact};
use xray_core::owned_node::OwnedNode;

struct NoOpCollector;
impl FactCollector for NoOpCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        Vec::new()
    }
}

fn write_java(dir: &Path, relative_path: &str, source: &str) {
    std::fs::write(dir.join(relative_path), source).unwrap();
}

/// The symbol of the declaration named `name` whose `MethodOwnerRecord`
/// names `enclosing_type` -- mirrors `bug_1898_round2_narrowing_
/// regressions.rs`'s own helper of the same name verbatim.
fn declaration_symbol_owned_by(index: &LocalIndex, name: &str, enclosing_type: &str) -> SymbolId {
    let owner_symbols: std::collections::HashSet<SymbolId> = index
        .method_owners
        .iter()
        .filter(|o| o.enclosing_type == enclosing_type)
        .map(|o| o.method_symbol)
        .collect();
    index
        .declarations
        .iter()
        .find(|d| d.name == name && owner_symbols.contains(&d.symbol))
        .unwrap_or_else(|| {
            panic!("fixture bug: no {name:?} declaration owned by {enclosing_type:?}")
        })
        .symbol
}

fn extract_index(dir: &Path, relative_path: &str) -> LocalIndex {
    let full_path = dir.join(relative_path);
    let root = xray_core::scanner::parse_file(&full_path).expect("fixture source must parse");
    JavaExtractor.extract(&root, file_id(relative_path))
}

/// Runs `build_repo_graph` (the REAL front-door entry point `analyze_graph`
/// uses) over every `.java` file in `dir`, with an unbounded budget.
fn build_graph_over(dir: &Path, relative_paths: &[&str]) -> xray_core::graph::csr::CodeGraph {
    let options = RepoIndexOptions {
        budget: IndexBudget::unlimited(),
        max_files: None,
    };
    let paths: Vec<String> = relative_paths.iter().map(|p| p.to_string()).collect();
    let result = build_repo_graph(dir, &paths, &options, &NoOpCollector)
        .expect("no file_id collision in this fixture");
    result.graph
}

/// Shared assertion: `owner_index`'s `name` declaration (owned by
/// `enclosing_type`) must keep a real caller edge in `graph`, and must
/// never be reported definitely dead.
fn assert_keeps_a_real_caller_edge(
    graph: &xray_core::graph::csr::CodeGraph,
    owner_index: &LocalIndex,
    name: &str,
    enclosing_type: &str,
) {
    let symbol = declaration_symbol_owned_by(owner_index, name, enclosing_type);
    let dense = graph
        .dense_id_for(symbol)
        .unwrap_or_else(|| panic!("{enclosing_type}.{name} must be interned"));
    assert_ne!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "{enclosing_type}.{name}() is genuinely called and must never be reported \
         definitely dead"
    );
    assert!(
        !graph.callers_index(dense).is_empty(),
        "{enclosing_type}.{name}() must keep its real caller edge"
    );
}

/// Shared assertion (#1898 scope split, epic #1906): `owner_index`'s
/// `name` declaration (owned by `enclosing_type`) is a coincidentally
/// same-named candidate that PRE-split was excluded by `apply_receiver_
/// type_narrowing`'s hard filter. Post-split, that filter is tag-only and
/// never removes anything, so this candidate now KEEPS a caller edge from
/// the receiver-type collision -- an accepted regression, not silently
/// dropped from the suite's coverage, pending the receiver-type
/// hard-narrowing follow-up issue named in `docs/xray-architecture.md`'s
/// candidate-admission section.
fn assert_now_carries_the_accepted_regression_edge(
    graph: &xray_core::graph::csr::CodeGraph,
    owner_index: &LocalIndex,
    name: &str,
    enclosing_type: &str,
) {
    let symbol = declaration_symbol_owned_by(owner_index, name, enclosing_type);
    let dense = graph
        .dense_id_for(symbol)
        .unwrap_or_else(|| panic!("{enclosing_type}.{name} must be interned"));
    assert!(
        !graph.callers_index(dense).is_empty(),
        "accepted regression (#1898 scope split): receiver-type narrowing is tag-only now, \
         so the unrelated {enclosing_type}.{name}() keeps this fabricated caller edge until \
         the receiver-type hard-narrowing follow-up issue lands"
    );
}

const HANDLE_COLLISION_CLASS: &str =
    "package m;\npublic class handle {\n    public static void helper() {}\n}\n";

// ---------------------------------------------------------------------
// Shape A: instanceof pattern variable (regular), class-name collision
// ---------------------------------------------------------------------

/// Round 4's own headline shape: `if (o instanceof Target handle) {
/// handle.helper(); }` where `Target` is the SAME type as the enclosing
/// class (so `Target.helper()` is legally callable even though it is
/// `private`), and a coincidentally same-named class `handle` also exists
/// in the repo. This round's own fix (real typed-name extraction for
/// `instanceof` pattern variables) still correctly resolves the receiver
/// to `Target` -- `Target.helper()` keeps its real edge below -- but the
/// #1898 SCOPE SPLIT (see this file's module doc) means the coincidental
/// class `handle` is no longer excluded from the candidate set either:
/// `apply_receiver_type_narrowing` is tag-only, so `handle.helper()` now
/// ALSO keeps a fabricated caller edge, an accepted regression pending
/// the receiver-type hard-narrowing follow-up.
#[test]
fn instanceof_pattern_variable_reaches_the_real_type_and_the_coincidental_class_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "handle.java", HANDLE_COLLISION_CLASS);
    write_java(
        dir.path(),
        "Target.java",
        "package m;\npublic class Target {\n    private void helper() {}\n    void run(Object o) {\n        if (o instanceof Target handle) {\n            handle.helper();\n        }\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["handle.java", "Target.java"]);
    let handle_index = extract_index(dir.path(), "handle.java");
    let target_index = extract_index(dir.path(), "Target.java");

    assert_keeps_a_real_caller_edge(&graph, &target_index, "helper", "Target");
    assert_now_carries_the_accepted_regression_edge(&graph, &handle_index, "helper", "handle");
}

// ---------------------------------------------------------------------
// Shape B: static initializer local, class-name collision
// ---------------------------------------------------------------------

/// A local declared inside a `static { ... }` initializer block. This
/// round's own fix (the generic "block reached while `enclosing_method`
/// is still `None` gets a synthetic scope" rule) still correctly resolves
/// the receiver to `Target` -- see the #1898 scope-split note on shape A
/// above for why the coincidental class `handle` now ALSO keeps a
/// fabricated edge (accepted regression, not silently dropped coverage).
#[test]
fn static_initializer_local_reaches_the_real_type_and_the_coincidental_class_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "handle.java", HANDLE_COLLISION_CLASS);
    write_java(
        dir.path(),
        "Target.java",
        "package m;\npublic class Target {\n    private void helper() {}\n    static {\n        Target handle = new Target();\n        handle.helper();\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["handle.java", "Target.java"]);
    let handle_index = extract_index(dir.path(), "handle.java");
    let target_index = extract_index(dir.path(), "Target.java");

    assert_keeps_a_real_caller_edge(&graph, &target_index, "helper", "Target");
    assert_now_carries_the_accepted_regression_edge(&graph, &handle_index, "helper", "handle");
}

// ---------------------------------------------------------------------
// Shape C: instance initializer local, class-name collision
// ---------------------------------------------------------------------

/// Same shape as B, mirrored onto a bare instance initializer block (`{
/// ... }` directly inside the class body, no `static` keyword) -- a
/// DIFFERENT real grammar node than `static_initializer` (verified via a
/// throwaway AST-dump probe: it is literally a bare `block` node, a
/// direct child of `class_body`), so this is a genuinely independent case
/// even though both are fixed by the SAME generic "block reached while
/// enclosing_method is None" rule. See the #1898 scope-split note on
/// shape A above for the accepted regression on the coincidental class.
#[test]
fn instance_initializer_local_reaches_the_real_type_and_the_coincidental_class_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "handle.java", HANDLE_COLLISION_CLASS);
    write_java(
        dir.path(),
        "Target.java",
        "package m;\npublic class Target {\n    private void helper() {}\n    {\n        Target handle = new Target();\n        handle.helper();\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["handle.java", "Target.java"]);
    let handle_index = extract_index(dir.path(), "handle.java");
    let target_index = extract_index(dir.path(), "Target.java");

    assert_keeps_a_real_caller_edge(&graph, &target_index, "helper", "Target");
    assert_now_carries_the_accepted_regression_edge(&graph, &handle_index, "helper", "handle");
}

// ---------------------------------------------------------------------
// Shape D: record compact constructor local, class-name collision
// ---------------------------------------------------------------------

/// A local declared inside a record's compact constructor
/// (`compact_constructor_declaration` -- verified a DIFFERENT real
/// grammar node from `constructor_declaration`, so `dispatch_node`'s
/// existing "method_declaration | constructor_declaration" arm never
/// caught it either). `Target2.helper()` is `public` (rather than
/// `private`, unlike shapes A-C/E-G) so the call from `MyRecord`'s own
/// compact constructor -- a DIFFERENT top-level type -- stays legal Java.
/// See the #1898 scope-split note on shape A above for the accepted
/// regression on the coincidental class.
#[test]
fn record_compact_constructor_local_reaches_the_real_type_and_the_coincidental_class_now_regresses(
) {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "handle.java", HANDLE_COLLISION_CLASS);
    write_java(
        dir.path(),
        "Target2.java",
        "package m;\npublic class Target2 {\n    public void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "MyRecord.java",
        "package m;\npublic record MyRecord(int x) {\n    public MyRecord {\n        Target2 handle = new Target2();\n        handle.helper();\n    }\n}\n",
    );

    let graph = build_graph_over(
        dir.path(),
        &["handle.java", "Target2.java", "MyRecord.java"],
    );
    let handle_index = extract_index(dir.path(), "handle.java");
    let target2_index = extract_index(dir.path(), "Target2.java");

    assert_keeps_a_real_caller_edge(&graph, &target2_index, "helper", "Target2");
    assert_now_carries_the_accepted_regression_edge(&graph, &handle_index, "helper", "handle");
}

// ---------------------------------------------------------------------
// Shape E: negated instanceof pattern (flow scope), class-name collision
// ---------------------------------------------------------------------

/// `if (!(o instanceof Target handle)) { return; } handle.helper();` --
/// the pattern binding's real Java scope only extends past the guard
/// (negated flow-scope narrowing), but this extractor's typed-name model
/// is deliberately METHOD-granular (not flow-sensitive), the same
/// simplification every other local-typed-name record already makes --
/// `instanceof_pattern_typed_name` does not special-case negation at all,
/// proving that choice was correct: the SAME extraction that handles
/// shape A handles this automatically. See the #1898 scope-split note on
/// shape A above for the accepted regression on the coincidental class.
#[test]
fn negated_instanceof_pattern_reaches_the_real_type_and_the_coincidental_class_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "handle.java", HANDLE_COLLISION_CLASS);
    write_java(
        dir.path(),
        "Target.java",
        "package m;\npublic class Target {\n    private void helper() {}\n    void run(Object o) {\n        if (!(o instanceof Target handle)) {\n            return;\n        }\n        handle.helper();\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["handle.java", "Target.java"]);
    let handle_index = extract_index(dir.path(), "handle.java");
    let target_index = extract_index(dir.path(), "Target.java");

    assert_keeps_a_real_caller_edge(&graph, &target_index, "helper", "Target");
    assert_now_carries_the_accepted_regression_edge(&graph, &handle_index, "helper", "handle");
}

// ---------------------------------------------------------------------
// Shapes F/G: initializer-block and instanceof locals, FIELD-name
// collision (rather than a class-name collision) -- the
// `unambiguous_field_type` fallback specifically, not `is_known_type_name`
// ---------------------------------------------------------------------

/// `Other.handle` is an ordinary FIELD (declared type `Wrong`) -- the
/// UNAMBIGUOUS-FIELD-TYPE fallback specifically (not the static-type-name
/// one shapes A-E exercise). Shared by both F (static initializer) and G
/// (instanceof pattern) below.
fn write_field_collision_fixture(dir: &Path) {
    write_java(
        dir,
        "Wrong.java",
        "package m;\npublic class Wrong {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir,
        "Other.java",
        "package m;\npublic class Other {\n    Wrong handle;\n}\n",
    );
}

/// Shape F: a static-initializer local, where the coincidental collision
/// is with an ordinary FIELD name elsewhere in the repo (`Other.handle`,
/// declared type `Wrong`) rather than a class name. `Target.helper()` is
/// `private` -- the "private target" variant named in the code review.
/// See the #1898 scope-split note on shape A above for the accepted
/// regression (here, on the coincidental field's type `Wrong`).
#[test]
fn static_initializer_local_reaches_the_real_type_and_the_coincidental_field_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_field_collision_fixture(dir.path());
    write_java(
        dir.path(),
        "Target.java",
        "package m;\npublic class Target {\n    private void helper() {}\n    static {\n        Target handle = new Target();\n        handle.helper();\n    }\n}\n",
    );

    let graph = build_graph_over(
        dir.path(),
        &["Wrong.java", "Other.java", "Target.java"],
    );
    let wrong_index = extract_index(dir.path(), "Wrong.java");
    let target_index = extract_index(dir.path(), "Target.java");

    assert_keeps_a_real_caller_edge(&graph, &target_index, "helper", "Target");
    assert_now_carries_the_accepted_regression_edge(&graph, &wrong_index, "helper", "Wrong");
}

/// Shape G: an instanceof pattern local, same field-name collision as F.
/// The "private target" variant named in the code review. See the #1898
/// scope-split note on shape A above for the accepted regression.
#[test]
fn instanceof_pattern_reaches_the_real_type_and_the_coincidental_field_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_field_collision_fixture(dir.path());
    write_java(
        dir.path(),
        "Target.java",
        "package m;\npublic class Target {\n    private void helper() {}\n    void run(Object o) {\n        if (o instanceof Target handle) {\n            handle.helper();\n        }\n    }\n}\n",
    );

    let graph = build_graph_over(
        dir.path(),
        &["Wrong.java", "Other.java", "Target.java"],
    );
    let wrong_index = extract_index(dir.path(), "Wrong.java");
    let target_index = extract_index(dir.path(), "Target.java");

    assert_keeps_a_real_caller_edge(&graph, &target_index, "helper", "Target");
    assert_now_carries_the_accepted_regression_edge(&graph, &wrong_index, "helper", "Wrong");
}

// ---------------------------------------------------------------------
// Shape H: external receiver, MATCHING arity -- PRE-split, proved the
// hard filter under POSITIVE evidence still discriminated this case; the
// #1898 scope split (see this file's module doc) makes that guarantee
// EXPLICITLY ACCEPTED DEBT (named in the task that authorized this split)
// -- receiver-type narrowing is tag-only, so a same-arity coincidence
// with an EXTERNAL receiver is no longer excluded either.
// ---------------------------------------------------------------------

/// `connection.commit()` where `connection`'s declared type `Connection`
/// is a genuine PARAMETER typed-name record (POSITIVE evidence, no
/// fallback involved at all) resolving to a type that is NOT declared
/// anywhere in this repo. `Bookkeeper.commit()` shares the bare name AND
/// the exact arity (0 params, 0 args) -- arity narrowing alone cannot
/// discriminate this case, and receiver-type narrowing no longer can
/// either (tag-only, never deletes): `Bookkeeper.commit()` now keeps a
/// fabricated caller edge from `connection.commit()`, an EXPLICITLY
/// ACCEPTED regression (named in the #1898 scope-split task) pending the
/// receiver-type hard-narrowing follow-up issue named in `docs/
/// xray-architecture.md`'s candidate-admission section.
#[test]
fn external_receiver_with_matching_arity_now_keeps_the_fabricated_edge_pending_the_followup() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Bookkeeper.java",
        "package m;\npublic class Bookkeeper {\n    void commit() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m;\nclass Caller {\n    void run(Connection connection) {\n        connection.commit();\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["Bookkeeper.java", "Caller.java"]);
    let bookkeeper_index = extract_index(dir.path(), "Bookkeeper.java");

    assert_now_carries_the_accepted_regression_edge(
        &graph,
        &bookkeeper_index,
        "commit",
        "Bookkeeper",
    );
}

// ---------------------------------------------------------------------
// Shape I: a receiver binding form this round did NOT special-case at
// all when originally written (a Java 21 record-pattern deconstruction
// component, `instanceof Wrapper(Target handle)`) -- proving the
// INVERTED CONTRACT degrades safely for a form nobody had taught the
// extractor about, rather than relying on enumerating one more shape.
// ---------------------------------------------------------------------

/// `instanceof Wrapper(Target handle)` binds `handle` via a
/// `record_pattern_component`. **#1910 SALVAGE update**: a later issue
/// (#1910) added real extraction for this exact node kind
/// (`java_receiver::record_pattern_component_typed_name`, kept in the
/// salvage that reverted #1910's receiver-type DELETION attempt) -- so
/// `handle` now DOES get real `TypedNameRecord` evidence (`Positive
///("Target")`), unlike when this test was first written. The assertion
/// below is UNCHANGED and still holds for a DIFFERENT, now more
/// fundamental reason: `Target.helper()` keeps its real caller edge
/// regardless of how `handle` resolves, because receiver-type narrowing
/// is PERMANENTLY tag-only (round 7's review proved it can never be made
/// a safe hard filter on this substrate) -- so this test no longer
/// depends on a binding form being "uncovered" at all, only on the
/// tag-only contract itself. `Wrong.java`/`Other.java` are kept as
/// harmless unrelated fixtures from the test's original shape (a
/// coincidental field-name collision that no longer has any bearing on
/// the outcome either way).
#[test]
fn an_uncovered_receiver_binding_form_never_deletes_the_real_candidate() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Wrong.java",
        "package m;\npublic class Wrong {\n}\n",
    );
    write_java(
        dir.path(),
        "Other.java",
        "package m;\npublic class Other {\n    Wrong handle;\n}\n",
    );
    write_java(
        dir.path(),
        "Target.java",
        "package m;\npublic class Target {\n    public void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Wrapper.java",
        "package m;\npublic record Wrapper(Target t) {}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m;\npublic class Caller {\n    void run(Object o) {\n        if (o instanceof Wrapper(Target handle)) {\n            handle.helper();\n        }\n    }\n}\n",
    );

    let graph = build_graph_over(
        dir.path(),
        &[
            "Wrong.java",
            "Other.java",
            "Target.java",
            "Wrapper.java",
            "Caller.java",
        ],
    );
    let target_index = extract_index(dir.path(), "Target.java");

    assert_keeps_a_real_caller_edge(&graph, &target_index, "helper", "Target");
}
