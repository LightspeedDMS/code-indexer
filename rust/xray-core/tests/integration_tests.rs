/// Integration tests for xray-core.
/// These tests cover OwnedNode construction, helpers, language detection,
/// Finding struct, and the built-in evaluators.
use xray_core::owned_node::OwnedNode;

// ---------------------------------------------------------------------------
// OwnedNode unit-level tests
// ---------------------------------------------------------------------------

fn make_leaf(kind: &str, text: &str, start_line: usize, is_named: bool) -> OwnedNode {
    OwnedNode {
        kind: kind.to_string(),
        start_line,
        start_byte: 0,
        end_byte: text.len(),
        children: vec![],
        is_named,
        text: text.to_string(),
    }
}

fn make_node(kind: &str, children: Vec<OwnedNode>) -> OwnedNode {
    OwnedNode {
        kind: kind.to_string(),
        start_line: 1,
        start_byte: 0,
        end_byte: 100,
        children,
        is_named: true,
        text: String::new(),
    }
}

#[test]
fn test_owned_node_construction() {
    let node = make_leaf("identifier", "foo", 5, true);
    assert_eq!(node.kind, "identifier");
    assert_eq!(node.text, "foo");
    assert_eq!(node.start_line, 5);
    assert!(node.is_named);
    assert!(node.children.is_empty());
}

#[test]
fn test_named_children_filters_unnamed() {
    let parent = make_node(
        "block",
        vec![
            make_leaf("{", "{", 1, false),  // anonymous punctuation
            make_leaf("identifier", "x", 2, true),
            make_leaf(";", ";", 2, false),  // anonymous punctuation
            make_leaf("identifier", "y", 3, true),
        ],
    );
    let named = parent.named_children();
    assert_eq!(named.len(), 2);
    assert_eq!(named[0].text, "x");
    assert_eq!(named[1].text, "y");
}

#[test]
fn test_named_children_all_named() {
    let parent = make_node(
        "block",
        vec![
            make_leaf("stmt", "a", 1, true),
            make_leaf("stmt", "b", 2, true),
        ],
    );
    let named = parent.named_children();
    assert_eq!(named.len(), 2);
}

#[test]
fn test_named_children_empty() {
    let leaf = make_leaf("identifier", "x", 1, true);
    assert!(leaf.named_children().is_empty());
}

#[test]
fn test_child_by_kind_found() {
    let parent = make_node(
        "try_statement",
        vec![
            make_node("block", vec![]),
            make_node("finally_clause", vec![]),
        ],
    );
    let found = parent.child_by_kind("finally_clause");
    assert!(found.is_some());
    assert_eq!(found.unwrap().kind, "finally_clause");
}

#[test]
fn test_child_by_kind_not_found() {
    let parent = make_node(
        "try_statement",
        vec![make_node("block", vec![])],
    );
    assert!(parent.child_by_kind("catch_clause").is_none());
}

#[test]
fn test_child_by_kind_returns_first() {
    let parent = make_node(
        "root",
        vec![
            make_leaf("identifier", "first", 1, true),
            make_leaf("identifier", "second", 2, true),
        ],
    );
    let found = parent.child_by_kind("identifier");
    assert_eq!(found.unwrap().text, "first");
}

#[test]
fn test_has_descendant_of_kind_direct_child() {
    let parent = make_node(
        "local_variable_declaration",
        vec![make_node("object_creation_expression", vec![])],
    );
    assert!(parent.has_descendant_of_kind("object_creation_expression"));
}

#[test]
fn test_has_descendant_of_kind_nested() {
    // object_creation_expression is 2 levels deep
    let inner = make_node("object_creation_expression", vec![]);
    let mid = make_node("variable_declarator", vec![inner]);
    let parent = make_node("local_variable_declaration", vec![mid]);
    assert!(parent.has_descendant_of_kind("object_creation_expression"));
}

#[test]
fn test_has_descendant_of_kind_absent() {
    let parent = make_node(
        "local_variable_declaration",
        vec![make_node("literal", vec![])],
    );
    assert!(!parent.has_descendant_of_kind("object_creation_expression"));
}

#[test]
fn test_has_descendant_of_kind_empty_children() {
    let leaf = make_leaf("identifier", "x", 1, true);
    assert!(!leaf.has_descendant_of_kind("anything"));
}
