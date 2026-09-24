//! Regression coverage for the #1898 code review ROUND 2 (epic #1906,
//! P1). Round 1 (`bug_1898_narrowing_regressions.rs`) removed the
//! "no candidate matches -> keep the entire bare-name pool" fallback from
//! `apply_arity_narrowing`/`apply_receiver_type_narrowing`/`apply_same_
//! class_or_super_narrowing`. Round 2 found the round-1 fix itself had two
//! NEW defects, both traced to `bind/receiver.rs::resolve_receiver_type`
//! feeding `apply_receiver_type_narrowing`'s now-hard filter a name that
//! is not a real type at all:
//!
//! - **P1-A**: `var` (Java 10+ local-variable type inference) and a
//!   generic type parameter (`T` in `<T extends Svc> void run(T t)`) both
//!   occupy a declared-type-node position, but neither is a narrowable
//!   class -- trusting either as the receiver type hard-empties every
//!   real candidate.
//! - **P1-B**: six binding forms the extractor had NO typed-name coverage
//!   for at all (enhanced-for, try-with-resources, catch parameter,
//!   lambda parameter, inner-class field access, inherited field) could
//!   fall through to `resolve_receiver_type`'s static-type-name fallback,
//!   which misresolves the identifier into an UNRELATED same-named
//!   in-repo type whenever one happens to exist.
//!
//! Every test here drives real, javac-shaped Java source through the
//! REAL front door -- `build_repo_graph` (not `bind()` directly) -- over
//! a real temp-dir repo, exactly the path `analyze_graph` takes in
//! production. No hand-built `LocalIndex`, no mocking of the extractor,
//! binder, or graph under test. `NoOpCollector` below satisfies
//! `build_repo_graph`'s mandatory `fact_collector` parameter for an
//! orthogonal, unrelated concern (user-fact collection) these tests do
//! not exercise -- the same pre-existing pattern
//! `ac4_bind_confidence_candidates.rs`/`bug_1898_narrowing_
//! regressions.rs`/`repo_index.rs`'s own tests already use.
//!
//! **#1898 SCOPE SPLIT (epic #1906, round-4 review, `.analysis/
//! 1898-review-rounds/round4-findings.md`)**: `apply_receiver_type_
//! narrowing` is now TAG-ONLY -- it never removes a candidate, empty
//! match or not, Positive evidence or Advisory (the `Positive`/`Advisory`
//! tiering this file's P1-A/P1-B fixtures were built to test was itself
//! found unsound: `Positive` is not closed-world either, and neither tier
//! ever guarded narrowing to a non-empty WRONG subset). Five of the P1-B
//! forms below (enhanced-for, try-with-resources, catch parameter,
//! inner-class field access, inherited field) originally asserted the
//! coincidentally same-named class/field could NEVER gain a fabricated
//! caller edge; that guarantee is retired and those five tests are
//! renamed/re-scoped to assert the actual (accepted-regression) outcome,
//! pending the receiver-type hard-narrowing follow-up issue named in
//! `docs/xray-architecture.md`'s candidate-admission section. The
//! genuinely-ambiguous lambda-parameter form (which never claimed
//! exclusion in the first place) and the `var`/generic-type-parameter
//! forms (which resolve via the unique-name shortcut, never receiver-type
//! narrowing at all) are UNCHANGED.

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
/// names `enclosing_type` -- mirrors `bug_1898_narrowing_regressions.rs`'s
/// own helper of the same name verbatim (each integration-test binary
/// already duplicates this small lookup). Extracted independently via the
/// real `JavaExtractor`, purely to look up the symbol for a `dense_id_for`
/// query -- the ACTUAL graph under test is always the one
/// `build_repo_graph` returns, never this standalone re-extraction.
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

/// Runs `build_repo_graph` (the REAL front-door entry point
/// `analyze_graph` uses, per the round-2 review's explicit demand) over
/// every `.java` file in `dir`, with an unbounded budget.
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

// ---------------------------------------------------------------------
// P1-A: `var` pseudo-type
// ---------------------------------------------------------------------

