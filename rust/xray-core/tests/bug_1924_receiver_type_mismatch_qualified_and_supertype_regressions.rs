//! #1924 (epic #1906): three further shapes where `RECEIVER_TYPE_MISMATCH`
//! must never land on a REAL edge, all involving an EXTERNAL library type
//! this repo's analyzed set never declares (so the binder has no evidence
//! about its real nature -- final or not, its own true package or not):
//!
//! - p12: a parameter declared with an EXPLICIT QUALIFIER naming a
//!   non-`java.lang` package (`com.lib.String s`) -- this binder's type
//!   model only ever records the BARE simple name (`String`), so a
//!   qualified `com.lib.String` and the real `java.lang.String` are
//!   indistinguishable by name alone. `com.lib.String` is not proven
//!   `final`; a repo type (`Sub`) can and does extend it, and dispatch
//!   through the declared parameter type can genuinely reach `Sub.go`.
//! - p21: an unqualified parameter type reaching an external type ONLY
//!   through a single-member STATIC import (`import static com.lib.Outer.
//!   Long;`) -- the same shadowing risk as an ordinary import, just via a
//!   different import form.
//! - p13: a call whose caller's OWN enclosing type extends an external,
//!   unindexed supertype (`Worker extends com.lib.Base`) -- this binder
//!   cannot see `Base`'s members, so it cannot rule out that the call's
//!   parameter type is itself a NESTED type of that invisible supertype,
//!   privately shadowing the closed-world name from further outside.
//! - p24: the unresolved external supertype sits on an INTERMEDIATE
//!   nesting level -- neither the call's immediate enclosing type nor
//!   the file's top-level type, but a type declared BETWEEN them
//!   (`Top { class Mid extends com.lib.Base { class Inner { ... } } }`,
//!   call inside `Inner`). A guard that only checks the immediate
//!   enclosing type and its top-level ancestor misses `Mid` entirely.
//!
//! Neutral naming throughout (`Worker`/`Sub`/`SubInt`/`SubLong`/`com.lib`/
//! `com.example.app` style) -- no third-party library identifiers, per
//! this repository's Disclosure Discipline. Each probe intentionally
//! analyzes ONLY the one file containing the call, never a declaration for
//! `com.lib.*` -- modeling a real external/JDK dependency this repo does
//! not index.

mod common;

use common::declaration_symbol_owned_by;
use xray_core::graph::reasons::RECEIVER_TYPE_MISMATCH;

/// p12: `void call(com.lib.String s) { s.go(); }` with a repo `class Sub
/// extends com.lib.String { void go() {} }` passed in at the only call
/// site. `Sub.go` is a real virtual-dispatch target reachable through the
/// declared parameter type and must never be tagged mismatched.
#[test]
fn parameter_declared_with_an_explicit_non_java_lang_qualifier_is_never_tagged_mismatched() {
    let dir = tempfile::TempDir::new().unwrap();
    common::write_source(
        dir.path(),
        "Worker.java",
        r#"package com.example.app;
public class Worker {
    void call(com.lib.String s) { s.go(); }
    void use() { call(new Sub()); }
}
class Sub extends com.lib.String { @Override public void go() {} }
"#,
    );
    let graph = common::build_graph_over(dir.path(), &["Worker.java"]);
    let index = common::extract_index(dir.path(), "Worker.java");

    let caller = declaration_symbol_owned_by(&index, "call", "Worker");
    let caller_dense = graph.dense_id_for(caller).expect("call must be interned");
    let go = declaration_symbol_owned_by(&index, "go", "Sub");
    let go_dense = graph.dense_id_for(go).expect("Sub.go must be interned");

    assert!(graph.callees_of(caller_dense).contains(&go_dense), "Sub.go must be a real edge");
    let evidence = graph.edge_evidence(caller_dense, go_dense).expect("edge must exist");
    assert!(
        (evidence & RECEIVER_TYPE_MISMATCH) == 0,
        "a parameter declared with an explicit non-java.lang qualifier (com.lib.String) is NOT \
         provably java.lang.String -- Sub.go must never be tagged RECEIVER_TYPE_MISMATCH, got {evidence:#06x}"
    );
}

