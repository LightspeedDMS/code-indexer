//! `narrowing::apply_overload_shape_narrowing`'s Cast/Constructor
//! named-type evidence must stay TAG-ONLY for EVERY type, closed-world
//! or not: it may never hard-narrow the candidate pool on a bare-name
//! match, whether comparing a single argument position in isolation
//! (fixtures 19-21, X08, X10) or across a multi-argument call where a
//! partial per-position match on one candidate would otherwise outrank
//! the real, fully-applicable target (fixture X07). A bare-name match on
//! one argument position never proves the WHOLE call resolves to that
//! candidate.
//!
//! Fixtures 23-24 are tag-accuracy regressions: a zero-argument varargs
//! call still tags a varargs-only candidate, and array element
//! compatibility respects primitive-array invariance rather than scalar
//! boxing rules. Fixture 25 pins that a Java caller into a non-Java
//! callee keeps its existing tag rather than losing it to a
//! cross-language type-vocabulary mismatch. Fixtures 26-28 extend the
//! array-covariance tag accuracy to multi-dimensional arrays, and
//! fixture 29 pins that a genuinely zero-parameter overload still earns
//! the tag on a zero-argument call.

mod common;

use common::{build_graph_over, dead_and_caller_count, declaration_symbol_owned_by, extract_index, write_source};
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::SymbolId;
use xray_core::graph::reasons::OVERLOAD_ARG_TYPE_MATCH;

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

fn assert_alive(graph: &xray_core::graph::csr::CodeGraph, symbol: SymbolId, label: &str) {
    let (dead, callers) = dead_and_caller_count(graph, symbol);
    assert_ne!(dead, Some(true), "{label} is genuinely reachable and must never be reported definitely dead");
    assert!(callers > 0, "{label} must keep its real caller edge");
}

fn edge_bits(graph: &xray_core::graph::csr::CodeGraph, from: SymbolId, to: SymbolId) -> Option<u16> {
    let from_dense = graph.dense_id_for(from)?;
    let to_dense = graph.dense_id_for(to)?;
    graph.edge_evidence(from_dense, to_dense)
}

// =====================================================================
// 22. A `null` literal argument against `t(int a[])` (C-style array) vs
// `t(String s)` -- `null` is assignable to any REFERENCE type, INCLUDING
// a primitive-element array (arrays are always reference types), so
// neither the pre-existing `NullLiteral` check nor the C-style
// array-dimension handling may exclude it from the tag.
// =====================================================================

const NULL_VS_ARRAY_SOURCE: &str = r#"package com.example.app22;

public class Sink22 {
    void t(int a[]) { }
    void t(String s) { }
}

class Caller22 {
    Sink22 sink = new Sink22();
    void run() {
        sink.t(null);
    }
}
"#;

#[test]
fn null_literal_argument_tags_the_c_style_primitive_array_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app22/Sink22.java", NULL_VS_ARRAY_SOURCE);
    let index = extract_index(dir.path(), "com/example/app22/Sink22.java");
    let caller = declaration_symbol_owned_by(&index, "run", "Caller22");
    let t_int_array = overload_symbol(&index, "t", "Sink22", &["int[]"]);
    let graph = build_graph_over(dir.path(), &["com/example/app22/Sink22.java"]);
    let bits = edge_bits(&graph, caller, t_int_array).expect("Sink22.t(int[]) edge must exist");
    assert_ne!(
        bits & OVERLOAD_ARG_TYPE_MATCH,
        0,
        "a null literal must tag t(int[]) -- arrays are reference types, never excluded by the primitive check"
    );
}

// =====================================================================
// 19. An ARRAY cast: `t(com.example.otherlib19b.Node19[])` vs
// `t(Object[])`, called with a CAST to a DIFFERENTLY-PACKAGED
// `com.example.otherlib19a.Node19[]` -- both `Node19` types normalize to
// the identical bare "Node19[]" after array-type normalization, so an
// "exact bare-name match" here is a coincidence, not genuine identity.
// =====================================================================

const ARRAY_CAST_NODE19A: &str = "package com.example.otherlib19a;\npublic class Node19 {\n}\n";
const ARRAY_CAST_NODE19B: &str = "package com.example.otherlib19b;\npublic class Node19 {\n}\n";
const ARRAY_CAST_SINK: &str = r#"package com.example.app19;

public class Sink19 {
    void t(com.example.otherlib19b.Node19[] a) { }
    void t(Object[] a) { }
}

