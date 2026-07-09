/// Integration tests for xray-core.
/// These tests cover OwnedNode construction, helpers, language detection,
/// Finding struct, and the built-in evaluators.
use std::io::Write;
use std::sync::Arc;
use tempfile::TempDir;
use xray_core::owned_node::OwnedNode;
use xray_core::scanner::parse_file;

// ---------------------------------------------------------------------------
// C and C++ grammar parse tests (Chunk A of Story #1077)
// ---------------------------------------------------------------------------

/// Write a temp file and return its path.
fn write_temp(dir: &TempDir, name: &str, content: &str) -> std::path::PathBuf {
    let path = dir.path().join(name);
    let mut f = std::fs::File::create(&path).unwrap();
    f.write_all(content.as_bytes()).unwrap();
    path
}

/// Recursively collect all node kinds present in the tree (DFS, unnamed included).
fn collect_kinds(node: &OwnedNode, out: &mut Vec<String>) {
    out.push(node.kind.clone());
    for child in &node.children {
        collect_kinds(child, out);
    }
}

/// Returns true if any node in the tree has kind == "ERROR".
fn has_error_node(node: &OwnedNode) -> bool {
    if node.kind == "ERROR" {
        return true;
    }
    node.children.iter().any(has_error_node)
}

// --- C parse tests ---

#[test]
fn test_c_parse_no_error() {
    // Parse a representative C file and assert no ERROR nodes in the tree.
    let dir = TempDir::new().unwrap();
    let src = r#"
#include <stdio.h>

/* A simple C file for grammar verification */

struct Point {
    int x;
    int y;
};

// Single-line comment
static int add(int a, int b) {
    if (a > 0) {
        return a + b;
    }
    for (int i = 0; i < 10; i++) {
        a += i;
    }
    while (a < 100) {
        a *= 2;
    }
    return a + b;
}

int main(void) {
    const char *msg = "hello, world";
    struct Point p = {1, 2};
    int result = add(p.x, p.y);
    printf("%s %d\n", msg, result);
    return 0;
}
"#;
    let path = write_temp(&dir, "sample.c", src);
    let root = parse_file(&path).expect("C parse_file must succeed");
    assert!(
        !has_error_node(&root),
        "C parse tree must have no ERROR nodes"
    );
}

#[test]
fn test_c_node_kinds() {
    // Parse C code and verify the exact node-kind names for key constructs.
    // These names are authoritative for playbook documentation.
    let dir = TempDir::new().unwrap();
    let src = r#"
/* C kinds verification file */
// Single-line comment

struct Point {
    int x;
    int y;
};

int add(int a, int b) {
    if (a > 0) {
        for (int i = 0; i < 10; i++) {}
        while (a > 0) { a--; }
    }
    return a + b;
}

int main(void) {
    const char *msg = "hello";
    int r = add(1, 2);
    return 0;
}
"#;
    let path = write_temp(&dir, "kinds.c", src);
    let root = parse_file(&path).expect("C parse must succeed");
    assert!(!has_error_node(&root), "C parse must have no ERROR nodes");

    let mut kinds = Vec::new();
    collect_kinds(&root, &mut kinds);

    // Root node for C is "translation_unit"
    assert_eq!(root.kind, "translation_unit",
        "C root node must be 'translation_unit', got '{}'", root.kind);

    // Function definition
    assert!(kinds.iter().any(|k| k == "function_definition"),
        "C must have 'function_definition' node; found: {:?}",
        kinds.iter().filter(|k| k.contains("func") || k.contains("decl")).collect::<Vec<_>>());

    // Struct
    assert!(kinds.iter().any(|k| k == "struct_specifier"),
        "C must have 'struct_specifier' node; found: {:?}",
        kinds.iter().filter(|k| k.contains("struct")).collect::<Vec<_>>());

    // If statement
    assert!(kinds.iter().any(|k| k == "if_statement"),
        "C must have 'if_statement' node");

    // For statement
    assert!(kinds.iter().any(|k| k == "for_statement"),
        "C must have 'for_statement' node");

    // While statement
    assert!(kinds.iter().any(|k| k == "while_statement"),
        "C must have 'while_statement' node");

    // String literal
    assert!(kinds.iter().any(|k| k == "string_literal"),
        "C must have 'string_literal' node; found: {:?}",
        kinds.iter().filter(|k| k.contains("string")).collect::<Vec<_>>());

    // Function call (call_expression)
    assert!(kinds.iter().any(|k| k == "call_expression"),
        "C must have 'call_expression' node; found: {:?}",
        kinds.iter().filter(|k| k.contains("call") || k.contains("expr")).collect::<Vec<_>>());

    // Comment
    assert!(kinds.iter().any(|k| k == "comment"),
        "C must have 'comment' node (need // or /* */ comment in source)");
}

// --- C++ parse tests ---

