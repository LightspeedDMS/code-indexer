//! Kotlin receiver-expression construction and callable-reference
//! extraction, split out of `kotlin.rs` (Messi Rule 6, anti-file-bloat --
//! issue #1936) mirroring `java_receiver.rs`'s own sibling-module split.
//! Coarser than Java's own receiver classification (no `Chained` variant,
//! and no typed-name tracking at all -- see `kotlin.rs`'s module doc for
//! why: `typed_names`/level 6 is explicitly out of this extractor's
//! scope) because, absent that substrate, a richer receiver shape here
//! would carry no resolvable evidence anyway.

use super::local_index::{LocalIndex, ReceiverExpr};
use crate::graph::identity::SymbolId;
use crate::owned_node::OwnedNode;

/// A simple identifier receiver, `this`/`super`, or `Other` for anything
/// this extractor does not attempt to type (a chained call, a
/// parenthesized/cast/null-asserted expression, ...) -- never fabricated.
pub(super) fn build_receiver_expr(node: Option<&OwnedNode>) -> ReceiverExpr {
    match node {
        None => ReceiverExpr::None,
        Some(n) => match n.kind.as_str() {
            "this_expression" => ReceiverExpr::SelfOrSuper,
            "super_expression" => ReceiverExpr::Super,
            "identifier" => ReceiverExpr::Identifier(n.text().to_string()),
            _ => ReceiverExpr::Other,
        },
    }
}

/// `Foo::instanceMethod` / `f::instanceMethod` -- a QUALIFIED callable
/// reference, used as a value (never itself a call). No argument-list
/// evidence exists for a bare reference (`arg_count: None`, empty
/// `arg_shapes`), mirroring `JavaExtractor::extract_method_reference`.
pub(super) fn extract_navigation_callable_reference(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let named = node.named_children();
    let Some(member) = named.last() else {
        return;
    };
    if member.kind != "identifier" {
        return;
    }
    let receiver = build_receiver_expr(named.first().copied());
    super::kotlin_invocations::push_invocation_and_maybe_construction(
        member.text().to_string(),
        node.start_line,
        None,
        Vec::new(),
        receiver,
        enclosing_type,
        enclosing_method,
        index,
    );
}

/// `::topLevelFn` (a bare top-level function reference) or `::Foo` (a
/// bare constructor reference -- see the module doc's ambiguity note).
pub(super) fn extract_bare_callable_reference(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let Some(name_node) = node.child_by_kind("identifier") else {
        return;
    };
    super::kotlin_invocations::push_invocation_and_maybe_construction(
        name_node.text().to_string(),
        node.start_line,
        None,
        Vec::new(),
        ReceiverExpr::None,
        enclosing_type,
        enclosing_method,
        index,
    );
}
