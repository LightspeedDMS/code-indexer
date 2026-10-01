//! Issue #1931: end-to-end regression coverage for dotted (multi-level
//! nested / fully-qualified) TYPE qualifiers -- `Outer.Inner.m()`,
//! `com.example.Target.m()` -- driving real javac-valid Java source
//! through the real `JavaExtractor` + `build_repo_graph` front door (no
//! hand-built `LocalIndex`, no mocking), mirroring
//! `bug_1922_type_qualifier_regressions.rs`'s own discriminating-fixture
//! discipline exactly (Rule 4: reuse `tests/common`, never a parallel
//! harness).
//!
//! Follow-up split from #1922 (both reviewers flagged it as pre-existing,
//! not a regression): #1922 only recognised a BARE `identifier` receiver
//! as a type qualifier -- a `field_access` chain (`Outer.Inner.m()`,
//! `com.example.Target.m()`) extracted as `ReceiverExpr::Other` and never
//! participated in type-qualifier binding at all, losing the real edge to
//! a same-named candidate elsewhere in the repo.
//!
//! Java private access is scoped to one TOP-LEVEL class's own body (JLS
//! 6.6.1) -- it never crosses top-level classes, even in the same file or
//! package. Every fixture below therefore uses the WEAKEST visibility
//! that is still genuinely javac-valid for its call shape: package-
//! private (no modifier) between separate top-level classes in the same
//! package, `public` across packages.
//!
//! Neutral naming throughout (`Outer`/`Inner`/`Caller`/`Target`/`Worker`/
//! `com.example` style) -- no third-party library identifiers, per this
//! repository's Disclosure Discipline.

mod common;

use common::{
    build_graph_over, dead_and_caller_count, declaration_symbol_owned_by, declaration_symbols_by_line,
    extract_index, write_source,
};

// =====================================================================
// The exact issue #1931 repro: `Outer.Inner.goD()` alongside a
// same-named static method on `Outer` itself AND a same-named method on
// the CALLER's own class `Caller` -- the two decoys that specifically
// exercise the "same-file self-candidate" collapse #1922's own binder
// narrowing fix targeted, now for a NESTED-type qualifier.
// =====================================================================

const NESTED_REPRO_SOURCE: &str = r#"package com.example.app;

class Outer {
    static void goD() {
    }

    static class Inner {
        static void goD() {
        }
    }
}

class Caller {
    static void goD() {
    }

    void run() {
        Outer.Inner.goD();
    }
}
"#;

#[test]
fn nested_qualifier_binds_to_the_real_nested_target_not_a_same_named_decoy() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.java", NESTED_REPRO_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Sample.java");
    let run = declaration_symbol_owned_by(&index, "run", "Caller");
    let outer_god = declaration_symbol_owned_by(&index, "goD", "Outer");
    let inner_god = declaration_symbol_owned_by(&index, "goD", "Inner");
    let caller_god = declaration_symbol_owned_by(&index, "goD", "Caller");

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.java"]);
    let run_dense = graph.dense_id_for(run).expect("run must be interned");
    let callees = graph.callees_index(run_dense);
    let inner_dense = graph.dense_id_for(inner_god).expect("Inner.goD must be interned");
    let outer_dense = graph.dense_id_for(outer_god).expect("Outer.goD must be interned");
    let caller_dense = graph.dense_id_for(caller_god).expect("Caller.goD must be interned");

    assert!(
        callees.contains(&inner_dense),
        "Caller.run must have a real edge to Outer.Inner.goD, the call's actual nested-type \
         qualified target"
    );
    assert!(
        !callees.contains(&outer_dense),
        "Caller.run must never bind to Outer.goD -- Outer.Inner.goD is the correctly \
         type-qualified target, not the outer class's own same-named method"
    );
    assert!(
        !callees.contains(&caller_dense),
        "Caller.run must never carry a self-loop to its OWN same-named goD -- this is exactly \
         the same-file self-candidate collapse #1922 fixed for a bare qualifier, now proven for \
         a nested one"
    );
    assert_eq!(
        callees.len(),
        1,
        "Caller.run's only callee must be Outer.Inner.goD -- got {} callee(s)",
        callees.len()
    );

    let (dead, callers) = dead_and_caller_count(&graph, inner_god);
    assert_ne!(dead, Some(true), "Outer.Inner.goD is genuinely called and must never be dead");
    assert!(callers >= 1, "Outer.Inner.goD must keep its real caller edge");
}

