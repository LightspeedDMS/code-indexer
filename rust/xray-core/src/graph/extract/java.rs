//! Java extraction (Story #1787, S2, AC2): the only language IMPLEMENTED
//! in this slice (the epic's primary validation corpus is
//! Keycloak/Elasticsearch, both Java). A single explicit-stack walk of the
//! parsed tree (mirrors `crate::owned_node`'s own iterative traversal style
//! -- Rule 14, anti-unbounded-loop, requires a provable termination bound
//! on every loop) that fully populates a `LocalIndex` before returning.
//! `crate::graph::fused` is what then calls `collect_facts` on the
//! completed index -- this module never calls into that seam itself.
//!
//! Node-kind names below were verified against the REAL tree-sitter-java
//! 0.23.5 grammar output (dumped from real parsed sample files covering
//! classes, interfaces, enums, constructors, class extends/implements,
//! interface extends (multiple), imports, static imports, wildcard
//! imports, annotations, qualified and bare method invocations, and object
//! creation), not guessed.

use super::local_index::{
    AnnotationRecord, ArgShape, ConstructionSite, Declaration, DeclarationKind, ImportKind, ImportRecord,
    InheritanceKind, InheritanceRecord, InvocationSite, LocalIndex, MethodOwnerRecord, TypeReferenceRecord,
};
use super::LanguageExtractor;
use crate::graph::identity::{make_symbol_id, SymbolId};
use crate::owned_node::OwnedNode;

pub struct JavaExtractor;

/// AC1 (Story #1793, S4): the bare name of a node's CURRENT immediately
/// enclosing type (`None` outside any type), threaded through `extract`'s
/// stack walk. `Rc<str>` rather than `String`: cloned on every child push,
/// and an `Rc` clone is a refcount bump, never a fresh heap allocation.
type TypeContext = Option<std::rc::Rc<str>>;

/// Dispatches ONE node to its extraction function (if any) and returns the
/// context its CHILDREN should see. A type declaration establishes a NEW
/// context (its own bare name) for its own children; every other node
/// kind simply inherits `enclosing_type` unchanged. This is why a nested
/// class's methods are attributed to the INNER type: the inner
/// `class_declaration` node overwrites the context before its own
/// children (including its methods) are pushed. Split out of `extract`
/// to keep that function under the per-function line budget.
fn dispatch_node(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: TypeContext,
    index: &mut LocalIndex,
) -> TypeContext {
    match node.kind.as_str() {
        "class_declaration" | "interface_declaration" | "enum_declaration" | "record_declaration" => {
            extract_type_declaration(node, file_id, next_local, index);
            node.child_by_kind("identifier").map(|n| std::rc::Rc::from(n.text()))
        }
        "method_declaration" | "constructor_declaration" => {
            extract_method_declaration(node, file_id, next_local, enclosing_type.as_deref(), index);
            enclosing_type
        }
        "field_declaration" => {
            extract_field_declaration(node, file_id, next_local, index);
            enclosing_type
        }
        "method_invocation" => {
            extract_invocation(node, index);
            enclosing_type
        }
        "object_creation_expression" => {
            extract_construction(node, index);
            enclosing_type
        }
        "type_identifier" => {
            extract_type_reference(node, index);
            enclosing_type
        }
        _ => enclosing_type,
    }
}

impl LanguageExtractor for JavaExtractor {
    fn extract(&self, root: &OwnedNode, file_id: u32) -> LocalIndex {
        let mut index = LocalIndex::new();
        let mut next_local: u32 = 0;

        extract_package(root, file_id, &mut next_local, &mut index);
        extract_imports(root, &mut index);

        let mut stack: Vec<(&OwnedNode, TypeContext)> = vec![(root, None)];
        // Bounded: each iteration pops one node from `stack` and pushes its
        // (finite) children; total pushes across the walk equal the tree's
        // finite node count -- the same bound `OwnedNode`'s own traversals
        // use (see owned_node.rs). This is the ONLY traversal of the tree
        // besides the two direct (non-recursive) top-level lookups above.
        while let Some((node, enclosing_type)) = stack.pop() {
            let child_context = dispatch_node(node, file_id, &mut next_local, enclosing_type, &mut index);
            stack.extend(node.children.iter().map(|c| (c, child_context.clone())));
        }

        index
    }
}

