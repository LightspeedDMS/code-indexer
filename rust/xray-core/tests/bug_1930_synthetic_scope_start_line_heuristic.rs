//! Issue #1930: a synthetic scope (a Java static/instance initializer or
//! record compact constructor body, or a Kotlin getter/setter/`init`
//! block) attributes a call/construction/type reference made directly
//! inside it to its LEXICALLY ENCLOSING TYPE -- never a line search over
//! `declarations` at all.
//!
//! An earlier, line-heuristic-based version of this fix evaluated the
//! ordinary nearest-preceding-declaration heuristic at the scope's own
//! START line instead of at the call's line, which fixed the narrow case
//! where the OFFENDING declaration sits INSIDE the scope making the
//! call. Real fixtures show that heuristic still reaches PAST the
//! scope's own boundary: an EARLIER nested type's own method, or a
//! PREVIOUS sibling initializer's anonymous class, can still be the
//! nearest declaration by line even though neither has anything to do
//! with the scope making the call. Attributing directly to the enclosing
//! type (`SyntheticScopeRecord::enclosing_type_symbol`) closes every
//! such case by construction: with no search, nothing else CAN win.
//!
//! The first four tests below pin that direct-attribution behaviour for
//! the same fixture shapes a static/instance initializer, a record
//! compact constructor, and a Kotlin `init` block each produce; the next
//! four pin the misattribution shapes the line-heuristic-only version
//! could still reach (an earlier nested type, or a previous sibling
//! initializer's anonymous class); the last is a documented residual
//! limitation (see its own doc comment).
//!
//! Every fixture below drives real source through the REAL front door
//! (`build_graph_over`), asserting on `callers_index` dense-id sets.
//! Neutral naming (`com.example.app`), per this repository's Disclosure
//! Discipline.

mod common;

use common::{
    build_graph_over, declaration_symbol, declaration_symbol_owned_by, extract_index,
    type_declaration_symbol, write_source,
};
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::SymbolId;

/// Shared execution/assertion flow every fixture below drives
/// identically: extract, resolve `check`/the FORBIDDEN declaration
/// (whichever inner declaration the OLD line heuristic wrongly picked),
/// build the real graph, and assert `check`'s ONLY caller is the
/// lexically enclosing type -- never the forbidden declaration, which
/// has nothing to do with the scope making the call.
fn assert_call_attributed_to_the_enclosing_type(
    relative_path: &str,
    source: &str,
    check_owner: &str,
    forbidden_name: &str,
    expected_caller: impl Fn(&LocalIndex) -> SymbolId,
    context: &str,
) {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), relative_path, source);

    let index = extract_index(dir.path(), relative_path);
    let check_symbol = declaration_symbol_owned_by(&index, "check", check_owner);
    let forbidden_symbol = declaration_symbol(&index, forbidden_name);
    let expected_caller_symbol = expected_caller(&index);

    let graph = build_graph_over(dir.path(), &[relative_path]);
    let check_dense = graph.dense_id_for(check_symbol).expect("check must be interned");
    let forbidden_dense = graph
        .dense_id_for(forbidden_symbol)
        .expect("the forbidden declaration must be interned");
    let expected_dense = graph
        .dense_id_for(expected_caller_symbol)
        .expect("the expected caller must be interned");

    let check_callers = graph.callers_index(check_dense);
    assert!(
        !check_callers.contains(&forbidden_dense),
        "{context}: check(..) must NEVER be credited to {forbidden_name}() -- it has nothing \
         to do with the scope making the call, got callers {check_callers:?}"
    );
    assert_eq!(
        check_callers,
        &[expected_dense],
        "{context}: check(..) must be attributed to its lexically enclosing type directly, \
         got {check_callers:?}"
    );
}

fn outer_type_declaration(index: &LocalIndex) -> SymbolId {
    type_declaration_symbol(index, "Outer")
}

fn wrapper_type_declaration(index: &LocalIndex) -> SymbolId {
    type_declaration_symbol(index, "Wrapper")
}

// =====================================================================
// Static initializer block: attributed to `Outer`'s own type
// declaration, not `before()`.
// =====================================================================

const STATIC_INIT_WITH_ANON_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void before() {}
    static {
        Runnable a = new Runnable() { public void run() { helper(); } };
        check(a);
    }
    void helper() {}
    static void check(Runnable a) {}
}
"#;

