//! Issue #1923: named-type (identifier/`this`) argument evidence is
//! TAG-ONLY. It decides whether `OVERLOAD_ARG_TYPE_MATCH` is set, and
//! can NEVER remove a candidate from the pool. Candidate-set EXCLUSION
//! is driven exclusively by the LITERAL-shape check.
//!
//! 1. `Target.select(cssQuery, this)`. All three overloads stay bound,
//!    and `select(Evaluator, Node)` never carries `OVERLOAD_ARG_TYPE_
//!    MATCH` (a `String` argument is closed-world provably not an
//!    `Evaluator`). `select(String, Iterable)` DOES also carry the tag
//!    alongside the real target `select(String, Node)`: no repo-
//!    supertype-chain mechanism is used for ANY named class/interface
//!    pair (a bare-name collision with an unrelated repo type would make
//!    a real external interface implementation invisible), so `Node` is
//!    never proven NOT `Iterable`.
//! 2. A `String[]` argument against `matchAny(String...)`/`matchAny(
//!    char...)` sibling overloads on different classes -- only the
//!    `String...` overload may carry the tag, since `char` and `String`
//!    share no closed-world relationship; both stay bound.

mod common;

use common::{build_graph_over, declaration_symbol_owned_by, write_source};
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::SymbolId;
use xray_core::graph::reasons::OVERLOAD_ARG_TYPE_MATCH;

/// Finds a `select` declaration whose OWN `param_types` equal
/// `param_types` exactly -- disambiguates `Target`'s three overloads
/// (and excludes the calling `Node.select(String)`, which never matches
/// a 2-element query).
fn overload_by_params(index: &LocalIndex, param_types: &[&str]) -> SymbolId {
    let wanted: Vec<String> = param_types.iter().map(|s| s.to_string()).collect();
    index
        .declarations
        .iter()
        .find(|d| d.name == "select" && d.param_types == wanted)
        .unwrap_or_else(|| panic!("fixture bug: no select{param_types:?} declaration"))
        .symbol
}

/// Asserts the `from -> to` edge exists (liveness -- named-type evidence
/// must never remove a candidate) AND that its `OVERLOAD_ARG_TYPE_MATCH`
/// bit matches `expect_tagged` (tag accuracy).
fn assert_tag(
    graph: &xray_core::graph::csr::CodeGraph,
    from: SymbolId,
    to: SymbolId,
    expect_tagged: bool,
    label: &str,
) {
    let from_dense = graph.dense_id_for(from).expect("caller must be interned");
    let to_dense = graph.dense_id_for(to).expect("callee must be interned");
    let bits = graph
        .edge_evidence(from_dense, to_dense)
        .unwrap_or_else(|| panic!("{label} edge must exist (named-type evidence is tag-only)"));
    assert_eq!(
        bits & OVERLOAD_ARG_TYPE_MATCH != 0,
        expect_tagged,
        "{label}: expected OVERLOAD_ARG_TYPE_MATCH tagged={expect_tagged}"
    );
}

// =====================================================================
// `Target.select(cssQuery, this)`. `Iterable` is deliberately RAW (no
// type argument) -- it mirrors the real bug report's own minimal repro
// shape and keeps the fixture focused on the arg-type discrimination
// under test, not generics.
// =====================================================================

const SELECT_SOURCE: &str = r#"package com.example.app;

public class Target {
    public static Object select(String query, Iterable scope) { return null; }
    public static Object select(Evaluator eval, Node root) { return null; }
    public static Object select(String query, Node root) { return null; }
}

interface Evaluator {
}

class Node {
    public Object select(String cssQuery) {
        return Target.select(cssQuery, this);
    }
}
"#;

#[test]
fn type_qualified_call_tags_overload_arg_type_match_only_where_provably_compatible() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Target.java", SELECT_SOURCE);

    let index = common::extract_index(dir.path(), "com/example/app/Target.java");
    let caller = declaration_symbol_owned_by(&index, "select", "Node");
    let string_node = overload_by_params(&index, &["String", "Node"]);
    let string_iterable = overload_by_params(&index, &["String", "Iterable"]);
    let evaluator_node = overload_by_params(&index, &["Evaluator", "Node"]);

    let graph = build_graph_over(dir.path(), &["com/example/app/Target.java"]);

    assert_tag(&graph, caller, string_node, true, "select(String, Node)");
    assert_tag(&graph, caller, string_iterable, true, "select(String, Iterable)");
    assert_tag(&graph, caller, evaluator_node, false, "select(Evaluator, Node)");
}

// =====================================================================
// A `String[]` field argument against two UNQUALIFIED same-name
// overloads on DIFFERENT classes -- `ExampleScanner.matchAny(String...)`
// and `ExampleReader.matchAny(char...)`. The bare, unqualified call
// `matchAny(EXAMPLE_VALUES)` reaches BOTH via this binder's own
// repo-wide bare-name candidate pool (`RepoNameIndex::lookup` matches by
// name+arity across the whole repo, independent of real Java lexical
// scoping); this is not real-javac-resolvable ambiguity, it is a
// bare-name-pool collision by construction. Only the `String...`
// overload may carry `OVERLOAD_ARG_TYPE_MATCH`; BOTH must stay bound
// (liveness).
// =====================================================================

const VARARGS_ARRAY_SOURCE: &str = r#"package com.example.app;

public class ExampleScanner {
    private static final String[] EXAMPLE_VALUES = {"a", "b"};

    public boolean scan() {
        return matchAny(EXAMPLE_VALUES);
    }

    public boolean matchAny(String... seq) { return true; }
}

class ExampleReader {
    public boolean matchAny(char... chars) { return true; }
}
"#;

#[test]
fn string_array_argument_tags_only_the_string_varargs_overload() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/ExampleScanner.java", VARARGS_ARRAY_SOURCE);

    let index = common::extract_index(dir.path(), "com/example/app/ExampleScanner.java");
    let caller = declaration_symbol_owned_by(&index, "scan", "ExampleScanner");
    let string_varargs = declaration_symbol_owned_by(&index, "matchAny", "ExampleScanner");
    let char_varargs = declaration_symbol_owned_by(&index, "matchAny", "ExampleReader");

    let graph = build_graph_over(dir.path(), &["com/example/app/ExampleScanner.java"]);

    assert_tag(&graph, caller, string_varargs, true, "matchAny(String...)");
    assert_tag(&graph, caller, char_varargs, false, "matchAny(char...)");
}
