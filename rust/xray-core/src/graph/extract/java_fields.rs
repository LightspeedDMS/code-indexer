//! Java field/constant declaration extraction, split out of `java.rs`
//! (Messi Rule 6, anti-file-bloat -- `java.rs` crossed the project's
//! 1000-line limit once #1922 added `constant_declaration` support)
//! mirroring `java_receiver.rs`/`java_invocations.rs`/`java_type_names.rs`'s
//! own sibling-module split. Owns `field_declaration` (ordinary class/
//! enum/record fields) and `constant_declaration` (interface/annotation-
//! type body members, implicitly `public static final`) extraction, plus
//! the shared access-modifier reading both `java.rs`'s type- and
//! method-declaration extraction also reuse (`visibility_of_modifiers`).

use super::local_index::{Declaration, DeclarationKind, LocalIndex, Visibility};
use crate::owned_node::OwnedNode;

fn has_modifier(modifiers: &OwnedNode, keyword: &str) -> bool {
    modifiers.children.iter().any(|c| c.kind == keyword)
}

/// Story #1835 AC1: resolves a declaration's explicit Java access
/// modifier off its own `modifiers` node, reusing the exact
/// child-node-kind check `field_declaration_kind` already relies on for
/// `static`/`final`. Absent modifiers (no `modifiers` node at all, or one
/// present but carrying none of `public`/`protected`/`private`) map to
/// `Visibility::Unknown`, NEVER to a restricted default -- see
/// `Visibility`'s own doc comment (`local_index.rs`) for why: an
/// interface/annotation-type member with no explicit modifier is
/// implicitly `public`, and this extractor does not track "is the
/// enclosing type an interface" context to tell that apart from a real
/// class member's package-private default.
pub(super) fn visibility_of_modifiers(node: &OwnedNode) -> Visibility {
    let Some(modifiers) = node.child_by_kind("modifiers") else {
        return Visibility::Unknown;
    };
    if has_modifier(modifiers, "private") {
        Visibility::Private
    } else if has_modifier(modifiers, "protected") {
        Visibility::Protected
    } else if has_modifier(modifiers, "public") {
        Visibility::Public
    } else {
        Visibility::Unknown
    }
}

fn field_declaration_kind(node: &OwnedNode) -> DeclarationKind {
    let is_constant = node
        .child_by_kind("modifiers")
        .map(|m| has_modifier(m, "static") && has_modifier(m, "final"))
        .unwrap_or(false);
    if is_constant {
        DeclarationKind::Constant
    } else {
        DeclarationKind::Field
    }
}

/// Reads `field_declaration`'s own `variable_declarator` children directly
/// (verified real grammar output: they are DIRECT children, one per
/// comma-separated variable in the declaration, never nested deeper) so
/// this stays within the single bounded stack-walk in `java.rs::extract`
/// above, rather than opening a second, separate traversal per field.
pub(super) fn extract_field_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    index: &mut LocalIndex,
) {
    let kind = field_declaration_kind(node);
    let keyword = if matches!(kind, DeclarationKind::Constant) {
        "constant"
    } else {
        "field"
    };
    index
        .typed_names
        .extend(super::java_receiver::field_typed_names(
            node,
            enclosing_type,
        ));

    for declarator in node
        .children
        .iter()
        .filter(|c| c.kind == "variable_declarator")
    {
        let Some(name_node) = declarator.child_by_kind("identifier") else {
            continue;
        };
        let name = name_node.text().to_string();
        let symbol = super::java::next_symbol(file_id, next_local);
        index.signatures.insert(symbol, format!("{keyword} {name}"));
        index
            .visibilities
            .insert(symbol, visibility_of_modifiers(node));
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

/// #1922: `constant_declaration` (an interface/annotation-type body
/// member, e.g. `interface Consts { Worker INSTANCE = new Worker(); }`)
/// shares `field_declaration`'s exact grammar shape, so `field_typed_
/// names` (which only reads that shared shape) is reused verbatim --
/// Rule 4, anti-duplication. Unlike `extract_field_declaration`, this
/// never inspects modifiers to classify constant-vs-field or to read an
/// explicit visibility: EVERY `constant_declaration` member is
/// implicitly `public static final` by Java's own language rule,
/// regardless of whether the source repeats those keywords (interface
/// constants are legal without them, and by far the common style omits
/// them) -- hardcoding `DeclarationKind::Constant`/`Visibility::Public`
/// here is not a guess, it is what the node kind itself already proves.
pub(super) fn extract_constant_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    index: &mut LocalIndex,
) {
    index
        .typed_names
        .extend(super::java_receiver::field_typed_names(
            node,
            enclosing_type,
        ));

    for declarator in node
        .children
        .iter()
        .filter(|c| c.kind == "variable_declarator")
    {
        let Some(name_node) = declarator.child_by_kind("identifier") else {
            continue;
        };
        let name = name_node.text().to_string();
        let symbol = super::java::next_symbol(file_id, next_local);
        index.signatures.insert(symbol, format!("constant {name}"));
        index.visibilities.insert(symbol, Visibility::Public);
        index.declarations.push(Declaration {
            kind: DeclarationKind::Constant,
            name,
            line: node.start_line,
            symbol,
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
        });
    }
}
