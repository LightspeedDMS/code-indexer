//! Issue #1922: five real Java-caller/Kotlin-target shapes where the
//! JAVA side's qualifier is a SYNTHETIC facade name (`UtilsKt`, a custom
//! `@file:JvmName` facade), a companion object's outer-class alias
//! (`@JvmStatic`), or a statically-imported Kotlin top-level property/
//! enum entry (`@JvmField`, an enum constant) -- none of which the
//! Kotlin extractor (`kotlin.rs`) records as a known in-repo TYPE (it
//! extracts no file-facade/`@JvmName`/`@JvmStatic`/`@JvmField` concept at
//! all), so `receiver::is_definite_type_qualifier`'s companion `resolve_
//! receiver_type` call resolves every one of these qualifiers to `None`
//! -- exactly the case `narrowing::apply_type_qualifier_narrowing` NEVER
//! hard-narrows or clears on. Driven through the real
//! `JavaExtractor`+`KotlinExtractor`+`build_repo_graph` front door (no
//! hand-built `LocalIndex`, no mocking), mirroring `bug_1920_
//! kotlin_instance_qualified_calls.rs`'s own harness.
//!
//! Neutral naming throughout (`com.example.app`-style packages,
//! `Util`/`Widget`/`Holder`-style names) -- no third-party identifiers,
//! per this repository's Disclosure Discipline.

mod common;

use common::{build_graph_over, dead_and_caller_count, declaration_symbol, extract_index, write_source};
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::identity::SymbolId;

fn assert_kept_alive(graph: &CodeGraph, symbol: SymbolId, what: &str) {
    let (dead, callers) = dead_and_caller_count(graph, symbol);
    assert_ne!(
        dead,
        Some(true),
        "{what} is genuinely called from Java and must never be reported definitely dead \
         just because its Java-side qualifier is a synthetic Kotlin facade/alias this \
         extractor does not model as a type"
    );
    assert!(
        callers >= 1,
        "{what} must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// Shape 1: a Kotlin TOP-LEVEL function, called from Java via its
// compiler-synthesized facade class (`UtilsKt`, Kotlin's default
// `<FileName>Kt` naming for a file with no `@file:JvmName`).
// =====================================================================

const TOP_LEVEL_FUN_KOTLIN: &str = r#"package com.example.app

fun topFun(): String {
    return "value"
}
"#;

const TOP_LEVEL_FUN_JAVA_CALLER: &str = r#"package com.example.app;

class Caller {
    String run() {
        return UtilsKt.topFun();
    }
}
"#;

#[test]
fn java_caller_of_a_kotlin_top_level_function_via_the_synthetic_facade_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Utils.kt", TOP_LEVEL_FUN_KOTLIN);
    write_source(
        dir.path(),
        "com/example/app/Caller.java",
        TOP_LEVEL_FUN_JAVA_CALLER,
    );

    let kotlin_index = extract_index(dir.path(), "com/example/app/Utils.kt");
    let top_fun = declaration_symbol(&kotlin_index, "topFun");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Utils.kt", "com/example/app/Caller.java"],
    );
    assert_kept_alive(&graph, top_fun, "Utils.kt's topFun()");
}

// =====================================================================
// Shape 2: a Kotlin file with an explicit `@file:JvmName("CustomFacade")`
// annotation -- the Java-visible qualifier is a name that appears
// NOWHERE in the Kotlin source's own declarations at all (unlike shape
// 1, where `UtilsKt` at least shares a substring with the file name).
// =====================================================================

const JVM_NAME_FACADE_KOTLIN: &str = r#"@file:JvmName("CustomFacade")
package com.example.app

fun facadeFun(): String {
    return "value"
}
"#;

const JVM_NAME_FACADE_JAVA_CALLER: &str = r#"package com.example.app;

class Caller2 {
    String run() {
        return CustomFacade.facadeFun();
    }
}
"#;

#[test]
fn java_caller_of_a_kotlin_function_via_an_explicit_jvmname_facade_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Named.kt",
        JVM_NAME_FACADE_KOTLIN,
    );
    write_source(
        dir.path(),
        "com/example/app/Caller2.java",
        JVM_NAME_FACADE_JAVA_CALLER,
    );

    let kotlin_index = extract_index(dir.path(), "com/example/app/Named.kt");
    let facade_fun = declaration_symbol(&kotlin_index, "facadeFun");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Named.kt", "com/example/app/Caller2.java"],
    );
    assert_kept_alive(&graph, facade_fun, "Named.kt's facadeFun()");
}

// =====================================================================
// Shape 3: a Kotlin `companion object` member annotated `@JvmStatic`,
// called from Java through the OUTER class name (`Widget.make()`), not
// through `Widget.Companion.make()` -- Java-visible only because of the
// annotation, a fact this extractor does not model at all.
// =====================================================================

