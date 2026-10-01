//! Issue #1930: `JavaExtractor::extract_method_
//! declaration`/`KotlinExtractor::extract_function_declaration` allocate a
//! symbol for a malformed/nameless declaration (parse-error recovery)
//! BEFORE looking up its name, and return that symbol as `WalkContext::
//! enclosing_method` for its children WITHOUT ever pushing a matching
//! `Declaration` when the name lookup fails. `bind::resolve::
//! enclosing_symbol_for_site` used to treat that shape as "should be
//! impossible" (a symbol matching neither a `Declaration` nor a recorded
//! `SyntheticScopeRecord`), reachable through this exact path -- a debug
//! build would panic via `debug_assert!` on legitimate malformed input
//! (a user's WIP file, a truncated save). The fix registers this
//! parse-recovery symbol as a `SyntheticScopeRecord` too (the SAME
//! mechanism a static/instance initializer block already uses), carrying
//! its real enclosing type, so the "impossible" branch stays genuinely
//! unreachable and a call inside the malformed declaration attributes to
//! that real enclosing type.
//!
//! **Why these fixtures build their own `OwnedNode` tree** (`OwnedNode::
//! new_node_for_test`/`new_leaf_for_test`, the crate's own sanctioned
//! test-construction API, gated on the `test-support` feature) rather
//! than writing malformed `.java`/`.kt` source to disk like every sibling
//! `bug_1930_*.rs` file: `child_by_kind("identifier")` returning `None`
//! requires the parsed tree to have NO identifier child at all, but real
//! tree-sitter-java 0.23.5 / tree-sitter-kotlin-ng error recovery was
//! probed against roughly twenty distinct malformed shapes (missing
//! name, missing type, missing modifiers, generics, `throws`, `native`,
//! interface defaults, truncated input, ...) and EVERY one that a parser
//! actually classifies as `method_declaration`/`function_declaration`
//! synthesizes a MISSING placeholder `identifier` child (present, empty
//! text) rather than omitting the field outright -- any shape lacking
//! enough context instead parses as a bare `ERROR` node, which never
//! reaches `dispatch_method_declaration`/`dispatch_function_declaration`
//! at all. The code path this test defends is real (and now provably
//! non-panicking), even though no real source text this grammar version
//! parses reaches it -- these fixtures call the REAL `JavaExtractor`/
//! `KotlinExtractor::extract` production code directly on a hand-built
//! tree shaped exactly like the one grammar would need to produce.
//!
//! Neutral naming (`Outer`/`target`), per this repository's Disclosure
//! Discipline.

use xray_core::graph::bind::{bind, FileForBind};
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::kotlin::KotlinExtractor;
use xray_core::graph::extract::local_index::DeclarationKind;
use xray_core::graph::extract::LanguageExtractor;
use xray_core::owned_node::OwnedNode;

fn leaf(kind: &str, text: &str, line: usize, is_named: bool) -> OwnedNode {
    OwnedNode::new_leaf_for_test(kind, text, line, is_named)
}

/// An interior (non-leaf) node -- always `is_named: true` here since
/// every interior shape these fixtures build is a real grammar
/// production, never anonymous punctuation.
fn interior(kind: &str, text: &str, line: usize, children: Vec<OwnedNode>) -> OwnedNode {
    OwnedNode::new_node_for_test(kind, text, line, 0, text.len(), children, true)
}

/// Wraps `malformed_member` and `target_member` (each an already-built
/// method/function declaration node) inside `class Outer { ... }`,
/// itself wrapped in a file-root node -- shared by both languages, which
/// differ only in the root node's own kind (`program` for Java,
/// `source_file` for Kotlin).
fn wrap_in_class(root_kind: &str, malformed_member: OwnedNode, target_member: OwnedNode) -> OwnedNode {
    let class_body = interior(
        "class_body",
        "{ ... }",
        4,
        vec![leaf("{", "{", 4, false), malformed_member, target_member, leaf("}", "}", 9, false)],
    );
    let class_decl = interior(
        "class_declaration",
        "class Outer { ... }",
        4,
        vec![leaf("class", "class", 4, false), leaf("identifier", "Outer", 4, true), class_body],
    );
    interior(root_kind, "class Outer { ... }", 1, vec![class_decl])
}