fn next_symbol(file_id: u32, next_local: &mut u32) -> SymbolId {
    let symbol = make_symbol_id(file_id, *next_local);
    *next_local += 1;
    symbol
}

/// Resolves a bare `type_identifier` OR a `generic_type` wrapping one
/// (e.g. `Base<String>`) to its base type name. Shared by every call site
/// that needs "the type name a node stands for" regardless of whether that
/// type carries generic arguments.
fn base_type_name(container: &OwnedNode) -> Option<String> {
    container
        .child_by_kind("type_identifier")
        .or_else(|| container.child_by_kind("generic_type").and_then(|g| g.child_by_kind("type_identifier")))
        .map(|t| t.text().to_string())
}

fn extract_package(root: &OwnedNode, file_id: u32, next_local: &mut u32, index: &mut LocalIndex) {
    let Some(decl) = root.child_by_kind("package_declaration") else { return };
    let Some(name_node) = decl
        .named_children()
        .into_iter()
        .find(|c| c.kind == "scoped_identifier" || c.kind == "identifier")
    else {
        return;
    };
    let name = name_node.text().to_string();
    let symbol = next_symbol(file_id, next_local);
    index.signatures.insert(symbol, format!("package {name}"));
    index.declarations.push(Declaration {
        kind: DeclarationKind::Package,
        name,
        line: decl.start_line,
        symbol,
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
    });
}

fn extract_imports(root: &OwnedNode, index: &mut LocalIndex) {
    for child in root.children.iter().filter(|c| c.kind == "import_declaration") {
        // Verified real grammar output: a wildcard import's `*` is wrapped
        // in a NAMED direct child of kind "asterisk"; the anonymous "*"
        // check is defensive in case a future grammar bump flattens that
        // wrapper node away.
        let is_wildcard =
            child.child_by_kind("asterisk").is_some() || child.child_by_kind("*").is_some();
        let is_static = child.child_by_kind("static").is_some();
        let kind = if is_wildcard {
            ImportKind::Wildcard
        } else if is_static {
            ImportKind::Static
        } else {
            ImportKind::Ordinary
        };
        let path = child
            .named_children()
            .into_iter()
            .find(|c| c.kind == "scoped_identifier" || c.kind == "identifier")
            .map(|n| n.text().to_string())
            .unwrap_or_default();
        index.imports.push(ImportRecord { kind, path, line: child.start_line });
    }
}

fn type_keyword(kind: &str) -> &'static str {
    match kind {
        "interface_declaration" => "interface",
        "enum_declaration" => "enum",
        "record_declaration" => "record",
        _ => "class",
    }
}

fn extract_type_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    index: &mut LocalIndex,
) {
    let Some(name_node) = node.child_by_kind("identifier") else { return };
    let name = name_node.text().to_string();
    let symbol = next_symbol(file_id, next_local);

    extract_annotations_from_modifiers(node, &name, index);
    extract_inheritance(node, &name, index);
    if node.kind == "interface_declaration" {
        index.interface_names.push(name.clone());
    }

    let signature = format!("{} {}", type_keyword(&node.kind), name);
    index.signatures.insert(symbol, signature);
    index.declarations.push(Declaration {
        kind: DeclarationKind::Type,
        name,
        line: node.start_line,
        symbol,
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
    });
}

