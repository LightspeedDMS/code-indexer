//! Regression coverage for issue #1915 (found during the #1910 salvage,
//! filed separately): `extract_imports` (`java.rs`) tested `is_wildcard`
//! BEFORE `is_static`, so a STATIC-ON-DEMAND import (`import static
//! m.Util.*;`) was classified as an ordinary `ImportKind::Wildcard`.
//! `import_reasons`'s `Wildcard` arm compares `decl.package` against the
//! import's WHOLE path (`"m.Util"`), which never matches a real package
//! (`"m"`), so a static-wildcard-imported method earned ZERO reason bits
//! at all -- not even the coarse name-only `STATIC_IMPORT` signal a
//! single-member static import gets.
//!
//! This is independent of, and predates, ALL of #1898/#1910's
//! same-class-or-super work: the mechanism that actually destroys the
//! edge is `apply_import_context_narrowing` (`narrowing.rs`), a
//! pre-existing, untouched pass that hard-narrows to CONTEXT_MASK-tagged
//! candidates whenever the tagged subset is non-empty and a STRICT
//! subset of the pool. A same-named DECOY method that legitimately earns
//! its own context bit (here, `SAME_PACKAGE` -- declared in the caller's
//! own package) is therefore what turns the classification bug into a
//! genuine "0 callers" annihilation of the REAL target: the decoy
//! survives (it has a real bit), the real static-wildcard-imported
//! target does not (it has none, pre-fix).
//!
//! Drives real, javac-shaped Java source through the REAL front door
//! (`build_repo_graph`, not `bind()` directly), mirroring every sibling
//! `bug_*_narrowing_regressions.rs` file's own established pattern.

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

/// `Caller` (package `m2`) does `import static m.Util.*;` and bare-calls
/// `helper()`. `Decoy` is ALSO in package `m2` (the caller's OWN
/// package) and ALSO declares a 0-arg `helper()` -- a same-named,
/// same-arity candidate that legitimately earns `SAME_PACKAGE` with no
/// import needed at all. Before the #1915 fix: `Util.helper` earned ZERO
/// reason bits (misclassified as plain `Wildcard`), `Decoy.helper` earned
/// `SAME_PACKAGE`, and `apply_import_context_narrowing` hard-narrowed the
/// two-candidate pool down to `{Decoy.helper}` only -- destroying the
/// REAL edge to `Util.helper` outright (0 callers), not merely
/// mis-tagging it.
#[test]
fn bare_call_through_a_static_wildcard_import_survives_a_same_package_decoy() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Util.java",
        "package m;\npublic final class Util {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Decoy.java",
        "package m2;\npublic class Decoy {\n    public void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m2;\nimport static m.Util.*;\npublic class Caller {\n    public void run() {\n        helper();\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["Util.java", "Decoy.java", "Caller.java"]);
    let util_index = extract_index(dir.path(), "Util.java");

    let util_helper = declaration_symbol_owned_by(&util_index, "helper", "Util");
    let util_helper_dense = graph
        .dense_id_for(util_helper)
        .expect("Util.helper must be interned");

    assert_ne!(
        graph.is_definitely_dead_code(util_helper_dense),
        Some(true),
        "Util.helper() is genuinely called (via a static-on-demand import) from Caller.run() \
         and must never be reported definitely dead"
    );
    assert!(
        !graph.callers_index(util_helper_dense).is_empty(),
        "#1915: Util.helper() must keep its real caller edge -- a static-on-demand import \
         must earn STATIC_IMPORT so apply_import_context_narrowing does not hard-narrow the \
         pool down to the same-package decoy Decoy.helper() alone"
    );
}

/// #1915 follow-up (found by dual review after the initial fix): a
/// static-on-demand import of a member of a ONE-LEVEL-NESTED declaring
/// class (`import static m.Outer.Util.*;`) -- the import path's prefix
/// (`"m.Outer"`) mixes the real package (`"m"`) with a NESTED-CLASS
/// segment (`"Outer"`), which the top-level-only `StaticWildcard` check
/// (`decl.package == prefix`) can never match. `Decoy` (same package as
/// `Caller`, no import needed) earns `SAME_PACKAGE` regardless. Before
/// the follow-up fix: `Outer.Util.helper` earned ZERO reason bits (the
/// same annihilation shape the first #1915 fix closed, one nesting level
/// deeper), `apply_import_context_narrowing` hard-narrowed to `{Decoy.
/// helper}` alone, and the real target went to zero callers.
#[test]
fn bare_call_through_a_static_wildcard_import_of_a_nested_class_survives_a_same_package_decoy() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Outer.java",
        "package m;\npublic class Outer {\n    public static class Util {\n        public static void helper() {}\n    }\n}\n",
    );
    write_java(
        dir.path(),
        "Decoy.java",
        "package m2;\nclass Decoy {\n    void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package m2;\nimport static m.Outer.Util.*;\nclass Caller {\n    void run() {\n        helper();\n    }\n}\n",
    );

    let graph = build_graph_over(dir.path(), &["Outer.java", "Decoy.java", "Caller.java"]);
    let outer_index = extract_index(dir.path(), "Outer.java");

    let util_helper = declaration_symbol_owned_by(&outer_index, "helper", "Util");
    let util_helper_dense = graph
        .dense_id_for(util_helper)
        .expect("Outer.Util.helper must be interned");

    assert_ne!(
        graph.is_definitely_dead_code(util_helper_dense),
        Some(true),
        "Outer.Util.helper() is genuinely called (via a static-on-demand import of a nested \
         class) from Caller.run() and must never be reported definitely dead"
    );
    assert!(
        !graph.callers_index(util_helper_dense).is_empty(),
        "#1915 follow-up: Outer.Util.helper() must keep its real caller edge -- a static-on- \
         demand import of a ONE-LEVEL-NESTED declaring class must also earn STATIC_IMPORT via \
         TypeIndex::top_level_of, so apply_import_context_narrowing does not hard-narrow the \
         pool down to the same-package decoy Decoy.helper() alone"
    );
}
