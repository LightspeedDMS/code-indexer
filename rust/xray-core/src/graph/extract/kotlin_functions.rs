//! Kotlin function/constructor declaration extraction, split out of
//! `kotlin.rs` (Messi Rule 6, anti-file-bloat -- issue #1936) mirroring
//! `java_methods.rs`'s own sibling-module split. Owns `function_
//! declaration`/`secondary_constructor` metadata extraction (the declared
//! parameter type list, varargs/`vararg_index` resolution -- #1929 rework
//! -- the function's own `Declaration`, and its owner record) plus a
//! secondary constructor's own `this(...)`/`super(...)` delegation call
//! (#1908 follow-up reviewer finding A).

use super::kotlin::WalkContext;
use super::local_index::{
    Declaration, DeclarationKind, InvocationSite, LocalIndex, MethodOwnerRecord, ReceiverExpr,
    SyntheticScopeRecord,
};
use crate::graph::identity::SymbolId;
use crate::owned_node::OwnedNode;

fn count_parameters(params: &OwnedNode) -> usize {
    params.named_children().iter().filter(|c| c.kind == "parameter").count()
}

/// Declared parameter type names (in call order, best-effort -- a
/// parameter whose type could not be read is simply skipped, never
/// fabricated), whether ANY parameter carries the `vararg` modifier, and
/// -- Bug #1929 rework item 1 (P2 review finding) -- that parameter's
/// REAL INDEX in `types`. `vararg` is a SIBLING `parameter_modifiers`
/// node immediately preceding the parameter it modifies (verified real
/// grammar shape), not nested inside the `parameter` node itself.
/// UNLIKE Java (JLS 8.4.1: varargs is always the LAST formal parameter),
/// Kotlin allows exactly ONE `vararg` parameter at ANY position -- every
/// parameter declared after it must then be passed by NAME at the call
/// site. This function used to assume "last position" (a false claim a
/// prior version of this doc comment made), which put the varargs marker
/// on the wrong parameter for e.g. `fun mid(vararg xs: Int, tail:
/// String)`. `vararg_index` is only set to a valid position when the
/// SAME parameter the modifier preceded actually got its type pushed to
/// `types` (best-effort: an unresolved type still consumes the pending
/// marker, so it is never misattributed to some LATER, unrelated
/// parameter).
fn extract_param_types_and_varargs(params: &OwnedNode) -> (Vec<String>, bool, Option<usize>) {
    let mut types = Vec::new();
    let mut is_varargs = false;
    let mut vararg_index = None;
    let mut pending_vararg = false;
    for child in params.named_children() {
        match child.kind.as_str() {
            "parameter_modifiers" => {
                if child.named_children().iter().any(|m| m.child_by_kind("vararg").is_some()) {
                    is_varargs = true;
                    pending_vararg = true;
                }
            }
            "parameter" => {
                let pushed_type = child
                    .child_by_kind("user_type")
                    .and_then(super::kotlin_type_names::last_identifier_text);
                if let Some(t) = pushed_type {
                    if pending_vararg {
                        vararg_index = Some(types.len());
                    }
                    types.push(t);
                }
                pending_vararg = false;
            }
            _ => {}
        }
    }
    (types, is_varargs, vararg_index)
}

