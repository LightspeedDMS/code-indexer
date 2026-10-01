//! Story #1787, S2, AC4 -- Bind produces confidence-scored candidates.
//! End-to-end integration tests: REAL tree-sitter parse, REAL
//! `JavaExtractor` (no mocking), through the real
//! `xray_core::graph::bind::bind` pipeline -- exactly the path production
//! indexing takes, unlike `graph::bind`'s own unit tests which build
//! `LocalIndex` fixtures by hand.

use std::path::Path;
use xray_core::graph::bind::{bind, FileForBind};
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::fused::process_file_fused;
use xray_core::graph::identity::file_id;
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

/// Extracts one file into a `FileForBind` via the REAL fused pipeline --
/// no hand-built `LocalIndex` anywhere in this test file.
fn extract_java_file(dir: &Path, relative_path: &str) -> FileForBind {
    let full_path = dir.join(relative_path);
    let result = process_file_fused(&full_path, relative_path, &NoOpCollector)
        .unwrap_or_else(|| panic!("failed to parse {relative_path}"));
    let index = result
        .index
        .unwrap_or_else(|| panic!("extraction did not complete for {relative_path}"));
    FileForBind { file_id: file_id(relative_path), language: "java".to_string(), index }
}

/// AC4's central discriminating case, proven end-to-end: several REAL
/// `getId` methods declared in different files/packages (none reachable
/// via file/package/import/arity evidence relative to the caller) must
/// resolve to a candidate SET with len > 1 -- never a picked "winner".
#[test]
fn ambiguous_same_name_methods_across_files_yield_a_multi_candidate_set_end_to_end() {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "Repo1.java", "package com.acme.a;\nclass Repo1 {\n    int getId() { return 1; }\n}\n");
    write_java(dir.path(), "Repo2.java", "package com.acme.b;\nclass Repo2 {\n    int getId() { return 2; }\n}\n");
    write_java(
        dir.path(),
        "Caller.java",
        "package com.acme.c;\nclass Caller {\n    void run() {\n        getId();\n    }\n}\n",
    );

    let files = vec![
        extract_java_file(dir.path(), "Repo1.java"),
        extract_java_file(dir.path(), "Repo2.java"),
        extract_java_file(dir.path(), "Caller.java"),
    ];
    let graph = bind(files);

    let caller_id = file_id("Caller.java");
    let reference = graph.references().iter().find(|r| r.file == caller_id).unwrap();
    let candidates = graph.candidates_for(reference);
    assert_eq!(candidates.len(), 2, "must be a multi-candidate set, not a picked winner");
}

/// AC4: a call to a name declared NOWHERE in the repository resolves to
/// an EMPTY candidate set -- never a guessed target.
#[test]
fn call_to_a_name_declared_nowhere_in_the_repo_yields_an_empty_candidate_set_end_to_end() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Caller.java",
        "class Caller {\n    void run() {\n        totallyExternalLibraryCall();\n    }\n}\n",
    );

    let files = vec![extract_java_file(dir.path(), "Caller.java")];
    let graph = bind(files);

    let caller_id = file_id("Caller.java");
    let reference = graph.references().iter().find(|r| r.file == caller_id).unwrap();
    assert!(reference.is_unresolved());
    assert!(graph.candidates_for(reference).is_empty());
}

/// AC4 Level 1 ("+arity"), proven end-to-end: two real `process` methods
/// with different declared parameter counts; a 2-argument call site must
/// resolve to the 2-parameter overload only.
#[test]
fn arity_narrowing_resolves_an_overload_the_bare_name_alone_could_not_end_to_end() {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "E.java", "class E {\n    void process(int a) {}\n}\n");
    write_java(dir.path(), "F.java", "class F {\n    void process(int a, int b) {}\n}\n");
    write_java(
        dir.path(),
        "Caller.java",
        "class Caller {\n    void run() {\n        process(1, 2);\n    }\n}\n",
    );

    let files = vec![
        extract_java_file(dir.path(), "E.java"),
        extract_java_file(dir.path(), "F.java"),
        extract_java_file(dir.path(), "Caller.java"),
    ];
    let graph = bind(files);

    let caller_id = file_id("Caller.java");
    let reference = graph.references().iter().find(|r| r.file == caller_id).unwrap();
    let candidates = graph.candidates_for(reference);
    assert_eq!(candidates.len(), 1, "arity must narrow to the single matching overload");
    let declaring_file = (graph.resolve_symbol(candidates[0].symbol()) >> 32) as u32;
    assert_eq!(declaring_file, file_id("F.java"), "must resolve to the 2-parameter overload");
}