#[test]
fn test_cpp_parse_no_error() {
    // Parse a representative C++ file and assert no ERROR nodes in the tree.
    let dir = TempDir::new().unwrap();
    let src = r#"
#include <string>
#include <stdexcept>

/* C++ grammar verification file */

namespace geometry {

template<typename T>
class Point {
public:
    T x;
    T y;

    // Constructor
    Point(T x, T y) : x(x), y(y) {}

    T sum() const {
        return x + y;
    }
};

} // namespace geometry

int compute(int a, int b) {
    if (a > 0) {
        for (int i = 0; i < 10; i++) {
            a += i;
        }
        while (a < 100) {
            a *= 2;
        }
    }
    try {
        if (b == 0) throw std::runtime_error("zero");
        return a / b;
    } catch (const std::exception& e) {
        return -1;
    }
}

int main() {
    const std::string msg = "hello, c++";
    geometry::Point<int> p(1, 2);
    int result = compute(p.sum(), 2);
    return result;
}
"#;
    let path = write_temp(&dir, "sample.cpp", src);
    let root = parse_file(&path).expect("C++ parse_file must succeed");
    assert!(
        !has_error_node(&root),
        "C++ parse tree must have no ERROR nodes"
    );
}

#[test]
fn test_cpp_node_kinds() {
    // Parse C++ code and verify the exact node-kind names for key constructs.
    let dir = TempDir::new().unwrap();
    let src = r#"
namespace geo {

template<typename T>
class Point {
public:
    T x;
    Point(T x) : x(x) {}
    T get() const { return x; }
};

} // namespace geo

int divide(int a, int b) {
    if (a > 0) {
        for (int i = 0; i < 5; i++) {}
        while (a > 1) { a--; }
    }
    try {
        if (b == 0) throw std::runtime_error("zero");
        return a / b;
    } catch (const std::exception& e) {
        return -1;
    }
}

int main() {
    const char* msg = "hello";
    geo::Point<int> p(3);
    int r = divide(p.get(), 2);
    return 0;
}
"#;
    let path = write_temp(&dir, "kinds.cpp", src);
    let root = parse_file(&path).expect("C++ parse must succeed");
    assert!(!has_error_node(&root), "C++ parse must have no ERROR nodes");

    let mut kinds = Vec::new();
    collect_kinds(&root, &mut kinds);

    // Root node for C++ is "translation_unit"
    assert_eq!(root.kind, "translation_unit",
        "C++ root node must be 'translation_unit', got '{}'", root.kind);

    // Function definition
    assert!(kinds.iter().any(|k| k == "function_definition"),
        "C++ must have 'function_definition' node");

    // Class specifier
    assert!(kinds.iter().any(|k| k == "class_specifier"),
        "C++ must have 'class_specifier' node; found: {:?}",
        kinds.iter().filter(|k| k.contains("class")).collect::<Vec<_>>());

    // Namespace definition
    assert!(kinds.iter().any(|k| k == "namespace_definition"),
        "C++ must have 'namespace_definition' node; found: {:?}",
        kinds.iter().filter(|k| k.contains("namespace")).collect::<Vec<_>>());

    // Template declaration
    assert!(kinds.iter().any(|k| k == "template_declaration"),
        "C++ must have 'template_declaration' node; found: {:?}",
        kinds.iter().filter(|k| k.contains("template")).collect::<Vec<_>>());

    // If statement
    assert!(kinds.iter().any(|k| k == "if_statement"),
        "C++ must have 'if_statement' node");

    // For statement
    assert!(kinds.iter().any(|k| k == "for_statement"),
        "C++ must have 'for_statement' node");

    // While statement
    assert!(kinds.iter().any(|k| k == "while_statement"),
        "C++ must have 'while_statement' node");

    // Try statement
    assert!(kinds.iter().any(|k| k == "try_statement"),
        "C++ must have 'try_statement' node; found: {:?}",
        kinds.iter().filter(|k| k.contains("try") || k.contains("catch")).collect::<Vec<_>>());

    // Catch clause
    assert!(kinds.iter().any(|k| k == "catch_clause"),
        "C++ must have 'catch_clause' node; found: {:?}",
        kinds.iter().filter(|k| k.contains("catch")).collect::<Vec<_>>());

    // String literal
    assert!(kinds.iter().any(|k| k == "string_literal"),
        "C++ must have 'string_literal' node");

    // Function call (call_expression)
    assert!(kinds.iter().any(|k| k == "call_expression"),
        "C++ must have 'call_expression' node");

    // Comment
    assert!(kinds.iter().any(|k| k == "comment"),
        "C++ must have 'comment' node");
}

// ---------------------------------------------------------------------------
// OwnedNode unit-level tests
// ---------------------------------------------------------------------------

fn make_leaf(kind: &str, text: &str, start_line: usize, is_named: bool) -> OwnedNode {
    let source: Arc<str> = Arc::from(text);
    OwnedNode {
        kind: kind.to_string(),
        start_line,
        start_byte: 0,
        end_byte: text.len(),
        children: vec![],
        is_named,
        source,
    }
}

fn make_node(kind: &str, children: Vec<OwnedNode>) -> OwnedNode {
    let source: Arc<str> = Arc::from("");
    OwnedNode {
        kind: kind.to_string(),
        start_line: 1,
        start_byte: 0,
        end_byte: 0,
        children,
        is_named: true,
        source,
    }
}

#[test]
fn test_owned_node_construction() {
    let node = make_leaf("identifier", "foo", 5, true);
    assert_eq!(node.kind, "identifier");
    assert_eq!(node.text(), "foo");
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
    assert_eq!(named[0].text(), "x");
    assert_eq!(named[1].text(), "y");
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
    assert_eq!(found.unwrap().text(), "first");
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