class Caller19 {
    Sink19 sink = new Sink19();
    void run(com.example.otherlib19a.Node19[] x) {
        sink.t((com.example.otherlib19a.Node19[]) x);
    }
}
"#;

#[test]
fn array_cast_across_packages_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/otherlib19a/Node19.java", ARRAY_CAST_NODE19A);
    write_source(dir.path(), "com/example/otherlib19b/Node19.java", ARRAY_CAST_NODE19B);
    write_source(dir.path(), "com/example/app19/Sink19.java", ARRAY_CAST_SINK);
    let index = extract_index(dir.path(), "com/example/app19/Sink19.java");
    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/otherlib19a/Node19.java",
            "com/example/otherlib19b/Node19.java",
            "com/example/app19/Sink19.java",
        ],
    );
    assert_alive(&graph, overload_symbol(&index, "t", "Sink19", &["Node19[]"]), "Sink19.t(Node19[])");
    assert_alive(&graph, overload_symbol(&index, "t", "Sink19", &["Object[]"]), "Sink19.t(Object[])");
}

// =====================================================================
// 20. The SAME trap for a SCALAR (non-array) named type: `t(com.
// example.otherlib20b.Node20)` vs `t(Object)`, called with a cast to a
// differently-packaged `com.example.otherlib20a.Node20`.
// =====================================================================

const SCALAR_CAST_NODE20A: &str = "package com.example.otherlib20a;\npublic class Node20 {\n}\n";
const SCALAR_CAST_NODE20B: &str = "package com.example.otherlib20b;\npublic class Node20 {\n}\n";
const SCALAR_CAST_SINK: &str = r#"package com.example.app20;

public class Sink20 {
    void t(com.example.otherlib20b.Node20 a) { }
    void t(Object o) { }
}

class Caller20 {
    Sink20 sink = new Sink20();
    void run(com.example.otherlib20a.Node20 x) {
        sink.t((com.example.otherlib20a.Node20) x);
    }
}
"#;

#[test]
fn scalar_cast_across_packages_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/otherlib20a/Node20.java", SCALAR_CAST_NODE20A);
    write_source(dir.path(), "com/example/otherlib20b/Node20.java", SCALAR_CAST_NODE20B);
    write_source(dir.path(), "com/example/app20/Sink20.java", SCALAR_CAST_SINK);
    let index = extract_index(dir.path(), "com/example/app20/Sink20.java");
    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/otherlib20a/Node20.java",
            "com/example/otherlib20b/Node20.java",
            "com/example/app20/Sink20.java",
        ],
    );
    assert_alive(&graph, overload_symbol(&index, "t", "Sink20", &["Node20"]), "Sink20.t(Node20)");
    assert_alive(&graph, overload_symbol(&index, "t", "Sink20", &["Object"]), "Sink20.t(Object)");
}

// =====================================================================
// 21. C-STYLE callee array param `t(com.example.otherlib21b.Node21
// a[])` vs `t(Object[])`, called with an UNQUALIFIED (imported) cast
// `(Node21[]) x` -- combines the cross-package bare-name collision with
// the C-style-array normalization path, and with a cast target that
// never carries package qualification at all in source.
// =====================================================================

const C_STYLE_CAST_NODE21A: &str = "package com.example.otherlib21a;\npublic class Node21 {\n}\n";
const C_STYLE_CAST_NODE21B: &str = "package com.example.otherlib21b;\npublic class Node21 {\n}\n";
const C_STYLE_CAST_SINK: &str = r#"package com.example.app21;

import com.example.otherlib21a.Node21;

public class Sink21 {
    void t(com.example.otherlib21b.Node21 a[]) { }
    void t(Object[] a) { }
}

class Caller21 {
    Sink21 sink = new Sink21();
    void run(Node21[] x) {
        sink.t((Node21[]) x);
    }
}
"#;

#[test]
fn c_style_array_cast_across_packages_never_falsely_excludes_either_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/otherlib21a/Node21.java", C_STYLE_CAST_NODE21A);
    write_source(dir.path(), "com/example/otherlib21b/Node21.java", C_STYLE_CAST_NODE21B);
    write_source(dir.path(), "com/example/app21/Sink21.java", C_STYLE_CAST_SINK);
    let index = extract_index(dir.path(), "com/example/app21/Sink21.java");
    let graph = build_graph_over(
        dir.path(),
        &[
            "com/example/otherlib21a/Node21.java",
            "com/example/otherlib21b/Node21.java",
            "com/example/app21/Sink21.java",
        ],
    );
    assert_alive(&graph, overload_symbol(&index, "t", "Sink21", &["Node21[]"]), "Sink21.t(Node21[])");
    assert_alive(&graph, overload_symbol(&index, "t", "Sink21", &["Object[]"]), "Sink21.t(Object[])");
}

