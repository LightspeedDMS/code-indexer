//! Regression coverage for the #1898 code review (epic #1906, P1). The
//! narrowing.rs fix for #1898 removed the "no candidate matches -> keep
//! the entire bare-name pool" fallback from THREE passes (arity,
//! receiver-type, same-class-or-super). For arity that is correct: once
//! the evidence is known, an unmatched call really has no in-repo target.
//! Receiver-type narrowing went through several review rounds trying the
//! same hard-empty shape, but shipped differently -- see the SCOPE SPLIT
//! paragraph below: `apply_receiver_type_narrowing` is TAG-ONLY, marking
//! `RECEIVER_TYPE_MATCH` without ever removing a candidate, matched or
//! not. But `apply_same_class_or_super_narrowing`'s
//! `allowed` set (`{enclosing_type} U supertypes_of(enclosing_type)`)
//! does NOT model two other places Java resolves an unqualified call:
//! the caller's LEXICALLY ENCLOSING type chain (inner/anonymous/static-
//! nested/local classes calling an outer method) and STATIC IMPORTS. A
//! hard-empty narrow there deleted real edges and could flip a private
//! target's dead-code verdict to a false `Some(true)` -- the exact
//! outcome epic #1786 declared structurally impossible.
//!
//! These tests drive real, javac-shaped Java source through the real
//! `JavaExtractor` + `bind` pipeline (no hand-built `LocalIndex`, no
//! mocking) and assert on `callers_index`/`is_definitely_dead_code`,
//! exactly the level the unit tests in `graph::bind::resolve_tests*`
//! cannot see (those hand-feed `resolve_reference` directly and never
//! exercise the lexical-nesting/static-import gap at all).
//!
//! **#1898 SCOPE SPLIT (epic #1906, round-4 review, `.analysis/
//! 1898-review-rounds/round4-findings.md`)**: `apply_receiver_type_
//! narrowing` is now TAG-ONLY -- it never removes a candidate, empty
//! match or not, Positive evidence or Advisory. This retired the AC3
//! "binds ONLY within the qualified type" guarantee this file originally
//! proved, deferring exclusive receiver-type binding to a follow-up issue
//! named in `docs/xray-architecture.md`'s candidate-admission section.
//! The arity-based tests in this file
//! (`ac2_unique_wrong_arity_external_receiver_call_yields_zero_callees`
//! and the P1-1/P1-2 same-class-or-super shapes) are UNCHANGED --
//! `apply_arity_narrowing` was never implicated in any of the four
//! review rounds.
//!
//! **#1922 (that follow-up landed)**: a genuinely TYPE-QUALIFIED call/
//! method-reference (`Type.m(x)`, `Type::m`) now DOES hard-narrow, via a
//! narrower, NEW mechanism (`narrowing::apply_type_qualifier_narrowing`,
//! gated on `receiver::is_definite_type_qualifier`) -- not by reversing
//! `apply_receiver_type_narrowing`'s TAG-ONLY doctrine, which stays
//! permanently soft for the INSTANCE-receiver case #1898/#1910 proved
//! unsafe to hard-narrow (inferred local-variable/field types, subject to
//! `(enclosing_method, name)` scope-key collisions). The two are
//! orthogonal: #1922's qualifier is the literal bare identifier the
//! source itself wrote, never an inferred type. `ac3_statically_
//! qualified_call_keeps_the_qualified_types_target_but_no_longer_
//! excludes_noise` below is UPDATED (not superseded) to assert the new,
//! correct exclusive-binding outcome.

use std::path::Path;
use xray_core::graph::bind::{bind, FileForBind};
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::extract::LanguageExtractor;
use xray_core::graph::fused::process_file_fused;
use xray_core::graph::identity::{file_id, SymbolId};
use xray_core::graph::user_facts::{FactCollector, UserFact};
use xray_core::owned_node::OwnedNode;

fn extract_java(source: &str) -> LocalIndex {
    let dir = tempfile::tempdir().expect("create temp dir");
    let path = dir.path().join("Sample.java");
    std::fs::write(&path, source).expect("write fixture source");
    let root = xray_core::scanner::parse_file(&path).expect("fixture source must parse");
    JavaExtractor.extract(&root, 1)
}

