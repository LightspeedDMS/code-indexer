//! Issue #1922: matching a supertype's name against a set of declared
//! type names is never sound evidence for hard-narrowing a type-
//! qualified call -- a file can declare its own unrelated type sharing
//! the exact bare name of a call's REAL, externally-qualified supertype
//! (a same-named `Base`/`Holder.Base`/`Root` in a different package, or
//! an unrelated Java `KBase` coincidentally named like a Kotlin one),
//! and the REAL supertype -- the one actually declaring a shadowing
//! field -- can stay excluded from the analyzed set (or be Kotlin) and
//! invisible to this binder regardless. Confirmed by bytecode
//! inspection: `getstatic Svc:Top$Worker; invokevirtual
//! Top$Worker.workA` for every shape below.
//!
//! The guard therefore asks a strictly syntactic question instead: does
//! ANY type declared in the caller's file -- including nested, local, or
//! anonymous classes -- carry ANY explicit `extends`/`implements` clause
//! at all, or unresolvable supertype evidence? See `receiver::file_has_
//! no_supertype_evidence`. There is no name lookup left for any of the
//! shapes below to fool: the real or decoy supertype's NAME is
//! irrelevant, only whether the caller's own file records ANY supertype
//! evidence anywhere in it -- so the whole file correctly falls back to
//! tag-only and the real edge survives.
//!
//! Neutral naming throughout (`com.example.lib`/`com.example.other`/
//! `com.example.app` packages, `Base`/`Root`/`Holder`/`Mid`/`Svc`/
//! `Worker`/`Sub`/`Top`/`KBase`) -- synthetic identifiers, per this
//! repository's Disclosure Discipline.

mod common;

use common::{build_graph_over, dead_and_caller_count, declaration_symbol_owned_by, extract_index, write_source};

// =====================================================================
// `Sub extends Base` where the REAL `Base` (declaring the shadowing
// field `Svc`) is EXCLUDED from the analyzed set, and an UNRELATED,
// analyzed, TOP-LEVEL `Base` in a different package coincidentally
// shares the bare name. A guard that matches "Base" only by bare name
// against the decoy would wrongly conclude the chain is fully resolved.
// =====================================================================

const G1_REAL_BASE_SOURCE: &str = r#"package com.example.lib;

import com.example.app.Top;

public class Base {
    protected static final Top.Worker Svc = new Top.Worker();
}
"#;

const G1_DECOY_BASE_SOURCE: &str = r#"package com.example.other;

public class Base {
}
"#;

const G1_TOP_SOURCE: &str = r#"package com.example.app;

import com.example.lib.Base;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    static class Worker {
        private void workA() {
        }
    }

    static class Sub extends Base {
        void run() {
            Svc.workA();
        }
    }
}
"#;