// =====================================================================
// 23. `Sink23.matchAny(String... seq)` called with ZERO arguments --
// varargs genuinely accepts zero elements, so the empty-args early
// return in `apply_overload_shape_narrowing` must still tag it rather
// than skip tagging entirely for lack of any per-argument evidence.
// =====================================================================

const ZERO_ARG_VARARGS_SOURCE: &str = r#"package com.example.app23;

public class Sink23 {
    static void matchAny(String... seq) { }
}

// A second, unrelated `matchAny` declaration -- with only ONE
// declaration in the repo, this call would take the AC4 unique-name
// shortcut, which bypasses `apply_overload_shape_narrowing` entirely
// (returning before it ever runs) and would make this fixture pass for
// the wrong reason.
class Other23 {
    static void matchAny(int x) { }
}

class Caller23 {
    void run() {
        Sink23.matchAny();
    }
}
"#;

#[test]
fn zero_argument_varargs_call_still_carries_the_tag() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app23/Sink23.java", ZERO_ARG_VARARGS_SOURCE);
    let index = extract_index(dir.path(), "com/example/app23/Sink23.java");
    let caller = declaration_symbol_owned_by(&index, "run", "Caller23");
    let match_any = overload_symbol(&index, "matchAny", "Sink23", &["String"]);
    let graph = build_graph_over(dir.path(), &["com/example/app23/Sink23.java"]);
    let bits = edge_bits(&graph, caller, match_any).expect("Sink23.matchAny() edge must exist");
    assert_ne!(
        bits & OVERLOAD_ARG_TYPE_MATCH,
        0,
        "a zero-argument call to a varargs-only method must still carry OVERLOAD_ARG_TYPE_MATCH"
    );
}

// =====================================================================
// 24. `t(Object[] a)` vs `t(int[] a)` called with an `int[]` argument --
// `int[]` is NOT assignable to `Object[]` in real Java (a primitive-
// element array is INVARIANT, unlike the whole array's own conversion
// to `Object`), so only `t(int[])` may carry the tag; both must stay
// alive (tag-only, never excludes).
// =====================================================================

const ARRAY_COVARIANCE_SOURCE: &str = r#"package com.example.app24;

public class Sink24 {
    void t(Object[] a) { }
    void t(int[] a) { }
}

class Caller24 {
    Sink24 sink = new Sink24();
    void run(int[] x) {
        sink.t(x);
    }
}
"#;

#[test]
fn primitive_array_argument_tags_only_the_exact_primitive_array_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app24/Sink24.java", ARRAY_COVARIANCE_SOURCE);
    let index = extract_index(dir.path(), "com/example/app24/Sink24.java");
    let caller = declaration_symbol_owned_by(&index, "run", "Caller24");
    let t_int_array = overload_symbol(&index, "t", "Sink24", &["int[]"]);
    let t_object_array = overload_symbol(&index, "t", "Sink24", &["Object[]"]);
    let graph = build_graph_over(dir.path(), &["com/example/app24/Sink24.java"]);

    assert_alive(&graph, t_int_array, "Sink24.t(int[])");
    assert_alive(&graph, t_object_array, "Sink24.t(Object[])");

    let int_bits = edge_bits(&graph, caller, t_int_array).expect("Sink24.t(int[]) edge must exist");
    let object_bits = edge_bits(&graph, caller, t_object_array).expect("Sink24.t(Object[]) edge must exist");
    assert_ne!(int_bits & OVERLOAD_ARG_TYPE_MATCH, 0, "int[] argument must tag t(int[]) exactly");
    assert_eq!(
        object_bits & OVERLOAD_ARG_TYPE_MATCH,
        0,
        "int[] must never tag t(Object[]) -- primitive-element arrays are invariant"
    );
}

// =====================================================================
// 25. A JAVA caller invoking a KOTLIN method `K25.a(x: Int)` -- Kotlin
// records its own type vocabulary (`Int`, not Java's `int`/`Integer`),
// which the Java-oriented closed-world compatibility rule cannot
// recognise as equivalent. A second, unrelated Kotlin `Other25.a(x:
// String)` avoids the AC4 unique-name shortcut, which would bypass the
// tag check entirely.
// =====================================================================

const KOTLIN_CALLEE_SOURCE: &str = r#"package com.example.app25