// =====================================================================
// A fully-qualified chain (`com.example.lib.Target.m()`) must resolve
// and keep its real edge to the type ACTUALLY declared in that package
// -- `resolve_dotted_qualifier_type`'s FQN rule proves the match by real
// package equality (`decl.package == Some("com.example.lib")`), never by
// bare name alone. It does NOT, however, make the DOWNSTREAM
// `RECEIVER_TYPE_MATCH` tagging (`apply_receiver_type_narrowing`)
// package-aware -- that pass, like every OTHER narrowing pass in this
// binder, matches a candidate's `enclosing_type` by BARE NAME only
// (extensively documented elsewhere in this crate). A same-bare-name
// decoy `Target` declared in a DIFFERENT package can therefore still be
// tagged ALONGSIDE the real one -- the EXACT SAME systemic tolerance a
// bare `Target.m()` qualifier (#1922) already has for two same-named
// types in different packages. This test proves the honest guarantee:
// the real target is NEVER lost, matching "same treatment as a bare type
// qualifier" -- never a stronger, package-exclusive guarantee #1922
// itself does not provide either. Cross-package calls, so `m()` must be
// `public`.
// =====================================================================

const FQN_CALLER_SOURCE: &str = r#"package com.example.app;

class Caller {
    void run() {
        com.example.lib.Target.m();
    }
}
"#;

const FQN_REAL_TARGET_SOURCE: &str = r#"package com.example.lib;

public class Target {
    public static void m() {
    }
}
"#;

const FQN_DECOY_TARGET_SOURCE: &str = r#"package com.example.other;

public class Target {
    public static void m() {
    }
}
"#;

#[test]
fn fully_qualified_chain_resolves_to_the_matching_package_and_never_loses_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Caller.java", FQN_CALLER_SOURCE);
    write_source(dir.path(), "com/example/lib/Target.java", FQN_REAL_TARGET_SOURCE);
    write_source(dir.path(), "com/example/other/Target.java", FQN_DECOY_TARGET_SOURCE);

    let caller_index = extract_index(dir.path(), "com/example/app/Caller.java");
    let real_index = extract_index(dir.path(), "com/example/lib/Target.java");
    let run = declaration_symbol_owned_by(&caller_index, "run", "Caller");
    let real_m = declaration_symbol_owned_by(&real_index, "m", "Target");

    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/app/Caller.java",
            "com/example/lib/Target.java",
            "com/example/other/Target.java",
        ],
    );
    let run_dense = graph.dense_id_for(run).expect("run must be interned");
    let callees = graph.callees_index(run_dense);
    let real_dense = graph.dense_id_for(real_m).expect("real Target.m must be interned");

    assert!(
        callees.contains(&real_dense),
        "Caller.run must bind to com.example.lib.Target.m, the type actually declared in the \
         qualified package -- the FQN rule must never lose this real edge, regardless of the \
         same-bare-name decoy declared in a different package"
    );

    let (dead, callers) = dead_and_caller_count(&graph, real_m);
    assert_ne!(dead, Some(true));
    assert!(callers >= 1);
}

// =====================================================================
// An IMPORTED nested type (`import com.example.lib.Outer; ...
// Outer.Inner.m()`) must keep its real edge across files -- the import
// does not change how the dotted qualifier itself resolves (this
// extractor never consults imports for the nested-type rule), but this
// proves the cross-file shape end to end. Cross-package call, so `m()`
// must be `public`.
// =====================================================================