/// Collects the type names out of a `type_list` node (the shared grammar
/// shape for BOTH `super_interfaces -> type_list` and
/// `extends_interfaces -> type_list`): each item is either a bare
/// `type_identifier` or a `generic_type` wrapping one as its base type.
fn type_names_in_type_list(type_list: &OwnedNode) -> Vec<String> {
    type_list
        .named_children()
        .into_iter()
        .filter_map(|child| match child.kind.as_str() {
            "type_identifier" => Some(child.text().to_string()),
            "generic_type" => child.child_by_kind("type_identifier").map(|t| t.text().to_string()),
            _ => None,
        })
        .collect()
}

/// Pushes one `InheritanceRecord` of `edge_kind` for every type name found
/// in `container_kind`'s own `type_list` child (used for both a class's
/// `super_interfaces` and an interface's `extends_interfaces`, which share
/// the identical `-> type_list -> type_identifier|generic_type` shape).
fn extract_type_list_edges(
    node: &OwnedNode,
    container_kind: &str,
    edge_kind: InheritanceKind,
    subtype_name: &str,
    index: &mut LocalIndex,
) {
    let Some(container) = node.child_by_kind(container_kind) else { return };
    let Some(type_list) = container.child_by_kind("type_list") else { return };
    for supertype_name in type_names_in_type_list(type_list) {
        index.inheritance.push(InheritanceRecord {
            kind: edge_kind,
            subtype_name: subtype_name.to_string(),
            supertype_name,
            line: container.start_line,
        });
    }
}

fn extract_inheritance(node: &OwnedNode, subtype_name: &str, index: &mut LocalIndex) {
    // Class: `class Foo extends Base` or `class Foo extends Base<String>`
    // -- single supertype, its OWN grammar shape (`superclass ->
    // type_identifier` OR `superclass -> generic_type -> type_identifier`
    // directly, no `type_list`).
    if let Some(superclass) = node.child_by_kind("superclass") {
        if let Some(supertype_name) = base_type_name(superclass) {
            index.inheritance.push(InheritanceRecord {
                kind: InheritanceKind::Extends,
                subtype_name: subtype_name.to_string(),
                supertype_name,
                line: superclass.start_line,
            });
        }
    }
    // Class: `class Foo implements A, B`.
    extract_type_list_edges(node, "super_interfaces", InheritanceKind::Implements, subtype_name, index);
    // Interface: `interface Foo extends A, B` -- unlike a class, an
    // interface can extend MULTIPLE other interfaces, hence its own
    // `extends_interfaces -> type_list` shape (verified via real grammar
    // dump), distinct from a class's single-supertype `superclass`.
    extract_type_list_edges(node, "extends_interfaces", InheritanceKind::Extends, subtype_name, index);
}

fn extract_annotations_from_modifiers(node: &OwnedNode, target_name: &str, index: &mut LocalIndex) {
    let Some(modifiers) = node.child_by_kind("modifiers") else { return };
    for annotation_node in
        modifiers.children.iter().filter(|c| c.kind == "marker_annotation" || c.kind == "annotation")
    {
        let Some(name_node) = annotation_node
            .named_children()
            .into_iter()
            .find(|c| c.kind == "identifier" || c.kind == "scoped_identifier")
        else {
            continue;
        };
        index.annotations.push(AnnotationRecord {
            name: name_node.text().to_string(),
            target_name: target_name.to_string(),
            line: annotation_node.start_line,
        });
    }
}

/// AC2 (Story #1793, S4): resolves a TYPE node's local-syntactic base name
/// -- generics stripped to the base `type_identifier`, exactly the same
/// stripping `base_type_name` above applies when searching a CONTAINER's
/// children, but here `type_node` has already been located by the caller
/// (it may itself be a primitive/array-type node, which has no children
/// to search and is returned verbatim via `.text()`).
fn base_name_of_type_node(type_node: &OwnedNode) -> String {
    if type_node.kind == "generic_type" {
        type_node.child_by_kind("type_identifier").map(|t| t.text().to_string()).unwrap_or_else(|| type_node.text().to_string())
    } else {
        type_node.text().to_string()
    }
}