class K25 {
    fun a(x: Int) { }
}

class Other25 {
    fun a(x: String) { }
}
"#;

const JAVA_CALLER_25_SOURCE: &str = r#"package com.example.app25;

public class JCaller25 {
    K25 k = new K25();
    void run(int x) {
        k.a(x);
    }
}
"#;

#[test]
fn java_caller_into_kotlin_callee_keeps_the_tag() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app25/K25.kt", KOTLIN_CALLEE_SOURCE);
    write_source(dir.path(), "com/example/app25/JCaller25.java", JAVA_CALLER_25_SOURCE);
    let java_index = extract_index(dir.path(), "com/example/app25/JCaller25.java");
    let kotlin_index = extract_index(dir.path(), "com/example/app25/K25.kt");
    let caller = declaration_symbol_owned_by(&java_index, "run", "JCaller25");
    let kotlin_a = declaration_symbol_owned_by(&kotlin_index, "a", "K25");
    let graph = build_graph_over(
        dir.path(),
        &["com/example/app25/K25.kt", "com/example/app25/JCaller25.java"],
    );
    let bits = edge_bits(&graph, caller, kotlin_a).expect("JCaller25.run -> K25.a edge must exist");
    assert_ne!(
        bits & OVERLOAD_ARG_TYPE_MATCH,
        0,
        "a Java int argument into a Kotlin K25.a(Int) callee must keep OVERLOAD_ARG_TYPE_MATCH, \
         matching pre-#1923 (HEAD) behaviour for a non-Java callee"
    );
}

// =====================================================================
// 26. `t(Object[] a)` vs `t(int[][] a)` called with an `int[][]`
// argument -- each ELEMENT of a 2-D `int[]` array is itself `int[]`, a
// REFERENCE type (arrays are always reference types), so `int[][]` IS
// assignable to `Object[]` (every element is an `Object`), unlike the
// 1-D `int[]` case in fixture 24. Array dimensions must be stripped one
// level at a time, never collapsed all at once to the deepest scalar
// name before the primitive-invariance check runs -- doing so would
// wrongly withhold the tag from `int[][]` here.
// =====================================================================

const MULTI_DIM_TO_OBJECT_ARRAY_SOURCE: &str = r#"package com.example.app26;

public class Sink26 {
    void t(Object[] a) { }
}

// A second, unrelated `t` declaration -- with only ONE declaration in
// the repo, this call would take the AC4 unique-name shortcut, which
// bypasses `apply_overload_shape_narrowing` entirely (returning before
// it ever runs) and would make this fixture pass for the wrong reason.
class Other26 {
    void t(String s) { }
}

class Caller26 {
    Sink26 sink = new Sink26();
    void run(int[][] x) {
        sink.t(x);
    }
}
"#;

#[test]
fn two_dimensional_primitive_array_argument_tags_the_object_array_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app26/Sink26.java", MULTI_DIM_TO_OBJECT_ARRAY_SOURCE);
    let index = extract_index(dir.path(), "com/example/app26/Sink26.java");
    let caller = declaration_symbol_owned_by(&index, "run", "Caller26");
    let t_object_array = overload_symbol(&index, "t", "Sink26", &["Object[]"]);
    let graph = build_graph_over(dir.path(), &["com/example/app26/Sink26.java"]);
    let bits = edge_bits(&graph, caller, t_object_array).expect("Sink26.t(Object[]) edge must exist");
    assert_ne!(
        bits & OVERLOAD_ARG_TYPE_MATCH,
        0,
        "int[][] must tag t(Object[]) -- each element (int[]) is itself a reference type"
    );
}

// =====================================================================
// 27. `t(Object[][] a)` vs `t(String[][] a)` called with a `String[][]`
// argument -- reference-element arrays are covariant at every level, so
// `String[][]` IS assignable to `Object[][]`; only the exact
// `String[][]` overload additionally carries the tag from the same call
// (both alive, tag-only).
// =====================================================================

const MULTI_DIM_REFERENCE_ARRAY_SOURCE: &str = r#"package com.example.app27;

public class Sink27 {
    void t(Object[][] a) { }
    void t(String[][] a) { }
}

class Caller27 {
    Sink27 sink = new Sink27();
    void run(String[][] x) {
        sink.t(x);
    }
}
"#;