#[test]
fn g1_unrelated_top_level_decoy_never_makes_an_excluded_real_supertype_look_resolved() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/lib/Base.java", G1_REAL_BASE_SOURCE);
    write_source(
        dir.path(),
        "com/example/other/Base.java",
        G1_DECOY_BASE_SOURCE,
    );
    write_source(dir.path(), "com/example/app/Top.java", G1_TOP_SOURCE);

    let top_index = extract_index(dir.path(), "com/example/app/Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    // com/example/lib/Base.java (the REAL supertype) is deliberately
    // EXCLUDED from the analyzed set -- only the decoy and Top.java are.
    let graph = build_graph_over(
        dir.path(),
        &["com/example/other/Base.java", "com/example/app/Top.java"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() is genuinely reached via the inherited field Svc (the REAL Base, \
         excluded from the analyzed set) and must never be reported definitely dead just \
         because an unrelated, analyzed Base in a different package coincidentally shares \
         the bare name"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Identical to the previous shape, except the unrelated decoy is a
// NESTED `Holder.Base` in another analyzed file rather than a top-level
// type -- the bare-name collision this guard must resist is not limited
// to top-level declarations.
// =====================================================================

const G1B_DECOY_HOLDER_SOURCE: &str = r#"package com.example.other;

public class Holder {
    static class Base {
    }
}
"#;

#[test]
fn g1b_unrelated_nested_decoy_never_makes_an_excluded_real_supertype_look_resolved() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/lib/Base.java", G1_REAL_BASE_SOURCE);
    write_source(
        dir.path(),
        "com/example/other/Holder.java",
        G1B_DECOY_HOLDER_SOURCE,
    );
    write_source(dir.path(), "com/example/app/Top.java", G1_TOP_SOURCE);

    let top_index = extract_index(dir.path(), "com/example/app/Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/other/Holder.java", "com/example/app/Top.java"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() must never be reported definitely dead just because an unrelated \
         NESTED Holder.Base in another analyzed file coincidentally shares the bare name \
         'Base' with the real, excluded supertype"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// A TWO-HOP chain -- `Sub extends Holder.Mid`, where `Holder.Mid` IS in
// the analyzed set (a legitimately resolved, correctly-Java, non-Kotlin
// intermediate supertype) but `Mid` itself `extends com.example.lib.
// Root`, and `Root` is EXCLUDED from the analyzed set. An unrelated,
// analyzed, top-level `Root` in a different package coincidentally
// shares the bare name. Walking the full transitive supertype set
// ({"Mid", "Root"}) and matching EACH bare name independently against
// the analyzed set would wrongly resolve both -- "Mid" genuinely (Holder
// .java IS analyzed) and the decoy "Root" coincidentally -- concluding
// the WHOLE chain is fully resolved even though the real Root is
// invisible. Requiring every hop to be declared INSIDE Top.java itself
// avoids that: neither "Mid" nor "Root" is, regardless of how many of
// them individually resolve elsewhere, so the file correctly stays
// tag-only.
// =====================================================================

const G6B_REAL_ROOT_SOURCE: &str = r#"package com.example.lib;

import com.example.app.Top;

public class Root {
    protected static final Top.Worker Svc = new Top.Worker();
}
"#;

const G6B_DECOY_ROOT_SOURCE: &str = r#"package com.example.other;

public class Root {
}
"#;

const G6B_HOLDER_SOURCE: &str = r#"package com.example.other;

import com.example.lib.Root;

public class Holder {
    static class Mid extends Root {
    }
}
"#;

const G6B_TOP_SOURCE: &str = r#"package com.example.app;

import com.example.other.Holder;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    static class Worker {
        private void workA() {
        }
    }

    static class Sub extends Holder.Mid {
        void run() {
            Svc.workA();
        }
    }
}
"#;

#[test]
fn g6b_a_two_hop_chain_with_an_excluded_terminal_supertype_and_an_unrelated_decoy_stays_tag_only()
{
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/lib/Root.java", G6B_REAL_ROOT_SOURCE);
    write_source(
        dir.path(),
        "com/example/other/Root.java",
        G6B_DECOY_ROOT_SOURCE,
    );
    write_source(dir.path(), "com/example/other/Holder.java", G6B_HOLDER_SOURCE);
    write_source(dir.path(), "com/example/app/Top.java", G6B_TOP_SOURCE);

    let top_index = extract_index(dir.path(), "com/example/app/Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    // com/example/lib/Root.java (the REAL terminal supertype) is
    // deliberately EXCLUDED from the analyzed set.
    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/other/Root.java",
            "com/example/other/Holder.java",
            "com/example/app/Top.java",
        ],
    );
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() must never be reported definitely dead just because an intermediate \
         hop (Holder.Mid) resolves correctly while the terminal hop (Root) is excluded and \
         coincidentally collides with an unrelated, analyzed decoy Root"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// The Kotlin-supertype shape, now WITH an unrelated, analyzed JAVA
// `KBase` in a DIFFERENT package coincidentally sharing the bare name of
// the REAL Kotlin `KBase` supertype -- a bare-name check is unsound for
// this shape too, not just the pure-Java shapes above.
// =====================================================================

const G9_KOTLIN_KBASE_SOURCE: &str = r#"package com.example.app

open class KBase {
    @JvmField
    protected val Svc: Top.Worker = Top.Worker()
}
"#;

const G9_DECOY_JAVA_KBASE_SOURCE: &str = r#"package com.example.other;

public class KBase {
}
"#;

const G9_TOP_SOURCE: &str = r#"package com.example.app;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    static class Worker {
        private void workA() {
        }
    }

    static class Sub extends KBase {
        void run() {
            Svc.workA();
        }
    }
}
"#;

#[test]
fn g9_unrelated_java_decoy_never_makes_a_kotlin_supertype_look_java_resolved() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/KBase.kt", G9_KOTLIN_KBASE_SOURCE);
    write_source(
        dir.path(),
        "com/example/other/KBase.java",
        G9_DECOY_JAVA_KBASE_SOURCE,
    );
    write_source(dir.path(), "com/example/app/Top.java", G9_TOP_SOURCE);

    let top_index = extract_index(dir.path(), "com/example/app/Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/app/KBase.kt",
            "com/example/other/KBase.java",
            "com/example/app/Top.java",
        ],
    );
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() is genuinely reached via the Kotlin-declared inherited field \
         KBase.Svc and must never be reported definitely dead just because an unrelated, \
         analyzed JAVA KBase in a different package coincidentally shares the bare name"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// A supertype clause ANYWHERE in a file -- even one that names another