/// P1-A's own proven fixture (round-2 review, verbatim): `var`-typed
/// receivers calling BOTH a private method of the enclosing class
/// (`self.helper()`) and an in-repo type's method (`svc.ping()`). Before
/// the fix, `resolve_receiver_type` returned the literal string `"var"`
/// for both receivers, which `apply_receiver_type_narrowing`'s hard
/// filter then matched against nothing, deleting every real candidate --
/// `Caller.helper()` flipped `Some(false)` -> a FALSE `Some(true)`, and
/// `Svc.ping()` lost its only caller.
#[test]
fn var_typed_receiver_reaches_both_a_private_enclosing_method_and_an_in_repo_types_method() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Svc.java",
        "package m;\npublic class Svc {\n    public void ping() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m;\nclass Caller {\n    private void helper() {}\n    void run() {\n        var svc = new Svc();\n        svc.ping();\n        var self = this;\n        self.helper();\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["Svc.java", "Caller.java"]);
    let caller_index = extract_index(dir.path(), "Caller.java");
    let svc_index = extract_index(dir.path(), "Svc.java");

    let helper = declaration_symbol_owned_by(&caller_index, "helper", "Caller");
    let helper_dense = graph.dense_id_for(helper).expect("Caller.helper must be interned");
    assert_ne!(
        graph.is_definitely_dead_code(helper_dense),
        Some(true),
        "Caller.helper() is genuinely called from run() via a var-typed `self` receiver and \
         must never be reported definitely dead"
    );
    assert!(
        !graph.callers_index(helper_dense).is_empty(),
        "Caller.helper() must keep its real caller edge from self.helper()"
    );

    let ping = declaration_symbol_owned_by(&svc_index, "ping", "Svc");
    let ping_dense = graph.dense_id_for(ping).expect("Svc.ping must be interned");
    assert!(
        !graph.callers_index(ping_dense).is_empty(),
        "Svc.ping() must keep its real caller edge from a var-typed `svc` receiver"
    );
}

// ---------------------------------------------------------------------
// P1-A: generic type parameter
// ---------------------------------------------------------------------

/// P1-A's second proven fixture: a generic type parameter (`<T extends
/// Svc> void run(T t)`) receiver. Before the fix, `resolve_receiver_type`
/// returned the literal string `"T"` (the type parameter's own bare
/// name), which the hard filter matched against nothing, deleting
/// `Svc.ping()`'s only caller.
#[test]
fn generic_type_parameter_receiver_reaches_the_bound_types_method() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Svc.java",
        "package m;\npublic class Svc {\n    public void ping() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m;\nclass Caller {\n    <T extends Svc> void run(T t) {\n        t.ping();\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["Svc.java", "Caller.java"]);
    let svc_index = extract_index(dir.path(), "Svc.java");

    let ping = declaration_symbol_owned_by(&svc_index, "ping", "Svc");
    let ping_dense = graph.dense_id_for(ping).expect("Svc.ping must be interned");
    assert!(
        !graph.callers_index(ping_dense).is_empty(),
        "Svc.ping() must keep its real caller edge from a generic-type-parameter receiver `t`"
    );
}

// ---------------------------------------------------------------------
// P1-B, form 1 (dead-verdict flip): enhanced-for loop variable
// ---------------------------------------------------------------------

/// P1-B's own proven fixture (round-2 review, verbatim): the enhanced-for
/// loop variable `handle` is declared `Caller` (the REAL type), but a
/// coincidentally same-named top-level class `handle` also exists in the
/// repo. Before the fix, the enhanced-for loop variable had NO typed-name
/// evidence at all, so `resolve_receiver_type`'s static-type-name
/// fallback misresolved `handle` as the CLASS `handle` -- deleting the
/// real edge to `Caller.close()` (flipping its dead-code verdict to a
/// FALSE `Some(true)`) and fabricating one to `handle.close()` instead.
/// #1898 SCOPE SPLIT (see this file's module doc): `Caller.close()` still
/// keeps its real edge (this round's typed-name fix is unchanged), but
/// `handle.close()` now ALSO keeps a fabricated edge -- receiver-type
/// narrowing is tag-only and no longer excludes it, an accepted
/// regression pending the hard-narrowing follow-up.
#[test]
fn enhanced_for_loop_variable_reaches_the_real_type_and_the_coincidental_class_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "handle.java",
        "package m;\npublic class handle {\n    public static void close() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m;\nimport java.util.List;\nclass Caller {\n    private void close() {}\n    void run(List<Caller> list) {\n        for (Caller handle : list) {\n            handle.close();\n        }\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["handle.java", "Caller.java"]);
    let handle_index = extract_index(dir.path(), "handle.java");
    let caller_index = extract_index(dir.path(), "Caller.java");

    let caller_close = declaration_symbol_owned_by(&caller_index, "close", "Caller");
    let caller_close_dense = graph
        .dense_id_for(caller_close)
        .expect("Caller.close must be interned");
    assert_ne!(
        graph.is_definitely_dead_code(caller_close_dense),
        Some(true),
        "Caller.close() is genuinely called from run() via the enhanced-for loop variable \
         `handle` and must never be reported definitely dead"
    );
    assert!(
        !graph.callers_index(caller_close_dense).is_empty(),
        "Caller.close() must keep its real caller edge"
    );

    let handle_close = declaration_symbol_owned_by(&handle_index, "close", "handle");
    let handle_close_dense = graph
        .dense_id_for(handle_close)
        .expect("handle.close must be interned");
    assert!(
        !graph.callers_index(handle_close_dense).is_empty(),
        "accepted regression (#1898 scope split): receiver-type narrowing is tag-only now, \
         so the unrelated class `handle`'s static close() keeps this fabricated caller edge \
         until the receiver-type hard-narrowing follow-up issue lands"
    );
}