/// Shared execution/assertion flow both language fixtures below drive
/// identically: run the REAL production `extractor` over `root` (a
/// hand-built tree containing a malformed declaration with NO
/// `identifier` child at line 5, followed by a real `target()` at line
/// 8), then `bind()` the result -- must not panic in a debug build --
/// and assert `target()`'s only caller is Outer, its lexically enclosing
/// type.
fn assert_malformed_declaration_attributes_to_the_enclosing_type(
    extractor: &dyn LanguageExtractor,
    root: OwnedNode,
    language: &str,
) {
    let index = extractor.extract(&root, 1);

    assert!(
        !index
            .declarations
            .iter()
            .any(|d| d.kind == DeclarationKind::Method && d.line == 5),
        "the malformed declaration must get NO Declaration -- never fabricated"
    );
    let outer_symbol = index
        .declarations
        .iter()
        .find(|d| d.name == "Outer" && d.kind == DeclarationKind::Type)
        .expect("fixture bug: Outer's own type declaration must exist")
        .symbol;
    let target_symbol = index
        .declarations
        .iter()
        .find(|d| d.name == "target" && d.kind == DeclarationKind::Method)
        .expect("fixture bug: target's own declaration must exist")
        .symbol;

    assert_eq!(
        index.synthetic_scopes.len(),
        1,
        "the malformed declaration's parse-recovery symbol must be registered as a synthetic scope"
    );
    assert_eq!(
        index.synthetic_scopes[0].enclosing_type_symbol,
        Some(outer_symbol),
        "the registered synthetic scope must carry Outer as its enclosing type"
    );

    let files = vec![FileForBind {
        file_id: 1,
        language: language.to_string(),
        index,
    }];
    // `bind()` must not panic (the debug_assert this test defends against
    // lives inside `enclosing_symbol_for_site`, called from here).
    let graph = bind(files);

    let target_dense = graph.dense_id_for(target_symbol).expect("target must be interned");
    let outer_dense = graph.dense_id_for(outer_symbol).expect("Outer must be interned");
    let target_callers = graph.callers_index(target_dense);
    assert_eq!(
        target_callers,
        &[outer_dense],
        "target(), called inside the malformed declaration, must be attributed to Outer -- \
         its real lexically enclosing type -- got {target_callers:?}"
    );
}

// =====================================================================
// Java: a `method_declaration` with NO `identifier` child at all --
// `void () { target(); }`, the exact shape `child_by_kind("identifier")
// == None` requires, followed by a real, named `target()` method.
// =====================================================================

fn java_malformed_method() -> OwnedNode {
    let invocation = interior(
        "method_invocation",
        "target()",
        6,
        vec![
            leaf("identifier", "target", 6, true),
            interior("argument_list", "()", 6, vec![leaf("(", "(", 6, false), leaf(")", ")", 6, false)]),
        ],
    );
    let expr_stmt = interior(
        "expression_statement",
        "target();",
        6,
        vec![invocation, leaf(";", ";", 6, false)],
    );
    let block = interior(
        "block",
        "{ target(); }",
        5,
        vec![leaf("{", "{", 5, false), expr_stmt, leaf("}", "}", 7, false)],
    );
    let params = interior("formal_parameters", "()", 5, vec![leaf("(", "(", 5, false), leaf(")", ")", 5, false)]);
    // No `identifier` child anywhere in this method_declaration.
    interior(
        "method_declaration",
        "void () { target(); }",
        5,
        vec![leaf("void_type", "void", 5, true), params, block],
    )
}

fn java_target_method() -> OwnedNode {
    let params = interior("formal_parameters", "()", 8, vec![leaf("(", "(", 8, false), leaf(")", ")", 8, false)]);
    let block = interior("block", "{}", 8, vec![leaf("{", "{", 8, false), leaf("}", "}", 8, false)]);
    interior(
        "method_declaration",
        "void target() {}",
        8,
        vec![leaf("void_type", "void", 8, true), leaf("identifier", "target", 8, true), params, block],
    )
}

#[test]
fn java_malformed_nameless_method_declaration_does_not_panic_and_attributes_to_the_enclosing_type() {
    let root = wrap_in_class("program", java_malformed_method(), java_target_method());
    assert_malformed_declaration_attributes_to_the_enclosing_type(&JavaExtractor, root, "java");
}

// =====================================================================
// Kotlin: a `function_declaration` with NO `identifier` child --
// `fun () { target() }`, same shape, same rationale.
// =====================================================================

fn kotlin_malformed_function() -> OwnedNode {
    let call = interior(
        "call_expression",
        "target()",
        6,
        vec![
            leaf("identifier", "target", 6, true),
            interior("value_arguments", "()", 6, vec![leaf("(", "(", 6, false), leaf(")", ")", 6, false)]),
        ],
    );
    let block = interior(
        "block",
        "{ target() }",
        5,
        vec![leaf("{", "{", 5, false), call, leaf("}", "}", 7, false)],
    );
    let body = interior("function_body", "{ target() }", 5, vec![block]);
    let params = interior(
        "function_value_parameters",
        "()",
        5,
        vec![leaf("(", "(", 5, false), leaf(")", ")", 5, false)],
    );
    // No `identifier` child anywhere in this function_declaration.
    interior(
        "function_declaration",
        "fun () { target() }",
        5,
        vec![leaf("fun", "fun", 5, false), params, body],
    )
}

fn kotlin_target_function() -> OwnedNode {
    let params = interior(
        "function_value_parameters",
        "()",
        8,
        vec![leaf("(", "(", 8, false), leaf(")", ")", 8, false)],
    );
    let block = interior("block", "{}", 8, vec![leaf("{", "{", 8, false), leaf("}", "}", 8, false)]);
    let body = interior("function_body", "{}", 8, vec![block]);
    interior(
        "function_declaration",
        "fun target() {}",
        8,
        vec![leaf("fun", "fun", 8, false), leaf("identifier", "target", 8, true), params, body],
    )
}

#[test]
fn kotlin_malformed_nameless_function_declaration_does_not_panic_and_attributes_to_the_enclosing_type()
{
    let root = wrap_in_class("source_file", kotlin_malformed_function(), kotlin_target_function());
    assert_malformed_declaration_attributes_to_the_enclosing_type(&KotlinExtractor, root, "kotlin");
}