/// p21: `import static com.lib.Outer.Long;` then `void m(Long l) { l.fire();
/// }` with a repo `class SubLong extends com.lib.Outer.Long { void fire()
/// {} }` passed in. `SubLong.fire` is a real dispatch target.
#[test]
fn parameter_type_reached_only_via_a_single_member_static_import_is_never_tagged_mismatched() {
    let dir = tempfile::TempDir::new().unwrap();
    common::write_source(
        dir.path(),
        "Worker.java",
        r#"package com.example.app;
import static com.lib.Outer.Long;
public class Worker {
    void m(Long l) { l.fire(); }
    void use() { m(new SubLong()); }
}
class SubLong extends com.lib.Outer.Long { @Override public void fire() {} }
"#,
    );
    let graph = common::build_graph_over(dir.path(), &["Worker.java"]);
    let index = common::extract_index(dir.path(), "Worker.java");

    let caller = declaration_symbol_owned_by(&index, "m", "Worker");
    let caller_dense = graph.dense_id_for(caller).expect("m must be interned");
    let fire = declaration_symbol_owned_by(&index, "fire", "SubLong");
    let fire_dense = graph.dense_id_for(fire).expect("SubLong.fire must be interned");

    assert!(graph.callees_of(caller_dense).contains(&fire_dense), "SubLong.fire must be a real edge");
    let evidence = graph.edge_evidence(caller_dense, fire_dense).expect("edge must exist");
    assert!(
        (evidence & RECEIVER_TYPE_MISMATCH) == 0,
        "a bare name reached only via a single-member STATIC import shadows the closed-world \
         name exactly like an ordinary import -- SubLong.fire must never be tagged \
         RECEIVER_TYPE_MISMATCH, got {evidence:#06x}"
    );
}

/// p13: `class Worker extends com.lib.Base` (an external, unindexed
/// supertype whose real members this binder cannot see) -- `void m(Integer
/// i) { i.fire(); }` with a repo `class SubInt extends com.lib.Base.Integer
/// { void fire() {} }` passed in. `SubInt.fire` is a real dispatch target.
#[test]
fn call_inside_a_type_extending_an_external_unindexed_supertype_is_never_tagged_mismatched() {
    let dir = tempfile::TempDir::new().unwrap();
    common::write_source(
        dir.path(),
        "Worker.java",
        r#"package com.example.app;
public class Worker extends com.lib.Base {
    void m(Integer i) { i.fire(); }
    void use() { m(new SubInt()); }
}
class SubInt extends com.lib.Base.Integer { @Override public void fire() {} }
"#,
    );
    let graph = common::build_graph_over(dir.path(), &["Worker.java"]);
    let index = common::extract_index(dir.path(), "Worker.java");

    let caller = declaration_symbol_owned_by(&index, "m", "Worker");
    let caller_dense = graph.dense_id_for(caller).expect("m must be interned");
    let fire = declaration_symbol_owned_by(&index, "fire", "SubInt");
    let fire_dense = graph.dense_id_for(fire).expect("SubInt.fire must be interned");

    assert!(graph.callees_of(caller_dense).contains(&fire_dense), "SubInt.fire must be a real edge");
    let evidence = graph.edge_evidence(caller_dense, fire_dense).expect("edge must exist");
    assert!(
        (evidence & RECEIVER_TYPE_MISMATCH) == 0,
        "a call site inside a type that extends an external, unindexed supertype must never \
         tag SubInt.fire RECEIVER_TYPE_MISMATCH -- the invisible supertype could privately \
         shadow the closed-world name, got {evidence:#06x}"
    );
}

/// p24: `Top { class Mid extends com.lib.Base { class Inner { void m
/// (Integer i){ i.fire(); } } } }` -- the unresolved external supertype
/// is on `Mid`, an INTERMEDIATE nesting level between the call's
/// immediate enclosing type (`Inner`) and the file's top-level type
/// (`Top`), which itself has no supertype at all. A repo `class SubInt
/// extends com.lib.Base.Integer` is passed in at the only call site.
#[test]
fn call_inside_a_type_whose_intermediate_nesting_level_extends_an_external_unindexed_supertype_is_never_tagged_mismatched(
) {
    let dir = tempfile::TempDir::new().unwrap();
    common::write_source(
        dir.path(),
        "Top.java",
        r#"package com.example.app;
public class Top {
    class Mid extends com.lib.Base {
        class Inner {
            void m(Integer i) { i.fire(); }
        }
    }
    void use() { new Mid().new Inner().m(new SubInt()); }
}
class SubInt extends com.lib.Base.Integer { @Override public void fire() {} }
"#,
    );
    let graph = common::build_graph_over(dir.path(), &["Top.java"]);
    let index = common::extract_index(dir.path(), "Top.java");

    let caller = declaration_symbol_owned_by(&index, "m", "Inner");
    let caller_dense = graph.dense_id_for(caller).expect("m must be interned");
    let fire = declaration_symbol_owned_by(&index, "fire", "SubInt");
    let fire_dense = graph.dense_id_for(fire).expect("SubInt.fire must be interned");

    assert!(graph.callees_of(caller_dense).contains(&fire_dense), "SubInt.fire must be a real edge");
    let evidence = graph.edge_evidence(caller_dense, fire_dense).expect("edge must exist");
    assert!(
        (evidence & RECEIVER_TYPE_MISMATCH) == 0,
        "an unresolved external supertype on an INTERMEDIATE nesting level (Mid) -- neither the \
         call's immediate enclosing type (Inner) nor the file's top-level type (Top) -- must \
         still suppress tagging; SubInt.fire must never be tagged RECEIVER_TYPE_MISMATCH, got {evidence:#06x}"
    );
}
