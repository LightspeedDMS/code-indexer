//! Liveness-guard regression suite for GitHub issue #1923. Named-type
//! argument evidence (a bare identifier's or `this`'s declared type) is
//! TAG-ONLY: it decides whether `OVERLOAD_ARG_TYPE_MATCH` is set on a
//! candidate. Candidate-set exclusion is driven exclusively by the
//! literal-shape check.
//!
//! Each fixture group guards one invariant:
//!
//! - `char`/`Character` widening stays within `char`/`Character`
//!   (fixtures 1-2).
//! - A qualified array element type (`java.lang.String[]`) and a
//!   type-variable element (`T[]`) normalize to the same string a plain
//!   `String[]` produces (fixtures 3, 7).
//! - C-style array declarator dimensions (`String xs[]`) are read from
//!   the declarator for a local, a callee parameter, and a caller's own
//!   parameter used as an identifier argument (fixtures 4-6).
//! - A bare-name compatibility check treats an external type and an
//!   unrelated repo type sharing its bare name as distinct (fixtures
//!   8-9; see `bug_1922_type_qualifier_regressions.rs` for the same
//!   trap in a different evidence path).
//! - Implicit JDK supertypes an enum/record/`String`/wrapper carries
//!   without an explicit `extends`/`implements` clause (`Enum<T>`,
//!   `Record`, `Comparable`, `Constable`, `Serializable`) are handled
//!   correctly (fixtures 10-13).
//! - Fixtures 14-18 preserve liveness without candidate exclusion for
//!   the varargs-scalar, boxing, repo-subtype, unknown-argument-type,
//!   and all-mismatched-pool shapes.

mod common;

use common::{build_graph_over, dead_and_caller_count, extract_index, write_source};
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::SymbolId;

/// Finds a `name`-declared symbol OWNED by `owner` whose OWN `param_types`
/// equal `param_types` -- disambiguates two overloads sharing both a bare
/// name AND an owner.
fn overload_symbol(index: &LocalIndex, name: &str, owner: &str, param_types: &[&str]) -> SymbolId {
    let owner_symbols: std::collections::HashSet<_> = index
        .method_owners
        .iter()
        .filter(|o| o.enclosing_type == owner)
        .map(|o| o.method_symbol)
        .collect();
    index
        .declarations
        .iter()
        .find(|d| {
            d.name == name
                && owner_symbols.contains(&d.symbol)
                && d.param_types == param_types.iter().map(|s| s.to_string()).collect::<Vec<_>>()
        })
        .unwrap_or_else(|| panic!("fixture bug: no {name:?}{param_types:?} declaration owned by {owner:?}"))
        .symbol
}

/// Finds the ONE `name`-declared symbol owned by `owner` whose OWN
/// `param_types` do NOT equal `excluded_param_types` -- robust against
/// param_types STRING formatting differences across extractor versions
/// (e.g. a qualified/C-style array type this rework normalizes but an
/// older tree renders differently), used when a fixture has exactly two
/// overloads and only ONE side's exact string representation is
/// version-stable.
fn overload_symbol_excluding(
    index: &LocalIndex,
    name: &str,
    owner: &str,
    excluded_param_types: &[&str],
) -> SymbolId {
    let owner_symbols: std::collections::HashSet<_> = index
        .method_owners
        .iter()
        .filter(|o| o.enclosing_type == owner)
        .map(|o| o.method_symbol)
        .collect();
    let excluded: Vec<String> = excluded_param_types.iter().map(|s| s.to_string()).collect();
    index
        .declarations
        .iter()
        .find(|d| d.name == name && owner_symbols.contains(&d.symbol) && d.param_types != excluded)
        .unwrap_or_else(|| panic!("fixture bug: no {name:?} declaration owned by {owner:?} other than {excluded:?}"))
        .symbol
}

/// Liveness assertion taking an already-resolved `symbol` directly.
fn assert_symbol_alive(graph: &xray_core::graph::csr::CodeGraph, symbol: SymbolId, label: &str) {
    let (dead, callers) = dead_and_caller_count(graph, symbol);
    assert_ne!(dead, Some(true), "{label} is genuinely reachable and must never be reported definitely dead");
    assert!(callers > 0, "{label} must keep its real caller edge");
}