const IMPORTED_NESTED_OUTER_SOURCE: &str = r#"package com.example.lib;

public class Outer {
    public static class Inner {
        public static void m() {
        }
    }
}
"#;

const IMPORTED_NESTED_CALLER_SOURCE: &str = r#"package com.example.app;

import com.example.lib.Outer;

class Caller {
    void run() {
        Outer.Inner.m();
    }
}
"#;

#[test]
fn imported_nested_qualifier_keeps_its_real_edge_across_files() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/lib/Outer.java", IMPORTED_NESTED_OUTER_SOURCE);
    write_source(dir.path(), "com/example/app/Caller.java", IMPORTED_NESTED_CALLER_SOURCE);

    let outer_index = extract_index(dir.path(), "com/example/lib/Outer.java");
    let inner_m = declaration_symbol_owned_by(&outer_index, "m", "Inner");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/lib/Outer.java", "com/example/app/Caller.java"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, inner_m);
    assert_ne!(
        dead,
        Some(true),
        "Outer.Inner.m() is genuinely called from Caller.run() via the imported-nested \
         qualifier and must never be reported definitely dead"
    );
    assert!(callers >= 1, "Outer.Inner.m() must keep its real caller edge");
}

// =====================================================================
// An ordinary FIELD-ACCESS chain (`obj.field.secretWork()`, `obj` a
// method PARAMETER) must stay completely untouched by the new
// dotted-qualifier rule -- the first-segment shadowing guard
// (`has_any_local_binding`) must fire, leaving this call exactly as
// tag-only as it always was. A decoy second `secretWork()` declaration
// forces `pool.len() > 1`, bypassing the AC4 unique-name shortcut, so a
// wrongly-firing hard-narrow here would be discriminating. `Worker` and
// `Caller` are separate top-level classes in the same package, so
// `secretWork()` is package-private (no modifier) -- `private` would not
// even be javac-valid across top-level classes (JLS 6.6.1).
// =====================================================================

const FIELD_ACCESS_CHAIN_SOURCE: &str = r#"package com.example.app;

class Container {
    Worker field;
}

class Worker {
    void secretWork() {
    }
}

class Decoy {
    void secretWork() {
    }
}

class Caller {
    void run(Container obj) {
        obj.field.secretWork();
    }
}
"#;

