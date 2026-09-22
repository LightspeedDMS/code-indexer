//! Issue #1930: caller attribution for a reference (`PendingReference::
//! from`) was computed EXCLUSIVELY by `resolve.rs::enclosing_symbol`, a
//! documented nearest-preceding-declaration-by-line heuristic -- even for
//! invocation/method-reference sites where the extractor's own AST walk
//! already recorded the TRUE enclosing method
//! (`InvocationSite::enclosing_method`, threaded by `java.rs`'s/
//! `kotlin.rs`'s stack-based `WalkContext`). When a method body contains
//! an anonymous (or local) class that declares its own method(s), a call
//! appearing textually AFTER that inner body is the nearest PRECEDING
//! declaration by line -- so the heuristic credits it to the inner
//! method, never the real enclosing method.
//!
//! Every fixture below drives real Java source through the REAL front
//! door (`build_graph_over` -> `build_repo_graph` -> `bind()`), never a
//! hand-built `LocalIndex`, and asserts on `callers_index`/
//! `callees_index` dense-id sets -- exactly this crate's established
//! real-extraction integration-test convention (see
//! `bug_1922_field_initializer_binding_regressions.rs`). Neutral naming
//! throughout (`com.example.app`, `Outer`/`Local`/`before`/`compute`) --
//! synthetic identifiers, per this repository's Disclosure Discipline.

mod common;

use common::{
    build_graph_over, declaration_symbol_owned_by, declaration_symbols_by_line, extract_index,
    type_declaration_symbol, write_source,
};

// `declaration_symbols_by_line` (used below where a fixture declares TWO
// same-named methods `declaration_symbol_owned_by`'s owner-name
// disambiguation cannot tell apart) lives in `common` -- see its own doc
// comment there. It is NOT because an anonymous class body "keeps" the
// surrounding `enclosing_type`: real extraction's `anonymous_body_
// context` (java.rs) actually resets `enclosing_type` to a SYNTHESIZED
// `{enclosing}$<anon@L{line}:{file_id}:{byte}>` name (Bug #1929 item 3;
// human-chaseable, but still per-body-unique) for every anonymous/
// enum-constant class body (verified directly against real extraction
// output), so two anonymous `run()`s below are owned by two DIFFERENT,
// unpredictable synthetic names, not by a shared "Outer". Line order,
// not owner name, is the
// only axis this bug is about regardless: the LAST (highest-line) match
// is the one the old heuristic wrongly credited a later call to.

// =====================================================================
// The issue's own reproduction shape: two anonymous `Runnable`s declared
// back to back, each with its own `run()` calling `helper()`, followed
// by a bare `target()` call. `target()` must be attributed to `test()`,
// never to the second anonymous `run()` (the nearest preceding
// declaration by line).
// =====================================================================

const TWO_ANON_RUNNABLES_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void test() {
        Runnable a = new Runnable() { public void run() { helper(); } };
        Runnable b = new Runnable() { public void run() { helper(); } };
        target();
    }
    void helper() {}
    void target() {}
}
"#;

#[test]
fn call_after_two_anonymous_runnables_is_attributed_to_the_enclosing_method() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Outer.java", TWO_ANON_RUNNABLES_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let test_symbol = declaration_symbol_owned_by(&index, "test", "Outer");
    let target_symbol = declaration_symbol_owned_by(&index, "target", "Outer");
    let run_symbols = declaration_symbols_by_line(&index, "run");
    assert_eq!(
        run_symbols.len(),
        2,
        "fixture bug: expected exactly two anonymous run() declarations"
    );
    let second_run_symbol = run_symbols[1];

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let target_dense = graph
        .dense_id_for(target_symbol)
        .expect("Outer.target must be interned");
    let test_dense = graph
        .dense_id_for(test_symbol)
        .expect("Outer.test must be interned");
    let second_run_dense = graph
        .dense_id_for(second_run_symbol)
        .expect("the second anonymous run() must be interned");

    let target_callers = graph.callers_index(target_dense);
    assert_eq!(
        target_callers,
        &[test_dense],
        "Outer.target() must be attributed to Outer.test() -- the real enclosing method -- \
         never to the anonymous run() method nearest-preceding it on the source line, \
         got caller dense ids {target_callers:?}"
    );

    let second_run_callees = graph.callees_index(second_run_dense);
    assert!(
        !second_run_callees.contains(&target_dense),
        "the second anonymous run() method must NOT be credited with calling target() -- \
         that call happens after its body ends, got callees {second_run_callees:?}"
    );
}

// =====================================================================
// A local class (not anonymous) declared inside a method body, whose own
// method is the nearest preceding declaration to a later call in the
// SAME enclosing method.
// =====================================================================

