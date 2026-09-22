//! Java method/constructor declaration extraction, split out of `java.rs`
//! (Messi Rule 6, anti-file-bloat -- `java.rs` crossed the project's
//! 1000-line limit once #1922 added `constant_declaration` support and
//! `java_fields.rs`'s own split alone was not enough) mirroring `java_
//! receiver.rs`/`java_invocations.rs`/`java_type_names.rs`/`java_fields.
//! rs`'s own sibling-module split. Owns `method_declaration`/`constructor_
//! declaration` metadata extraction: the declared parameter type list,
//! the method's own `Declaration`, its owner/return-type/parameter-typed-
//! name records.

use super::java::{
    extract_annotations_from_modifiers, formal_parameter_type_name, next_symbol,
    push_type_parameter_names,
};
use super::local_index::{
    Declaration, DeclarationKind, LocalIndex, MethodOwnerRecord, MethodReturnTypeRecord,
    NameScope, TypedNameRecord,
};
use crate::graph::identity::SymbolId;
use crate::owned_node::OwnedNode;

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

/// AC1/AC2 (Story #1806, S2b): returns the method's own `SymbolId` --
/// `java.rs::dispatch_node` threads it into `WalkContext.enclosing_
/// method` for this method's children (nested invocations, local
/// variable declarations). The symbol is allocated BEFORE the name
/// lookup so a malformed/nameless declaration (parse-error recovery)
/// still yields a valid symbol for its children's context -- no
/// `Declaration` is pushed for it (never fabricated), but the symbol
/// counter itself stays deterministic and every child still has SOME
/// enclosing-method handle.
pub(super) fn extract_method_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    index: &mut LocalIndex,
) -> SymbolId {
    let symbol = next_symbol(file_id, next_local);
    push_type_parameter_names(node, index);
    let Some(name_node) = node.child_by_kind("identifier") else {
        return symbol;
    };
    let name = name_node.text().to_string();

    extract_annotations_from_modifiers(node, &name, index);

    let formal_parameters = node.child_by_kind("formal_parameters");
    let param_count = formal_parameters
        .map(|p| p.named_children().len())
        .unwrap_or(0);
    let (param_types, is_varargs) = formal_parameters
        .map(extract_param_types_and_varargs)
        .unwrap_or_default();
    index
        .signatures
        .insert(symbol, format!("{name}({param_count} params)"));
    index
        .visibilities
        .insert(symbol, super::java_fields::visibility_of_modifiers(node));

    index.declarations.push(Declaration {
        kind: DeclarationKind::Method,
        name,
        line: node.start_line,
        symbol,
        param_count: Some(param_count),
        param_types,
        is_varargs,
    });

    record_method_declaration_metadata(node, symbol, enclosing_type, formal_parameters, index);
    symbol
}

/// Split out of `extract_method_declaration` (F5/F6, #1873/#1875 rework)
/// to keep that function under the per-function line budget: the owner
/// record (AC1, Story #1793 S4 -- every method-shaped declaration,
/// including a constructor, gets one when an enclosing type is known;
/// constructors do not participate in method-family expansion, but their
/// owner is required when constructor invocation sites resolve by the
/// ordinary `DeclarationKind::Method` path), the return-type record (AC2,
/// Story #1806 S2b -- methods only, constructors have no return type at
/// all), and per-parameter typed-name records.
fn record_method_declaration_metadata(
    node: &OwnedNode,
    symbol: SymbolId,
    enclosing_type: Option<&str>,
    formal_parameters: Option<&OwnedNode>,
    index: &mut LocalIndex,
) {
    if let Some(enclosing_type) = enclosing_type {
        index.method_owners.push(MethodOwnerRecord {
            method_symbol: symbol,
            enclosing_type: enclosing_type.to_string(),
        });
    }
    if node.kind == "method_declaration" {
        if let Some(return_type) = super::java_receiver::method_return_type_name(node) {
            index.method_return_types.push(MethodReturnTypeRecord {
                method_symbol: symbol,
                return_type,
            });
        }
    }
    if let Some(formal_parameters) = formal_parameters {
        push_parameter_typed_names(formal_parameters, symbol, index);
    }
}

/// AC1 (Story #1806, S2b): pushes one `TypedNameRecord` per parameter in
/// `formal_parameters`, scoped to `enclosing_method` -- shared by both
/// `method_declaration` and `constructor_declaration` (constructor
/// parameters are just as valid a receiver-typing source as a method's).
fn push_parameter_typed_names(
    formal_parameters: &OwnedNode,
    enclosing_method: SymbolId,
    index: &mut LocalIndex,
) {
    for param in formal_parameters.named_children() {
        let Some((name, declared_type)) = super::java_receiver::parameter_name_and_type(param)
        else {
            continue;
        };
        index.typed_names.push(TypedNameRecord {
            name,
            declared_type,
            scope: NameScope::Local { enclosing_method },
        });
    }
}
