//! #1924 (epic #1906): `RECEIVER_TYPE_MISMATCH` must never land on a REAL
//! edge. A closed-world-looking receiver type is not enough on its own --
//! the bare simple name can be legally shadowed by a repo-declared type,
//! and this binder's typed-name lookup is keyed per METHOD rather than per
//! BLOCK (#1919), so a field can be misresolved to an unrelated, same-
//! method local's declared type. These four javac-verified shapes pin both
//! guards end-to-end (`build_repo_graph`, no hand-built `LocalIndex`):
//!
//! - p1: a repo-declared `class String { void go() {} }` (same package,
//!   legally shadowing `java.lang.String` for code in that package) --
//!   `callGo(String s) { s.go(); }` must NOT tag `String.go`.
//! - p10: a NESTED `static class Integer { void fire() {} }` -- `m(Integer
//!   i) { i.fire(); }` must NOT tag `Worker.Integer.fire`.
//! - p3: a field `Target t` shadowed, within the SAME method but a
//!   DIFFERENT block, by a local `String t` -- `t.fire()` (the FIELD
//!   access, outside the shadowing block) must NOT tag `Target.fire`
//!   (#1919: locals are keyed per METHOD, not per BLOCK, so the two `t`s
//!   share one lookup key).
//! - p11: identical to p3, but the field is INHERITED from a `Base` class
//!   declared in a SEPARATE file.
//!
//! Neutral naming throughout (`Worker`/`Target`/`Decoy`/`Base`/`Sub`/
//! `com.example` style) -- no third-party library identifiers, per this
//! repository's Disclosure Discipline.

mod common;

use common::declaration_symbol_owned_by;
use xray_core::graph::reasons::RECEIVER_TYPE_MISMATCH;

/// p1: `class String { void go() {} }`, same package as the caller.
#[test]
fn repo_declared_class_named_string_is_never_tagged_mismatched() {
    let dir = tempfile::TempDir::new().unwrap();
    common::write_source(
        dir.path(),
        "String.java",
        "package com.example.app;\npublic class String {\n    public void go() {}\n}\n",
    );
    common::write_source(
        dir.path(),
        "User.java",
        r#"package com.example.app;
public class User {
    void callGo(String s) { s.go(); }
}
class Decoy { public void go() {} }
"#,
    );
    let graph = common::build_graph_over(dir.path(), &["String.java", "User.java"]);
    let string_index = common::extract_index(dir.path(), "String.java");
    let user_index = common::extract_index(dir.path(), "User.java");

    let caller = declaration_symbol_owned_by(&user_index, "callGo", "User");
    let caller_dense = graph.dense_id_for(caller).expect("callGo must be interned");
    let go = declaration_symbol_owned_by(&string_index, "go", "String");
    let go_dense = graph.dense_id_for(go).expect("String.go must be interned");

    assert!(graph.callees_of(caller_dense).contains(&go_dense), "String.go must be a real edge");
    let evidence = graph.edge_evidence(caller_dense, go_dense).expect("edge must exist");
    assert!(
        evidence & RECEIVER_TYPE_MISMATCH == 0,
        "a repo-declared class literally named String must never be tagged RECEIVER_TYPE_MISMATCH, got {evidence:#06x}"
    );
}

/// p10: a NESTED `static class Integer { void fire() {} }`.
#[test]
fn nested_class_named_integer_is_never_tagged_mismatched() {
    let dir = tempfile::TempDir::new().unwrap();
    common::write_source(
        dir.path(),
        "Worker.java",
        r#"package com.example.app;
public class Worker {
    static class Integer { void fire() {} }
    void m(Integer i) { i.fire(); }
}
class Decoy { void fire() {} }
"#,
    );
    let graph = common::build_graph_over(dir.path(), &["Worker.java"]);
    let index = common::extract_index(dir.path(), "Worker.java");

    let caller = declaration_symbol_owned_by(&index, "m", "Worker");
    let caller_dense = graph.dense_id_for(caller).expect("m must be interned");
    let fire = declaration_symbol_owned_by(&index, "fire", "Integer");
    let fire_dense = graph.dense_id_for(fire).expect("Worker.Integer.fire must be interned");

    assert!(graph.callees_of(caller_dense).contains(&fire_dense), "Worker.Integer.fire must be a real edge");
    let evidence = graph.edge_evidence(caller_dense, fire_dense).expect("edge must exist");
    assert!(
        evidence & RECEIVER_TYPE_MISMATCH == 0,
        "a nested class named Integer must never be tagged RECEIVER_TYPE_MISMATCH, got {evidence:#06x}"
    );
}