/// The shared liveness assertion every test in this file makes: the
/// SPECIFIC overload named by `owner`/`name`/`param_types` (never an
/// ambiguously-picked "first match" among same-name-same-owner
/// overloads) must never be reported definitely dead and must keep at
/// least one real caller edge.
fn assert_alive(
    index: &LocalIndex,
    graph: &xray_core::graph::csr::CodeGraph,
    name: &str,
    owner: &str,
    param_types: &[&str],
) {
    let symbol = overload_symbol(index, name, owner, param_types);
    let (dead, callers) = dead_and_caller_count(graph, symbol);
    assert_ne!(
        dead,
        Some(true),
        "{owner}.{name}{param_types:?} is genuinely reachable and must never be reported definitely dead"
    );
    assert!(callers > 0, "{owner}.{name}{param_types:?} must keep its real caller edge");
}

// =====================================================================
// 1. char argument vs t(int)/t(Object) -- char widens to int, long,
// float, double (JLS 5.1.2), which the old compatibility rule missed.
// =====================================================================

const CHAR_WIDENING_SOURCE: &str = r#"package com.example.app;

public class Sink1 {
    void t(int x) { }
    void t(Object o) { }
}

class Caller1 {
    Sink1 sink = new Sink1();
    void run(char c) {
        sink.t(c);
    }
}
"#;

#[test]
fn char_argument_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sink1.java", CHAR_WIDENING_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Sink1.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Sink1.java"]);
    assert_alive(&index, &graph, "t", "Sink1", &["int"]);
    assert_alive(&index, &graph, "t", "Sink1", &["Object"]);
}

// =====================================================================
// 2. Character argument vs t(int) (unboxing + widening) / a BOUNDED
// generic <T extends Number> t(T) -- same widening bug, plus a bounded
// type-parameter overload.
// =====================================================================

const CHARACTER_WIDENING_SOURCE: &str = r#"package com.example.app;

public class Sink2 {
    void t(int x) { }
    <T extends Number> void t(T value) { }
}

class Caller2 {
    Sink2 sink = new Sink2();
    void run(Character c) {
        sink.t(c);
    }
}
"#;

#[test]
fn boxed_character_argument_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sink2.java", CHARACTER_WIDENING_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Sink2.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Sink2.java"]);
    assert_alive(&index, &graph, "t", "Sink2", &["int"]);
    assert_alive(&index, &graph, "t", "Sink2", &["T"]);
}

// =====================================================================
// 3. A QUALIFIED array type t(java.lang.String[] a) vs t(Object) with a
// String[] argument -- the old raw-text array fallback never normalized
// `java.lang.String[]` to the same string a plain `String[]` produces.
// =====================================================================

const QUALIFIED_ARRAY_SOURCE: &str = r#"package com.example.app;

public class Sink3 {
    void t(java.lang.String[] a) { }
    void t(Object o) { }
}

class Caller3 {
    Sink3 sink = new Sink3();
    void run(String[] a) {
        sink.t(a);
    }
}
"#;

#[test]
fn qualified_array_element_type_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sink3.java", QUALIFIED_ARRAY_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Sink3.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Sink3.java"]);
    // The qualified-array overload's OWN param_types string is version-
    // dependent (see `overload_symbol_excluding`'s own doc comment) --
    // found by elimination against the version-stable `Object` sibling.
    let qualified_array = overload_symbol_excluding(&index, "t", "Sink3", &["Object"]);
    assert_symbol_alive(&graph, qualified_array, "Sink3.t(qualified array)");
    assert_alive(&index, &graph, "t", "Sink3", &["Object"]);
}

// =====================================================================
// 4. A C-STYLE LOCAL `String xs[];` (dimensions on the declarator, not
// the type node) used as an identifier argument -- the old extraction
// dropped C-style dimensions entirely, so `xs` resolved as a bare
// `String`, not `String[]`.
// =====================================================================

const C_STYLE_LOCAL_SOURCE: &str = r#"package com.example.app;

public class Sink4 {
    void t(String[] a) { }
    void t(Object o) { }
}

class Caller4 {
    Sink4 sink = new Sink4();
    void run() {
        String xs[] = null;
        sink.t(xs);
    }
}
"#;

#[test]
fn c_style_local_array_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sink4.java", C_STYLE_LOCAL_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Sink4.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Sink4.java"]);
    assert_alive(&index, &graph, "t", "Sink4", &["String[]"]);
    assert_alive(&index, &graph, "t", "Sink4", &["Object"]);
}

// =====================================================================
// 5. A CALLEE C-STYLE array parameter `t(String a[])` -- dimensions on
// the `formal_parameter`'s own declarator, not its type field.
// =====================================================================

const C_STYLE_CALLEE_PARAM_SOURCE: &str = r#"package com.example.app;

public class Sink5 {
    void t(String a[]) { }
    void t(Object o) { }
}