/// Multi-file variant (static import / qualified-call fixtures need
/// several real files on disk, each with its own package): mirrors
/// `ac4_bind_confidence_candidates.rs`'s own extraction harness verbatim
/// (real fused pipeline, no hand-built `LocalIndex`).
struct NoOpCollector;
impl FactCollector for NoOpCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        Vec::new()
    }
}

fn write_java(dir: &Path, relative_path: &str, source: &str) {
    std::fs::write(dir.join(relative_path), source).unwrap();
}

fn extract_java_file(dir: &Path, relative_path: &str) -> FileForBind {
    let full_path = dir.join(relative_path);
    let result = process_file_fused(&full_path, relative_path, &NoOpCollector)
        .unwrap_or_else(|| panic!("failed to parse {relative_path}"));
    let index = result
        .index
        .unwrap_or_else(|| panic!("extraction did not complete for {relative_path}"));
    FileForBind { file_id: file_id(relative_path), language: "java".to_string(), index }
}

/// The symbol of the declaration named `name` whose `MethodOwnerRecord`
/// names `enclosing_type` -- for fixtures that deliberately declare the
/// SAME method name in more than one place, so the AC4 Level 5
/// unique-name-in-repo shortcut (which bypasses every narrowing pass,
/// `apply_same_class_or_super_narrowing` included) never applies and the
/// call is genuinely forced through the real narrowing pipeline.
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

/// P1-1 (inner class): `Inner`, a non-static inner class of `Outer`, makes
/// a bare call to `Outer`'s own `private` method `helper()`. Real javac
/// accepts this (an inner class has access to every member of its
/// enclosing class, private included). The caller's OWN `enclosing_type`
/// as recorded by the extractor is `"Inner"`, which has no recorded
/// inheritance relationship to `"Outer"` at all -- so
/// `apply_same_class_or_super_narrowing`'s `allowed` set for this call is
/// `{"Inner"}` only, and `helper`'s candidate (`enclosing_type: "Outer"`)
/// never matches it. `Unrelated.helper()` (an unconnected top-level type,
/// same 0-param arity) makes `"helper"` NON-unique repo-wide, so the AC4
/// Level 5 unique-name shortcut -- which uses a DIFFERENT, top-level-
/// domain-aware visibility check that happens to also accept this exact
/// case -- never fires; this genuinely forces the call through
/// `apply_same_class_or_super_narrowing` itself.
#[test]
fn inner_class_calling_enclosing_private_method_keeps_a_real_caller_edge() {
    let source = r#"
class Outer {
    private void helper() {}

    class Inner {
        void run() {
            helper();
        }
    }
}

class Unrelated {
    private void helper() {}
}
"#;
    let index = extract_java(source);
    let helper = declaration_symbol_owned_by(&index, "helper", "Outer");
    let graph = bind(vec![FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }]);
    let dense = graph.dense_id_for(helper).expect("helper must be interned");
    assert!(
        !graph.callers_index(dense).is_empty(),
        "Inner.run()'s bare helper() call must reach Outer's private helper() -- an inner \
         class has access to its enclosing class's private members under real javac"
    );
    assert_ne!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "Outer.helper() is genuinely called from Inner.run() and must never be reported \
         definitely dead"
    );
}

/// P1-1 (anonymous inner class): the `class_body` of `new Runnable() { ... }`
/// is a distinct anonymous type (see `anonymous_body_context` in
/// `extract/java.rs`) whose ONLY recorded supertype is `Runnable` --
/// nothing at all ties it to `Outer`.
#[test]
fn anonymous_class_calling_enclosing_private_method_keeps_a_real_caller_edge() {
    let source = r#"
class Outer {
    private void helper() {}

    Runnable make() {
        return new Runnable() {
            public void run() {
                helper();
            }
        };
    }
}

class Unrelated {
    private void helper() {}
}
"#;
    let index = extract_java(source);
    let helper = declaration_symbol_owned_by(&index, "helper", "Outer");
    let graph = bind(vec![FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }]);
    let dense = graph.dense_id_for(helper).expect("helper must be interned");
    assert!(
        !graph.callers_index(dense).is_empty(),
        "the anonymous Runnable's run() calling bare helper() must reach Outer's private \
         helper() -- an anonymous inner class has access to its enclosing class's private \
         members under real javac"
    );
    assert_ne!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "Outer.helper() is genuinely called from the anonymous class body and must never be \
         reported definitely dead"
    );
}