// ---------------------------------------------------------------------
// P1-B, form 2 (dead-verdict flip): try-with-resources resource variable
// ---------------------------------------------------------------------

/// Same shape as the enhanced-for case, mirrored onto a try-with-resources
/// resource declaration (`try (Caller handle = new Caller())`). `Caller`
/// implements `AutoCloseable` (required for try-with-resources to be
/// javac-valid at all) so its own `close()` cannot double as the private
/// regression target -- `helper()` plays that role instead, called on the
/// SAME resource variable.
/// #1898 SCOPE SPLIT (see this file's module doc): `Caller.helper()` still
/// keeps its real edge, but `handle.helper()` now ALSO keeps a fabricated
/// edge -- an accepted regression pending the hard-narrowing follow-up.
#[test]
fn try_with_resources_variable_reaches_the_real_type_and_the_coincidental_class_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "handle.java",
        "package m;\npublic class handle {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m;\nclass Caller implements AutoCloseable {\n    private void helper() {}\n    public void close() {}\n    void run() {\n        try (Caller handle = new Caller()) {\n            handle.helper();\n        }\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["handle.java", "Caller.java"]);
    let handle_index = extract_index(dir.path(), "handle.java");
    let caller_index = extract_index(dir.path(), "Caller.java");

    let caller_helper = declaration_symbol_owned_by(&caller_index, "helper", "Caller");
    let caller_helper_dense = graph
        .dense_id_for(caller_helper)
        .expect("Caller.helper must be interned");
    assert_ne!(
        graph.is_definitely_dead_code(caller_helper_dense),
        Some(true),
        "Caller.helper() is genuinely called from run() via the try-with-resources variable \
         `handle` and must never be reported definitely dead"
    );
    assert!(
        !graph.callers_index(caller_helper_dense).is_empty(),
        "Caller.helper() must keep its real caller edge"
    );

    let handle_helper = declaration_symbol_owned_by(&handle_index, "helper", "handle");
    let handle_helper_dense = graph
        .dense_id_for(handle_helper)
        .expect("handle.helper must be interned");
    assert!(
        !graph.callers_index(handle_helper_dense).is_empty(),
        "accepted regression (#1898 scope split): receiver-type narrowing is tag-only now, \
         so the unrelated class `handle`'s static helper() keeps this fabricated caller edge \
         until the receiver-type hard-narrowing follow-up issue lands"
    );
}

// ---------------------------------------------------------------------
// P1-B, form 3: catch parameter
// ---------------------------------------------------------------------

/// The catch parameter `handle` is declared `MyException` (the REAL
/// type), coincidentally sharing its bare name with an unrelated
/// top-level class `handle`. Before the fix, the catch parameter had no
/// typed-name evidence, so the static-type-name fallback misresolved
/// `handle` as the CLASS.
/// #1898 SCOPE SPLIT (see this file's module doc): `MyException.helper()`
/// still keeps its real edge, but `handle.helper()` now ALSO keeps a
/// fabricated edge -- an accepted regression pending the follow-up.
#[test]
fn catch_parameter_reaches_the_real_exception_type_and_the_coincidental_class_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "handle.java",
        "package m;\npublic class handle {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "MyException.java",
        "package m;\npublic class MyException extends Exception {\n    void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m;\nclass Caller {\n    void run() {\n        try {\n            doSomething();\n        } catch (MyException handle) {\n            handle.helper();\n        }\n    }\n    void doSomething() throws MyException {}\n}\n",
    );

    let graph = build_graph_over(
        dir.path(),
        &["handle.java", "MyException.java", "Caller.java"],
    );
    let handle_index = extract_index(dir.path(), "handle.java");
    let exception_index = extract_index(dir.path(), "MyException.java");

    let exception_helper = declaration_symbol_owned_by(&exception_index, "helper", "MyException");
    let exception_helper_dense = graph
        .dense_id_for(exception_helper)
        .expect("MyException.helper must be interned");
    assert!(
        !graph.callers_index(exception_helper_dense).is_empty(),
        "MyException.helper() must keep its real caller edge from the catch parameter `handle`"
    );

    let handle_helper = declaration_symbol_owned_by(&handle_index, "helper", "handle");
    let handle_helper_dense = graph
        .dense_id_for(handle_helper)
        .expect("handle.helper must be interned");
    assert!(
        !graph.callers_index(handle_helper_dense).is_empty(),
        "accepted regression (#1898 scope split): receiver-type narrowing is tag-only now, \
         so the unrelated class `handle`'s static helper() keeps this fabricated caller edge \
         until the receiver-type hard-narrowing follow-up issue lands"
    );
}