/// Reads a function-shaped declaration's own name, parameters, signature,
/// and visibility, and pushes its `Declaration` (+ `MethodOwnerRecord`
/// when `enclosing_type` is known). Shared by both a `function_
/// declaration`'s own extraction and this module's own `dispatch_
/// function_declaration`'s context threading, mirroring `JavaExtractor::
/// extract_method_declaration`'s split. Works uniformly for a member function, a
/// top-level function, AND an extension function (`fun Point.dist()`):
/// the extension receiver's own `user_type` never collides with the
/// direct-child `identifier` lookup below, since it is nested one level
/// deeper (inside `user_type`, not a direct child of `function_
/// declaration` itself) -- verified against the real grammar dump.
pub(super) fn extract_function_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    enclosing_type_symbol: Option<SymbolId>,
    index: &mut LocalIndex,
) -> SymbolId {
    let symbol = super::kotlin::next_symbol(file_id, next_local);
    super::kotlin_declarations::push_type_parameter_names(node, index);
    let Some(name_node) = node.child_by_kind("identifier") else {
        // Issue #1930: registers this parse-recovery symbol as a
        // synthetic scope carrying its real enclosing type -- mirrors
        // `JavaExtractor::extract_method_declaration`'s identical fix;
        // see its own doc comment.
        index.synthetic_scopes.push(SyntheticScopeRecord {
            symbol,
            start_line: node.start_line,
            enclosing_type_symbol,
        });
        return symbol;
    };
    let name = name_node.text().to_string();
    let params = node.child_by_kind("function_value_parameters");
    let param_count = params.map(count_parameters).unwrap_or(0);
    let (param_types, is_varargs, vararg_index) =
        params.map(extract_param_types_and_varargs).unwrap_or_default();
    index
        .signatures
        .insert(symbol, format!("{name}({param_count} params)"));
    index
        .visibilities
        .insert(symbol, super::kotlin_fields::visibility_of_modifiers(node));
    index.declarations.push(Declaration {
        kind: DeclarationKind::Method,
        name,
        line: node.start_line,
        symbol,
        param_count: Some(param_count),
        param_types,
        is_varargs,
        vararg_index,
    });
    if let Some(enclosing_type) = enclosing_type {
        index.method_owners.push(MethodOwnerRecord {
            method_symbol: symbol,
            enclosing_type: enclosing_type.to_string(),
        });
    }
    symbol
}

/// A `secondary_constructor` has no `identifier` child of its own (just
/// the `constructor` keyword) -- its declared NAME is the enclosing
/// type's own bare name, mirroring `JavaExtractor::extract_method_
/// declaration`'s identical convention for a Java `constructor_
/// declaration`. A constructor with no known enclosing type (malformed
/// input) still gets a symbol for its children's context, but no
/// `Declaration` is pushed -- never fabricated.
///
/// Issue #1930: that symbol is registered as a
/// `SyntheticScopeRecord` too, exactly like `extract_function_
/// declaration`'s own fix -- `enclosing_type_symbol` is `None` here in
/// the SAME case `enclosing_type` (the bare name) is `None`, so `bind::
/// resolve::enclosing_symbol_for_site` falls back to the ordinary line
/// heuristic for it, a DOCUMENTED, EXPECTED path rather than the
/// debug-only "should be impossible" one this used to reach.
pub(super) fn extract_secondary_constructor(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    enclosing_type_symbol: Option<SymbolId>,
    index: &mut LocalIndex,
) -> SymbolId {
    let symbol = super::kotlin::next_symbol(file_id, next_local);
    let Some(enclosing_type) = enclosing_type else {
        index.synthetic_scopes.push(SyntheticScopeRecord {
            symbol,
            start_line: node.start_line,
            enclosing_type_symbol,
        });
        return symbol;
    };
    let params = node.child_by_kind("function_value_parameters");
    let param_count = params.map(count_parameters).unwrap_or(0);
    let (param_types, is_varargs, vararg_index) =
        params.map(extract_param_types_and_varargs).unwrap_or_default();
    index
        .signatures
        .insert(symbol, format!("{enclosing_type}({param_count} params)"));
    index
        .visibilities
        .insert(symbol, super::kotlin_fields::visibility_of_modifiers(node));
    index.declarations.push(Declaration {
        kind: DeclarationKind::Method,
        name: enclosing_type.to_string(),
        line: node.start_line,
        symbol,
        param_count: Some(param_count),
        param_types,
        is_varargs,
        vararg_index,
    });
    index.method_owners.push(MethodOwnerRecord {
        method_symbol: symbol,
        enclosing_type: enclosing_type.to_string(),
    });
    symbol
}