const LOCAL_CLASS_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void test() {
        class Local {
            void run() {
                helper();
            }
        }
        new Local().run();
        target();
    }
    void helper() {}
    void target() {}
}
"#;

#[test]
fn call_after_a_local_class_declaration_is_attributed_to_the_enclosing_method() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Outer.java", LOCAL_CLASS_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let test_symbol = declaration_symbol_owned_by(&index, "test", "Outer");
    let target_symbol = declaration_symbol_owned_by(&index, "target", "Outer");
    let local_run_symbol = declaration_symbol_owned_by(&index, "run", "Local");

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let target_dense = graph
        .dense_id_for(target_symbol)
        .expect("Outer.target must be interned");
    let test_dense = graph
        .dense_id_for(test_symbol)
        .expect("Outer.test must be interned");
    let local_run_dense = graph
        .dense_id_for(local_run_symbol)
        .expect("Local.run must be interned");

    let target_callers = graph.callers_index(target_dense);
    assert_eq!(
        target_callers,
        &[test_dense],
        "Outer.target() must be attributed to Outer.test(), never to Local.run() -- the local \
         class's own method, nearest-preceding it on the source line -- got caller dense ids \
         {target_callers:?}"
    );

    let local_run_callees = graph.callees_index(local_run_dense);
    assert!(
        !local_run_callees.contains(&target_dense),
        "Local.run() must NOT be credited with calling target() -- that call happens after the \
         local class declaration ends, got callees {local_run_callees:?}"
    );
}

// =====================================================================
// Nested anonymous classes: an inner anonymous Runnable declared inside
// an outer anonymous Runnable's own run() body. A call made in the OUTER
// run() body, after the inner anonymous class, must stay attributed to
// the OUTER run(), never fall to the INNER run() (the nearer declaration
// by line).
// =====================================================================

const NESTED_ANON_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void test() {
        Runnable outer = new Runnable() {
            public void run() {
                Runnable inner = new Runnable() { public void run() { helper(); } };
                target();
            }
        };
    }
    void helper() {}
    void target() {}
}
"#;

#[test]
fn nested_anonymous_classes_attribute_a_call_to_the_correct_inner_method() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Outer.java", NESTED_ANON_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let target_symbol = declaration_symbol_owned_by(&index, "target", "Outer");
    let run_symbols = declaration_symbols_by_line(&index, "run");
    assert_eq!(
        run_symbols.len(),
        2,
        "fixture bug: expected exactly two nested anonymous run() declarations"
    );
    let outer_run_symbol = run_symbols[0];
    let inner_run_symbol = run_symbols[1];

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let target_dense = graph
        .dense_id_for(target_symbol)
        .expect("Outer.target must be interned");
    let outer_run_dense = graph
        .dense_id_for(outer_run_symbol)
        .expect("the outer anonymous run() must be interned");
    let inner_run_dense = graph
        .dense_id_for(inner_run_symbol)
        .expect("the inner anonymous run() must be interned");

    let target_callers = graph.callers_index(target_dense);
    assert_eq!(
        target_callers,
        &[outer_run_dense],
        "target() is called from the OUTER anonymous run()'s own body, after the inner \
         anonymous class -- it must never be credited to the inner run(), got caller dense \
         ids {target_callers:?}"
    );

    let inner_run_callees = graph.callees_index(inner_run_dense);
    assert!(
        !inner_run_callees.contains(&target_dense),
        "the inner anonymous run() must NOT be credited with calling target() -- got callees \
         {inner_run_callees:?}"
    );
}

// =====================================================================
// A lambda is NOT a declaration -- its body must stay attributed to the
// REAL enclosing method, exactly as it always was (this is a
// non-regression guard, not a new failure mode).
// =====================================================================

const LAMBDA_SOURCE: &str = r#"package com.example.app;

import java.util.function.Supplier;

public class Outer {
    void test() {
        Supplier<Object> s = () -> helper();
        s.get();
    }
    Object helper() {
        return null;
    }
}
"#;

#[test]
fn lambda_body_call_is_attributed_to_the_enclosing_method_not_a_declaration() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Outer.java", LAMBDA_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let test_symbol = declaration_symbol_owned_by(&index, "test", "Outer");
    let helper_symbol = declaration_symbol_owned_by(&index, "helper", "Outer");

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let helper_dense = graph
        .dense_id_for(helper_symbol)
        .expect("Outer.helper must be interned");
    let test_dense = graph
        .dense_id_for(test_symbol)
        .expect("Outer.test must be interned");

    let helper_callers = graph.callers_index(helper_dense);
    assert_eq!(
        helper_callers,
        &[test_dense],
        "a lambda expression is not a declaration -- a call inside its body must stay \
         attributed to the real enclosing method Outer.test(), got caller dense ids \
         {helper_callers:?}"
    );
}