/// AC4 Level 2 ("+import context"), proven end-to-end with a REALISTIC,
/// semantically valid Java scenario: three real PUBLIC `Widget` classes
/// across three packages (a package-qualified simple-name collision,
/// `public` so the cross-package import is legal), and a caller with a
/// genuine `import pkg.h.Widget;` statement and a real `new Widget()`
/// construction site. Only the import narrows the set to the one
/// package-matching candidate. The reference under test is selected
/// explicitly by `REF_KIND_CONSTRUCTION` -- never "whichever reference
/// happens to be first in the file" -- since the caller's local variable
/// declaration (`Widget w = ...`) also produces its own type-reference
/// entry in the same file.
#[test]
fn import_context_narrows_to_the_imported_candidate_end_to_end() {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "G.java", "package pkg.g;\npublic class Widget {}\n");
    write_java(dir.path(), "H.java", "package pkg.h;\npublic class Widget {}\n");
    write_java(dir.path(), "I.java", "package pkg.i;\npublic class Widget {}\n");
    write_java(
        dir.path(),
        "Caller.java",
        "package pkg.caller;\nimport pkg.h.Widget;\nclass Caller {\n    void run() {\n        Widget w = new Widget();\n    }\n}\n",
    );

    let files = vec![
        extract_java_file(dir.path(), "G.java"),
        extract_java_file(dir.path(), "H.java"),
        extract_java_file(dir.path(), "I.java"),
        extract_java_file(dir.path(), "Caller.java"),
    ];
    let graph = bind(files);

    let caller_id = file_id("Caller.java");
    let reference = graph
        .references()
        .iter()
        .find(|r| r.file == caller_id && r.kind == xray_core::graph::bind::REF_KIND_CONSTRUCTION)
        .unwrap();
    let candidates = graph.candidates_for(reference);
    assert_eq!(candidates.len(), 1, "import context must narrow to the one imported candidate");
    let declaring_file = (graph.resolve_symbol(candidates[0].symbol()) >> 32) as u32;
    assert_eq!(declaring_file, file_id("H.java"));
}

/// AC4 Level 5, proven end-to-end: a real method whose bare name is
/// unique across the whole repo resolves to a single candidate with
/// `Confidence::Exact` via `UNIQUE_NAME_IN_REPO`.
#[test]
fn a_repo_unique_method_name_resolves_to_exact_confidence_end_to_end() {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "Unique.java", "class Unique {\n    void uniqueOnly() {}\n}\n");
    write_java(
        dir.path(),
        "Caller.java",
        "class Caller {\n    void run() {\n        uniqueOnly();\n    }\n}\n",
    );

    let files = vec![
        extract_java_file(dir.path(), "Unique.java"),
        extract_java_file(dir.path(), "Caller.java"),
    ];
    let graph = bind(files);

    let caller_id = file_id("Caller.java");
    let reference = graph.references().iter().find(|r| r.file == caller_id).unwrap();
    let candidates = graph.candidates_for(reference);
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].confidence(), xray_core::graph::confidence::Confidence::Exact);
    assert_ne!(candidates[0].reasons() & xray_core::graph::reasons::UNIQUE_NAME_IN_REPO, 0);
}