// type declared in that SAME file -- disables hard-narrowing for the
// whole file. `Sub extends Base` (both declared in `Multi.java`) records
// a real `extends` clause, so `Sub.m()`'s call to `Target.m(x)` must
// fall back to ordinary name+arity binding rather than being hard-
// narrowed: the real edge to `Target.m` must never be lost.
// =====================================================================

const SAME_FILE_SUPERTYPE_SOURCE: &str = r#"package com.example.app;

public class Multi {
    static class Base {
    }

    static class Sub extends Base {
        static String m(String x) {
            return Target.m(x);
        }
    }

    static class Target {
        static String m(String x) {
            return x;
        }
    }
}
"#;

#[test]
fn a_supertype_clause_declared_in_the_same_file_still_disables_hard_narrowing() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Multi.java", SAME_FILE_SUPERTYPE_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Multi.java");
    let sub_m = declaration_symbol_owned_by(&index, "m", "Sub");
    let target_m = declaration_symbol_owned_by(&index, "m", "Target");

    let graph = build_graph_over(dir.path(), &["com/example/app/Multi.java"]);
    let sub_dense = graph.dense_id_for(sub_m).expect("Sub.m must be interned");
    let callees = graph.callees_index(sub_dense);
    let target_dense = graph.dense_id_for(target_m).expect("Target.m must be interned");

    assert!(
        callees.contains(&target_dense),
        "Sub.m must keep its real edge to Target.m even though Sub's own file also records \
         an extends clause (Sub extends Base) -- that clause disables hard-narrowing, but \
         must never drop a real edge"
    );
    assert!(
        callees.contains(&sub_dense),
        "Sub.m's own self-referencing candidate must still be present -- proving hard-\
         narrowing is genuinely disabled (ordinary tag-only binding, not a narrowed pool) \
         because Sub's file also records an extends clause"
    );
}

// =====================================================================
// Positive companion: a file with NO supertype clause anywhere in it (an
// ordinary static facade) must still hard-narrow. `A.m()` explicitly
// delegates to `B.m(x)`; neither `A` nor `B` declares any `extends`/
// `implements` clause, so the file has zero supertype evidence and hard-
// narrowing must fire -- `A.m`'s only callee is `B.m`, never a self-loop.
// =====================================================================

const FACADE_NO_SUPERTYPES_SOURCE: &str = r#"package com.example.app;

public class Facade {
    static class A {
        static String m(String x) {
            return B.m(x);
        }
    }

    static class B {
        static String m(String x) {
            return x;
        }
    }
}
"#;

#[test]
fn a_file_with_no_supertype_clause_anywhere_still_hard_narrows() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Facade.java", FACADE_NO_SUPERTYPES_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Facade.java");
    let a_m = declaration_symbol_owned_by(&index, "m", "A");
    let b_m = declaration_symbol_owned_by(&index, "m", "B");

    let graph = build_graph_over(dir.path(), &["com/example/app/Facade.java"]);
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
// A file declares its own unrelated `static class Base {}`, but `Sub`
// extends a FULLY-QUALIFIED `com.example.lib.Base` -- a different type
// entirely, EXCLUDED from the analyzed set, declaring the shadowing
// field `Svc`. The extractor resolves a qualified extends clause to its
// bare LAST identifier ("Base"), coincidentally matching the file's own
// unrelated nested type by name alone -- name matching must never be
// trusted here; only the mere presence of the clause matters.
//
// `Worker` is declared `public` so the excluded `Base` (a different
// package) can legally construct it -- only `workA()` itself is
// `private`, which is what the dead-code check exercises.
// =====================================================================

const H_REAL_BASE_SOURCE: &str = r#"package com.example.lib;

import com.example.app.Top;

public class Base {
    protected static final Top.Worker Svc = new Top.Worker();
}
"#;

const H1_TOP_SOURCE: &str = r#"package com.example.app;

public class Top {
    static class Base {
    }

    static class Svc {
        static void workA() {
        }
    }

    public static class Worker {
        private void workA() {
        }
    }

    static class Sub extends com.example.lib.Base {
        void run() {
            Svc.workA();
        }
    }
}
"#;

