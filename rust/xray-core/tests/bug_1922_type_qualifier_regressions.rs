//! Issue #1922: end-to-end regression coverage for `narrowing::apply_
//! type_qualifier_narrowing`, driving real javac-valid Java source
//! through the real `JavaExtractor` + `build_repo_graph` front door (no
//! hand-built `LocalIndex`, no mocking) -- the level the unit tests in
//! `graph::bind::resolve_tests_type_qualifier` cannot see (those
//! hand-feed `resolve_reference` directly and never exercise real
//! extraction, e.g. `constant_declaration`/static-import extraction, at
//! all).
//!
//! Neutral naming throughout (`A`/`B`/`Caller`/`Target`/`Worker`/
//! `com.example` style) -- no third-party library identifiers, per this
//! repository's Disclosure Discipline.

mod common;

use common::{build_graph_over, declaration_symbol_owned_by, dead_and_caller_count, extract_index, write_source};

// =====================================================================
// An interface constant field (`Worker INSTANCE = new Worker();`)
// receiver is structurally indistinguishable from a genuine type
// qualifier unless the Java extractor records interface/annotation-type
// `constant_declaration` members in `typed_names` too (not just ordinary
// `field_declaration`s and record components) -- without that,
// `INSTANCE.secretWork()`'s candidate set could be hard-narrowed away
// from `Worker.secretWork()` (a genuinely-called PRIVATE method),
// flipping it to `is_definitely_dead_code() == Some(true)`, `callers:
// 0`. Guarded two ways at once: `extract_constant_declaration`
// (java.rs) records `INSTANCE` as a Field-scope `TypedNameRecord`, so
// `is_known_field_name` blocks the type-qualifier promotion outright;
// AND `apply_type_qualifier_narrowing` never hard-empties on an
// unresolved qualifier anyway.
// =====================================================================

const INTERFACE_CONSTANT_SOURCE: &str = r#"package com.example.app;

public class Top {
    interface Consts {
        Worker INSTANCE = new Worker();
    }

    static class Worker {
        private void secretWork() {
        }
    }

    static class Impl implements Consts {
        void run() {
            INSTANCE.secretWork();
        }
    }
}
"#;

