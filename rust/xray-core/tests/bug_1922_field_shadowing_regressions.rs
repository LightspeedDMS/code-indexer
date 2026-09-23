//! Issue #1922: coverage for two shapes where "the qualifier resolves to
//! a known in-repo Java type with a matching candidate" is not, by
//! itself, sufficient evidence to hard-narrow -- an INHERITED field can
//! shadow that same identifier (JLS 6.4.2: a field always wins over a
//! same-named type at a qualifier position). Two shapes:
//!
//! - An uppercase field inherited from a supertype this binder cannot
//!   see into (declared OUTSIDE the analyzed file set, or declared by a
//!   KOTLIN file, which never populates `typed_names` at all) is
//!   invisible to `TypeIndex::is_known_field_name` even though the field
//!   genuinely shadows a coincidentally same-named in-repo type.
//! - A static WILDCARD import (`import static x.Holder.*;`) can bring
//!   an uppercase field into scope without naming it anywhere this
//!   binder can check.
//!
//! `receiver::file_is_safe_for_type_qualifier_narrowing` (`bind/
//! receiver.rs`) is the guard this file proves: hard-narrowing is
//! disabled for an ENTIRE FILE whenever either risk is present, falling
//! back to a plain name+arity binding.
//!
//! Neutral naming throughout (`Base`/`Top`/`Svc`/`Worker`/`Sub`) --
//! synthetic identifiers, not third-party source, per this repository's
//! Disclosure Discipline.

mod common;

use common::{build_graph_over, dead_and_caller_count, declaration_symbol_owned_by, write_source};

// =====================================================================
// `Sub extends Base`, `Base` declares an uppercase field `Svc` whose
// declared type is `Top.Worker`. `Top` ALSO declares an unrelated nested
// class literally named `Svc`. `Svc.workA()` inside `Sub.run()` must
// resolve to the FIELD's type's method (`Worker.workA()` -- confirmed by
// bytecode inspection: `getstatic Svc:Top$Worker; invokevirtual
// Top$Worker.workA`), never the coincidentally same-named nested class
// `Top.Svc`.
// =====================================================================

const BASE_SOURCE: &str = r#"public class Base {
    protected static final Top.Worker Svc = new Top.Worker();
}
"#;

const TOP_SOURCE: &str = r#"public class Top {
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

/// Analyzing `Top.java` ALONE (`include_patterns`-style scoping --
/// `Base.java` exists on disk but is NOT part of the analyzed set).
/// Without this guard, `Base` was not a
/// known Java type in the analyzed set, so `is_definite_type_qualifier`
/// still cleared every guard for `Svc` and `apply_type_qualifier_
/// narrowing` hard-narrowed onto the coincidental `Top.Svc` nested class,
/// dropping the real edge to `Worker.workA()` and flipping it to a false
/// `is_definitely_dead_code() == Some(true)`.
#[test]
fn invisible_inherited_field_keeps_the_real_edge_when_the_supertype_is_outside_the_analyzed_set() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "Base.java", BASE_SOURCE);
    write_source(dir.path(), "Top.java", TOP_SOURCE);

    let top_index = common::extract_index(dir.path(), "Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    // Analyzing ONLY Top.java: Base.java is on disk but excluded from the
    // build -- this is the exact scoping shape that reproduced the bug.
    let graph = build_graph_over(dir.path(), &["Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() is genuinely reached via the inherited field Svc (Base.Svc, outside \
         the analyzed set) and must never be reported definitely dead just because this \
         binder cannot see Base's own field declaration"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

/// Companion: when `Base.java` IS included in the analyzed set, the call
/// must STILL resolve correctly -- now via the pre-existing repo-wide
/// `TypeIndex::is_known_field_name` mechanism (Base's own field
/// declaration becomes visible), never contradicted by the new
/// supertype-chain guard.
#[test]
fn invisible_inherited_field_still_resolves_correctly_when_the_supertype_is_in_the_analyzed_set() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "Base.java", BASE_SOURCE);
    write_source(dir.path(), "Top.java", TOP_SOURCE);

    let top_index = common::extract_index(dir.path(), "Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    let graph = build_graph_over(dir.path(), &["Base.java", "Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() must still resolve correctly once Base.java (and its field \
         declaration) is part of the analyzed set"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Kotlin variant: the supertype (`KBase`) is declared by a KOTLIN file,
// which never populates `typed_names` at all -- so EVEN WHEN `KBase.kt`
// is part of the analyzed set, its field `Svc` stays invisible to
// `TypeIndex::is_known_field_name`. `KBase` is declared in a DIFFERENT
// file than `Top.java`, so the supertype chain never closes within
// `Top.java` either way, and hard-narrowing stays disabled for the whole
// file.
// =====================================================================

const KBASE_SOURCE: &str = r#"open class KBase {
    @JvmField
    protected val Svc: Top.Worker = Top.Worker()
}
"#;

const TOP_KOTLIN_SUPERTYPE_SOURCE: &str = r#"public class Top {
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
fn invisible_inherited_field_keeps_the_real_edge_when_the_supertype_is_declared_by_kotlin() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "KBase.kt", KBASE_SOURCE);
    write_source(dir.path(), "Top.java", TOP_KOTLIN_SUPERTYPE_SOURCE);

    let top_index = common::extract_index(dir.path(), "Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    let graph = build_graph_over(dir.path(), &["KBase.kt", "Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() is genuinely reached via the Kotlin-declared inherited field \
         KBase.Svc and must never be reported definitely dead -- the Kotlin extractor never \
         populates typed_names, so this field is invisible to is_known_field_name even \
         though KBase itself is a known type"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// A static WILDCARD import can bring an uppercase field into scope
// without naming it anywhere this binder can check. `Target` is a REAL
// in-repo type with its own `m()`; `Other` is an unrelated type also
// declaring `m()`. Without the wildcard guard, the qualifier `Target`
// resolves positively (a known Java type, not a repo field) and
// hard-narrows exclusively onto `Target.m()`, excluding `Other.m()` from
// the caller's candidate set entirely. With the guard, the file's static
// wildcard import disables hard-narrowing altogether, and BOTH stay
// reachable through ordinary name+arity binding, matching what a
// genuinely ambiguous qualifier (it might really be a wildcard-imported
// field, not the in-repo class) must fall back to.
// =====================================================================

const WILDCARD_CALLER_SOURCE: &str = r#"package com.example.app;

import static com.example.ext.Holder.*;

class Caller {
    void run() {
        Target.m();
    }
}
"#;

const WILDCARD_TARGET_SOURCE: &str = r#"package com.example.app;

class Target {
    void m() {
    }
}
"#;

const WILDCARD_OTHER_SOURCE: &str = r#"package com.example.app;

class Other {
    void m() {
    }
}
"#;

#[test]
fn static_wildcard_import_disables_hard_narrowing_for_the_whole_file() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Caller.java", WILDCARD_CALLER_SOURCE);
    write_source(dir.path(), "com/example/app/Target.java", WILDCARD_TARGET_SOURCE);
    write_source(dir.path(), "com/example/app/Other.java", WILDCARD_OTHER_SOURCE);

    let caller_index = common::extract_index(dir.path(), "com/example/app/Caller.java");
    let target_index = common::extract_index(dir.path(), "com/example/app/Target.java");
    let other_index = common::extract_index(dir.path(), "com/example/app/Other.java");
    let run_symbol = declaration_symbol_owned_by(&caller_index, "run", "Caller");
    let target_m = declaration_symbol_owned_by(&target_index, "m", "Target");
    let other_m = declaration_symbol_owned_by(&other_index, "m", "Other");

    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/app/Caller.java",
            "com/example/app/Target.java",
            "com/example/app/Other.java",
        ],
    );
    let run_dense = graph.dense_id_for(run_symbol).expect("Caller.run must be interned");
    let callees = graph.callees_index(run_dense);
    let target_dense = graph.dense_id_for(target_m).expect("Target.m must be interned");
    let other_dense = graph.dense_id_for(other_m).expect("Other.m must be interned");

    assert!(
        callees.contains(&target_dense),
        "Target.m must remain a reachable candidate"
    );
    assert!(
        callees.contains(&other_dense),
        "Other.m must ALSO remain a reachable candidate (accepted noise) -- a static \
         wildcard import in this file means Target could just as easily be a \
         wildcard-imported field, so hard-narrowing to Target.m alone is unsafe"
    );
}