#[test]
fn h1_a_qualified_extends_clause_colliding_with_a_same_file_decoy_still_disables_hard_narrowing()
{
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/lib/Base.java", H_REAL_BASE_SOURCE);
    write_source(dir.path(), "com/example/app/Top.java", H1_TOP_SOURCE);

    let top_index = extract_index(dir.path(), "com/example/app/Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    // com/example/lib/Base.java (the REAL supertype) is deliberately
    // EXCLUDED from the analyzed set.
    let graph = build_graph_over(dir.path(), &["com/example/app/Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() must never be reported definitely dead just because Top.java \
         coincidentally declares its own unrelated Base type sharing the bare name of the \
         real, excluded, fully-qualified supertype"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Same shape as above, but the qualified supertype is named through an
// ANONYMOUS class body (`new com.example.lib.Base() { ... }`) rather
// than an explicit `extends` clause on a named type -- the anonymous
// class body's own inheritance evidence must disable hard-narrowing for
// the whole file too, even for an unrelated call site elsewhere in it.
// =====================================================================

const H9_TOP_SOURCE: &str = r#"package com.example.app;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    public static class Worker {
        private void workA() {
        }
    }

    static class Sub {
        Object anon = new com.example.lib.Base() {
            @Override
            public String toString() {
                return "anon";
            }
        };

        void run() {
            Svc.workA();
        }
    }
}
"#;

#[test]
fn h9_an_anonymous_class_extending_a_qualified_excluded_supertype_still_disables_hard_narrowing()
{
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/lib/Base.java", H_REAL_BASE_SOURCE);
    write_source(dir.path(), "com/example/app/Top.java", H9_TOP_SOURCE);

    let top_index = extract_index(dir.path(), "com/example/app/Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    let graph = build_graph_over(dir.path(), &["com/example/app/Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() must never be reported definitely dead just because the only \
         supertype evidence in the file comes from an unrelated anonymous class body"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// The file declares an unrelated `Base` NESTED INSIDE A SIBLING type
// (`A.Base`), out of lexical scope at `Sub` -- `Sub extends Base` (bare)
// resolves, by real Java scoping rules, through the IMPORT to the REAL,
// excluded `com.example.lib.Base`. The extractor's own bare-name
// resolution cannot tell `A.Base` apart from the imported `Base` by name
// alone; only the presence of the clause matters.
// =====================================================================

const H2_TOP_SOURCE: &str = r#"package com.example.app;

import com.example.lib.Base;

public class Top {
    static class A {
        static class Base {
        }
    }

    static class Svc {
        static void workA() {
        }
    }

    public static class Worker {
        private void workA() {
        }
    }

    static class Sub extends Base {
        void run() {
            Svc.workA();
        }
    }
}
"#;

#[test]
fn h2_a_sibling_nested_decoy_with_an_import_resolving_elsewhere_still_disables_hard_narrowing() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/lib/Base.java", H_REAL_BASE_SOURCE);
    write_source(dir.path(), "com/example/app/Top.java", H2_TOP_SOURCE);

    let top_index = extract_index(dir.path(), "com/example/app/Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    let graph = build_graph_over(dir.path(), &["com/example/app/Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() must never be reported definitely dead just because the file also \
         declares an unrelated Base nested inside a sibling type, out of scope at Sub"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Same shape as above, but with NO import at all: `Sub extends Base`
// resolves, by real Java same-package visibility rules, to an EXCLUDED
// `Base` declared in the same package as `Top.java`.
// =====================================================================

const H2P_BASE_SAME_PACKAGE_SOURCE: &str = r#"package com.example.app;

public class Base {
    protected static final Top.Worker Svc = new Top.Worker();
}
"#;

const H2P_TOP_SOURCE: &str = r#"package com.example.app;

public class Top {
    static class A {
        static class Base {
        }
    }

    static class Svc {
        static void workA() {
        }
    }

    public static class Worker {
        private void workA() {
        }
    }

    static class Sub extends Base {
        void run() {
            Svc.workA();
        }
    }
}
"#;

#[test]
fn h2p_a_sibling_nested_decoy_with_no_import_resolving_via_same_package_still_disables_hard_narrowing(
) {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Base.java",
        H2P_BASE_SAME_PACKAGE_SOURCE,
    );
    write_source(dir.path(), "com/example/app/Top.java", H2P_TOP_SOURCE);

    let top_index = extract_index(dir.path(), "com/example/app/Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    // com/example/app/Base.java (the REAL, same-package supertype) is
    // deliberately EXCLUDED from the analyzed set.
    let graph = build_graph_over(dir.path(), &["com/example/app/Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() must never be reported definitely dead just because the file also \
         declares an unrelated Base nested inside a sibling type, out of scope at Sub, with \
         the real supertype resolved only via implicit same-package visibility"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}
