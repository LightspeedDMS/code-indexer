//! Permanent liveness-guard regression suite for the #1910 salvage (issue
//! #1910, rounds 5-7). Promoted, per explicit coordinator instruction after
//! the salvage's revert, from the round-7 adversarial probe (scratch tree,
//! since deleted) -- this project's own conventions say a scratch A/B/probe
//! tree is disposable once its finding is captured here permanently.
//!
//! **Why this file exists, and why it is not optional.** Across issues
//! #1898 and #1910, SEVEN straight rounds each shipped a variant of
//! `apply_receiver_type_narrowing`/`apply_same_class_or_super_narrowing`
//! that hard-deleted a candidate on evidence that turned out not to be
//! closed-world -- and in every one of the seven, a live PRIVATE method got
//! reported `is_definitely_dead_code() == Some(true)`, the exact outcome
//! epic #1786 declared structurally impossible. `cargo test --workspace`
//! was green every single time (580 tests then, 800+ now) -- not one of
//! the seven defects was caught by the existing suite, and not one was
//! found by reading a diff. Every one was found by driving a real,
//! javac-valid adversarial fixture through the actual `build_repo_graph`
//! front door. #1910's salvage (see `docs/xray-architecture.md`'s
//! candidate-admission section) made both narrowing passes PERMANENTLY
//! tag-only, closing this defect class entirely -- but "closed today"
//! and "guarded against tomorrow" are different claims. Without this
//! file, a future attempt to reintroduce hard-narrowing on this evidence
//! could reproduce any of these seven shapes and `cargo test --workspace`
//! would stay green again, exactly as blind as rounds 1 through 7.
//!
//! **What these tests actually assert.** Every test below is liveness-
//! shaped (`assert_alive`): a genuinely-called method must never be
//! reported definitely dead and must keep at least one real caller edge.
//! On today's permanently-tag-only tree, EVERY one of these passes
//! trivially -- that is the point, not a weakness: neither narrowing pass
//! can delete a candidate anymore, so nothing here is expected to fail
//! today. The suite exists to fail the moment someone reintroduces
//! candidate deletion on this evidence without re-solving the underlying
//! soundness problem (see "Prove they discriminate" below for direct
//! proof this is not decoration).
//!
//! **Fixtures dropped from the probe, and why** (do not re-add without
//! re-deriving the reasoning):
//! - **A6** (`a6_wildcard_import_static_call_does_not_hard_bind`): probes
//!   whether an ordinary (non-static) wildcard TYPE import causes a
//!   coincidental binding to an unrelated external-looking class -- a
//!   SPURIOUS-BINDING concern (over-inclusion), not a live-method-
//!   reported-dead concern. It never asserted anything in the probe
//!   (`println!`-only, explicitly marked "(informational)"), and it does
//!   not fit this file's liveness-guard shape -- promoting it here would
//!   misrepresent what it guards. Left out deliberately.
//! - **C1** (`c1_counter_fires_on_a_receiver_type_strict_subset_narrowing`):
//!   its premise -- a receiver-type narrowing shrinking a 2-candidate pool
//!   to 1 -- required `apply_receiver_type_narrowing` to hard-narrow.
//!   That mechanism no longer exists (permanently tag-only): the fixture
//!   now produces `narrowed_to_nonempty_strict_subset_count == 0`, so the
//!   original `>= 1` assertion would be FALSE against today's code, not
//!   merely trivially true. The counter itself is still real and still
//!   tested (`repo_index.rs::narrowed_to_nonempty_strict_subset_count_
//!   counts_a_wrong_subset_narrowing`, an arity-driven fixture that never
//!   depended on receiver-type/same-class-or-super narrowing at all) --
//!   only THIS fixture's specific premise died.
//! - **C2** (`c2_counter_on_the_within_file_nested_collision`): same
//!   root cause as C1 (no assertion in the probe either, `println!`-only)
//!   plus a second dead premise -- it measured the A1 within-file
//!   nested-class collision through `direct_lexical_parent`, a substrate
//!   the salvage deleted entirely (see `docs/xray-architecture.md`'s
//!   "What survived the revert" item 5). Nothing left to measure.
//!
//! Every fixture below uses neutral, public-repository-safe naming
//! (`com.example.*` packages, `Util`/`Target`/`Outer`/`Decoy`-style
//! class names) -- no customer or third-party identifiers.

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
    let full = dir.join(relative_path);
    if let Some(parent) = full.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(full, source).unwrap();
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