#[test]
fn interface_constant_receiver_keeps_the_real_private_method_alive() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Top.java", INTERFACE_CONSTANT_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Top.java");
    let secret_work = declaration_symbol_owned_by(&index, "secretWork", "Worker");

    let graph = build_graph_over(dir.path(), &["com/example/app/Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, secret_work);

    assert_ne!(
        dead,
        Some(true),
        "Worker.secretWork() is genuinely called via the interface-constant receiver \
         (INSTANCE.secretWork()) and must never be reported definitely dead"
    );
    assert!(
        callers >= 1,
        "Worker.secretWork() must keep its real caller edge through the interface-constant \
         receiver -- got {callers} caller(s)"
    );
}

/// Companion to the test above, ISOLATING `extract_constant_declaration`'s
/// own necessity specifically: the fixture above alone is ALSO protected
/// by the AC4 Level 5 unique-name shortcut (`secretWork` has only one
/// declaration, and D2's "same top-level type" exception admits a
/// private cross-nested-class call within one compilation unit
/// regardless of receiver evidence) -- it would still pass even without
/// this extraction fix, since the sound #1922 "never clear on an
/// unresolved qualifier" rule alone already protects it. This variant
/// adds a SECOND, unrelated `secretWork()` declaration (forcing
/// `pool.len() > 1`, bypassing the shortcut) AND a coincidentally
/// same-named in-repo type `INSTANCE` in another package (giving
/// `is_known_type_name("INSTANCE")` a real, WRONG type to resolve to if
/// the interface constant is not recorded as a field) -- discriminating:
/// without `extract_constant_declaration`, `INSTANCE` misresolves to the
/// coincidental type and `apply_type_qualifier_narrowing` hard-narrows
/// to ONLY that decoy's `secretWork()`, dropping `Worker.secretWork()`'s
/// real edge.
const INTERFACE_CONSTANT_COLLISION_SOURCE: &str = r#"package com.example.app;

public class Top {
    interface Consts {
        Worker INSTANCE = new Worker();
    }

    static class Worker {
        private void secretWork() {
        }
    }

    static class Impl implements Consts {
        void run() {
            INSTANCE.secretWork();
        }
    }
}
"#;

const INTERFACE_CONSTANT_COLLISION_DECOY_SOURCE: &str = r#"package com.example.other;

public class INSTANCE {
    void secretWork() {
    }
}
"#;

#[test]
fn interface_constant_receiver_is_not_misresolved_by_a_coincidentally_same_named_type() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Top.java",
        INTERFACE_CONSTANT_COLLISION_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/other/INSTANCE.java",
        INTERFACE_CONSTANT_COLLISION_DECOY_SOURCE,
    );

    let top_index = extract_index(dir.path(), "com/example/app/Top.java");
    let secret_work = declaration_symbol_owned_by(&top_index, "secretWork", "Worker");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Top.java", "com/example/other/INSTANCE.java"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, secret_work);

    assert_ne!(
        dead,
        Some(true),
        "Worker.secretWork() must never be reported dead just because an unrelated type in a \
         different package coincidentally shares the interface constant's bare name"
    );
    assert!(
        callers >= 1,
        "Worker.secretWork() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Output-contract test: an interface constant's `constant_declaration`
// extraction stays graph-visible -- class constants already appear as
// `Constant` declarations, so this closes a gap rather than introducing
// new risk. Pins the exact
// shape `extract_constant_declaration` (java_fields.rs) produces: kind
// `Constant`, visibility `Public` (implicit by Java's own rule for every
// interface/annotation-type body member, regardless of whether the
// source repeats the keywords), a signature in the SAME `"constant
// {name}"` format an ordinary class constant's own `field_declaration`
// path already produces, and `is_definitely_dead_code() == None` at the
// graph level (the dead-code predicate's own documented contract: only
// `Method`/`Type` kind ever return `Some(true)`; `Constant` never does,
// regardless of reference count).
// =====================================================================

const INTERFACE_CONSTANT_CONTRACT_SOURCE: &str = r#"package com.example.app;

interface Consts {
    int LIMIT = 10;
}
"#;

#[test]
fn interface_constant_matches_the_ordinary_class_constant_output_contract() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Consts.java",
        INTERFACE_CONSTANT_CONTRACT_SOURCE,
    );

    let index = extract_index(dir.path(), "com/example/app/Consts.java");
    let limit = index
        .declarations
        .iter()
        .find(|d| d.name == "LIMIT")
        .expect("fixture bug: LIMIT declaration must be extracted");

    assert_eq!(
        limit.kind,
        xray_core::graph::extract::local_index::DeclarationKind::Constant,
        "an interface constant must extract as DeclarationKind::Constant, exactly like an \
         ordinary class constant"
    );
    assert_eq!(
        index.visibilities.get(&limit.symbol),
        Some(&xray_core::graph::extract::local_index::Visibility::Public),
        "an interface constant is implicitly public by Java's own rule, regardless of \
         whether the source repeats the keyword"
    );
    assert_eq!(
        index.signatures.get(&limit.symbol),
        Some(&"constant LIMIT".to_string()),
        "an interface constant's signature must match the same 'constant {{name}}' format an \
         ordinary class constant's field_declaration path already produces"
    );

    let graph = build_graph_over(dir.path(), &["com/example/app/Consts.java"]);
    let (dead, _callers) = dead_and_caller_count(&graph, limit.symbol);
    assert_eq!(
        dead, None,
        "is_definitely_dead_code() must be None for a Constant declaration regardless of \
         reference count -- only Method/Type kind ever return Some(true)"
    );
}

// =====================================================================
// #1922's own core discriminating case, through REAL extraction: a
// static facade `class A { static R m(X x) { return B.m(x); } }`
// alongside `class B { static R m(X x) {...} }`. Pre-#1922, the call
// site (inside A's own file) collapsed to the SELF candidate (A.m,
// SAME_FILE) and dropped the real edge to B.m entirely.
// =====================================================================