class Caller5 {
    Sink5 sink = new Sink5();
    void run(String[] a) {
        sink.t(a);
    }
}
"#;

#[test]
fn c_style_callee_parameter_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sink5.java", C_STYLE_CALLEE_PARAM_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Sink5.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Sink5.java"]);
    // The C-style-array overload's OWN param_types string is version-
    // dependent (see `overload_symbol_excluding`'s own doc comment) --
    // found by elimination against the version-stable `Object` sibling.
    let c_style_array = overload_symbol_excluding(&index, "t", "Sink5", &["Object"]);
    assert_symbol_alive(&graph, c_style_array, "Sink5.t(C-style array)");
    assert_alive(&index, &graph, "t", "Sink5", &["Object"]);
}

// =====================================================================
// 6. The CALLER's OWN C-style array parameter `run(String xs[])`, used
// as the identifier argument -- same underlying bug as fixture 4, on the
// PARAMETER side of `formal_parameter_type_name` instead of the local-
// declarator side.
// =====================================================================

const C_STYLE_CALLER_PARAM_SOURCE: &str = r#"package com.example.app;

public class Sink6 {
    void t(String[] a) { }
    void t(Object o) { }
}

class Caller6 {
    Sink6 sink = new Sink6();
    void run(String xs[]) {
        sink.t(xs);
    }
}
"#;

#[test]
fn c_style_caller_parameter_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sink6.java", C_STYLE_CALLER_PARAM_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Sink6.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Sink6.java"]);
    assert_alive(&index, &graph, "t", "Sink6", &["String[]"]);
    assert_alive(&index, &graph, "t", "Sink6", &["Object"]);
}

// =====================================================================
// 7. A TYPE-VARIABLE array element `<T> t(T[] a)` vs `t(Object)` with a
// `String[]` argument -- the old raw-text array fallback never
// normalized `T[]` the same way `array_type_base_name` now does, and
// the type-parameter exemption never reached an array's ELEMENT type.
// =====================================================================

const TYPE_VARIABLE_ARRAY_SOURCE: &str = r#"package com.example.app;

public class Sink7 {
    <T> void t(T[] a) { }
    void t(Object o) { }
}

class Caller7 {
    Sink7 sink = new Sink7();
    void run(String[] a) {
        sink.t(a);
    }
}
"#;

#[test]
fn type_variable_array_element_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sink7.java", TYPE_VARIABLE_ARRAY_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Sink7.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Sink7.java"]);
    assert_alive(&index, &graph, "t", "Sink7", &["T[]"]);
    assert_alive(&index, &graph, "t", "Sink7", &["Object"]);
}

// =====================================================================
// 8. `java.util.List<String>` argument WHILE the repo ALSO declares its
// own unrelated `com.example.otherlib8.List` class (bare-name collision)
// -- against `t(java.util.Collection<?>)`/`t(Object)`. The dropped repo-
// supertype-chain mechanism used to resolve the ARGUMENT's bare type
// name ("List") against the REPO's own unrelated `List` class's
// (empty) ancestor set, wrongly concluding `java.util.List` is not a
// `Collection`.
// =====================================================================

const LIST_SOURCE: &str = r#"package com.example.app8;

public class Sink8 {
    void t(java.util.Collection<?> c) { }
    void t(Object o) { }
}

class Caller8 {
    Sink8 sink = new Sink8();
    void run(java.util.List<String> items) {
        sink.t(items);
    }
}
"#;

const UNRELATED_LIST_SOURCE: &str = r#"package com.example.otherlib8;

public class List {
}
"#;

#[test]
fn repo_namesake_of_an_external_generic_type_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app8/Sink8.java", LIST_SOURCE);
    write_source(dir.path(), "com/example/otherlib8/List.java", UNRELATED_LIST_SOURCE);
    let index = extract_index(dir.path(), "com/example/app8/Sink8.java");
    let graph = build_graph_over(
        dir.path(),
        &["com/example/app8/Sink8.java", "com/example/otherlib8/List.java"],
    );
    assert_alive(&index, &graph, "t", "Sink8", &["Collection"]);
    assert_alive(&index, &graph, "t", "Sink8", &["Object"]);
}

// =====================================================================
// 9. `Sub extends com.lib.Base9` -- an EXTERNAL type never declared
// anywhere in this repo -- WHILE the repo ALSO declares its own
// unrelated `com.example.otherlib9.Base9` class (bare-name collision) --
// against `t(Runnable)`/`t(Object)`. The dropped mechanism resolved
// `Sub9`'s recorded ancestor ("Base9", a bare name) against the REPO's
// own unrelated `Base9` (which implements nothing), wrongly concluding
// `Sub9` is not `Runnable` -- when in the real, external hierarchy this
// binder cannot see, `com.lib.Base9` genuinely does implement it.
// =====================================================================

