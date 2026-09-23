//! Kotlin property/enum-entry declaration extraction and visibility
//! resolution, split out of `kotlin.rs` (Messi Rule 6, anti-file-bloat --
//! issue #1936) mirroring `java_fields.rs`'s own sibling-module split.
//! Owns `property_declaration` (including `const val`) and `enum_entry`
//! extraction, plus the shared access-modifier reading `kotlin.rs`'s own
//! type- and primary-constructor-property extraction, and `kotlin_
//! functions.rs`'s function/secondary-constructor extraction, all reuse
//! (`visibility_of_modifiers`).

use super::local_index::{Declaration, DeclarationKind, LocalIndex, Visibility};
use crate::owned_node::OwnedNode;

fn has_property_modifier(modifiers: &OwnedNode, keyword: &str) -> bool {
    modifiers
        .children
        .iter()
        .any(|c| c.kind == "property_modifier" && c.child_by_kind(keyword).is_some())
}

pub(super) fn extract_property_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    index: &mut LocalIndex,
) {
    let Some(var_decl) = node.child_by_kind("variable_declaration") else {
        return;
    };
    let Some(name_node) = var_decl.child_by_kind("identifier") else {
        return;
    };
    let name = name_node.text().to_string();
    let is_const = node
        .child_by_kind("modifiers")
        .map(|m| has_property_modifier(m, "const"))
        .unwrap_or(false);
    let kind = if is_const {
        DeclarationKind::Constant
    } else {
        DeclarationKind::Field
    };
    let symbol = super::kotlin::next_symbol(file_id, next_local);
    let keyword = if is_const { "const" } else { "property" };
    index.signatures.insert(symbol, format!("{keyword} {name}"));
    index.visibilities.insert(symbol, visibility_of_modifiers(node));
    index.declarations.push(Declaration {
        kind,
        name,
        line: node.start_line,
        symbol,
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    });
}

/// An enum entry (`RED` in `enum class Color { RED, GREEN }`) is
/// effectively a `public static final` instance of the enum type itself
/// -- recorded as a `Constant`, mirroring `JavaExtractor`'s equivalent
/// `enum_constant` handling (which records it as a field-scope typed name
/// instead, since Java tracks receiver typing this extractor deliberately
/// does not -- the DECLARATION-level treatment as a public constant is
/// the part that transfers).
pub(super) fn extract_enum_entry(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    index: &mut LocalIndex,
) {
    if enclosing_type.is_none() {
        return;
    }
    let Some(name_node) = node.child_by_kind("identifier") else {
        return;
    };
    let name = name_node.text().to_string();
    let symbol = super::kotlin::next_symbol(file_id, next_local);
    index.signatures.insert(symbol, format!("enum entry {name}"));
    index.visibilities.insert(symbol, Visibility::Public);
    index.declarations.push(Declaration {
        kind: DeclarationKind::Constant,
        name,
        line: node.start_line,
        symbol,
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    });
}

/// Unlike Java (whose no-modifier default is context-dependent --
/// `Visibility::Unknown` because a class member's real default,
/// package-private, differs from an interface member's implicit public),
/// Kotlin's no-explicit-modifier default is UNAMBIGUOUSLY `public` in
/// every declaration position this extractor covers -- so absent evidence
/// here safely maps to `Public` directly, not `Unknown`. `internal`
/// (module-scoped) conservatively maps to `Unknown`: this repo-only
/// analysis cannot prove it is unreachable from outside the compiled
/// module, so it must never be treated as `Private`-equivalent.
pub(super) fn visibility_of_modifiers(node: &OwnedNode) -> Visibility {
    let Some(modifiers) = node.child_by_kind("modifiers") else {
        return Visibility::Public;
    };
    let Some(vis) = modifiers.children.iter().find(|c| c.kind == "visibility_modifier") else {
        return Visibility::Public;
    };
    if vis.child_by_kind("private").is_some() {
        Visibility::Private
    } else if vis.child_by_kind("protected").is_some() {
        Visibility::Protected
    } else if vis.child_by_kind("public").is_some() {
        Visibility::Public
    } else {
        Visibility::Unknown
    }
}