/// Reads one `formal_parameter`/`spread_parameter` node's declared type.
/// Verified real tree-sitter-java 0.23.5 grammar shapes: `(formal_parameter
/// [modifiers]? type: (T) name: (identifier))` and `(spread_parameter
/// [modifiers]? (T) (variable_declarator name: (identifier)))` -- an
/// optional leading `modifiers` node (present for e.g. `final String s` or
/// `@NonNull int x`) is skipped explicitly rather than assumed absent, so
/// the type is "the first named child that isn't `modifiers`", never a
/// fixed position.
fn formal_parameter_type_name(param_node: &OwnedNode) -> Option<String> {
    let type_node = param_node.named_children().into_iter().find(|c| c.kind != "modifiers")?;
    Some(base_name_of_type_node(type_node))
}

/// AC2: declared parameter type names (in call order) and whether the
/// method's last parameter is variable-arity, read from its
/// `formal_parameters` node -- the SAME node `param_count` above already
/// reads, so this adds no second tree walk.
fn extract_param_types_and_varargs(formal_parameters: &OwnedNode) -> (Vec<String>, bool) {
    let mut param_types = Vec::new();
    let mut is_varargs = false;
    for param in formal_parameters.named_children() {
        match param.kind.as_str() {
            "formal_parameter" => param_types.extend(formal_parameter_type_name(param)),
            "spread_parameter" => {
                is_varargs = true;
                param_types.extend(formal_parameter_type_name(param));
            }
            _ => {}
        }
    }
    (param_types, is_varargs)
}

fn extract_method_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    index: &mut LocalIndex,
) {
    let Some(name_node) = node.child_by_kind("identifier") else { return };
    let name = name_node.text().to_string();
    let symbol = next_symbol(file_id, next_local);

    extract_annotations_from_modifiers(node, &name, index);

    let formal_parameters = node.child_by_kind("formal_parameters");
    let param_count = formal_parameters.map(|p| p.named_children().len()).unwrap_or(0);
    let (param_types, is_varargs) =
        formal_parameters.map(extract_param_types_and_varargs).unwrap_or_default();
    index.signatures.insert(symbol, format!("{name}({param_count} params)"));

    index.declarations.push(Declaration {
        kind: DeclarationKind::Method,
        name,
        line: node.start_line,
        symbol,
        param_count: Some(param_count),
        param_types,
        is_varargs,
    });

    // AC1 (Story #1793, S4): only real methods (this function is also
    // called for `constructor_declaration`, which has no useful "family"
    // semantics -- a constructor is never overridden the way an interface
    // method is) get an owner record, and only when an enclosing type is
    // actually known.
    if node.kind == "method_declaration" {
        if let Some(enclosing_type) = enclosing_type {
            index
                .method_owners
                .push(MethodOwnerRecord { method_symbol: symbol, enclosing_type: enclosing_type.to_string() });
        }
    }
}

fn has_modifier(modifiers: &OwnedNode, keyword: &str) -> bool {
    modifiers.children.iter().any(|c| c.kind == keyword)
}

fn field_declaration_kind(node: &OwnedNode) -> DeclarationKind {
    let is_constant = node
        .child_by_kind("modifiers")
        .map(|m| has_modifier(m, "static") && has_modifier(m, "final"))
        .unwrap_or(false);
    if is_constant { DeclarationKind::Constant } else { DeclarationKind::Field }
}

/// Reads `field_declaration`'s own `variable_declarator` children directly
/// (verified real grammar output: they are DIRECT children, one per
/// comma-separated variable in the declaration, never nested deeper) so
/// this stays within the single bounded stack-walk in `extract` above,
/// rather than opening a second, separate traversal per field.
fn extract_field_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    index: &mut LocalIndex,
) {
    let kind = field_declaration_kind(node);
    let keyword = if matches!(kind, DeclarationKind::Constant) { "constant" } else { "field" };

    for declarator in node.children.iter().filter(|c| c.kind == "variable_declarator") {
        let Some(name_node) = declarator.child_by_kind("identifier") else { continue };
        let name = name_node.text().to_string();
        let symbol = next_symbol(file_id, next_local);
        index.signatures.insert(symbol, format!("{keyword} {name}"));
        index.declarations.push(Declaration {
            kind,
            name,
            line: node.start_line,
            symbol,
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
        });
    }
}

