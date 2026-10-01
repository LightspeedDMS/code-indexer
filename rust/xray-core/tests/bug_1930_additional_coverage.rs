//! Issue #1930 (rework, item 1 -- Codex P2 coverage): the original fix
//! and its follow-ups (items 2/3) are proven correct by real-extraction
//! tests, but several AST shapes the fix logic depends on -- Kotlin
//! object literals, companion objects, extension functions; a Java
//! method REFERENCE (`this::target`, as opposed to an ordinary call)
//! after an anonymous body; and an enum constant's own inline body --
//! had no dedicated coverage. Every test here drives real source through
//! the REAL front door (`build_graph_over`), asserting on
//! `callers_index` dense-id sets, mirroring the sibling `bug_1930_*.rs`
//! files' established convention. Neutral naming (`com.example.app`),
//! per this repository's Disclosure Discipline.

mod common;

use common::{build_graph_over, extract_index, write_source};
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::SymbolId;

/// Shared execution/assertion flow every fixture below drives
/// identically: extract, let `resolve_symbols` pick out (the real
/// enclosing method, `target`, every symbol that must NOT be `target`'s
/// caller) from the extracted `LocalIndex`, build the real graph, and
/// assert `target`'s ONLY caller is the enclosing method.
fn assert_target_attributed_only_to_the_enclosing_method(
    relative_path: &str,
    source: &str,
    resolve_symbols: impl Fn(&LocalIndex) -> (SymbolId, SymbolId, Vec<SymbolId>),
    context: &str,
) {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), relative_path, source);

    let index = extract_index(dir.path(), relative_path);
    let (enclosing_symbol, target_symbol, forbidden_symbols) = resolve_symbols(&index);

    let graph = build_graph_over(dir.path(), &[relative_path]);
    let target_dense = graph.dense_id_for(target_symbol).expect("target must be interned");
    let enclosing_dense = graph
        .dense_id_for(enclosing_symbol)
        .expect("the enclosing method must be interned");

    let target_callers = graph.callers_index(target_dense);
    assert_eq!(
        target_callers,
        &[enclosing_dense],
        "{context}: target must be attributed to the real enclosing method alone, got \
         {target_callers:?}"
    );
    for forbidden_symbol in forbidden_symbols {
        let forbidden_dense = graph
            .dense_id_for(forbidden_symbol)
            .expect("a forbidden symbol must be interned");
        assert!(
            !target_callers.contains(&forbidden_dense),
            "{context}: target must never be attributed to an inner method declared earlier \
             in the same body, got {target_callers:?}"
        );
    }
}

// =====================================================================
// Kotlin: a function containing an `object :` literal AND a local `fun`,
// followed by `target()`. `target()` must be attributed to the
// enclosing function alone.
// =====================================================================

const KOTLIN_OBJECT_LITERAL_AND_LOCAL_FUN_SOURCE: &str = r#"package com.example.app

interface Worker {
    fun run()
}

class Outer {
    fun test() {
        val w = object : Worker {
            override fun run() {
                helper()
            }
        }
        fun localFun() {
            helper()
        }
        localFun()
        target()
    }
    fun helper() {}
    fun target() {}
}
"#;

#[test]
fn kotlin_function_with_object_literal_and_local_fun_then_target_is_attributed_to_the_function() {
    assert_target_attributed_only_to_the_enclosing_method(
        "com/example/app/Outer.kt",
        KOTLIN_OBJECT_LITERAL_AND_LOCAL_FUN_SOURCE,
        |index| {
            let test_symbol = common::declaration_symbol_owned_by(index, "test", "Outer");
            let target_symbol = common::declaration_symbol_owned_by(index, "target", "Outer");
            let local_fun_symbol = common::declaration_symbol_owned_by(index, "localFun", "Outer");
            // `run` collides between the interface's own abstract
            // declaration (lower line) and the object literal's override
            // (higher line) -- see `declaration_symbols_by_line`'s own
            // doc comment.
            let run_symbols = common::declaration_symbols_by_line(index, "run");
            assert_eq!(run_symbols.len(), 2, "fixture bug: expected Worker.run() and the override");
            (test_symbol, target_symbol, vec![local_fun_symbol, run_symbols[1]])
        },
        "Kotlin function with object literal + local fun",
    );
}

// =====================================================================
// Kotlin: a companion object function.
// =====================================================================

const KOTLIN_COMPANION_OBJECT_SOURCE: &str = r#"package com.example.app