const FACADE_CALLER_SOURCE: &str = r#"package com.example.app;

public class A {
    static String m(String x) {
        return B.m(x);
    }
}
"#;

const FACADE_TARGET_SOURCE: &str = r#"package com.example.app;

public class B {
    static String m(String x) {
        return x;
    }
}
"#;

#[test]
fn static_facade_binds_to_the_qualified_type_not_the_callers_own_same_named_method() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/A.java", FACADE_CALLER_SOURCE);
    write_source(dir.path(), "com/example/app/B.java", FACADE_TARGET_SOURCE);

    let a_index = extract_index(dir.path(), "com/example/app/A.java");
    let b_index = extract_index(dir.path(), "com/example/app/B.java");
    let a_m = declaration_symbol_owned_by(&a_index, "m", "A");
    let b_m = declaration_symbol_owned_by(&b_index, "m", "B");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/A.java", "com/example/app/B.java"],
    );

    let a_dense = graph.dense_id_for(a_m).expect("A.m must be interned");
    let callees = graph.callees_index(a_dense);
    let b_dense = graph.dense_id_for(b_m).expect("B.m must be interned");

    assert!(
        callees.contains(&b_dense),
        "A.m must have a real edge to B.m (the qualified type it explicitly delegates to)"
    );
    assert!(
        !callees.contains(&a_dense),
        "A.m must never carry a self-loop edge to itself"
    );
    assert_eq!(
        callees.len(),
        1,
        "A.m's only callee must be B.m -- got {} callee(s)",
        callees.len()
    );
}

// =====================================================================
// `import static ext.Holder.CONSTANT; ... CONSTANT.m();` with `m`
// declared on some OTHER, unrelated INDEXED type -- an externally
// static-imported qualifier that resolves to no known in-repo type must
// never be treated as proof the real target doesn't exist. The "never
// clear on an unresolved qualifier" rule already fixes this exact shape
// (proven below); the static-import guard (`is_statically_imported_
// member`, `receiver.rs`) additionally exists
// for the NARROWER, coincidental-collision case (an externally
// static-imported name that ALSO happens to match a real in-repo TYPE
// name) -- covered precisely, at the unit level, by `receiver_tests.rs`'s
// `is_definite_type_qualifier_is_false_for_a_statically_imported_
// member_name` (a decoy end-to-end fixture for that narrower case is not
// added here: `Target.m()`'s own D2 private-visibility exclusion and
// `apply_import_context_narrowing`'s unrelated same-package/import bits
// would confound which pass produced the outcome, so the unit test is
// the precise instrument for the guard itself).
// =====================================================================

const STATIC_IMPORT_CALLER_SOURCE: &str = r#"package com.example.app;

import static com.example.ext.Holder.CONSTANT;

class Caller {
    void run() {
        CONSTANT.m();
    }
}
"#;

/// The unrelated, in-repo INDEXED type whose edge must never be lost --
/// shares only the bare method name `m` with the externally
/// static-imported qualifier, no name collision with `CONSTANT` itself.
const STATIC_IMPORT_TARGET_SOURCE: &str = r#"package com.example.app;

class Target {
    void m() {
    }
}
"#;

#[test]
fn statically_imported_external_qualifier_never_drops_an_unrelated_indexed_types_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Caller.java",
        STATIC_IMPORT_CALLER_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/app/Target.java",
        STATIC_IMPORT_TARGET_SOURCE,
    );

    let target_index = extract_index(dir.path(), "com/example/app/Target.java");
    let target_m = declaration_symbol_owned_by(&target_index, "m", "Target");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Caller.java", "com/example/app/Target.java"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, target_m);

    assert_ne!(
        dead,
        Some(true),
        "Target.m() is genuinely called from Caller.run() (via CONSTANT.m(), the only 'm' \
         in this repo) and must never be reported definitely dead just because the call's \
         qualifier is externally static-imported"
    );
    assert!(
        callers >= 1,
        "Target.m() must keep its real caller edge -- got {callers} caller(s)"
    );
}