/// p3: field `Target t` shadowed, within the SAME method but a DIFFERENT
/// block, by a local `String t`.
#[test]
fn field_shadowed_by_a_same_method_block_scoped_local_is_never_tagged_mismatched() {
    let dir = tempfile::TempDir::new().unwrap();
    common::write_source(
        dir.path(),
        "Worker.java",
        r#"package com.example.app;
public class Worker {
    Target t = new Target();
    void m(boolean b) {
        if (b) { String t = "x"; t.length(); }
        t.fire();
    }
}
class Target { void fire() {} }
class Decoy { void fire() {} }
"#,
    );
    let graph = common::build_graph_over(dir.path(), &["Worker.java"]);
    let index = common::extract_index(dir.path(), "Worker.java");

    let caller = declaration_symbol_owned_by(&index, "m", "Worker");
    let caller_dense = graph.dense_id_for(caller).expect("m must be interned");
    let fire = declaration_symbol_owned_by(&index, "fire", "Target");
    let fire_dense = graph.dense_id_for(fire).expect("Target.fire must be interned");

    assert!(graph.callees_of(caller_dense).contains(&fire_dense), "Target.fire must be a real edge");
    let evidence = graph.edge_evidence(caller_dense, fire_dense).expect("edge must exist");
    assert!(
        evidence & RECEIVER_TYPE_MISMATCH == 0,
        "the field access t.fire() must never be tagged RECEIVER_TYPE_MISMATCH just because a \
         DIFFERENT, block-scoped local named t shadows it elsewhere in the same method, got {evidence:#06x}"
    );
}

/// p11: identical to p3, but the field is INHERITED from a `Base` class in
/// a separate file.
#[test]
fn inherited_field_shadowed_by_a_same_method_block_scoped_local_is_never_tagged_mismatched() {
    let dir = tempfile::TempDir::new().unwrap();
    common::write_source(
        dir.path(),
        "Base.java",
        "package com.example.app;\npublic class Base {\n    protected Target t = new Target();\n}\n",
    );
    common::write_source(
        dir.path(),
        "Sub.java",
        r#"package com.example.app;
public class Sub extends Base {
    void m(boolean b) {
        if (b) { String t = "x"; t.length(); }
        t.fire();
    }
}
class Target { void fire() {} }
class Decoy { void fire() {} }
"#,
    );
    let graph = common::build_graph_over(dir.path(), &["Base.java", "Sub.java"]);
    let sub_index = common::extract_index(dir.path(), "Sub.java");

    let caller = declaration_symbol_owned_by(&sub_index, "m", "Sub");
    let caller_dense = graph.dense_id_for(caller).expect("m must be interned");
    let fire = declaration_symbol_owned_by(&sub_index, "fire", "Target");
    let fire_dense = graph.dense_id_for(fire).expect("Target.fire must be interned");

    assert!(graph.callees_of(caller_dense).contains(&fire_dense), "Target.fire must be a real edge");
    let evidence = graph.edge_evidence(caller_dense, fire_dense).expect("edge must exist");
    assert!(
        evidence & RECEIVER_TYPE_MISMATCH == 0,
        "the INHERITED field access t.fire() must never be tagged RECEIVER_TYPE_MISMATCH just \
         because a DIFFERENT, block-scoped local named t shadows it elsewhere in the same \
         method, got {evidence:#06x}"
    );
}