interface Worker {
    fun run()
}

class Outer {
    companion object {
        fun test() {
            val w = object : Worker {
                override fun run() {
                    helper()
                }
            }
            target()
        }
        fun helper() {}
        fun target() {}
    }
}
"#;

#[test]
fn kotlin_companion_object_function_call_after_an_object_literal_is_attributed_to_the_function() {
    assert_target_attributed_only_to_the_enclosing_method(
        "com/example/app/Outer.kt",
        KOTLIN_COMPANION_OBJECT_SOURCE,
        |index| {
            let test_symbol = common::declaration_symbol_owned_by(index, "test", "Companion");
            let target_symbol = common::declaration_symbol_owned_by(index, "target", "Companion");
            let run_symbols = common::declaration_symbols_by_line(index, "run");
            assert_eq!(run_symbols.len(), 2, "fixture bug: expected Worker.run() and the override");
            (test_symbol, target_symbol, vec![run_symbols[1]])
        },
        "Kotlin companion object function",
    );
}

// =====================================================================
// Kotlin: an extension function (top-level, no enclosing type).
// =====================================================================

const KOTLIN_EXTENSION_FUNCTION_SOURCE: &str = r#"package com.example.app

interface Worker {
    fun run()
}

class Thing

fun Thing.test() {
    val w = object : Worker {
        override fun run() {
            helper()
        }
    }
    target()
}

fun helper() {}
fun target() {}
"#;

#[test]
fn kotlin_extension_function_call_after_an_object_literal_is_attributed_to_the_function() {
    assert_target_attributed_only_to_the_enclosing_method(
        "com/example/app/Outer.kt",
        KOTLIN_EXTENSION_FUNCTION_SOURCE,
        |index| {
            // An extension function declared at file scope has no
            // `enclosing_type` (it is not a member of `Thing`, merely
            // scoped BY it) -- real extraction records no
            // `MethodOwnerRecord` for it, so it is looked up unqualified,
            // same as `target` (unique in this fixture).
            let test_symbol = common::declaration_symbol(index, "test");
            let target_symbol = common::declaration_symbol(index, "target");
            let run_symbols = common::declaration_symbols_by_line(index, "run");
            assert_eq!(run_symbols.len(), 2, "fixture bug: expected Worker.run() and the override");
            (test_symbol, target_symbol, vec![run_symbols[1]])
        },
        "Kotlin extension function",
    );
}

// =====================================================================
// Java: `this::target` (a METHOD REFERENCE, not an ordinary call) after
// an anonymous class body -- `push_constructor_reference`/`extract_
// method_reference` route through the SAME `InvocationSite::enclosing_
// method` field an ordinary call uses; this pins that the shared path
// really is exercised for the method-reference grammar shape, not just
// ordinary calls.
// =====================================================================

const JAVA_METHOD_REFERENCE_AFTER_ANON_SOURCE: &str = r#"package com.example.app;

import java.util.function.Supplier;

public class Outer {
    void test() {
        Runnable a = new Runnable() { public void run() { helper(); } };
        Supplier<Object> s = this::target;
    }
    void helper() {}
    Object target() {
        return null;
    }
}
"#;

#[test]
fn java_this_target_method_reference_after_an_anonymous_body_is_attributed_to_the_enclosing_method() {
    assert_target_attributed_only_to_the_enclosing_method(
        "com/example/app/Outer.java",
        JAVA_METHOD_REFERENCE_AFTER_ANON_SOURCE,
        |index| {
            let test_symbol = common::declaration_symbol_owned_by(index, "test", "Outer");
            let target_symbol = common::declaration_symbol_owned_by(index, "target", "Outer");
            let run_symbol = common::declaration_symbol(index, "run");
            (test_symbol, target_symbol, vec![run_symbol])
        },
        "Java this::target method reference after an anonymous body",
    );
}

// =====================================================================
// REGRESSION GUARD (Issue #1930 rework, item 4/P4): an enum constant's
// own inline body method (`RED { void run() {...} }`) must never leak
// into a SIBLING method's own call attribution -- coverage for the AST
// shape `anonymous_body_context` (java.rs) also handles for `enum_
// constant`, not just `object_creation_expression`. This test already
// PASSES on HEAD without any production change (`test`/`target` are
// declared as real methods, and enum constants live in a separate type
// entirely from `Outer`'s own methods, so no shared-scope misattribution
// is possible here) -- it exists to CATCH a future regression, never to
// prove the current fix.
// =====================================================================