// =====================================================================
// Field initializer attribution is UNCHANGED by this fix: a call inside
// a field initializer has no `enclosing_method` at all (the extractor
// never sets one outside a method/constructor/initializer-block body),
// so it must keep falling through to the line heuristic exactly as
// before -- here, that heuristic lands on the FIELD's own declaration
// (the nearest declaration at or before the call's line, since the
// field's own `Declaration` is recorded at that same line).
// =====================================================================

const FIELD_INITIALIZER_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void before() {}
    static int VALUE = compute();
    static int compute() {
        return 1;
    }
}
"#;

#[test]
fn field_initializer_call_attribution_is_unaffected_by_the_enclosing_method_fix() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Outer.java", FIELD_INITIALIZER_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let compute_symbol = declaration_symbol_owned_by(&index, "compute", "Outer");
    let value_field_symbol = index
        .declarations
        .iter()
        .find(|d| d.name == "VALUE")
        .expect("fixture bug: VALUE field declaration must exist")
        .symbol;

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let compute_dense = graph
        .dense_id_for(compute_symbol)
        .expect("Outer.compute must be interned");
    let value_field_dense = graph
        .dense_id_for(value_field_symbol)
        .expect("Outer.VALUE must be interned");

    let compute_callers = graph.callers_index(compute_dense);
    assert_eq!(
        compute_callers,
        &[value_field_dense],
        "a field initializer call has no enclosing_method -- it must keep resolving via the \
         line heuristic (the field's own declaration), unaffected by this fix, got caller \
         dense ids {compute_callers:?}"
    );
}

// =====================================================================
// A static initializer block's call attribution is UNCHANGED by this
// fix: the extractor DOES assign a synthetic `enclosing_method` symbol
// to a static-initializer-block body (mirroring an ordinary method, for
// LOCAL-BINDING resolution purposes only), but that synthetic symbol
// never gets a `Declaration` pushed for it. The fix must recognize this
// and keep falling through to the line heuristic, never attribute the
// call to a symbol with no declaration anywhere in the graph.
// =====================================================================

const STATIC_INITIALIZER_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void before() {}
    static {
        compute();
    }
    static int compute() {
        return 1;
    }
}
"#;

#[test]
fn static_initializer_block_call_is_attributed_to_the_enclosing_type() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Outer.java", STATIC_INITIALIZER_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let compute_symbol = declaration_symbol_owned_by(&index, "compute", "Outer");
    let outer_symbol = type_declaration_symbol(&index, "Outer");

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let compute_dense = graph
        .dense_id_for(compute_symbol)
        .expect("Outer.compute must be interned");
    let outer_dense = graph.dense_id_for(outer_symbol).expect("Outer must be interned");

    let compute_callers = graph.callers_index(compute_dense);
    assert_eq!(
        compute_callers,
        &[outer_dense],
        "a static initializer block's synthetic enclosing_method symbol has no Declaration -- \
         Issue #1930 attributes the call DIRECTLY to Outer, its lexically enclosing type (see \
         bug_1930_synthetic_scope_start_line_heuristic.rs for the full rationale), got caller \
         dense ids {compute_callers:?}"
    );
}

// =====================================================================
// Same guard, for an instance initializer block.
// =====================================================================

const INSTANCE_INITIALIZER_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void before() {}
    {
        compute();
    }
    int compute() {
        return 1;
    }
}
"#;

#[test]
fn instance_initializer_block_call_is_attributed_to_the_enclosing_type() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Outer.java", INSTANCE_INITIALIZER_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let compute_symbol = declaration_symbol_owned_by(&index, "compute", "Outer");
    let outer_symbol = type_declaration_symbol(&index, "Outer");

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let compute_dense = graph
        .dense_id_for(compute_symbol)
        .expect("Outer.compute must be interned");
    let outer_dense = graph.dense_id_for(outer_symbol).expect("Outer must be interned");

    let compute_callers = graph.callers_index(compute_dense);
    assert_eq!(
        compute_callers,
        &[outer_dense],
        "an instance initializer block's synthetic enclosing_method symbol has no Declaration \
         -- Issue #1930 attributes the call DIRECTLY to Outer, its lexically enclosing type \
         (see bug_1930_synthetic_scope_start_line_heuristic.rs for the full rationale), got \
         caller dense ids {compute_callers:?}"
    );
}