#[test]
fn two_dimensional_reference_array_argument_tags_both_covariant_overloads() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app27/Sink27.java", MULTI_DIM_REFERENCE_ARRAY_SOURCE);
    let index = extract_index(dir.path(), "com/example/app27/Sink27.java");
    let caller = declaration_symbol_owned_by(&index, "run", "Caller27");
    let t_object_array = overload_symbol(&index, "t", "Sink27", &["Object[][]"]);
    let t_string_array = overload_symbol(&index, "t", "Sink27", &["String[][]"]);
    let graph = build_graph_over(dir.path(), &["com/example/app27/Sink27.java"]);

    assert_alive(&graph, t_object_array, "Sink27.t(Object[][])");
    assert_alive(&graph, t_string_array, "Sink27.t(String[][])");

    let object_bits = edge_bits(&graph, caller, t_object_array).expect("Sink27.t(Object[][]) edge must exist");
    let string_bits = edge_bits(&graph, caller, t_string_array).expect("Sink27.t(String[][]) edge must exist");
    assert_ne!(object_bits & OVERLOAD_ARG_TYPE_MATCH, 0, "String[][] must tag t(Object[][]) via reference covariance");
    assert_ne!(string_bits & OVERLOAD_ARG_TYPE_MATCH, 0, "String[][] must tag t(String[][]) exactly");
}

// =====================================================================
// 28. `t(Object[][] a)` vs `t(int[][] a)` called with an `int[][]`
// argument -- element-wise, `int[][]`'s element is `int[]` and
// `Object[][]`'s element is `Object[]`; `int[]` is NOT assignable to
// `Object[]` (fixture 24's own invariance), so `int[][]` is NOT
// assignable to `Object[][]` either, one level up. Only `t(int[][])` may
// carry the tag.
// =====================================================================

const MULTI_DIM_PRIMITIVE_INVARIANCE_SOURCE: &str = r#"package com.example.app28;

public class Sink28 {
    void t(Object[][] a) { }
    void t(int[][] a) { }
}

class Caller28 {
    Sink28 sink = new Sink28();
    void run(int[][] x) {
        sink.t(x);
    }
}
"#;

#[test]
fn two_dimensional_primitive_array_argument_never_tags_the_two_dimensional_object_array_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app28/Sink28.java", MULTI_DIM_PRIMITIVE_INVARIANCE_SOURCE);
    let index = extract_index(dir.path(), "com/example/app28/Sink28.java");
    let caller = declaration_symbol_owned_by(&index, "run", "Caller28");
    let t_object_array = overload_symbol(&index, "t", "Sink28", &["Object[][]"]);
    let t_int_array = overload_symbol(&index, "t", "Sink28", &["int[][]"]);
    let graph = build_graph_over(dir.path(), &["com/example/app28/Sink28.java"]);

    assert_alive(&graph, t_object_array, "Sink28.t(Object[][])");
    assert_alive(&graph, t_int_array, "Sink28.t(int[][])");

    let object_bits = edge_bits(&graph, caller, t_object_array).expect("Sink28.t(Object[][]) edge must exist");
    let int_bits = edge_bits(&graph, caller, t_int_array).expect("Sink28.t(int[][]) edge must exist");
    assert_eq!(
        object_bits & OVERLOAD_ARG_TYPE_MATCH,
        0,
        "int[][] must never tag t(Object[][]) -- int[] is not assignable to Object[] one level down"
    );
    assert_ne!(int_bits & OVERLOAD_ARG_TYPE_MATCH, 0, "int[][] argument must tag t(int[][]) exactly");
}

// =====================================================================
// 29. `m()` (genuinely zero parameters) vs `m(int)`, called `m()` --
// the empty-args branch of `apply_overload_shape_narrowing` must tag the
// zero-parameter overload even though it carries no `param_types` at
// all to check evidence against (there being no parameters IS the
// evidence: arity alone already proves acceptance).
// =====================================================================

const ZERO_PARAM_OVERLOAD_SOURCE: &str = r#"package com.example.app29;

public class Sink29 {
    void m() { }
    void m(int a) { }
}

class Caller29 {
    Sink29 sink = new Sink29();
    void run() {
        sink.m();
    }
}
"#;