const JAVA_ENUM_CONSTANT_BODY_SOURCE: &str = r#"package com.example.app;

public class Outer {
    enum Color {
        RED {
            void run() {
                helper();
            }
        };
        abstract void run();
    }
    void test() {
        target();
    }
    void helper() {}
    void target() {}
}
"#;

#[test]
fn java_enum_constant_body_method_does_not_leak_into_a_sibling_methods_attribution() {
    assert_target_attributed_only_to_the_enclosing_method(
        "com/example/app/Outer.java",
        JAVA_ENUM_CONSTANT_BODY_SOURCE,
        |index| {
            let test_symbol = common::declaration_symbol_owned_by(index, "test", "Outer");
            let target_symbol = common::declaration_symbol_owned_by(index, "target", "Outer");
            // `run` collides between `Color`'s own abstract declaration
            // and `RED`'s inline override -- see `declaration_symbols_
            // by_line`'s own doc comment.
            let run_symbols = common::declaration_symbols_by_line(index, "run");
            assert_eq!(run_symbols.len(), 2, "fixture bug: expected Color.run() and RED's override");
            (test_symbol, target_symbol, vec![run_symbols[1]])
        },
        "Java enum constant body method",
    );
}

// =====================================================================
// Java: annotation usage attribution (Issue #1930). The moved
// `extract_annotation_usage_reference` (java_invocations.rs) now
// receives `ctx.enclosing_method`, exactly like an ordinary
// call. Deliberate decision, not an oversight: attributing an IN-BODY
// annotation usage (`@Marker int local = 0;`, inside `test()`) to the
// enclosing method is the consistent, correct behaviour -- it is written
// lexically inside `test()`, exactly like any other reference there, and
// this test PINS that. A CLASS-LEVEL annotation usage (`@Marker` on
// `Outer` itself) is UNCHANGED: `ctx.enclosing_method` is `None` there
// (a type declaration always resets it), so it falls straight through to
// the same line heuristic annotation usages always used, before or
// after this rework -- here that lands self-referentially on `Outer`'s
// own declaration (the nearest declaration at its own line, since a
// `class_declaration` node's span starts at its own leading annotation).
// =====================================================================

const JAVA_ANNOTATION_USAGE_SOURCE: &str = r#"package com.example.app;

@Marker
public class Outer {
    void test() {
        Runnable a = new Runnable() { public void run() { helper(); } };
        @Marker
        int local = 0;
    }
    void helper() {}
}

@interface Marker {}
"#;

#[test]
fn java_in_body_annotation_usage_is_attributed_to_the_enclosing_method_class_level_unchanged() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Outer.java", JAVA_ANNOTATION_USAGE_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let marker_symbol = common::type_declaration_symbol(&index, "Marker");
    let test_symbol = common::declaration_symbol_owned_by(&index, "test", "Outer");
    let outer_symbol = common::type_declaration_symbol(&index, "Outer");
    // The anonymous Runnable's own run() -- declared BEFORE the in-body
    // @Marker usage -- is the nearest preceding declaration by line. A
    // plain line heuristic (no enclosing_method threading for annotation
    // sites) would wrongly attribute @Marker's in-body usage to it; this
    // is what makes the fixture genuinely discriminating (the original
    // fixture, with no declaration between test()'s start and @Marker,
    // would have passed even without the fix).
    let run_symbol = common::declaration_symbol(&index, "run");

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let marker_dense = graph.dense_id_for(marker_symbol).expect("Marker must be interned");
    let test_dense = graph.dense_id_for(test_symbol).expect("test must be interned");
    let outer_dense = graph.dense_id_for(outer_symbol).expect("Outer must be interned");
    let run_dense = graph.dense_id_for(run_symbol).expect("run must be interned");

    let mut marker_callers = graph.callers_index(marker_dense).to_vec();
    marker_callers.sort_unstable();
    assert!(
        !marker_callers.contains(&run_dense),
        "@Marker's in-body usage must NEVER be attributed to the anonymous run() method \
         declared earlier in the same body, got {marker_callers:?}"
    );
    let mut expected_callers = vec![test_dense, outer_dense];
    expected_callers.sort_unstable();
    assert_eq!(
        marker_callers, expected_callers,
        "@Marker must be attributed to BOTH test() (the in-body usage, attributed to its \
         enclosing method) AND Outer itself (the class-level usage, unchanged -- still the \
         line heuristic's own self-referential result), got {marker_callers:?}"
    );
}