/// Writes every real Java fixture scenario used by the tests above into
/// ONE repo directory, so the combined test below binds them all at once.
fn write_full_repo_fixture(dir: &Path) {
    write_java(dir, "Repo1.java", "package com.acme.a;\nclass Repo1 {\n    int getId() { return 1; }\n}\n");
    write_java(dir, "Repo2.java", "package com.acme.b;\nclass Repo2 {\n    int getId() { return 2; }\n}\n");
    write_java(dir, "Unique.java", "class Unique {\n    void uniqueOnly() {}\n}\n");
    write_java(dir, "E.java", "class E {\n    void process(int a) {}\n}\n");
    write_java(dir, "F.java", "class F {\n    void process(int a, int b) {}\n}\n");
    write_java(dir, "G.java", "package pkg.g;\npublic class Widget {}\n");
    write_java(dir, "H.java", "package pkg.h;\npublic class Widget {}\n");
    write_java(dir, "I.java", "package pkg.i;\npublic class Widget {}\n");
    write_java(
        dir,
        "Caller.java",
        "package com.acme.c;\nimport pkg.h.Widget;\nclass Caller {\n    void run() {\n        getId();\n        uniqueOnly();\n        totallyExternalLibraryCall();\n        process(1, 2);\n        Widget w = new Widget();\n    }\n}\n",
    );
}

const EXTRACTORLESS_TEXT_FILE_ID: u32 = 9999;

/// Extracts every file `write_full_repo_fixture` wrote, plus a synthetic
/// non-Java `FileForBind` with an empty `LocalIndex` (simulating a
/// language with no extractor, since only Java has one in this slice).
fn extract_full_repo_fixture(dir: &Path) -> Vec<FileForBind> {
    let names = ["Repo1.java", "Repo2.java", "Unique.java", "E.java", "F.java", "G.java", "H.java", "I.java", "Caller.java"];
    let mut files: Vec<FileForBind> = names.iter().map(|name| extract_java_file(dir, name)).collect();
    files.push(FileForBind {
        file_id: EXTRACTORLESS_TEXT_FILE_ID,
        language: "text".to_string(),
        index: LocalIndex::new(),
    });
    files
}

/// Asserts `BinderDepth` reports Java's REAL levels while claiming NONE
/// for the extractor-less "text" language.
fn assert_binder_depth_is_honest(graph: &xray_core::graph::csr::CodeGraph) {
    use xray_core::graph::bind::depth::{
        LEVEL_0_BARE_NAME, LEVEL_1_ARITY, LEVEL_2_IMPORT_CONTEXT, LEVEL_5_UNIQUE_NAME,
    };

    let java_depth = graph.binder_depths().iter().find(|d| d.language == "java").unwrap();
    assert!(java_depth.reached(LEVEL_0_BARE_NAME));
    assert!(java_depth.reached(LEVEL_1_ARITY));
    assert!(java_depth.reached(LEVEL_2_IMPORT_CONTEXT));
    assert!(java_depth.reached(LEVEL_5_UNIQUE_NAME));

    let text_depth = graph.binder_depths().iter().find(|d| d.language == "text").unwrap();
    assert_eq!(text_depth.levels_reached, 0, "a language with no extractor must claim no depth");
}

/// Whole-pipeline regression guard PLUS AC4's `BinderDepth` honesty
/// requirement, both proven together through a single real, multi-file,
/// multi-scenario bind covering every narrowing level exercised by the
/// tests above (ambiguous names, out-of-repo, arity overloads, import
/// context, unique name).
#[test]
fn every_candidate_has_confidence_matching_derive_and_binder_depth_is_honest_end_to_end() {
    use xray_core::graph::confidence::Confidence;

    let dir = tempfile::tempdir().unwrap();
    write_full_repo_fixture(dir.path());
    let graph = bind(extract_full_repo_fixture(dir.path()));

    let mut checked_any = false;
    for reference in graph.references() {
        for candidate in graph.candidates_for(reference) {
            checked_any = true;
            assert_eq!(candidate.confidence(), Confidence::derive(candidate.reasons()));
        }
    }
    assert!(checked_any, "test fixture produced no candidates to check");

    assert_binder_depth_is_honest(&graph);
}