#[test]
fn static_initializer_call_is_attributed_to_the_enclosing_type() {
    assert_call_attributed_to_the_enclosing_type(
        "com/example/app/Outer.java",
        STATIC_INIT_WITH_ANON_SOURCE,
        "Outer",
        "run",
        outer_type_declaration,
        "static initializer",
    );
}

// =====================================================================
// Instance initializer block: same shape, same expectation.
// =====================================================================

const INSTANCE_INIT_WITH_ANON_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void before() {}
    {
        Runnable a = new Runnable() { public void run() { helper(); } };
        check(a);
    }
    void helper() {}
    void check(Runnable a) {}
}
"#;

#[test]
fn instance_initializer_call_is_attributed_to_the_enclosing_type() {
    assert_call_attributed_to_the_enclosing_type(
        "com/example/app/Outer.java",
        INSTANCE_INIT_WITH_ANON_SOURCE,
        "Outer",
        "run",
        outer_type_declaration,
        "instance initializer",
    );
}

// =====================================================================
// Record compact constructor: attributed directly to Wrapper's own type
// declaration, with no line search at all.
// =====================================================================

const RECORD_COMPACT_CTOR_WITH_ANON_SOURCE: &str = r#"package com.example.app;

public class Outer {
    record Wrapper(int x) {
        Wrapper {
            Runnable a = new Runnable() { public void run() { helper(); } };
            check(a);
        }
        void helper() {}
        static void check(Runnable a) {}
    }
}
"#;

#[test]
fn record_compact_constructor_call_is_attributed_to_the_enclosing_type() {
    assert_call_attributed_to_the_enclosing_type(
        "com/example/app/Outer.java",
        RECORD_COMPACT_CTOR_WITH_ANON_SOURCE,
        "Wrapper",
        "run",
        wrapper_type_declaration,
        "record compact constructor",
    );
}

// =====================================================================
// Kotlin `init` block: same shape as the Java static/instance
// initializer cases above.
// =====================================================================

const KOTLIN_INIT_WITH_OBJECT_LITERAL_SOURCE: &str = r#"package com.example.app

interface Worker {
    fun run()
}

class Outer {
    fun before() {}
    init {
        val a = object : Worker {
            override fun run() {
                helper()
            }
        }
        check(a)
    }
    fun helper() {}
    fun check(a: Worker) {}
}
"#;

#[test]
fn kotlin_init_block_call_is_attributed_to_the_enclosing_type() {
    assert_call_attributed_to_the_enclosing_type(
        "com/example/app/Outer.kt",
        KOTLIN_INIT_WITH_OBJECT_LITERAL_SOURCE,
        "Outer",
        "run",
        outer_type_declaration,
        "Kotlin init block",
    );
}

// =====================================================================
// The WHOLE static block written on a SINGLE source line. A start-
// line-anchored heuristic evaluates at the block's own start line --
// but that is the SAME line the anonymous run() is declared on AND the
// same line check() is called on, so run() is STILL `<= ` that line and
// still wins as the max. Only attributing directly to the enclosing
// type (never comparing lines at all) fixes this.
// =====================================================================

const SINGLE_LINE_STATIC_BLOCK_SOURCE: &str = r#"package com.example.app;

public class Outer {
    static { Runnable r1 = new Runnable() { public void run() { helper(); } }; check(); }
    void helper() {}
    static void check() {}
}
"#;

#[test]
fn single_line_static_block_call_is_attributed_to_the_enclosing_type() {
    assert_call_attributed_to_the_enclosing_type(
        "com/example/app/Outer.java",
        SINGLE_LINE_STATIC_BLOCK_SOURCE,
        "Outer",
        "run",
        outer_type_declaration,
        "single-line static block",
    );
}

// =====================================================================
// An instance initializer's own call must not be credited to an
// anonymous class declared inside a DIFFERENT, PRECEDING sibling scope
// (a static initializer) -- the two are independent synthetic scopes,
// each with its own record, but a start-line-anchored heuristic for the
// INSTANCE block still searches declarations globally and finds the
// static block's anonymous run() as the nearest preceding one.
// =====================================================================

const INSTANCE_INIT_AFTER_STATIC_BLOCK_WITH_ANON_SOURCE: &str = r#"package com.example.app;

public class Outer {
    static {
        Runnable a = new Runnable() { public void run() { helper(); } };
    }
    {
        check();
    }
    void helper() {}
    void check() {}
}
"#;