/// Reports (dead_verdict, caller_count) for `enclosing_type.name`.
fn probe(
    graph: &xray_core::graph::csr::CodeGraph,
    owner_index: &LocalIndex,
    name: &str,
    enclosing_type: &str,
) -> (Option<bool>, usize) {
    let symbol = declaration_symbol_owned_by(owner_index, name, enclosing_type);
    let dense = graph
        .dense_id_for(symbol)
        .unwrap_or_else(|| panic!("{enclosing_type}.{name} must be interned"));
    (graph.is_definitely_dead_code(dense), graph.callers_index(dense).len())
}

/// The shared liveness assertion every test in this file makes: a
/// genuinely-called method must never be reported definitely dead and
/// must keep at least one real caller edge. A REAL assertion, not a
/// `println!` -- a test that only prints and always passes is worse than
/// no test at all.
fn assert_alive(
    graph: &xray_core::graph::csr::CodeGraph,
    owner_index: &LocalIndex,
    name: &str,
    enclosing_type: &str,
) {
    let (dead, callers) = probe(graph, owner_index, name, enclosing_type);
    assert_ne!(
        dead,
        Some(true),
        "{enclosing_type}.{name}() is genuinely called and must never be reported definitely dead"
    );
    assert!(
        callers > 0,
        "{enclosing_type}.{name}() must keep its real caller edge"
    );
}

// =====================================================================
// A1 (round 6/7, finding 2 lineage): WITHIN-FILE same-simple-name nested
// classes. Guards against a lexical-parent/enclosing-type substrate that
// collides on a bare simple name within one file (two `Builder` nested
// classes in one compilation unit, an everyday Java shape) and widens a
// caller's `allowed` set against the WRONG outer class.
// =====================================================================
const REGISTRY: &str = r#"package com.example;

public final class Registry {

    public static final class User {
        private static void validate() { System.out.println("user"); }

        public static final class Builder {
            public User build() {
                validate();
                return null;
            }
        }
    }

    public static final class Group {
        private static void validate() { System.out.println("group"); }

        public static final class Builder {
            public Group build() {
                validate();
                return null;
            }
        }
    }
}
"#;

#[test]
fn a1_two_same_named_nested_builders_in_one_file_keep_their_own_enclosing_privates() {
    let dir = tempfile::tempdir().unwrap();
    write_java(dir.path(), "Registry.java", REGISTRY);
    let graph = build_graph_over(dir.path(), &["Registry.java"]);
    let idx = extract_index(dir.path(), "Registry.java");
    assert_alive(&graph, &idx, "validate", "User");
    assert_alive(&graph, &idx, "validate", "Group");
}

// =====================================================================
// A2 / A2b (issue #1915): static-import classification. BOTH need a
// same-package `Decoy` declaring its own `helper()` -- without one, the
// AC4 Level 5 unique-name shortcut (pool of 1) or `apply_import_context_
// narrowing`'s empty-`reachable` early-return would keep the real target
// regardless of how the import was classified, making the test pass
// whether or not the fix under test even exists (dual review caught
// exactly this: both were green on HEAD before #1915's fix, and would
// stay green if the `extract_imports` branch order were reverted).
//
// A2 is genuinely #1915-discriminating: it exercises the STATIC-ON-DEMAND
// form (`import static ...*;`), the form `extract_imports` misclassified.
// A2b is a CONTROL, not a #1915 regression guard: the single-member
// explicit form it exercises (`import static ...NAME;`) was classified
// correctly by the PRE-EXISTING, `#1915`-unrelated `ImportKind::Static`
// name-only heuristic both before and after the #1915 fix -- it proves
// that path stays robust under the same decoy pressure, nothing more.
// =====================================================================
#[test]
fn a2_static_wildcard_import_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Util.java",
        "package com.example;\npublic final class Util {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Decoy.java",
        "package com.example.caller;\nclass Decoy {\n    void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package com.example.caller;\nimport static com.example.Util.*;\npublic class Caller {\n    public void run() {\n        helper();\n    }\n}\n",
    );
    let graph = build_graph_over(dir.path(), &["Util.java", "Decoy.java", "Caller.java"]);
    let util = extract_index(dir.path(), "Util.java");
    assert_alive(&graph, &util, "helper", "Util");
}