// ---------------------------------------------------------------------
// P1-B, form 4: lambda parameter
// ---------------------------------------------------------------------

/// The lambda parameter `handle` (untyped, target-type-inferred to
/// `Caller`) coincidentally shares its bare name with an unrelated
/// top-level class `handle`. Before the fix, an untyped lambda parameter
/// had no typed-name evidence at all, so the static-type-name fallback
/// misresolved `handle` as the CLASS -- deleting the real edge entirely.
///
/// Unlike the other five P1-B forms, this one is genuinely UNTYPED in
/// source (no `(Caller handle) -> ...` annotation) and this extractor
/// performs no type inference at all (module doc) -- there is no real
/// evidence anywhere in the repo of `handle`'s true type here, so exact
/// resolution is impossible without guessing. The fix's actual guarantee
/// is narrower and still correct: `resolve_receiver_type` now recognizes
/// `handle` as a genuine LOCAL BINDING (not a coincidental type-name
/// match) and returns `None`, which SKIPS narrowing entirely rather than
/// HARD-narrowing to the wrong class -- the real edge survives, but so
/// does the coincidental one (an honest, safely ambiguous pool, per this
/// codebase's "missing evidence retains, never guesses" doctrine), unlike
/// the other five forms where a real declared type exists SOMEWHERE in
/// the repo and lets the fix narrow exactly.
#[test]
fn lambda_parameter_reaches_the_real_target_type_not_a_coincidentally_same_named_class() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "handle.java",
        "package m;\npublic class handle {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m;\nimport java.util.function.Consumer;\nclass Caller {\n    void helper() {}\n    void run() {\n        Consumer<Caller> c = (handle) -> {\n            handle.helper();\n        };\n        c.accept(this);\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["handle.java", "Caller.java"]);
    let handle_index = extract_index(dir.path(), "handle.java");
    let caller_index = extract_index(dir.path(), "Caller.java");

    let caller_helper = declaration_symbol_owned_by(&caller_index, "helper", "Caller");
    let caller_helper_dense = graph
        .dense_id_for(caller_helper)
        .expect("Caller.helper must be interned");
    assert!(
        !graph.callers_index(caller_helper_dense).is_empty(),
        "Caller.helper() must keep its real caller edge from the lambda parameter `handle`"
    );

    let handle_helper = declaration_symbol_owned_by(&handle_index, "helper", "handle");
    let handle_helper_dense = graph
        .dense_id_for(handle_helper)
        .expect("handle.helper must be interned");
    assert!(
        !graph.callers_index(handle_helper_dense).is_empty(),
        "an untyped lambda parameter's target genuinely cannot be resolved without a real type \
         checker -- the coincidental class `handle` staying an AMBIGUOUS candidate alongside \
         Caller.helper() is the honest, safe outcome (never a wrongly EXCLUSIVE guess), unlike \
         the other five P1-B forms where a real declared type exists somewhere in the repo"
    );
}

// ---------------------------------------------------------------------
// P1-B, form 5: inner-class field access
// ---------------------------------------------------------------------