#[test]
fn zero_parameter_overload_carries_the_tag_on_a_zero_argument_call() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app29/Sink29.java", ZERO_PARAM_OVERLOAD_SOURCE);
    let index = extract_index(dir.path(), "com/example/app29/Sink29.java");
    let caller = declaration_symbol_owned_by(&index, "run", "Caller29");
    let m_zero = overload_symbol(&index, "m", "Sink29", &[]);
    let m_int = overload_symbol(&index, "m", "Sink29", &["int"]);
    let graph = build_graph_over(dir.path(), &["com/example/app29/Sink29.java"]);

    let zero_bits = edge_bits(&graph, caller, m_zero).expect("Sink29.m() edge must exist");
    assert_ne!(zero_bits & OVERLOAD_ARG_TYPE_MATCH, 0, "a zero-argument call must tag the zero-parameter overload");

    assert!(
        edge_bits(&graph, caller, m_int).map(|b| b & OVERLOAD_ARG_TYPE_MATCH == 0).unwrap_or(true),
        "a zero-argument call must never tag the one-parameter overload"
    );
}

// =====================================================================
// X07. TWO cast/typed arguments: `t((int) v, s)` (`s` a `String`) called
// against `t(int a, Integer b)` and `t(long a, String b)` -- per-position
// bare-name matching alone would prefer `t(int, Integer)` (its first
// parameter's bare name exactly matches the cast's bare name), even
// though `t(long, String)` is the real, fully call-compatible target
// (real javac resolves the call there). A per-position match on ONE
// argument must never outrank a candidate across the WHOLE call; both
// stay live and reachable regardless of which one(s) end up tagged.
// =====================================================================

const TWO_ARG_CAST_SOURCE: &str = r#"package com.example.appx07;

public class X07 {
    private void t(int a, Integer b) { }
    private void t(long a, String b) { }

    void call(int v, String s) {
        t((int) v, s);
    }
}
"#;

#[test]
fn two_arg_call_with_one_cast_keeps_both_overloads_live_when_second_arg_disambiguates() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/appx07/X07.java", TWO_ARG_CAST_SOURCE);
    let index = extract_index(dir.path(), "com/example/appx07/X07.java");
    let graph = build_graph_over(dir.path(), &["com/example/appx07/X07.java"]);
    assert_alive(&graph, overload_symbol(&index, "t", "X07", &["int", "Integer"]), "X07.t(int, Integer)");
    assert_alive(&graph, overload_symbol(&index, "t", "X07", &["long", "String"]), "X07.t(long, String)");
}

// =====================================================================
// X08. `R1.t(long)` (private) called as `t((int) v)` from within `R1`;
// an unrelated public `R2.t(int)` shares the bare name `t` with the same
// arity. A bare-name EXACT match against `R2.t(int)`'s parameter must
// never exclude `R1.t(long)` from the candidate pool -- it is the
// genuinely reachable target the call actually reaches.
// =====================================================================

const SINGLE_ARG_CAST_UNRELATED_CLASS_SOURCE: &str = r#"package com.example.appx08;

class R1 {
    private void t(long a) { }
    void call(int v) {
        t((int) v);
    }
}

class R2 {
    public void t(int a) { }
}
"#;

#[test]
fn single_arg_cast_never_falsely_excludes_an_unrelated_overload_in_a_different_class() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/appx08/R1.java", SINGLE_ARG_CAST_UNRELATED_CLASS_SOURCE);
    let index = extract_index(dir.path(), "com/example/appx08/R1.java");
    let graph = build_graph_over(dir.path(), &["com/example/appx08/R1.java"]);
    assert_alive(&graph, overload_symbol(&index, "t", "R1", &["long"]), "R1.t(long)");
}

// =====================================================================
// X10. `X10.t(CharSequence)` (private) called `t((String) o)` from
// within `X10`; an unrelated public static `Other10.t(String)` shares
// the bare name `t` with the same arity. A bare-name EXACT match against
// `Other10.t(String)`'s parameter must never exclude
// `X10.t(CharSequence)` from the candidate pool -- it is the genuinely
// reachable target the call actually reaches (`String` is assignable to
// `CharSequence` by subtyping).
// =====================================================================

const SUBTYPE_CAST_UNRELATED_CLASS_SOURCE: &str = r#"package com.example.appx10;

class X10 {
    private void t(CharSequence c) { }
    void call(Object o) {
        t((String) o);
    }
}

class Other10 {
    public static void t(String s) { }
}
"#;

#[test]
fn subtype_compatible_cast_never_falsely_excludes_the_reachable_private_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/appx10/X10.java", SUBTYPE_CAST_UNRELATED_CLASS_SOURCE);
    let index = extract_index(dir.path(), "com/example/appx10/X10.java");
    let graph = build_graph_over(dir.path(), &["com/example/appx10/X10.java"]);
    assert_alive(&graph, overload_symbol(&index, "t", "X10", &["CharSequence"]), "X10.t(CharSequence)");
}
