//! Java invocation, construction, and type-reference extraction.
//!
//! This module owns the call-site details that `java`'s single tree walk
//! dispatches to. Keeping receiver and argument-shape extraction beside the
//! `InvocationSite` writes prevents those three call forms from drifting.

use super::java_type_names::{base_name_of_type_node, base_type_name};
use super::local_index::{
    ArgShape, ConstructionSite, InheritanceKind, InvocationSite, LocalIndex, ReceiverExpr,
    TypeReferenceRecord,
};
use crate::graph::identity::SymbolId;
use crate::owned_node::OwnedNode;

/// An explicit cast's (`(Foo) x`) target type. `cast_expression` has the
/// type as its first named child, unlike `formal_parameter` and
/// `spread_parameter`.
fn cast_target_type_name(cast_node: &OwnedNode) -> Option<String> {
    cast_node
        .named_children()
        .into_iter()
        .next()
        .map(base_name_of_type_node)
}

/// The coarse shape of one invocation argument. Unknown shapes deliberately
/// retain no narrowing evidence rather than fabricating a type name.
///
/// Bug #1923: `"identifier"` and `"this"` now carry their own shapes
/// (`ArgShape::Identifier`/`ArgShape::SelfReference`) instead of falling
/// into `Other` -- their declared TYPE is resolved later, at BIND time
/// (see `bind::receiver::resolve_argument_identifier_type`), since it
/// requires the per-file typed-name substrate this single-node,
/// extraction-time function has no access to.
fn arg_shape_for(arg_node: &OwnedNode) -> ArgShape {
    match arg_node.kind.as_str() {
        "string_literal" => ArgShape::StringLiteral,
        "true" | "false" => ArgShape::BooleanLiteral,
        "null_literal" => ArgShape::NullLiteral,
        "decimal_integer_literal"
        | "hex_integer_literal"
        | "octal_integer_literal"
        | "binary_integer_literal"
        | "decimal_floating_point_literal"
        | "hex_floating_point_literal" => ArgShape::NumericLiteral,
        "cast_expression" => cast_target_type_name(arg_node)
            .map(ArgShape::Cast)
            .unwrap_or(ArgShape::Other),
        "object_creation_expression" => base_type_name(arg_node)
            .map(ArgShape::Constructor)
            .unwrap_or(ArgShape::Other),
        "lambda_expression" => ArgShape::Lambda,
        "method_reference" => ArgShape::MethodReference,
        "identifier" => ArgShape::Identifier(arg_node.text().to_string()),
        "this" => ArgShape::SelfReference,
        _ => ArgShape::Other,
    }
}

/// Keeps an absent receiver distinct from a nested bare invocation used as a
/// receiver; `java_receiver` resolves that latter case itself.
pub(super) fn receiver_expr_for(object: Option<&OwnedNode>) -> ReceiverExpr {
    match object {
        Some(node) => super::java_receiver::build_receiver_expr(node),
        None => ReceiverExpr::None,
    }
}

/// Shared extraction for all call forms. Missing argument lists preserve the
/// parser's uncertainty as `None`/empty rather than inventing zero arguments.
fn arg_count_and_shapes(node: &OwnedNode) -> (Option<usize>, Vec<ArgShape>) {
    let argument_list = node.child_by_kind("argument_list");
    let arg_count = argument_list.map(|arguments| arguments.named_children().len());
    let arg_shapes = argument_list
        .map(|arguments| {
            arguments
                .named_children()
                .into_iter()
                .map(arg_shape_for)
                .collect()
        })
        .unwrap_or_default();
    (arg_count, arg_shapes)
}

pub(super) fn extract_invocation(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let (object, name) = super::java_receiver::invocation_object_and_name(node);
    let Some(callee) = name else {
        return;
    };
    let (arg_count, arg_shapes) = arg_count_and_shapes(node);
    index.invocations.push(InvocationSite {
        callee_name: callee.text().to_string(),
        line: node.start_line,
        arg_count,
        arg_shapes,
        receiver: receiver_expr_for(object),
        enclosing_type: enclosing_type.map(str::to_string),
        enclosing_method,
    });
}

pub(super) fn extract_construction(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let Some(type_name) = base_type_name(node) else {
        return;
    };
    let (arg_count, arg_shapes) = arg_count_and_shapes(node);
    index.constructions.push(ConstructionSite {
        type_name: type_name.clone(),
        line: node.start_line,
    });
    index.invocations.push(InvocationSite {
        callee_name: type_name,
        line: node.start_line,
        arg_count,
        arg_shapes,
        receiver: ReceiverExpr::Other,
        enclosing_type: enclosing_type.map(str::to_string),
        enclosing_method,
    });
}

pub(super) fn extract_explicit_constructor_invocation(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let Some(constructor) = node
        .child_by_kind("this")
        .or_else(|| node.child_by_kind("super"))
    else {
        return;
    };
    let callee_name = match constructor.kind.as_str() {
        "this" => enclosing_type.map(str::to_string),
        "super" => enclosing_type.and_then(|type_name| {
            index
                .inheritance
                .iter()
                .find(|record| {
                    record.kind == InheritanceKind::Extends && record.subtype_name == type_name
                })
                .map(|record| record.supertype_name.clone())
        }),
        _ => None,
    };
    let Some(callee_name) = callee_name else {
        return;
    };
    let (arg_count, arg_shapes) = arg_count_and_shapes(node);
    let receiver = if constructor.kind == "this" {
        ReceiverExpr::SelfOrSuper
    } else {
        ReceiverExpr::Other
    };
    index.invocations.push(InvocationSite {
        callee_name,
        line: node.start_line,
        arg_count,
        arg_shapes,
        receiver,
        enclosing_type: enclosing_type.map(str::to_string),
        enclosing_method,
    });
}

pub(super) fn extract_type_reference(node: &OwnedNode, index: &mut LocalIndex) {
    index.type_references.push(TypeReferenceRecord {
        type_name: node.text().to_string(),
        line: node.start_line,
    });
}
