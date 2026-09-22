//! #1931 rework: real-extraction regressions for THREE reviewer-found
//! defects, all javac+javap-verified end to end.
//!
//! **Defect 1 (root cause, both reviewers)**: `resolve_dotted_qualifier_
//! type`'s shadowing guards originally only inspected `segments[0]`, so
//! a LATER segment that is a FIELD (obscuring a same-named TYPE, per JLS
//! 6.5.2's field-over-type precedence) could still resolve via the
//! nested-type or FQN rule and hard-narrow away the real, field-reached
//! target:
//! - Opus F16: `class A { private static class Helper { private void
//!   run(){} } static Helper B = new Helper(); static class B { static
//!   void run(){} } void go(){ A.B.run(); } }` -- javap confirms
//!   `getstatic B:A$Helper` then `invokevirtual A$Helper.run`. Reproduced
//!   same-file, cross-file, and via a method reference (`A.B::run`).
//! - Codex: `a.b.C.m()` where `b` is a field of `a` and `C` a field of
//!   `Holder`, coincidentally ALSO matching an unrelated real FQN
//!   `a.b.C` declared elsewhere in the repo.
//!
//! Fixed by checking EVERY segment (not just the first) against the same
//! three shadowing guards -- see `receiver_type_qualifier::resolve_
//! dotted_qualifier_type`'s own doc comment for the full rationale.
//!
//! **Defect 2 (found while adding a genuinely HEAD-discriminating FQN
//! fixture)**: even with defect 1 fixed, a fully-qualified chain whose
//! bare-name-resolved receiver type collides with a same-bare-name decoy
//! declared in the CALLER's own file could still lose its real edge --
//! `apply_type_qualifier_narrowing` correctly retains BOTH the real and
//! decoy candidates (bare-name tagging), but `apply_import_context_
//! narrowing`, which runs immediately after, then gets a SECOND,
//! unintended chance to narrow that already-confirmed pool down to
//! whichever one happens to carry a SAME_FILE/SAME_PACKAGE/import bit --
//! exactly the mechanism #1922's own commit message warned about
//! ("the import-context pass kept the same-file self-candidate and
//! discarded the correctly RECEIVER_TYPE_MATCH-tagged real target").
//! Fixed in `resolve.rs`: `apply_type_qualifier_narrowing` now returns
//! whether it actually fired, and `apply_import_context_narrowing` is
//! skipped entirely when it did.
//!
//! **Defect 3 (Codex P1, second round)**: guards (1)-(3) from defect 1
//! can only see a field this extractor actually INDEXED -- a field
//! inherited from an external/UNINDEXED superclass is invisible to them
//! no matter how many segments are checked. `class a extends ExternalBase
//! {}` (declaring `static Holder b;` on the excluded `ExternalBase`)
//! makes `a.b.C.m()` read, absent a further guard, as a PACKAGE PATH
//! ("a.b") coinciding with an unrelated real `a.b.C` elsewhere. Fixed by
//! two additional guards in `resolve_dotted_qualifier_type`: (a) the FQN
//! rule bails if ANY prefix segment ALSO names a repo type (a
//! package/type ambiguity is never proof -- Java never reads an
//! accessible type name as a package fragment); (b) any segment that
//! resolves to a repo type must have FULLY RESOLVED supertypes (`TypeIndex
//! ::has_unresolved_external_supertype`, the same substrate #1924's
//! `RECEIVER_TYPE_MISMATCH` tagging already reuses) -- an inherited field
//! can otherwise shadow it invisibly.
//!
//! Neutral naming (`A`/`B`/`Helper`/`com.example` style; Codex's own
//! `a`/`b`/`C` single-letter identifiers kept verbatim -- already
//! neutral) per this repository's Disclosure Discipline.

mod common;

use common::{build_graph_over, dead_and_caller_count, declaration_symbol_owned_by, extract_index, write_source};

