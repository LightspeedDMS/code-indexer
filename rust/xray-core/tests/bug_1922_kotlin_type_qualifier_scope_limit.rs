//! Issue #1922's Java-only scope, proven end-to-end for Kotlin.
//!
//! #1922's `narrowing::apply_type_qualifier_narrowing` hard-narrows a call
//! DEFINITELY qualified by a type reference -- `receiver::is_definite_
//! type_qualifier` concludes "definitely a type" from an ABSENCE of
//! local/parameter/field evidence in `LocalIndex::typed_names`. That
//! absence is trustworthy for Java (the extractor populates `typed_names`
//! for every local/parameter/field it sees). It is NOT trustworthy for
//! Kotlin: `kotlin.rs`'s own module doc states plainly that "receiver-type
//! substrate (level 6, `LocalIndex::typed_names`) ... this extractor never
//! populates `typed_names`" -- so EVERY Kotlin identifier looks exactly
//! like "no local evidence anywhere", uppercase local/property names
//! included. `mod.rs` therefore gates `receiver_is_type_qualifier` to
//! `file.language == "java"` -- this file proves that gate is load-bearing,
//! not decoration: without it, an uppercase Kotlin local coincidentally
//! sharing a name with an unrelated in-repo type would be wrongly treated
//! as a type qualifier and hard-narrowed onto that unrelated type, dropping
//! the real edge and risking exactly the false `is_definitely_dead_code()
//! == Some(true)` verdict `bug_1910_narrowing_liveness_guards.rs`'s A3/A4/
//! A5 fixtures guard for Java's analogous captured-local shape.
//!
//! Neutral `com.example.*` naming throughout (Disclosure Discipline).

mod common;

use common::{build_graph_over, dead_and_caller_count, declaration_symbol_owned_by, extract_index, write_source};

const KOTLIN_HELPER_TYPE: &str = r#"package com.example.app

class Helper {
    fun helper() {}
}
"#;

/// `Caller.run()` calls `Helper.helper()` where `Helper` is an UPPERCASE-
/// named LOCAL VARIABLE (`val Helper = Caller()`) -- an unusual but legal
/// Kotlin style (e.g. a Builder-pattern local) -- NOT the unrelated
/// `class Helper` declared in a sibling file, which shares only the bare
/// name. The real target is `Caller.helper()` (the private method reached
/// through the local). Kotlin's `typed_names` substrate cannot see the
/// local at all (structural, not a gap #1922 can close the way it closed
/// Java's captured-local case), so `is_definite_type_qualifier` would
/// wrongly conclude "definitely a type" without the `file.language ==
/// "java"` gate in `mod.rs` -- proving that gate is necessary.
const KOTLIN_CALLER: &str = r#"package com.example.app

class Caller {
    private fun helper(): String {
        return "real"
    }

    fun run(): String {
        val Helper = Caller()
        return Helper.helper()
    }
}
"#;

#[test]
fn kotlin_uppercase_local_coincidentally_matching_an_unrelated_type_never_loses_its_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Helper.kt", KOTLIN_HELPER_TYPE);
    write_source(dir.path(), "com/example/app/Caller.kt", KOTLIN_CALLER);

    let caller_index = extract_index(dir.path(), "com/example/app/Caller.kt");
    let real_helper = declaration_symbol_owned_by(&caller_index, "helper", "Caller");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Helper.kt", "com/example/app/Caller.kt"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, real_helper);
    assert_eq!(
        dead,
        Some(false),
        "Caller.helper() is genuinely called (via the captured-local-shaped Helper.helper() \
         call) and must never be reported definitely dead -- #1922's type-qualifier \
         hard-narrowing must stay scoped to Java, never Kotlin"
    );
    assert!(
        callers >= 1,
        "Caller.helper() must keep its real caller edge through the uppercase local receiver"
    );
}