#[test]
fn a2b_explicit_static_import_control_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Util.java",
        "package com.example;\npublic final class Util {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Decoy.java",
        "package com.example.caller;\nclass Decoy {\n    void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package com.example.caller;\nimport static com.example.Util.helper;\npublic class Caller {\n    public void run() {\n        helper();\n    }\n}\n",
    );
    let graph = build_graph_over(dir.path(), &["Util.java", "Decoy.java", "Caller.java"]);
    let util = extract_index(dir.path(), "Util.java");
    assert_alive(&graph, &util, "helper", "Util");
}

// =====================================================================
// A3 (round 7, captured-local finding): a captured local in an anonymous
// class body, coincidentally same-named as a type in the caller's own
// package. Guards the `FileTypedNames`/`(enclosing_method, name)` scope-
// key gap (the local is recorded under the OUTER method, the call site's
// `enclosing_method` is the anonymous class's own method -> lookup
// misses). Legal Java: a variable obscures a same-named type (JLS 6.4.2).
// =====================================================================
#[test]
fn a3_captured_local_obscuring_a_same_package_type_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Helper.java",
        "package com.example;\npublic class Helper {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Target.java",
        concat!(
            "package com.example;\n",
            "public class Target {\n",
            "    private void helper() { System.out.println(\"real\"); }\n",
            "    Runnable make() {\n",
            "        final Target Helper = new Target();\n",
            "        return new Runnable() {\n",
            "            public void run() {\n",
            "                Helper.helper();\n",
            "            }\n",
            "        };\n",
            "    }\n",
            "}\n"
        ),
    );
    let graph = build_graph_over(dir.path(), &["Helper.java", "Target.java"]);
    let target = extract_index(dir.path(), "Target.java");
    assert_alive(&graph, &target, "helper", "Target");
}

/// Same shape as A3, DEFAULT package (no `package` statement anywhere) --
/// `caller_package` is `None`, a distinct code path from A3's same-
/// package case.
#[test]
fn a4_default_package_captured_local_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Helper.java",
        "public class Helper {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Target.java",
        concat!(
            "public class Target {\n",
            "    private void helper() { System.out.println(\"real\"); }\n",
            "    Runnable make() {\n",
            "        final Target Helper = new Target();\n",
            "        return new Runnable() {\n",
            "            public void run() {\n",
            "                Helper.helper();\n",
            "            }\n",
            "        };\n",
            "    }\n",
            "}\n"
        ),
    );
    let graph = build_graph_over(dir.path(), &["Helper.java", "Target.java"]);
    let target = extract_index(dir.path(), "Target.java");
    assert_alive(&graph, &target, "helper", "Target");
}

/// Same shape as A3/A4, but the obscured name is a NESTED TYPE THIS SAME
/// FILE itself declares (no package involvement at all).
#[test]
fn a5_captured_local_obscuring_a_nested_type_of_the_same_file_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Target.java",
        concat!(
            "package com.example;\n",
            "public class Target {\n",
            "    static class Helper { static void helper() {} }\n",
            "    private void helper() { System.out.println(\"real\"); }\n",
            "    Runnable make() {\n",
            "        final Target Helper = new Target();\n",
            "        return new Runnable() {\n",
            "            public void run() {\n",
            "                Helper.helper();\n",
            "            }\n",
            "        };\n",
            "    }\n",
            "}\n"
        ),
    );
    let graph = build_graph_over(dir.path(), &["Target.java"]);
    let target = extract_index(dir.path(), "Target.java");
    assert_alive(&graph, &target, "helper", "Target");
}