const EXTERNAL_SUPERCLASS_SOURCE: &str = r#"package com.example.app9;

public class Sink9 {
    void t(Runnable r) { }
    void t(Object o) { }
}

class Sub9 extends com.lib.Base9 {
}

class Caller9 {
    Sink9 sink = new Sink9();
    void run(Sub9 s) {
        sink.t(s);
    }
}
"#;

const UNRELATED_BASE9_SOURCE: &str = r#"package com.example.otherlib9;

public class Base9 {
}
"#;

#[test]
fn repo_namesake_of_an_external_superclass_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app9/Sink9.java", EXTERNAL_SUPERCLASS_SOURCE);
    write_source(dir.path(), "com/example/otherlib9/Base9.java", UNRELATED_BASE9_SOURCE);
    let index = extract_index(dir.path(), "com/example/app9/Sink9.java");
    let graph = build_graph_over(
        dir.path(),
        &["com/example/app9/Sink9.java", "com/example/otherlib9/Base9.java"],
    );
    assert_alive(&index, &graph, "t", "Sink9", &["Runnable"]);
    assert_alive(&index, &graph, "t", "Sink9", &["Object"]);
}

// =====================================================================
// 10. A `String` argument vs `t(java.lang.constant.Constable)`/
// `t(Object)` -- `String` implicitly implements `Constable` (JDK 12+)
// with no explicit `implements` clause this extractor could ever see.
// =====================================================================

const STRING_CONSTABLE_SOURCE: &str = r#"package com.example.app;

public class Sink10 {
    void t(java.lang.constant.Constable c) { }
    void t(Object o) { }
}

class Caller10 {
    Sink10 sink = new Sink10();
    void run(String s) {
        sink.t(s);
    }
}
"#;

#[test]
fn string_argument_against_constable_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sink10.java", STRING_CONSTABLE_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Sink10.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Sink10.java"]);
    assert_alive(&index, &graph, "t", "Sink10", &["Constable"]);
    assert_alive(&index, &graph, "t", "Sink10", &["Object"]);
}

// =====================================================================
// 11. A REPO ENUM argument vs `t(Comparable<?>)`/`t(Object)` -- every
// Java enum implicitly implements `Comparable<E>` via its `Enum<E>`
// supertype, never recorded as an inheritance edge by this extractor.
// =====================================================================

const ENUM_COMPARABLE_SOURCE: &str = r#"package com.example.app;

public enum Status11 {
    ACTIVE, INACTIVE
}

class Sink11 {
    void t(Comparable<?> c) { }
    void t(Object o) { }
}

class Caller11 {
    Sink11 sink = new Sink11();
    void run(Status11 s) {
        sink.t(s);
    }
}
"#;

#[test]
fn repo_enum_argument_against_comparable_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Status11.java", ENUM_COMPARABLE_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Status11.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Status11.java"]);
    assert_alive(&index, &graph, "t", "Sink11", &["Comparable"]);
    assert_alive(&index, &graph, "t", "Sink11", &["Object"]);
}

// =====================================================================
// 12. A REPO ENUM argument vs `t(Enum<?>)`/`t(Object)` -- the implicit
// `Enum<E>` supertype ITSELF is never recorded as an inheritance edge.
// =====================================================================

const ENUM_ENUM_SOURCE: &str = r#"package com.example.app;

public enum Status12 {
    ACTIVE, INACTIVE
}

class Sink12 {
    void t(Enum<?> e) { }
    void t(Object o) { }
}

class Caller12 {
    Sink12 sink = new Sink12();
    void run(Status12 s) {
        sink.t(s);
    }
}
"#;

#[test]
fn repo_enum_argument_against_enum_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Status12.java", ENUM_ENUM_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Status12.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Status12.java"]);
    assert_alive(&index, &graph, "t", "Sink12", &["Enum"]);
    assert_alive(&index, &graph, "t", "Sink12", &["Object"]);
}

// =====================================================================
// 13. A REPO RECORD argument vs `t(Record)`/`t(Object)` -- every Java
// record implicitly extends `java.lang.Record`, never recorded as an
// inheritance edge (records have no `superclass` grammar node at all).
// =====================================================================

const RECORD_SOURCE: &str = r#"package com.example.app;

public record Point13(int x, int y) {
}

class Sink13 {
    void t(Record r) { }
    void t(Object o) { }
}