#[test]
fn ordinary_field_access_chain_stays_untouched_by_dotted_qualifier_narrowing() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.java", FIELD_ACCESS_CHAIN_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Sample.java");
    let secret_work = declaration_symbol_owned_by(&index, "secretWork", "Worker");

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, secret_work);

    assert_ne!(
        dead,
        Some(true),
        "Worker.secretWork() is genuinely called via obj.field.secretWork() and must never be \
         reported definitely dead just because the receiver is now a DottedQualifier"
    );
    assert!(
        callers >= 1,
        "Worker.secretWork() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// `Outer.FIELD.secretWork()` where `FIELD` is a STATIC FIELD (never a
// nested type) must ALSO stay untouched -- neither the nested-type rule
// nor the FQN rule can structurally resolve `FIELD` as a type, so this
// must resolve to no evidence at all, exactly as it did before this fix
// (when the receiver collapsed to `Other`). A decoy second
// `secretWork()` again forces `pool.len() > 1`. Package-private, same
// rationale as the field-access test above.
// =====================================================================

const STATIC_FIELD_QUALIFIER_SOURCE: &str = r#"package com.example.app;

class Outer {
    static Worker FIELD = new Worker();
}

class Worker {
    void secretWork() {
    }
}

class Decoy {
    void secretWork() {
    }
}

class Caller {
    void run() {
        Outer.FIELD.secretWork();
    }
}
"#;

#[test]
fn static_field_qualifier_stays_untouched_by_dotted_qualifier_narrowing() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.java", STATIC_FIELD_QUALIFIER_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Sample.java");
    let secret_work = declaration_symbol_owned_by(&index, "secretWork", "Worker");

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, secret_work);

    assert_ne!(
        dead,
        Some(true),
        "Worker.secretWork() is genuinely called via Outer.FIELD.secretWork() (FIELD a static \
         field, never a nested type) and must never be reported definitely dead"
    );
    assert!(
        callers >= 1,
        "Worker.secretWork() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Two DIFFERENT outers each nesting a SAME-NAMED `Inner` type
// (`OuterA.Inner`/`OuterB.Inner`) -- the real, correctly-qualified edge
// (`OuterA.Inner.goD` from an `OuterA.Inner.goD()` call) must NEVER be
// lost. This binder is bare-name-keyed throughout (documented
// extensively elsewhere in this crate), so `RECEIVER_TYPE_MATCH` tagging
// cannot itself disambiguate the two same-named `Inner` types -- the
// acceptance bar this test enforces is "never the WRONG one", which is
// satisfied as long as the real target is present, even if an unrelated
// same-named sibling is imprecisely tagged alongside it (the SAME
// systemic bare-name limitation #1922's own hard-narrow already
// tolerates for a bare qualifier).
// =====================================================================

const AMBIGUOUS_NESTED_NAME_SOURCE: &str = r#"package com.example.app;

class OuterA {
    static class Inner {
        static void goD() {
        }
    }
}

class OuterB {
    static class Inner {
        static void goD() {
        }
    }
}

class Caller {
    void run() {
        OuterA.Inner.goD();
    }
}
"#;

#[test]
fn ambiguous_same_named_nested_type_under_two_outers_never_loses_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.java", AMBIGUOUS_NESTED_NAME_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Sample.java");
    let run = declaration_symbol_owned_by(&index, "run", "Caller");

    // Two DIFFERENT `Inner` declarations share the bare name -- neither
    // `declaration_symbol_owned_by` (its owner-name filter also matches
    // BOTH, since `MethodOwnerRecord::enclosing_type` is bare-name only)
    // nor raw declaration order can safely disambiguate them. LINE order
    // is unambiguous real evidence: `OuterA.Inner.goD` is declared
    // earlier in the fixture's own source text than `OuterB.Inner.goD`.
    let god_by_line = declaration_symbols_by_line(&index, "goD");
    assert_eq!(god_by_line.len(), 2, "fixture bug: expected exactly two goD declarations");
    let outer_a_god = god_by_line[0];

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.java"]);
    let run_dense = graph.dense_id_for(run).expect("run must be interned");
    let callees = graph.callees_index(run_dense);
    let outer_a_dense = graph
        .dense_id_for(outer_a_god)
        .expect("OuterA.Inner.goD must be interned");

    assert!(
        callees.contains(&outer_a_dense),
        "Caller.run's call to OuterA.Inner.goD() must keep a real edge to the correct \
         OuterA.Inner.goD -- the real target must never be lost just because an unrelated \
         OuterB.Inner also declares a same-named goD"
    );

    let (dead, callers) = dead_and_caller_count(&graph, outer_a_god);
    assert_ne!(
        dead,
        Some(true),
        "OuterA.Inner.goD is genuinely called and must never be reported definitely dead"
    );
    assert!(callers >= 1, "OuterA.Inner.goD must keep its real caller edge");
}

// =====================================================================
// Adversarial probe: a CAPTURED LOCAL literally named `Outer` (declared
// type `Helper`, itself ALSO declaring a nested `Inner.goD`) is read
// inside an anonymous class body as `Outer.Inner.goD()`. Real javac
// semantics: this resolves to `Helper.Inner.goD` (the captured local's
// OWN declared type), never the unrelated TOP-LEVEL class also literally
// named `Outer` (which coincidentally ALSO declares a nested
// `Inner.goD`). Without the first-segment shadowing guard
// (`has_any_local_binding`), `resolve_dotted_qualifier_type` would
// wrongly resolve `Outer.Inner` to the TOP-LEVEL `Outer.Inner`, hard-
// narrowing the call's candidate pool away from the real
// `Helper.Inner.goD` -- exactly the false-dead-code failure mode #1922's
// own seven review rounds hardened against.
// =====================================================================