/// P1-1 (static nested class): `Nested` is `static`, so it has no
/// implicit outer-instance reference, but Java still grants it access to
/// `Outer`'s private STATIC members via a bare call -- and, exactly like
/// the non-static inner-class case, the extractor records no inheritance
/// relationship between `Nested` and `Outer` at all.
#[test]
fn static_nested_class_calling_enclosing_private_method_keeps_a_real_caller_edge() {
    let source = r#"
class Outer {
    private static void helper() {}

    static class Nested {
        void run() {
            helper();
        }
    }
}

class Unrelated {
    private void helper() {}
}
"#;
    let index = extract_java(source);
    let helper = declaration_symbol_owned_by(&index, "helper", "Outer");
    let graph = bind(vec![FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }]);
    let dense = graph.dense_id_for(helper).expect("helper must be interned");
    assert!(
        !graph.callers_index(dense).is_empty(),
        "Nested.run()'s bare helper() call must reach Outer's private static helper() under \
         real javac"
    );
    assert_ne!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "Outer.helper() is genuinely called from Nested.run() and must never be reported \
         definitely dead"
    );
}

/// P1-1 (local class): `Local` is declared INSIDE a method body -- Java
/// grants it access to the enclosing class's private members exactly like
/// an ordinary inner class, and the extractor's `dispatch_type_declaration`
/// records no inheritance relationship for it either.
#[test]
fn local_class_calling_enclosing_private_method_keeps_a_real_caller_edge() {
    let source = r#"
class Outer {
    private void helper() {}

    void run() {
        class Local {
            void go() {
                helper();
            }
        }
        new Local().go();
    }
}

class Unrelated {
    private void helper() {}
}
"#;
    let index = extract_java(source);
    let helper = declaration_symbol_owned_by(&index, "helper", "Outer");
    let graph = bind(vec![FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }]);
    let dense = graph.dense_id_for(helper).expect("helper must be interned");
    assert!(
        !graph.callers_index(dense).is_empty(),
        "Local.go()'s bare helper() call must reach Outer's private helper() -- a local class \
         has access to its enclosing class's private members under real javac"
    );
    assert_ne!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "Outer.helper() is genuinely called from Local.go() and must never be reported \
         definitely dead"
    );
}

/// P1-2: `import static util.Util.helper;` then a bare `helper()` call.
/// `same_class_context` for this call is the CALLER's own enclosing type
/// (`"Caller"`), which has no inheritance relationship to `"Util"` at
/// all -- `resolve.rs::context_reasons`/`import_reasons` already tags the
/// candidate `reasons::STATIC_IMPORT`, but `apply_same_class_or_super_
/// narrowing` runs AFTER that tagging and, pre-fix, ignored it entirely,
/// wiping the candidate to empty. `Unrelated.helper()` again forces the
/// call through the real narrowing pipeline rather than the unique-name
/// shortcut.
#[test]
fn static_imported_in_repo_method_keeps_a_real_caller_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "Util.java",
        "package util;\npublic class Util {\n    public static void helper() {}\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package other;\nimport static util.Util.helper;\nclass Caller {\n    void run() {\n        helper();\n    }\n}\nclass Unrelated {\n    private void helper() {}\n}\n",
    );

    let util_file = extract_java_file(dir.path(), "Util.java");
    let util_helper = declaration_symbol_owned_by(&util_file.index, "helper", "Util");
    let files = vec![util_file, extract_java_file(dir.path(), "Caller.java")];
    let graph = bind(files);

    let dense = graph
        .dense_id_for(util_helper)
        .expect("Util.helper must be interned");
    assert!(
        !graph.callers_index(dense).is_empty(),
        "the static-imported bare helper() call in Caller.run() must reach Util.helper() -- \
         a static import is real evidence a same-class-or-super narrowing miss must not erase"
    );
    assert_ne!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "Util.helper() is genuinely called (via static import) from Caller.run() and must \
         never be reported definitely dead"
    );
}