/// AC2: an explicit cast's (`(Foo) x`) target type. `cast_expression`
/// never carries a leading `modifiers` node (verified real grammar
/// output for both a class cast and a primitive cast: `cast_expression
/// type: (T) value: (V))`), so its type is always the first named child,
/// unlike `formal_parameter`/`spread_parameter` above.
fn cast_target_type_name(cast_node: &OwnedNode) -> Option<String> {
    cast_node.named_children().into_iter().next().map(base_name_of_type_node)
}

/// AC2: the coarse `ArgShape` for one call-site argument node, verified
/// against real tree-sitter-java 0.23.5 output for every documented
/// category: string/boolean/null/numeric literals (`true`/`false` are
/// their own literal node kinds, not a shared `boolean_literal`), an
/// explicit cast, an inline constructor call, a lambda, and a method
/// reference. Any other argument shape (bare identifier, field access,
/// nested call, ...) -- OR a cast/constructor whose target type name
/// could not be determined -- carries no discriminating evidence at this
/// position: `Other`, never a fabricated empty-string `Cast`/`Constructor`
/// (Rule 2, anti-fallback).
fn arg_shape_for(arg_node: &OwnedNode) -> ArgShape {
    match arg_node.kind.as_str() {
        "string_literal" => ArgShape::StringLiteral,
        "true" | "false" => ArgShape::BooleanLiteral,
        "null_literal" => ArgShape::NullLiteral,
        "decimal_integer_literal" | "hex_integer_literal" | "octal_integer_literal"
        | "binary_integer_literal" | "decimal_floating_point_literal" | "hex_floating_point_literal" => {
            ArgShape::NumericLiteral
        }
        "cast_expression" => cast_target_type_name(arg_node).map(ArgShape::Cast).unwrap_or(ArgShape::Other),
        "object_creation_expression" => base_type_name(arg_node).map(ArgShape::Constructor).unwrap_or(ArgShape::Other),
        "lambda_expression" => ArgShape::Lambda,
        "method_reference" => ArgShape::MethodReference,
        _ => ArgShape::Other,
    }
}

fn extract_invocation(node: &OwnedNode, index: &mut LocalIndex) {
    let Some(callee) = node.named_children().into_iter().filter(|c| c.kind == "identifier").next_back()
    else {
        return;
    };
    // Verified real grammar output: `method_invocation`'s call arguments are
    // its own direct `argument_list` child (the same shape
    // `formal_parameters` has for a declaration's parameters above) --
    // still part of the ONE existing walk over this node, no new
    // traversal. `None` (never a fabricated `Some(0)`) if that child is
    // genuinely absent, e.g. under parse-error recovery on malformed source.
    let argument_list = node.child_by_kind("argument_list");
    let arg_count = argument_list.map(|a| a.named_children().len());
    let arg_shapes =
        argument_list.map(|a| a.named_children().into_iter().map(arg_shape_for).collect()).unwrap_or_default();
    index.invocations.push(InvocationSite {
        callee_name: callee.text().to_string(),
        line: node.start_line,
        arg_count,
        arg_shapes,
    });
}

fn extract_construction(node: &OwnedNode, index: &mut LocalIndex) {
    let Some(type_name) = base_type_name(node) else { return };
    index.constructions.push(ConstructionSite { type_name, line: node.start_line });
}