// =====================================================================
// A7 (round 6, finding 1 lineage): `Ambiguous` local whose name ALSO
// matches a type this file imports. Guards `LocalLookup::Ambiguous`
// staying TERMINAL -- it must never fall through to the imported-type
// fallback even when that fallback would otherwise resolve cleanly.
// =====================================================================
#[test]
fn a7_ambiguous_local_whose_name_matches_an_imported_type_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Handle.java",
        "package com.example.other;\npublic class Handle {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Target.java",
        concat!(
            "package com.example;\n",
            "import com.example.other.Handle;\n",
            "public class Target {\n",
            "    private void helper() { System.out.println(\"real\"); }\n",
            "    void run(boolean flag) {\n",
            "        if (flag) {\n",
            "            Target Handle = new Target();\n",
            "            Handle.helper();\n",
            "        } else {\n",
            "            String Handle = \"x\";\n",
            "            System.out.println(Handle);\n",
            "        }\n",
            "    }\n",
            "}\n"
        ),
    );
    let graph = build_graph_over(dir.path(), &["Handle.java", "Target.java"]);
    let target = extract_index(dir.path(), "Target.java");
    assert_alive(&graph, &target, "helper", "Target");
}

// =====================================================================
// A8-A12 (round 6/7, lexical-nesting lineage): five distinct lexical-
// enclosing shapes, each a bare call from a nested/local/anonymous
// context to an outer PRIVATE member, each with an unrelated same-named
// `Decoy` in a different package/top-level type forcing the call through
// the real narrowing pipeline (never the AC4 Level 5 unique-name
// shortcut).
// =====================================================================

/// A8: a nested INTERFACE's default method calling the outer class's
/// private static method.
#[test]
fn a8_nested_interface_default_method_calls_outer_private_static() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Outer.java",
        concat!(
            "package com.example;\n",
            "public class Outer {\n",
            "    private static void secret() { System.out.println(\"s\"); }\n",
            "    public interface Inner {\n",
            "        default void go() { secret(); }\n",
            "    }\n",
            "}\n"
        ),
    );
    write_java(
        dir.path(),
        "Decoy.java",
        "package com.example.decoy;\npublic class Decoy {\n    private static void secret() {}\n    void use() { secret(); }\n}\n",
    );
    let graph = build_graph_over(dir.path(), &["Outer.java", "Decoy.java"]);
    let outer = extract_index(dir.path(), "Outer.java");
    assert_alive(&graph, &outer, "secret", "Outer");
}

/// A9: a nested class inside an ENUM calling the enum's private static.
#[test]
fn a9_nested_class_in_enum_calls_enum_private_static() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Color.java",
        concat!(
            "package com.example;\n",
            "public enum Color {\n",
            "    RED;\n",
            "    private static void secret() { System.out.println(\"s\"); }\n",
            "    static class Helper {\n",
            "        void go() { secret(); }\n",
            "    }\n",
            "}\n"
        ),
    );
    write_java(
        dir.path(),
        "Decoy.java",
        "package com.example.decoy;\npublic class Decoy {\n    private static void secret() {}\n    void use() { secret(); }\n}\n",
    );
    let graph = build_graph_over(dir.path(), &["Color.java", "Decoy.java"]);
    let color = extract_index(dir.path(), "Color.java");
    assert_alive(&graph, &color, "secret", "Color");
}

/// A10: an anonymous class created inside a STATIC INITIALIZER block,
/// bare-calling the outer class's private static method.
#[test]
fn a10_anon_class_in_static_initializer_calls_outer_private_static() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Outer.java",
        concat!(
            "package com.example;\n",
            "public class Outer {\n",
            "    static Runnable R;\n",
            "    private static void secret() { System.out.println(\"s\"); }\n",
            "    static {\n",
            "        R = new Runnable() {\n",
            "            public void run() { secret(); }\n",
            "        };\n",
            "    }\n",
            "}\n"
        ),
    );
    write_java(
        dir.path(),
        "Decoy.java",
        "package com.example.decoy;\npublic class Decoy {\n    private static void secret() {}\n    void use() { secret(); }\n}\n",
    );
    let graph = build_graph_over(dir.path(), &["Outer.java", "Decoy.java"]);
    let outer = extract_index(dir.path(), "Outer.java");
    assert_alive(&graph, &outer, "secret", "Outer");
}