#[test]
fn instance_initializer_after_a_static_block_with_an_anonymous_class_is_attributed_to_the_enclosing_type(
) {
    assert_call_attributed_to_the_enclosing_type(
        "com/example/app/Outer.java",
        INSTANCE_INIT_AFTER_STATIC_BLOCK_WITH_ANON_SOURCE,
        "Outer",
        "run",
        outer_type_declaration,
        "instance initializer after a static block containing an anonymous class",
    );
}

// =====================================================================
// A static initializer's own call must not be credited to an EARLIER,
// UNRELATED nested type's own method -- the nested type is a SIBLING
// declaration, not part of the static block at all, but a plain line
// heuristic still reaches it as "the nearest preceding declaration".
// =====================================================================

const STATIC_CLASS_THEN_STATIC_BLOCK_SOURCE: &str = r#"package com.example.app;

public class Outer {
    static class Inner {
        void im() {}
    }
    static {
        check();
    }
    static void check() {}
}
"#;

#[test]
fn static_block_after_an_earlier_nested_static_class_is_attributed_to_the_enclosing_type() {
    assert_call_attributed_to_the_enclosing_type(
        "com/example/app/Outer.java",
        STATIC_CLASS_THEN_STATIC_BLOCK_SOURCE,
        "Outer",
        "im",
        outer_type_declaration,
        "static block after an earlier nested static class",
    );
}

// =====================================================================
// An instance initializer's own call must not be credited to an
// earlier, unrelated NON-STATIC nested class's own method either -- the
// same defect as the shape above, for a plain (non-static) nested class
// and an instance initializer.
// =====================================================================

const NESTED_CLASS_THEN_INSTANCE_INIT_SOURCE: &str = r#"package com.example.app;

public class Outer {
    class Inner {
        void wrongOwner() {}
    }
    {
        check();
    }
    void check() {}
}
"#;

#[test]
fn instance_init_after_an_earlier_nested_class_is_attributed_to_the_enclosing_type() {
    assert_call_attributed_to_the_enclosing_type(
        "com/example/app/Outer.java",
        NESTED_CLASS_THEN_INSTANCE_INIT_SOURCE,
        "Outer",
        "wrongOwner",
        outer_type_declaration,
        "instance initializer after an earlier nested class",
    );
}

// =====================================================================
// RESIDUAL CASE, DOCUMENTED LIMITATION: an instance initializer block
// written directly inside a Java ANONYMOUS class's own body. Java never
// gives an anonymous class body its own type symbol (no real
// `Declaration` exists for it either -- see `anonymous_body_context`,
// java.rs), so `SyntheticScopeRecord::enclosing_type_symbol` is `None`
// for this one shape, and `enclosing_symbol_for_site` falls back to the
// ordinary nearest-preceding-declaration line heuristic (evaluated at
// the block's own start line) instead of attributing straight to a
// known enclosing type. This test pins that CURRENT, line-heuristic-
// based behaviour -- it is a documented limitation, not a claim of
// correctness: a different fixture shape (a nested anonymous/local
// class declared between the outer method and this instance
// initializer) could still misattribute here, the same way every other
// site this heuristic serves always could.
// =====================================================================

const INSTANCE_INIT_INSIDE_ANONYMOUS_CLASS_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void before() {}
    void test() {
        Runnable a = new Runnable() {
            {
                target();
            }
            public void run() {}
        };
    }
    void target() {}
}
"#;

#[test]
fn instance_initializer_inside_an_anonymous_class_body_falls_back_to_the_line_heuristic() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Outer.java",
        INSTANCE_INIT_INSIDE_ANONYMOUS_CLASS_SOURCE,
    );

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let test_symbol = declaration_symbol_owned_by(&index, "test", "Outer");
    let target_symbol = declaration_symbol_owned_by(&index, "target", "Outer");
    assert_eq!(
        index.synthetic_scopes.len(),
        1,
        "fixture bug: expected exactly one synthetic scope -- the anonymous class's own \
         instance initializer"
    );
    assert_eq!(
        index.synthetic_scopes[0].enclosing_type_symbol, None,
        "documented limitation: an anonymous class's own instance initializer has no known \
         enclosing type symbol"
    );

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let target_dense = graph.dense_id_for(target_symbol).expect("target must be interned");
    let test_dense = graph.dense_id_for(test_symbol).expect("test must be interned");

    let target_callers = graph.callers_index(target_dense);
    assert_eq!(
        target_callers,
        &[test_dense],
        "current (heuristic-based) behaviour: target(), called inside the anonymous class's \
         own instance initializer, falls back to the nearest preceding real declaration \
         (test(), at the initializer's own start line) -- got {target_callers:?}"
    );
}