fn extract_type_reference(node: &OwnedNode, index: &mut LocalIndex) {
    index.type_references.push(TypeReferenceRecord { type_name: node.text().to_string(), line: node.start_line });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn extract_source(source: &str) -> LocalIndex {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("Sample.java");
        std::fs::write(&path, source).unwrap();
        let root = crate::scanner::parse_file(Path::new(&path)).unwrap();
        JavaExtractor.extract(&root, 7)
    }

    #[test]
    fn extracts_package_declaration_with_a_signature() {
        let index = extract_source("package com.example;\nclass Foo {}\n");
        let pkg = index.declaration_named("com.example").unwrap();
        assert_eq!(pkg.kind, DeclarationKind::Package);
        assert_eq!(index.signatures.get(&pkg.symbol).unwrap(), "package com.example");
    }

    #[test]
    fn extracts_ordinary_static_and_wildcard_imports() {
        let index = extract_source(
            "import java.util.List;\nimport static java.lang.Math.max;\nimport java.util.*;\nclass Foo {}\n",
        );
        assert_eq!(index.imports.len(), 3);
        assert_eq!(index.imports[0].kind, ImportKind::Ordinary);
        assert_eq!(index.imports[1].kind, ImportKind::Static);
        assert_eq!(index.imports[2].kind, ImportKind::Wildcard);
    }

    #[test]
    fn extracts_class_interface_and_enum_declarations() {
        let index = extract_source("interface Shape {}\nenum Color { RED }\nclass First {}\n");
        assert!(index.declaration_named("Shape").is_some());
        assert!(index.declaration_named("Color").is_some());
        assert!(index.declaration_named("First").is_some());
    }

    #[test]
    fn extracts_class_extends_and_implements_edges() {
        let index = extract_source("class First extends Base implements Runnable, Comparable {}\n");
        assert!(index.inheritance.iter().any(|i| i.kind == InheritanceKind::Extends
            && i.subtype_name == "First"
            && i.supertype_name == "Base"));
        assert!(index.inheritance.iter().any(|i| i.kind == InheritanceKind::Implements
            && i.supertype_name == "Runnable"));
        assert!(index.inheritance.iter().any(|i| i.kind == InheritanceKind::Implements
            && i.supertype_name == "Comparable"));
    }

    /// AC2 correctness gap fixed in review: `class Foo extends Base<String>`
    /// represents its supertype as `superclass -> generic_type ->
    /// type_identifier`, not a bare `type_identifier` directly.
    #[test]
    fn extracts_generic_superclass_as_extends_edge() {
        let index = extract_source("class First extends Base<String> {}\n");
        assert!(index.inheritance.iter().any(|i| i.kind == InheritanceKind::Extends
            && i.subtype_name == "First"
            && i.supertype_name == "Base"));
    }

    /// Interfaces can extend MULTIPLE other interfaces via their OWN
    /// `extends_interfaces` grammar node (distinct from a class's single
    /// `superclass`) -- both must produce `Extends` edges.
    #[test]
    fn extracts_interface_extends_multiple_interfaces_as_extends_edges() {
        let index = extract_source("interface Foo extends Bar, Baz {}\n");
        assert!(index.inheritance.iter().any(|i| i.kind == InheritanceKind::Extends
            && i.subtype_name == "Foo"
            && i.supertype_name == "Bar"));
        assert!(index.inheritance.iter().any(|i| i.kind == InheritanceKind::Extends
            && i.subtype_name == "Foo"
            && i.supertype_name == "Baz"));
    }

    #[test]
    fn extracts_annotations_on_types_and_methods() {
        let index = extract_source("@Deprecated\nclass First {\n    @Override\n    void run() {}\n}\n");
        assert!(index.annotations.iter().any(|a| a.name == "Deprecated" && a.target_name == "First"));
        assert!(index.annotations.iter().any(|a| a.name == "Override" && a.target_name == "run"));
    }

    #[test]
    fn extracts_fields_and_constants_distinctly() {
        let index = extract_source(
            "class First {\n    private int count;\n    public static final int MAX = 10;\n}\n",
        );
        let field = index.declaration_named("count").unwrap();
        assert_eq!(field.kind, DeclarationKind::Field);
        let constant = index.declaration_named("MAX").unwrap();
        assert_eq!(constant.kind, DeclarationKind::Constant);
    }

    #[test]
    fn extracts_multiple_comma_separated_field_declarators() {
        let index = extract_source("class First {\n    private int a, b;\n}\n");
        assert!(index.declaration_named("a").is_some());
        assert!(index.declaration_named("b").is_some());
    }

    #[test]
    fn extracts_qualified_and_bare_invocations() {
        let index = extract_source(
            "class First {\n    void run() {\n        obj.doSomething();\n        bareCall();\n    }\n}\n",
        );
        assert!(index.invocations.iter().any(|i| i.callee_name == "doSomething"));
        assert!(index.invocations.iter().any(|i| i.callee_name == "bareCall"));
    }

    #[test]
    fn extracts_construction_sites() {
        let index = extract_source("class First {\n    void run() {\n        Object x = new Last();\n    }\n}\n");
        assert!(index.constructions.iter().any(|c| c.type_name == "Last"));
    }

    #[test]
    fn extracts_type_references() {
        let index = extract_source("class First {\n    void run() {\n        Last x = null;\n    }\n}\n");
        assert!(index.type_references.iter().any(|t| t.type_name == "Last"));
    }

    #[test]
    fn assigns_a_cached_signature_line_per_symbol() {
        let index = extract_source("class First {\n    void run() {}\n}\n");
        let decl = index.declaration_named("run").unwrap();
        let signature = index.signatures.get(&decl.symbol).unwrap();
        assert_eq!(signature, "run(0 params)");
    }

    /// AC4 (Story #1787, S2) Level-1 "+arity" narrowing needs a genuine
    /// call-site argument count and a genuine declared parameter count --
    /// neither existed on `InvocationSite`/`Declaration` before this slice
    /// (the declared count was previously only baked into the opaque
    /// `signatures` string, unusable for a structural comparison). Both are
    /// captured during the SAME single walk `extract` already performs
    /// (`method_invocation`'s `argument_list`, `method_declaration`'s
    /// `formal_parameters`) -- no second tree walk is introduced.
    #[test]
    fn extracts_argument_count_at_call_sites_and_param_count_on_method_declarations() {
        let index = extract_source(
            "class First {\n    void run(int a, int b) {}\n    void go() {\n        run(1, 2);\n        bare();\n    }\n}\n",
        );
        let call = index.invocations.iter().find(|i| i.callee_name == "run").unwrap();
        assert_eq!(call.arg_count, Some(2));
        let bare_call = index.invocations.iter().find(|i| i.callee_name == "bare").unwrap();
        assert_eq!(bare_call.arg_count, Some(0));

        let run_decl = index.declaration_named("run").unwrap();
        assert_eq!(run_decl.param_count, Some(2));
        let go_decl = index.declaration_named("go").unwrap();
        assert_eq!(go_decl.param_count, Some(0));

        // Non-method declarations never carry a param_count.
        let type_decl = index.declaration_named("First").unwrap();
        assert_eq!(type_decl.param_count, None);
    }

    /// AC2 (Story #1793, S4): declared parameter TYPE names (beyond the
    /// existing `param_count`) and the varargs flag, verified against the
    /// real tree-sitter-java 0.23.5 grammar shapes for `formal_parameter`
    /// (`type: (T) name: (identifier)`) and `spread_parameter`
    /// (`(T) (variable_declarator name: (identifier))`).
    #[test]
    fn extracts_declared_parameter_type_names_and_varargs_flag() {
        let index = extract_source(
            "class First {\n    void save(String s, int x) {}\n    void tail(String... parts) {}\n}\n",
        );
        let save_decl = index.declaration_named("save").unwrap();
        assert_eq!(save_decl.param_types, vec!["String".to_string(), "int".to_string()]);
        assert!(!save_decl.is_varargs);

        let tail_decl = index.declaration_named("tail").unwrap();
        assert_eq!(tail_decl.param_types, vec!["String".to_string()]);
        assert!(tail_decl.is_varargs, "a spread_parameter must set is_varargs");
    }

    /// Discriminating regression for a code-review-caught defect: a
    /// PARAMETER with a modifier (`final`) or annotation (`@NonNull`) puts
    /// a `modifiers` node BEFORE the type in the real grammar shape -- a
    /// naive "first named child is always the type" implementation would
    /// wrongly record the modifiers node's text instead of the real type.
    #[test]
    fn parameter_modifiers_and_annotations_do_not_shift_the_declared_type() {
        let index = extract_source(
            "class First {\n    void save(final String s, @NonNull int x) {}\n}\n",
        );
        let save_decl = index.declaration_named("save").unwrap();
        assert_eq!(save_decl.param_types, vec!["String".to_string(), "int".to_string()]);
    }

    /// AC2 (Story #1793, S4): every documented `ArgShape` category is read
    /// off real call-site argument nodes -- string/numeric/boolean/null
    /// literals, an explicit cast, an inline constructor, a lambda, and a
    /// method reference -- verified against real tree-sitter-java 0.23.5
    /// grammar output for each argument kind.
    #[test]
    fn extracts_per_argument_shapes_at_call_sites() {
        let index = extract_source(
            "class First {\n    void go() {\n        save(\"a\", 1, true, null, (Foo) obj, new Foo(), x -> x, Foo::bar);\n    }\n}\n",
        );
        let call = index.invocations.iter().find(|i| i.callee_name == "save").unwrap();
        assert_eq!(
            call.arg_shapes,
            vec![
                ArgShape::StringLiteral,
                ArgShape::NumericLiteral,
                ArgShape::BooleanLiteral,
                ArgShape::NullLiteral,
                ArgShape::Cast("Foo".to_string()),
                ArgShape::Constructor("Foo".to_string()),
                ArgShape::Lambda,
                ArgShape::MethodReference,
            ]
        );
    }

    /// AC1 (Story #1793, S4): the family binder's `is_interface` check
    /// reads `LocalIndex.interface_names` -- populated ONLY for
    /// `interface_declaration` nodes, never for `class`/`enum`/`record`.
    #[test]
    fn extracts_interface_names_only_for_interface_declarations() {
        let index = extract_source("interface Shape {}\nclass Foo {}\nenum Color { RED }\n");
        assert_eq!(index.interface_names, vec!["Shape".to_string()]);
    }

    /// AC1: the enclosing-type context threaded through the stack walk
    /// must attribute each method to its IMMEDIATELY enclosing type --
    /// the discriminating case is a NESTED class, whose own method must
    /// be attributed to the inner type, never to the outer one a naive
    /// "last type seen" implementation might wrongly keep using.
    #[test]
    fn attributes_methods_to_their_immediately_enclosing_type_including_nested_classes() {
        let index = extract_source(
            "class Outer {\n    void outerMethod() {}\n    class Inner {\n        void innerMethod() {}\n    }\n}\n",
        );
        let outer_decl = index.declaration_named("outerMethod").unwrap();
        let inner_decl = index.declaration_named("innerMethod").unwrap();
        let owner_of = |symbol: SymbolId| {
            index.method_owners.iter().find(|o| o.method_symbol == symbol).map(|o| o.enclosing_type.clone())
        };
        assert_eq!(owner_of(outer_decl.symbol), Some("Outer".to_string()));
        assert_eq!(
            owner_of(inner_decl.symbol),
            Some("Inner".to_string()),
            "a nested class's method must be attributed to the INNER type, not Outer"
        );
    }

    #[test]
    fn symbol_ids_carry_the_given_file_id() {
        let index = extract_source("class First {}\n");
        let decl = index.declaration_named("First").unwrap();
        assert_eq!((decl.symbol >> 32) as u32, 7);
    }
}