// =====================================================================
// Opus F16, same-file variant: `class A` declares BOTH a field `B`
// (type `Helper`) and an unrelated nested class also named `B`. Real
// javac semantics (JLS 6.5.2): `A.B.run()` resolves via the FIELD `B`,
// never the nested TYPE `B` -- so the real target is `Helper.run()`.
// =====================================================================

const F16_SAME_FILE_SOURCE: &str = r#"package com.example.app;

class A {
    private static class Helper {
        private void run() {
        }
    }

    static Helper B = new Helper();

    static class B {
        static void run() {
        }
    }

    void go() {
        A.B.run();
    }
}
"#;

#[test]
fn f16_same_file_field_shadows_a_same_named_nested_type() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.java", F16_SAME_FILE_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Sample.java");
    let helper_run = declaration_symbol_owned_by(&index, "run", "Helper");

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, helper_run);

    assert_ne!(
        dead,
        Some(true),
        "Helper.run() is the REAL target of A.B.run() (A.B resolves via the FIELD B, per JLS \
         6.5.2's field-over-type precedence) and must never be reported definitely dead just \
         because A ALSO happens to declare an unrelated nested type also named B"
    );
    assert!(
        callers >= 1,
        "Helper.run() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Opus F16, CROSS-FILE variant: the real `Helper.run()` is declared in a
// SEPARATE file (package-private, same package) from `A`/its field `B`/
// its nested-type decoy `B` -- proving the guard works against the
// repo-wide `TypeIndex`/`FileTypedNames` substrate, not merely within
// one file.
// =====================================================================

const F16_CROSS_FILE_A_SOURCE: &str = r#"package com.example.app;

class A {
    static Helper B = new Helper();

    static class B {
        static void run() {
        }
    }

    void go() {
        A.B.run();
    }
}
"#;

const F16_CROSS_FILE_HELPER_SOURCE: &str = r#"package com.example.app;

class Helper {
    void run() {
    }
}
"#;

#[test]
fn f16_cross_file_field_shadows_a_same_named_nested_type() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/A.java", F16_CROSS_FILE_A_SOURCE);
    write_source(dir.path(), "com/example/app/Helper.java", F16_CROSS_FILE_HELPER_SOURCE);

    let helper_index = extract_index(dir.path(), "com/example/app/Helper.java");
    let helper_run = declaration_symbol_owned_by(&helper_index, "run", "Helper");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/A.java", "com/example/app/Helper.java"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, helper_run);

    assert_ne!(
        dead,
        Some(true),
        "Helper.run() (declared in a SEPARATE file from A/its field B/its nested decoy B) is \
         the REAL target of A.B.run() and must never be reported definitely dead"
    );
    assert!(
        callers >= 1,
        "Helper.run() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Opus F16, METHOD-REFERENCE variant: `A.B::run` (a BOUND instance
// method reference) reuses the exact same `receiver_expr_for` extraction
// path a direct invocation's object does, so the guard must apply
// identically here.
// =====================================================================

const F16_METHOD_REFERENCE_SOURCE: &str = r#"package com.example.app;

class A {
    private static class Helper {
        private void run() {
        }
    }

    static Helper B = new Helper();

    static class B {
        static void run() {
        }
    }

    void go() {
        Runnable r = A.B::run;
        r.run();
    }
}
"#;

#[test]
fn f16_method_reference_field_shadows_a_same_named_nested_type() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.java", F16_METHOD_REFERENCE_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Sample.java");
    let helper_run = declaration_symbol_owned_by(&index, "run", "Helper");

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, helper_run);

    assert_ne!(
        dead,
        Some(true),
        "Helper.run() is the REAL target of the method reference A.B::run and must never be \
         reported definitely dead just because A ALSO declares an unrelated nested type B"
    );
    // Opus P3: a weaker `callers >= 1` assertion here passes EVEN WITH
    // the all-segments guard reverted, because the fixture's SECOND call
    // site (`r.run()`, a bare call through a local of JDK interface type
    // `Runnable`) independently supplies its own tag-only caller edge to
    // Helper.run regardless of how the method reference resolves. The
    // EXACT count is what actually discriminates: with the guard
    // correctly protecting `A.B::run`, Helper.run gets edges from BOTH
    // call sites (2); with the guard broken (reverted to a first-
    // segment-only check), the method reference's own edge is wrongly
    // hard-narrowed away to the decoy nested type `B`, leaving ONLY the
    // `r.run()` edge (1).
    assert_eq!(
        callers, 2,
        "Helper.run() must have EXACTLY 2 callers: one from the method reference A.B::run \
         (guarded by the all-segments fix) and one from the separate r.run() call site -- got \
         {callers}"
    );
}

