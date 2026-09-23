//! Shared Kotlin type-name resolution helpers, split out of `kotlin.rs`
//! (Messi Rule 6, anti-file-bloat -- issue #1936) mirroring `java_type_
//! names.rs`'s own sibling-module split. Owns the two functions that turn
//! a raw tree-sitter node into a bare, generics-stripped declared-type or
//! type-reference name: a type DECLARATION's own name (`type_declaration_
//! name`, including the anonymous-object and unnamed-companion synthesis
//! rules) and a `user_type` node's bare rightmost segment (`last_
//! identifier_text`, and its `extract_type_reference` call site).

use super::local_index::{LocalIndex, TypeReferenceRecord};
use crate::graph::identity::SymbolId;
use crate::owned_node::OwnedNode;

/// A declared type's bare name: its own `identifier` child when present
/// (class/interface/enum/object declarations, and a NAMED companion
/// object), `"Companion"` for an unnamed companion object (Kotlin's own
/// real default name -- accessible as `Type.Companion`, and at most one
/// per enclosing class so this can never collide within one nesting), or
/// a synthesized human-chaseable name (Bug #1929 item 3, shared with
/// `JavaExtractor`'s identical F1 anonymous-class scheme via
/// `super::synthesize_anon_type_name`) for an anonymous object expression
/// (`object : Base() { ... }`).
pub(super) fn type_declaration_name(
    node: &OwnedNode,
    file_id: u32,
    enclosing_type: Option<&str>,
) -> std::rc::Rc<str> {
    if let Some(id) = node.child_by_kind("identifier") {
        return std::rc::Rc::from(id.text());
    }
    if node.kind == "companion_object" {
        return std::rc::Rc::from("Companion");
    }
    super::synthesize_anon_type_name(enclosing_type, file_id, node.start_line, node.start_byte)
}

/// A `user_type`'s bare, generics-stripped, rightmost segment name: its
/// LAST direct `identifier` child. Works uniformly for a simple name
/// (`Foo`), a qualified/dotted name (`com.example.Foo` -- the grammar
/// decomposes this into multiple sibling `identifier` children joined by
/// anonymous `.` tokens, unlike Java's single-node `scoped_type_
/// identifier`), and a generic name (`List<String>` -- `String` lives two
/// levels deeper, inside `type_arguments -> type_projection -> user_type`,
/// never a DIRECT child of the outer `user_type`, so it is never picked
/// up here by accident).
pub(super) fn last_identifier_text(node: &OwnedNode) -> Option<String> {
    node.children
        .iter()
        .rev()
        .find(|c| c.kind == "identifier")
        .map(|c| c.text().to_string())
}

pub(super) fn extract_type_reference(
    node: &OwnedNode,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    if let Some(name) = last_identifier_text(node) {
        index.type_references.push(TypeReferenceRecord {
            type_name: name,
            line: node.start_line,
            enclosing_method,
        });
    }
}