// =====================================================================
// Companion: single-member static import. A coincidental in-repo type
// named EXACTLY like the imported member forces `receiver_type` to
// resolve positively, so this test exercises `is_statically_imported_
// member`'s own guard directly, distinct from the AC4 Level 5
// unique-name shortcut covered in `bug_1922_type_qualifier_regressions.rs`.
// =====================================================================

const SINGLE_MEMBER_CALLER_SOURCE: &str = r#"package com.example.app;

import static com.example.ext.Holder.CONSTANT;

class Caller2 {
    void run() {
        CONSTANT.m();
    }
}
"#;

const SINGLE_MEMBER_DECOY_SOURCE: &str = r#"package com.example.app;

class CONSTANT {
    void m() {
    }
}
"#;

const SINGLE_MEMBER_OTHER_SOURCE: &str = r#"package com.example.app;

class Other2 {
    void m() {
    }
}
"#;

#[test]
fn single_member_static_import_disables_hard_narrowing_when_the_imported_name_coincidentally_matches_a_repo_type(
) {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Caller2.java",
        SINGLE_MEMBER_CALLER_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/app/CONSTANT.java",
        SINGLE_MEMBER_DECOY_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/app/Other2.java",
        SINGLE_MEMBER_OTHER_SOURCE,
    );

    let caller_index = common::extract_index(dir.path(), "com/example/app/Caller2.java");
    let decoy_index = common::extract_index(dir.path(), "com/example/app/CONSTANT.java");
    let other_index = common::extract_index(dir.path(), "com/example/app/Other2.java");
    let run_symbol = declaration_symbol_owned_by(&caller_index, "run", "Caller2");
    let decoy_m = declaration_symbol_owned_by(&decoy_index, "m", "CONSTANT");
    let other_m = declaration_symbol_owned_by(&other_index, "m", "Other2");

    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/app/Caller2.java",
            "com/example/app/CONSTANT.java",
            "com/example/app/Other2.java",
        ],
    );
    let run_dense = graph.dense_id_for(run_symbol).expect("Caller2.run must be interned");
    let callees = graph.callees_index(run_dense);
    let decoy_dense = graph.dense_id_for(decoy_m).expect("CONSTANT.m must be interned");
    let other_dense = graph.dense_id_for(other_m).expect("Other2.m must be interned");

    assert!(
        callees.contains(&decoy_dense),
        "the coincidentally same-named CONSTANT.m must remain a reachable candidate"
    );
    assert!(
        callees.contains(&other_dense),
        "Other2.m must ALSO remain reachable (accepted noise) -- CONSTANT is explicitly \
         static-imported, so it must never be treated as a definite type qualifier even \
         though it coincidentally matches an in-repo class name"
    );
}