// =====================================================================
// Codex's counterexample: `a.b.C.m()` where `b` is a FIELD of `a` (type
// `Holder`) and `C` is a FIELD of `Holder` (type `Target`) -- the real
// target is `Target.m()`. The chain's own TEXT `a.b.C` coincidentally
// ALSO matches a real, unrelated FQN type `a.b.C` declared elsewhere in
// the repo, which the FQN rule alone (without the all-segments guard)
// would wrongly resolve to.
// =====================================================================

const CODEX_FIELD_CHAIN_SOURCE: &str = r#"package com.example.app;

class a {
    static Holder b;
}

class Holder {
    static Target C;
}

class Target {
    static void m() {
    }
}

class Caller {
    void run() {
        a.b.C.m();
    }
}
"#;

const CODEX_UNRELATED_FQN_SOURCE: &str = r#"package a.b;

class C {
    static void m() {
    }
}
"#;

#[test]
fn codex_field_chain_collides_with_an_unrelated_real_fqn() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.java", CODEX_FIELD_CHAIN_SOURCE);
    write_source(dir.path(), "a/b/C.java", CODEX_UNRELATED_FQN_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Sample.java");
    let target_m = declaration_symbol_owned_by(&index, "m", "Target");

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.java", "a/b/C.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, target_m);

    assert_ne!(
        dead,
        Some(true),
        "Target.m() is the REAL target of a.b.C.m() (a.b.C resolves via the FIELDS b and C, \
         never as a fully-qualified type reference) and must never be reported definitely \
         dead just because the chain's own text a.b.C ALSO matches an unrelated real FQN type"
    );
    assert!(
        callers >= 1,
        "Target.m() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// A genuinely RED-on-HEAD FQN fixture (distinct from `bug_1931_dotted_
// type_qualifier_regressions.rs`'s own FQN test, which does NOT
// discriminate HEAD -- 7 of that file's 8 tests pass even without any
// #1931 resolution, since the generic AC4 binder already keeps those
// edges alive by other means). Here the same-bare-name decoy `Target` is
// declared in the CALLER's OWN file: on HEAD (no dotted-qualifier
// resolution at all), `apply_import_context_narrowing`'s SAME_FILE
// preference -- the EXACT mechanism #1922's own commit message
// describes ("the import-context pass kept the same-file self-candidate
// and discarded the correctly RECEIVER_TYPE_MATCH-tagged real target")
// -- hard-narrows to the same-file decoy alone, losing the real,
// cross-package, fully-qualified target entirely.
// =====================================================================

const FQN_SAME_FILE_DECOY_CALLER_SOURCE: &str = r#"package com.example.app;

class Target {
    static void m() {
    }
}

class Caller {
    void run() {
        com.example.lib.Target.m();
    }
}
"#;

const FQN_SAME_FILE_DECOY_REAL_TARGET_SOURCE: &str = r#"package com.example.lib;

public class Target {
    public static void m() {
    }
}
"#;

#[test]
fn fqn_survives_a_same_bare_name_decoy_in_the_callers_own_file() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Caller.java",
        FQN_SAME_FILE_DECOY_CALLER_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/lib/Target.java",
        FQN_SAME_FILE_DECOY_REAL_TARGET_SOURCE,
    );

    let real_index = extract_index(dir.path(), "com/example/lib/Target.java");
    let real_m = declaration_symbol_owned_by(&real_index, "m", "Target");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Caller.java", "com/example/lib/Target.java"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, real_m);

    assert_ne!(
        dead,
        Some(true),
        "com.example.lib.Target.m() is the REAL, fully-qualified target of \
         com.example.lib.Target.m() and must never be reported definitely dead just because \
         Caller's OWN file ALSO declares an unrelated same-bare-name Target"
    );
    assert!(
        callers >= 1,
        "com.example.lib.Target.m() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Codex's second counterexample (P1, javac+javap-verified): the
// all-segments guard alone cannot see a field INHERITED from an
// external/UNINDEXED superclass -- `ExternalBase`/`ExternalHolder` are
// real, compilable classes (declaring the shadowing fields `b`/`C`) but
// deliberately EXCLUDED from the analyzed set, mirroring `bug_1931_
// dotted_type_qualifier_regressions.rs`'s own "unresolved supertype"
// pattern. `a.b.C.m()` reads, absent the two new guards, as a PACKAGE
// PATH ("a.b") coinciding with an unrelated real `a.b.C` type elsewhere
// -- real javac instead resolves it through the two INHERITED fields.
// =====================================================================

// `a`/`Holder`/`Target` (carrying the `extends` clauses) live in a
// SEPARATE file from `Caller`. This matters: if the `extends` clauses
// were in Caller's OWN file, the PRE-EXISTING #1922 whole-file guard
// (`file_is_safe_for_type_qualifier_narrowing`, unchanged by this fix)
// would already disable hard-narrowing for that file regardless of the
// two NEW guards below -- an earlier revision of this fixture made
// exactly that mistake and passed even with both new guards reverted,
// proving nothing about them. Keeping Caller's own file free of ANY
// `extends`/`implements` clause is what makes this test genuinely
// isolate guards (a)/(b), not the pre-existing file-wide one.
const CODEX_EXTERNAL_HOP_DECLARATIONS_SOURCE: &str = r#"package com.example.app;

class a extends ExternalBase {
}

class Holder extends ExternalHolder {
}

class Target {
    static void m() {
    }
}
"#;

const CODEX_EXTERNAL_HOP_CALLER_SOURCE: &str = r#"package com.example.app;

class Caller {
    void run() {
        a.b.C.m();
    }
}
"#;

/// Written to disk (so the fixture is genuinely javac-valid) but
/// deliberately NOT passed to `build_graph_over` below -- from this
/// binder's own perspective, exactly as unresolved as a real
/// external/JDK supertype.
const CODEX_EXTERNAL_BASE_SOURCE: &str = r#"package com.example.app;

class ExternalBase {
    static Holder b;
}
"#;

/// Also written to disk but excluded from analysis, same rationale.
const CODEX_EXTERNAL_HOLDER_SOURCE: &str = r#"package com.example.app;

class ExternalHolder {
    static Target C;
}
"#;

const CODEX_EXTERNAL_HOP_UNRELATED_FQN_SOURCE: &str = r#"package a.b;

class C {
    static void m() {
    }
}
"#;

#[test]
fn codex_field_chain_reached_through_an_unresolved_external_supertype() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Declarations.java",
        CODEX_EXTERNAL_HOP_DECLARATIONS_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/app/Caller.java",
        CODEX_EXTERNAL_HOP_CALLER_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/app/ExternalBase.java",
        CODEX_EXTERNAL_BASE_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/app/ExternalHolder.java",
        CODEX_EXTERNAL_HOLDER_SOURCE,
    );
    write_source(dir.path(), "a/b/C.java", CODEX_EXTERNAL_HOP_UNRELATED_FQN_SOURCE);

    let index = extract_index(dir.path(), "com/example/app/Declarations.java");
    let target_m = declaration_symbol_owned_by(&index, "m", "Target");

    // `ExternalBase.java`/`ExternalHolder.java` are deliberately NOT
    // included here -- they exist on disk only so the fixture is
    // genuinely compilable.
    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/app/Declarations.java",
            "com/example/app/Caller.java",
            "a/b/C.java",
        ],
    );
    let (dead, callers) = dead_and_caller_count(&graph, target_m);

    assert_ne!(
        dead,
        Some(true),
        "Target.m() is the REAL target of a.b.C.m() (reached through the fields INHERITED \
         from ExternalBase/ExternalHolder, both unresolved/unindexed) and must never be \
         reported definitely dead just because the chain's own text a.b.C ALSO matches an \
         unrelated real FQN type elsewhere"
    );
    assert!(
        callers >= 1,
        "Target.m() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Codex's THIRD counterexample (P1, javac+javap-verified -- INDEPENDENTLY
// re-confirmed with real javac 17: `javap -c -p Caller.class` on this
// exact fixture shows `getstatic Outer.Inner:Target` then `invokevirtual
// Target.m:()V`, proving the INHERITED field wins over the directly-
// declared nested-type decoy). The DIRECT-only supertype check is
// insufficient -- an INDEXED parent with an EXTERNAL GRANDPARENT slips
// through it entirely. `Outer`'s own direct parent, `IndexedBase`, IS a
// known repo type (passes the direct-only check trivially); only
// `IndexedBase`'s OWN parent, `ExternalBase`, is unindexed. `Caller`
// deliberately has NO `extends`/`implements` clause anywhere in its own
// file, so the pre-existing whole-file #1922 guard is NOT what protects
// this call site either -- only the NEW transitive supertype check can.
// =====================================================================

const TRANSITIVE_EXTERNAL_SUPERTYPE_CALLER_SOURCE: &str = r#"package com.example.app;

class Caller {
    void run() {
        Outer.Inner.m();
    }
}
"#;

const TRANSITIVE_EXTERNAL_SUPERTYPE_DECLARATIONS_SOURCE: &str = r#"package com.example.app;

class Outer extends IndexedBase {
    static class Inner {
        static void m() {
        }
    }
}

class IndexedBase extends ExternalBase {
}

class Target {
    void m() {
    }
}
"#;

/// Written to disk (so the fixture is genuinely javac-valid) but
/// deliberately NOT passed to `build_graph_over` below -- from this
/// binder's own perspective, exactly as unresolved as a real
/// external/JDK supertype. Declares the shadowing field `Inner`.
const TRANSITIVE_EXTERNAL_SUPERTYPE_EXTERNAL_BASE_SOURCE: &str = r#"package com.example.app;

class ExternalBase {
    static Target Inner = new Target();
}
"#;

#[test]
fn transitive_external_supertype_field_beats_nested_type_decoy() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Caller.java",
        TRANSITIVE_EXTERNAL_SUPERTYPE_CALLER_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/app/Declarations.java",
        TRANSITIVE_EXTERNAL_SUPERTYPE_DECLARATIONS_SOURCE,
    );
    write_source(
        dir.path(),
        "com/example/app/ExternalBase.java",
        TRANSITIVE_EXTERNAL_SUPERTYPE_EXTERNAL_BASE_SOURCE,
    );

    let index = extract_index(dir.path(), "com/example/app/Declarations.java");
    let target_m = declaration_symbol_owned_by(&index, "m", "Target");

    // `ExternalBase.java` is deliberately NOT included here -- it exists
    // on disk only so the fixture is genuinely compilable.
    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/app/Caller.java",
            "com/example/app/Declarations.java",
        ],
    );
    let (dead, callers) = dead_and_caller_count(&graph, target_m);

    assert_ne!(
        dead,
        Some(true),
        "Target.m() is the REAL target of Outer.Inner.m() (reached through the field \
         INHERITED from ExternalBase -- an INDEXED parent, IndexedBase, with an EXTERNAL \
         GRANDPARENT) and must never be reported definitely dead just because Outer ALSO \
         declares a decoy nested type Inner"
    );
    assert!(
        callers >= 1,
        "Target.m() must keep its real caller edge -- got {callers} caller(s)"
    );
}