class Caller13 {
    Sink13 sink = new Sink13();
    void run(Point13 p) {
        sink.t(p);
    }
}
"#;

#[test]
fn repo_record_argument_against_record_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Point13.java", RECORD_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Point13.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Point13.java"]);
    assert_alive(&index, &graph, "t", "Sink13", &["Record"]);
    assert_alive(&index, &graph, "t", "Sink13", &["Object"]);
}

// =====================================================================
// 14. Varargs POSITIVE case: a scalar `String` parameter reference vs a
// `String...`/`int` sibling overload pair.
// =====================================================================

const VARARGS_SCALAR_SOURCE: &str = r#"package com.example.app;

public class Logger14 {
    static void log(String... parts) { }
    static void log(int code) { }
}

class Caller14 {
    void run(String value) {
        Logger14.log(value);
    }
}
"#;

#[test]
fn scalar_string_argument_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Logger14.java", VARARGS_SCALAR_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Logger14.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Logger14.java"]);
    assert_alive(&index, &graph, "log", "Logger14", &["String"]);
    assert_alive(&index, &graph, "log", "Logger14", &["int"]);
}

// =====================================================================
// 15. Boxing: `int` -> `Integer`/`String` sibling overloads.
// =====================================================================

const BOXING_SOURCE: &str = r#"package com.example.app;

public class Boxer15 {
    void run(int code) {
        Sink15.accept(code);
    }
}

class Sink15 {
    static void accept(Integer value) { }
    static void accept(String value) { }
}
"#;

#[test]
fn primitive_int_argument_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Boxer15.java", BOXING_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Boxer15.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Boxer15.java"]);
    assert_alive(&index, &graph, "accept", "Sink15", &["Integer"]);
    assert_alive(&index, &graph, "accept", "Sink15", &["String"]);
}

// =====================================================================
// 16. Repo subtype via `extends`: `Sub16` (extends `Base16`, both
// repo-declared) vs `accept(Base16)`/`accept(OtherType16)`.
// =====================================================================

const SUBTYPE_SOURCE: &str = r#"package com.example.app;

public class Base16 {
}

class Sub16 extends Base16 {
}

class OtherType16 {
}

class Caller16 {
    void run(Sub16 s) {
        Sink16.accept(s);
    }
}

class Sink16 {
    static void accept(Base16 b) { }
    static void accept(OtherType16 t) { }
}
"#;

#[test]
fn repo_subtype_argument_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Base16.java", SUBTYPE_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Base16.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Base16.java"]);
    assert_alive(&index, &graph, "accept", "Sink16", &["Base16"]);
    assert_alive(&index, &graph, "accept", "Sink16", &["OtherType16"]);
}

// =====================================================================
// 17. Unknown argument type -- a FIELD ACCESS through another object
// (`helper.value`, never resolved by this binder's argument-type
// evidence) vs two same-owner overloads.
// =====================================================================

const UNKNOWN_ARG_SOURCE: &str = r#"package com.example.app;

public class Caller17 {
    Helper17 helper;

    void run() {
        Sink17.accept(helper.value);
    }
}

class Helper17 {
    Base17 value;
}

class Base17 {
}

class OtherType17 {
}

class Sink17 {
    static void accept(Base17 b) { }
    static void accept(OtherType17 t) { }
}
"#;

#[test]
fn unknown_argument_type_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Caller17.java", UNKNOWN_ARG_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/Caller17.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/Caller17.java"]);
    assert_alive(&index, &graph, "accept", "Sink17", &["Base17"]);
    assert_alive(&index, &graph, "accept", "Sink17", &["OtherType17"]);
}

// =====================================================================
// 18. All-mismatch pool: an `int` argument against `pick(String)`/
// `pick(boolean)`, where EVERY candidate has a known mismatch.
// =====================================================================

const ALL_MISMATCH_SOURCE: &str = r#"package com.example.app;

public class AllMismatch18 {
    void run(int code) {
        Sink18.pick(code);
    }
}

class Sink18 {
    static void pick(String a) { }
    static void pick(boolean b) { }
}
"#;

#[test]
fn all_candidates_mismatched_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/AllMismatch18.java", ALL_MISMATCH_SOURCE);
    let index = extract_index(dir.path(), "com/example/app/AllMismatch18.java");
    let graph = build_graph_over(dir.path(), &["com/example/app/AllMismatch18.java"]);
    assert_alive(&index, &graph, "pick", "Sink18", &["String"]);
    assert_alive(&index, &graph, "pick", "Sink18", &["boolean"]);
}