const CAPTURED_LOCAL_SHADOW_SOURCE: &str = r#"package com.example.app;

class Outer {
    static class Inner {
        static void goD() {
        }
    }
}

class Helper {
    static class Inner {
        static void goD() {
        }
    }
}

class Caller {
    void run() {
        final Helper Outer = new Helper();
        Runnable r = new Runnable() {
            public void run() {
                Outer.Inner.goD();
            }
        };
        r.run();
    }
}
"#;

#[test]
fn captured_local_shadowing_a_real_top_level_type_name_stays_tag_only() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.java", CAPTURED_LOCAL_SHADOW_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Sample.java");
    // Both nested classes share the bare name `Inner` (owned by two
    // different top-level types), so `declaration_symbol_owned_by`
    // cannot disambiguate their `goD` methods by owner name alone --
    // LINE order is unambiguous real evidence: `Outer.Inner.goD` is
    // declared first in the fixture's own source text, `Helper.Inner.goD`
    // second.
    let god_by_line = declaration_symbols_by_line(&index, "goD");
    assert_eq!(god_by_line.len(), 2, "fixture bug: expected exactly two goD declarations");
    let helper_inner_god = god_by_line[1];

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, helper_inner_god);

    assert_ne!(
        dead,
        Some(true),
        "Helper.Inner.goD() is genuinely called via the captured local `Outer` (declared type \
         Helper) and must never be reported definitely dead just because an UNRELATED \
         top-level class is also literally named Outer and also nests an Inner.goD"
    );
    assert!(
        callers >= 1,
        "Helper.Inner.goD() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Adversarial probe: the caller's file declares `Caller extends
// SomeBase`, where `SomeBase` is a REAL, compilable class deliberately
// EXCLUDED from the analyzed set (mirroring `bug_1922_bare_name_
// supertype_regressions.rs`'s own "real supertype excluded from the
// analyzed set" pattern) -- structurally identical to an external/JDK
// supertype this binder never sees. `file_has_no_supertype_evidence`
// (reused VERBATIM, unmodified, for the new `DottedQualifier` match arm)
// must disable hard-narrowing for the WHOLE file, exactly as it already
// does for a bare qualifier. The real `Outer.Inner.goD` target must
// never become falsely dead.
// =====================================================================

const NESTED_QUALIFIER_UNRESOLVED_SUPERTYPE_SOURCE: &str = r#"package com.example.app;

class Outer {
    static void goD() {
    }

    static class Inner {
        static void goD() {
        }
    }
}

class Caller extends SomeBase {
    static void goD() {
    }

    void run() {
        Outer.Inner.goD();
    }
}
"#;

const UNANALYZED_SOME_BASE_SOURCE: &str = r#"package com.example.app;

public class SomeBase {
}
"#;

#[test]
fn nested_qualifier_stays_safe_when_the_callers_file_has_an_unresolved_supertype() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Sample.java",
        NESTED_QUALIFIER_UNRESOLVED_SUPERTYPE_SOURCE,
    );
    // Written to disk (so the fixture is genuinely javac-valid) but
    // deliberately NOT passed to `build_graph_over` below -- from this
    // binder's own perspective `SomeBase` is exactly as unresolved as a
    // real external/JDK supertype.
    write_source(dir.path(), "com/example/app/SomeBase.java", UNANALYZED_SOME_BASE_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Sample.java");
    let inner_god = declaration_symbol_owned_by(&index, "goD", "Inner");

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, inner_god);

    assert_ne!(
        dead,
        Some(true),
        "Outer.Inner.goD() is genuinely called and must never be reported definitely dead just \
         because Caller's file also declares an extends clause on an unresolved supertype"
    );
    assert!(
        callers >= 1,
        "Outer.Inner.goD() must keep its real caller edge -- got {callers} caller(s)"
    );
}