/// `constructor(x: Int) : this(...)`/`: super(...)` -- a secondary
/// constructor's own delegation call, a SIBLING of its `block` (not
/// nested inside it). `this(...)` resolves to the enclosing type's own
/// name.
///
/// Bug #1908 follow-up (reviewer finding A): `super(...)` used to look up
/// ONLY an `InheritanceKind::Extends`-classified record. But Kotlin's own
/// grammar makes that combination impossible to satisfy: `Extends` is
/// recorded exclusively for the `constructor_invocation` shape
/// (`class Sub : Base(x)`, a PRIMARY-constructor super-call already
/// embedded in the specifier) -- and a class with a primary constructor's
/// secondary constructors must delegate via `this(...)`, never
/// `super(...)`. A class WITHOUT a primary constructor -- the ONLY shape
/// whose secondary constructors are required to write `super(...)` --
/// lists its supertype BARE (`class Sub : Base`), which this extractor's
/// own ambiguity default classifies as `Implements` (see
/// `kotlin_declarations::extract_inheritance`'s doc comment: bare local
/// syntax cannot tell a superclass from an interface). So every real
/// `super(...)` call site
/// missed its only recorded edge -- a dead branch reachable by no legal
/// Kotlin input. Fixed by accepting ANY recorded supertype for the
/// enclosing type (regardless of `InheritanceKind`) and emitting one
/// candidate per DISTINCT name: `super(...)` can target only the true
/// superclass, but when a bare specifier list also contains implemented
/// interfaces this extractor cannot always tell which entry is which from
/// local syntax alone -- per the over-binding-is-safe mandate, emitting
/// all of them is the safe direction (a spurious interface edge is
/// harmless noise; a missing superclass edge is a false dead-code
/// verdict).
pub(super) fn extract_constructor_delegation_call(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let Some(kind_node) = node.child_by_kind("this").or_else(|| node.child_by_kind("super")) else {
        return;
    };
    let callee_names: Vec<String> = match kind_node.kind.as_str() {
        "this" => enclosing_type.map(|t| vec![t.to_string()]).unwrap_or_default(),
        "super" => enclosing_type
            .map(|t| {
                let mut names: Vec<String> = index
                    .inheritance
                    .iter()
                    .filter(|r| r.subtype_name == t)
                    .map(|r| r.supertype_name.clone())
                    .collect();
                names.sort();
                names.dedup();
                names
            })
            .unwrap_or_default(),
        _ => Vec::new(),
    };
    if callee_names.is_empty() {
        return;
    }
    let (arg_count, arg_shapes) = super::kotlin_invocations::arg_count_and_shapes(node);
    let receiver = if kind_node.kind == "this" {
        ReceiverExpr::SelfOrSuper
    } else {
        ReceiverExpr::Other
    };
    for callee_name in callee_names {
        index.invocations.push(InvocationSite {
            callee_name,
            line: node.start_line,
            arg_count,
            arg_shapes: arg_shapes.clone(),
            receiver: receiver.clone(),
            enclosing_type: enclosing_type.map(str::to_string),
            enclosing_method,
        });
    }
}

/// `WalkContext`-mutating wrapper around `extract_function_declaration`:
/// keeps `enclosing_type`/`top_level_type` unchanged but sets `enclosing_
/// method` to the function's OWN symbol for its children's context.
/// Stays paired with `extract_function_declaration` (rather than living in
/// `kotlin.rs`) so this module owns the whole function-declaration
/// concern, mirroring `java.rs::dispatch_method_declaration`'s equivalent
/// role for `java_methods::extract_method_declaration`.
pub(super) fn dispatch_function_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: &WalkContext,
    index: &mut LocalIndex,
) -> WalkContext {
    let symbol = extract_function_declaration(
        node,
        file_id,
        next_local,
        ctx.enclosing_type.as_deref(),
        ctx.enclosing_type_symbol,
        index,
    );
    WalkContext {
        enclosing_type: ctx.enclosing_type.clone(),
        top_level_type: ctx.top_level_type.clone(),
        enclosing_method: Some(symbol),
        enclosing_type_symbol: ctx.enclosing_type_symbol,
    }
}

/// `WalkContext`-mutating wrapper around `extract_secondary_constructor`,
/// for the same reason `dispatch_function_declaration` wraps `extract_
/// function_declaration`.
pub(super) fn dispatch_secondary_constructor(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: &WalkContext,
    index: &mut LocalIndex,
) -> WalkContext {
    let symbol = extract_secondary_constructor(
        node,
        file_id,
        next_local,
        ctx.enclosing_type.as_deref(),
        ctx.enclosing_type_symbol,
        index,
    );
    WalkContext {
        enclosing_type: ctx.enclosing_type.clone(),
        top_level_type: ctx.top_level_type.clone(),
        enclosing_method: Some(symbol),
        enclosing_type_symbol: ctx.enclosing_type_symbol,
    }
}