const COMPANION_JVMSTATIC_KOTLIN: &str = r#"package com.example.app

class Widget private constructor(val value: String) {
    companion object {
        @JvmStatic
        fun make(): Widget {
            return Widget("value")
        }
    }
}
"#;

const COMPANION_JVMSTATIC_JAVA_CALLER: &str = r#"package com.example.app;

class Caller3 {
    Widget run() {
        return Widget.make();
    }
}
"#;

#[test]
fn java_caller_of_a_kotlin_companion_jvmstatic_member_via_the_outer_class_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Widget.kt",
        COMPANION_JVMSTATIC_KOTLIN,
    );
    write_source(
        dir.path(),
        "com/example/app/Caller3.java",
        COMPANION_JVMSTATIC_JAVA_CALLER,
    );

    let kotlin_index = extract_index(dir.path(), "com/example/app/Widget.kt");
    let make_fn = declaration_symbol(&kotlin_index, "make");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Widget.kt", "com/example/app/Caller3.java"],
    );
    assert_kept_alive(&graph, make_fn, "Widget.kt's companion make()");
}

// =====================================================================
// Shape 4: a Kotlin top-level `@JvmField`-annotated property, reached
// from Java via a SINGLE-MEMBER STATIC IMPORT (`import static
// pkg.UtilsKt.FIELD;`) -- exercises the static-import guard
// (`receiver::is_statically_imported_member`) for a Kotlin target, not
// just a Java one.
// =====================================================================

const JVMFIELD_KOTLIN: &str = r#"package com.example.app

@JvmField
val FIELD: String = "value"

fun useField(): String {
    return FIELD
}
"#;

const JVMFIELD_JAVA_CALLER: &str = r#"package com.example.app;

import static com.example.app.UtilsKt.FIELD;

class Caller4 {
    String run() {
        return FIELD.trim();
    }
}
"#;

#[test]
fn java_caller_of_a_statically_imported_kotlin_jvmfield_keeps_the_kotlin_files_own_edges_intact()
{
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Utils2.kt", JVMFIELD_KOTLIN);
    write_source(
        dir.path(),
        "com/example/app/Caller4.java",
        JVMFIELD_JAVA_CALLER,
    );

    let kotlin_index = extract_index(dir.path(), "com/example/app/Utils2.kt");
    let use_field = declaration_symbol(&kotlin_index, "useField");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Utils2.kt", "com/example/app/Caller4.java"],
    );
    // `useField()` itself is unreferenced by this fixture -- the real
    // assertion is that adding the Java caller (whose qualifier
    // statically imports a Kotlin `@JvmField`) does not corrupt
    // extraction/binding for the REST of the Kotlin file's own graph, or
    // fabricate a spurious caller edge for an unrelated declaration.
    // (Kotlin top-level functions are public by default and this
    // extractor does not track Kotlin visibility precisely enough to
    // ever mark one `Some(true)` -- see `is_definitely_dead_code`'s own
    // "Public/Protected/Unknown visibility -> None" contract -- so
    // `callers == 0` is the applicable, precise assertion here, not the
    // dead-code predicate.)
    let (_, callers) = dead_and_caller_count(&graph, use_field);
    assert_eq!(
        callers, 0,
        "useField() is genuinely unreferenced by anyone in this fixture -- the \
         static-imported-Kotlin-field caller must not fabricate a spurious caller edge for it"
    );
}

// =====================================================================
// Shape 5: a Kotlin enum entry, reached from Java via a SINGLE-MEMBER
// STATIC IMPORT (`import static pkg.Mode.ACTIVE;`) then a further call
// on it -- the qualifier ("ACTIVE") is an enum CONSTANT, not a type, but
// looks exactly like one syntactically (uppercase, no local evidence).
// =====================================================================

const ENUM_ENTRY_KOTLIN: &str = r#"package com.example.app

enum class Mode {
    ACTIVE, INACTIVE;

    fun describe(): String {
        return name
    }
}
"#;

const ENUM_ENTRY_JAVA_CALLER: &str = r#"package com.example.app;

import static com.example.app.Mode.ACTIVE;

class Caller5 {
    String run() {
        return ACTIVE.describe();
    }
}
"#;

#[test]
fn java_caller_of_a_statically_imported_kotlin_enum_entry_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Mode.kt", ENUM_ENTRY_KOTLIN);
    write_source(
        dir.path(),
        "com/example/app/Caller5.java",
        ENUM_ENTRY_JAVA_CALLER,
    );

    let kotlin_index = extract_index(dir.path(), "com/example/app/Mode.kt");
    let describe_fn = declaration_symbol(&kotlin_index, "describe");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Mode.kt", "com/example/app/Caller5.java"],
    );
    assert_kept_alive(&graph, describe_fn, "Mode.kt's describe()");
}