/// P1-3 / AC2's own repro (#1898): `releaseHandle` calls `connection.close()`
/// (0 args) on a local variable whose declared type is the EXTERNAL
/// `java.sql.Connection`. The repo's ONLY declaration named `close` is
/// `Something.close(int code)` (1 param, unrelated top-level type) --
/// making `"close"` globally UNIQUE in the repo, so `resolve.rs::
/// try_unique_name_shortcut` fires BEFORE arity narrowing ever runs and,
/// pre-fix, admitted the wrong-arity candidate unconditionally (consulting
/// neither arity nor receiver type). `releaseHandle` must end up with
/// ZERO in-repo callees.
#[test]
fn ac2_unique_wrong_arity_external_receiver_call_yields_zero_callees() {
    let source = r#"
class ConnectionHolder {
    private void releaseHandle(java.sql.Connection connection) {
        connection.close();
    }
}

class Something {
    void close(int code) {}
}
"#;
    let index = extract_java(source);
    let release_handle = declaration_symbol_owned_by(&index, "releaseHandle", "ConnectionHolder");
    let graph = bind(vec![FileForBind {
        file_id: 1,
        language: "java".to_string(),
        index,
    }]);
    let dense = graph
        .dense_id_for(release_handle)
        .expect("releaseHandle must be interned");
    assert!(
        graph.callees_index(dense).is_empty(),
        "connection.close() (0 args, external java.sql.Connection receiver) must never bind \
         to the repo's unrelated Something.close(int) (1 param) -- got {} callee(s)",
        graph.callees_index(dense).len()
    );
}

/// AC3's own repro (#1898), UPDATED by #1922 (the deferred
/// follow-up): `TimeUtil.parse("a")` is a statically QUALIFIED call on
/// `TimeUtil`, a type this repo's graph knows about and which declares
/// its OWN 1-param `parse` method. Two unrelated classes (`ParserA`/
/// `ParserB`) also declare a same-named, same-arity `parse` -- the exact
/// shape from #1898's own bug report (`TimeUtil.parse(x)` fanning out to
/// 165 unrelated `parse` callees repo-wide) and from #1922's own static-
/// facade repro (a caller binding to itself instead of the qualified
/// type it explicitly delegates to). `apply_type_qualifier_narrowing`
/// (#1922) now hard-narrows this exact shape: `TimeUtil.parse` keeps its
/// real edge, and `ParserA.parse`/`ParserB.parse` are correctly EXCLUDED
/// -- no longer accepted noise.
#[test]
fn ac3_statically_qualified_call_binds_only_within_the_qualified_type() {
    let dir = tempfile::tempdir().unwrap();
    write_java(
        dir.path(),
        "TimeUtil.java",
        "package time;\npublic class TimeUtil {\n    static String parse(String s) { return s; }\n}\n",
    );
    write_java(
        dir.path(),
        "ParserA.java",
        "package other;\npublic class ParserA {\n    String parse(String s) { return s; }\n}\n",
    );
    write_java(
        dir.path(),
        "ParserB.java",
        "package other2;\npublic class ParserB {\n    String parse(String s) { return s; }\n}\n",
    );
    write_java(
        dir.path(),
        "Caller.java",
        "package caller;\nimport time.TimeUtil;\nclass Caller {\n    void run() {\n        TimeUtil.parse(\"a\");\n    }\n}\n",
    );

    let time_util_file = extract_java_file(dir.path(), "TimeUtil.java");
    let time_util_parse = declaration_symbol_owned_by(&time_util_file.index, "parse", "TimeUtil");
    let files = vec![
        time_util_file,
        extract_java_file(dir.path(), "ParserA.java"),
        extract_java_file(dir.path(), "ParserB.java"),
        extract_java_file(dir.path(), "Caller.java"),
    ];
    let graph = bind(files);

    let caller_index = extract_java_file(dir.path(), "Caller.java").index;
    let run_symbol = declaration_symbol_owned_by(&caller_index, "run", "Caller");
    let run_dense = graph
        .dense_id_for(run_symbol)
        .expect("Caller.run must be interned");
    let callees = graph.callees_index(run_dense);
    let time_util_dense = graph
        .dense_id_for(time_util_parse)
        .expect("TimeUtil.parse must be interned");
    assert!(
        callees.contains(&time_util_dense),
        "TimeUtil.parse(\"a\") must keep its real edge to TimeUtil.parse"
    );
    assert_eq!(
        callees.len(),
        1,
        "#1922: a statically-qualified call must bind EXCLUSIVELY within the qualified type -- \
         ParserA.parse/ParserB.parse must no longer survive as accepted noise -- got {} \
         callee(s)",
        callees.len()
    );
}