/// A11: a local class declared inside a method, bare-calling the
/// enclosing class's private method. **Discriminating power verified by
/// execution -- see this crate's own salvage report: temporarily
/// reintroducing a hard-empty filter in `apply_receiver_type_narrowing`
/// makes THIS test fail** (`Outer.secret()` loses its caller edge), and
/// restoring tag-only makes it pass again.
#[test]
fn a11_local_class_calls_outer_private() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Outer.java",
        concat!(
            "package com.example;\n",
            "public class Outer {\n",
            "    private void secret() { System.out.println(\"s\"); }\n",
            "    Runnable make() {\n",
            "        class Local implements Runnable {\n",
            "            public void run() { secret(); }\n",
            "        }\n",
            "        return new Local();\n",
            "    }\n",
            "}\n"
        ),
    );
    write_java(
        dir.path(),
        "Decoy.java",
        "package com.example.decoy;\npublic class Decoy {\n    private void secret() {}\n    void use() { secret(); }\n}\n",
    );
    let graph = build_graph_over(dir.path(), &["Outer.java", "Decoy.java"]);
    let outer = extract_index(dir.path(), "Outer.java");
    assert_alive(&graph, &outer, "secret", "Outer");
}

/// A12: an anonymous class nested inside another anonymous class, the
/// innermost one bare-calling the outermost enclosing class's private
/// method -- proves the lexical chain isn't just one level deep.
#[test]
fn a12_nested_anonymous_classes_call_outer_private() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Outer.java",
        concat!(
            "package com.example;\n",
            "public class Outer {\n",
            "    private void secret() { System.out.println(\"s\"); }\n",
            "    Runnable make() {\n",
            "        return new Runnable() {\n",
            "            public void run() {\n",
            "                Runnable inner = new Runnable() {\n",
            "                    public void run() { secret(); }\n",
            "                };\n",
            "                inner.run();\n",
            "            }\n",
            "        };\n",
            "    }\n",
            "}\n"
        ),
    );
    write_java(
        dir.path(),
        "Decoy.java",
        "package com.example.decoy;\npublic class Decoy {\n    private void secret() {}\n    void use() { secret(); }\n}\n",
    );
    let graph = build_graph_over(dir.path(), &["Outer.java", "Decoy.java"]);
    let outer = extract_index(dir.path(), "Outer.java");
    assert_alive(&graph, &outer, "secret", "Outer");
}

// =====================================================================
// B1 (round 6, finding 2): order independence. Two DIFFERENT top-level
// classes, each declaring a same-named nested `Mid` with a bare call to
// its own enclosing type's private method, bound in BOTH file orderings.
// Guards against any future re-introduction of a bare-name-keyed
// lexical/enclosing-type substrate that resolves last-write-wins.
// =====================================================================
#[test]
fn b1_cross_file_same_named_nested_classes_both_orderings() {
    for order in [["A.java", "B.java"], ["B.java", "A.java"]] {
        let dir = tempfile::tempdir().unwrap();
        write_java(
            dir.path(),
            "A.java",
            "package com.example;\npublic class A {\n    private void shared() {}\n    class Mid {\n        void go() { shared(); }\n    }\n}\n",
        );
        write_java(
            dir.path(),
            "B.java",
            "package com.example;\npublic class B {\n    private void shared() {}\n    class Mid {\n        void go() { shared(); }\n    }\n}\n",
        );
        let graph = build_graph_over(dir.path(), &order);
        let a = extract_index(dir.path(), "A.java");
        let b = extract_index(dir.path(), "B.java");
        assert_alive(&graph, &a, "shared", "A");
        assert_alive(&graph, &b, "shared", "B");
    }
}