/// `Inner`'s bare `outerField.helper()` reads a FIELD declared on its
/// enclosing type `Outer` -- `FileTypedNames::lookup`'s field branch is
/// keyed by the EXACT `enclosing_type` passed to it (`"Inner"`), never an
/// outer lexically-enclosing type, so it misses this field entirely.
/// `outerField` coincidentally shares its bare name with an unrelated
/// top-level class.
/// #1898 SCOPE SPLIT (see this file's module doc): `Svc.helper()` still
/// keeps its real edge, but `outerField.helper()` now ALSO keeps a
/// fabricated edge (this shape was already reaching via the Advisory
/// "prefer a non-empty match" path pre-split, which is retired too) --
/// an accepted regression pending the follow-up.
#[test]
fn inner_class_field_access_reaches_the_fields_real_type_and_the_coincidental_class_now_regresses()
{
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "outerField.java",
        "package m;\npublic class outerField {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Svc.java",
        "package m;\npublic class Svc {\n    void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Outer.java",
        "package m;\nclass Outer {\n    Svc outerField = new Svc();\n\n    class Inner {\n        void run() {\n            outerField.helper();\n        }\n    }\n}\n",
    );

    let graph = build_graph_over(
        dir.path(),
        &["outerField.java", "Svc.java", "Outer.java"],
    );
    let field_class_index = extract_index(dir.path(), "outerField.java");
    let svc_index = extract_index(dir.path(), "Svc.java");

    let svc_helper = declaration_symbol_owned_by(&svc_index, "helper", "Svc");
    let svc_helper_dense = graph
        .dense_id_for(svc_helper)
        .expect("Svc.helper must be interned");
    assert!(
        !graph.callers_index(svc_helper_dense).is_empty(),
        "Svc.helper() must keep its real caller edge from Inner.run()'s outerField.helper() call"
    );

    let field_class_helper =
        declaration_symbol_owned_by(&field_class_index, "helper", "outerField");
    let field_class_helper_dense = graph
        .dense_id_for(field_class_helper)
        .expect("outerField.helper must be interned");
    assert!(
        !graph.callers_index(field_class_helper_dense).is_empty(),
        "accepted regression (#1898 scope split): receiver-type narrowing is tag-only now, \
         so the unrelated class `outerField`'s static helper() keeps this fabricated caller \
         edge until the receiver-type hard-narrowing follow-up issue lands"
    );
}

// ---------------------------------------------------------------------
// P1-B, form 6: inherited field (declared in a different file)
// ---------------------------------------------------------------------

/// `Caller.run()` reads `inheritedField`, a field declared on `Base`
/// (a DIFFERENT file) and inherited by `Caller`. `FileTypedNames` is
/// per-file (`receiver::FileTypedNames`'s own doc comment), so `Caller`'s
/// own file carries no typed-name evidence for a field it never declares
/// itself. `inheritedField` coincidentally shares its bare name with an
/// unrelated top-level class.
/// #1898 SCOPE SPLIT (see this file's module doc): `Svc.helper()` still
/// keeps its real edge, but `inheritedField.helper()` now ALSO keeps a
/// fabricated edge -- an accepted regression pending the follow-up.
#[test]
fn inherited_field_reaches_the_fields_real_type_and_the_coincidental_class_now_regresses() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "inheritedField.java",
        "package m;\npublic class inheritedField {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Svc.java",
        "package m;\npublic class Svc {\n    void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Base.java",
        "package m;\npublic class Base {\n    protected Svc inheritedField = new Svc();\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m;\nclass Caller extends Base {\n    void run() {\n        inheritedField.helper();\n    }\n}\n",
    );

    let graph = build_graph_over(
        dir.path(),
        &["inheritedField.java", "Svc.java", "Base.java", "Caller.java"],
    );
    let field_class_index = extract_index(dir.path(), "inheritedField.java");
    let svc_index = extract_index(dir.path(), "Svc.java");

    let svc_helper = declaration_symbol_owned_by(&svc_index, "helper", "Svc");
    let svc_helper_dense = graph
        .dense_id_for(svc_helper)
        .expect("Svc.helper must be interned");
    assert!(
        !graph.callers_index(svc_helper_dense).is_empty(),
        "Svc.helper() must keep its real caller edge from Caller.run()'s inheritedField.helper() \
         call"
    );

    let field_class_helper =
        declaration_symbol_owned_by(&field_class_index, "helper", "inheritedField");
    let field_class_helper_dense = graph
        .dense_id_for(field_class_helper)
        .expect("inheritedField.helper must be interned");
    assert!(
        !graph.callers_index(field_class_helper_dense).is_empty(),
        "accepted regression (#1898 scope split): receiver-type narrowing is tag-only now, \
         so the unrelated class `inheritedField`'s static helper() keeps this fabricated \
         caller edge until the receiver-type hard-narrowing follow-up issue lands"
    );
}
