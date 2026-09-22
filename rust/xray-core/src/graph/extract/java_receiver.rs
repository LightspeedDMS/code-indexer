//! Receiver-expression construction for Java `method_invocation` sites
//! (Story #1806, S2b, AC1/AC2). Sibling module to `super::java` -- split
//! out to keep `java.rs` under its own module line budget while this
//! module carries the NEW extraction logic for this story: receiver-type
//! resolution (AC1) and return-type chaining (AC2) both start from
//! knowing WHAT a call's receiver expression is, computed here.
//!
//! Node-kind names below were verified against the REAL tree-sitter-java
//! 0.23.5 grammar output (dumped from real parsed sample files covering
//! every receiver shape this module handles: a bare/unqualified call, a
//! simple identifier receiver, `this`/`super`, a chained method
//! invocation, a field-access receiver, and a parenthesized-cast
//! receiver), not guessed -- see the story's own diagnostic dump for the
//! exact output this module's shapes were read off.

use super::local_index::{NameScope, ReceiverExpr, TypedNameRecord};
use crate::graph::identity::SymbolId;
use crate::owned_node::OwnedNode;

/// Hard ceiling on how many `object` levels `build_receiver_expr` will
/// ever descend for one call's receiver chain. A real fluent chain
/// (`a.b().c().d()...`) is rarely more than a handful of calls deep; this
/// is deliberately generous relative to that so it only ever engages on a
/// pathological/adversarial receiver chain, never a normal one. Exceeding
/// it returns `ReceiverExpr::Other` (never a guessed/truncated chain) --
/// this is what makes the walk's termination PROVABLE (Rule 14,
/// anti-unbounded-loop) rather than merely "usually fine": the loop below
/// runs at most this many iterations, full stop -- an explicit bounded
/// LOOP rather than a recursive AST walk, mirroring `owned_node.rs`'s own
/// recursion-avoidance rationale (Bug #1795).
const MAX_RECEIVER_CHAIN_DEPTH: usize = 32;

/// Splits a `method_invocation` node's named children (excluding
/// `argument_list` and, defensively, `type_arguments` -- a generic method
/// call's `<T>` type-witness node, which sits between `object` and `name`
/// and must never be mistaken for either) into `(object, name)`. Real
/// grammar shape has exactly one OPTIONAL `object` field and exactly one
/// REQUIRED `name` field: zero remaining named children after filtering
/// means a malformed/error-recovery parse (never observed on well-formed
/// source) -- `(None, None)`, never a guessed name. Exactly one remaining
/// child means a bare/unqualified call (`bareCall()`) -- `(None,
/// Some(name))`. Exactly two means object+name, in that order. More than
/// two is an unexpected grammar shape this extractor does not attempt to
/// interpret (Rule 2, anti-fallback): the name is still identified (last
/// child, matching the pre-existing heuristic this replaces) but no
/// object is reported, so callers never misattribute an unrelated node as
/// the receiver.
pub(super) fn invocation_object_and_name(
    node: &OwnedNode,
) -> (Option<&OwnedNode>, Option<&OwnedNode>) {
    let candidates: Vec<&OwnedNode> = node
        .named_children()
        .into_iter()
        .filter(|c| c.kind != "argument_list" && c.kind != "type_arguments")
        .collect();
    match candidates.len() {
        0 => (None, None),
        1 => (None, Some(candidates[0])),
        2 => (Some(candidates[0]), Some(candidates[1])),
        _ => (None, candidates.last().copied()),
    }
}

/// AC1/AC2: builds the `ReceiverExpr` for a call whose `object` field is
/// `start` (the caller passes the `method_invocation`'s own object node --
/// there is no "receiver of a bare call" entry point, since a bare call's
/// receiver is always `ReceiverExpr::None`, decided by the caller before
/// this function is ever invoked).
///
/// Bounded, non-recursive walk (Rule 14): each iteration either returns
/// (base case: `identifier`/`this`/`super`/anything else) or descends one
/// level into a `method_invocation`'s own `object`, up to
/// `MAX_RECEIVER_CHAIN_DEPTH` times. The per-level method names collected
/// while descending are folded back into nested `ReceiverExpr::Chained`
/// values afterward -- that fold is over an already-bounded, already-built
/// `Vec` (at most `MAX_RECEIVER_CHAIN_DEPTH` entries), not a second
/// unbounded walk.
pub(super) fn build_receiver_expr(start: &OwnedNode) -> ReceiverExpr {
    let mut node = start;
    let mut method_names: Vec<String> = Vec::new();
    let mut depth = 0usize;
    let base = loop {
        depth += 1;
        if depth > MAX_RECEIVER_CHAIN_DEPTH {
            return ReceiverExpr::Other;
        }
        match node.kind.as_str() {
            "identifier" => break ReceiverExpr::Identifier(node.text().to_string()),
            "this" => break ReceiverExpr::SelfOrSuper,
            "super" => break ReceiverExpr::Super,
            "method_invocation" => {
                let (object, name) = invocation_object_and_name(node);
                let Some(name_node) = name else {
                    return ReceiverExpr::Other;
                };
                method_names.push(name_node.text().to_string());
                match object {
                    Some(inner) => node = inner,
                    None => break ReceiverExpr::None,
                }
            }
            _ => return ReceiverExpr::Other,
        }
    };
    method_names
        .into_iter()
        .rev()
        .fold(base, |acc, method_name| ReceiverExpr::Chained {
            method_name,
            receiver: Box::new(acc),
        })
}

/// AC2 (Story #1806, S2b): a `method_declaration`'s declared return type
/// (never called for `constructor_declaration`, which has no return
/// type). Verified real grammar shape: `[modifiers]? [type_parameters]?
/// type name: (identifier) formal_parameters ...` -- the first named
/// child that is neither `modifiers` nor `type_parameters` is always the
/// return type; it can never itself be `identifier` (that is always the
/// method's own NAME, appearing strictly after the return type), so this
/// never risks reading past the type into the name.
pub(super) fn method_return_type_name(node: &OwnedNode) -> Option<String> {
    let type_node = node
        .named_children()
        .into_iter()
        .find(|c| c.kind != "modifiers" && c.kind != "type_parameters")?;
    Some(super::java_type_names::base_name_of_type_node(type_node))
}

/// AC1 (Story #1806, S2b): one `formal_parameter`/`spread_parameter`
/// node's declared `(name, type)` pair. The TYPE half reuses
/// `super::java::formal_parameter_type_name` directly (Rule 4,
/// anti-duplication) rather than re-deriving it. The NAME half mirrors
/// the same verified asymmetry that function's own doc comment already
/// documents: a `formal_parameter`'s name is a direct `identifier` child,
/// while a `spread_parameter`'s name is nested inside its own
/// `variable_declarator`.
pub(super) fn parameter_name_and_type(param_node: &OwnedNode) -> Option<(String, String)> {
    let declared_type = super::java::formal_parameter_type_name(param_node)?;
    let name_node = if param_node.kind == "spread_parameter" {
        param_node
            .child_by_kind("variable_declarator")?
            .child_by_kind("identifier")?
    } else {
        param_node
            .named_children()
            .into_iter()
            .find(|c| c.kind == "identifier")?
    };
    Some((name_node.text().to_string(), declared_type))
}

/// AC1 (Story #1806, S2b): shared by `field_typed_names` and
/// `local_variable_typed_names` below -- both `field_declaration` and
/// `local_variable_declaration` share the IDENTICAL verified real grammar
/// shape `[modifiers]? type (variable_declarator)+`. Finds the declared
/// type (first named child that is neither `modifiers` nor
/// `variable_declarator`) and builds one `TypedNameRecord` per declarator
/// (there can be several comma-separated, e.g. `int a, b;`), all sharing
/// `scope`. Empty (never a guessed type) when the declared type could not
/// be determined.
fn typed_names_from_declarators(node: &OwnedNode, scope: NameScope) -> Vec<TypedNameRecord> {
    let Some(declared_type) = node
        .named_children()
        .into_iter()
        .find(|c| c.kind != "modifiers" && c.kind != "variable_declarator")
        .map(super::java_type_names::base_name_of_type_node)
    else {
        return Vec::new();
    };
    node.children
        .iter()
        .filter(|c| c.kind == "variable_declarator")
        .filter_map(|d| d.child_by_kind("identifier"))
        .map(|name_node| TypedNameRecord {
            name: name_node.text().to_string(),
            declared_type: declared_type.clone(),
            scope: scope.clone(),
        })
        .collect()
}

/// AC1: every FIELD `TypedNameRecord` declared by one `field_declaration`
/// node, scoped to `enclosing_type`. `Vec::new()` when `enclosing_type`
/// is unknown -- never a guessed scope.
pub(super) fn field_typed_names(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
) -> Vec<TypedNameRecord> {
    let Some(enclosing_type) = enclosing_type else {
        return Vec::new();
    };
    typed_names_from_declarators(
        node,
        NameScope::Field {
            enclosing_type: enclosing_type.to_string(),
        },
    )
}

/// AC1: every LOCAL VARIABLE `TypedNameRecord` declared by one
/// `local_variable_declaration` node, scoped to `enclosing_method`.
/// `Vec::new()` when `enclosing_method` is unknown (e.g. a declaration
/// outside any method body) -- never a guessed scope.
pub(super) fn local_variable_typed_names(
    node: &OwnedNode,
    enclosing_method: Option<SymbolId>,
) -> Vec<TypedNameRecord> {
    let Some(enclosing_method) = enclosing_method else {
        return Vec::new();
    };
    typed_names_from_declarators(node, NameScope::Local { enclosing_method })
}

/// P1-B (#1898 code review round 2, epic #1906): one `TypedNameRecord`
/// for an `enhanced_for_statement`'s loop variable (`for (Type name :
/// expr)`), scoped to `enclosing_method`. Verified real grammar shape:
/// direct-child fields `type` (`_unannotated_type`) and `name`
/// (`identifier`), with `name` always positioned before the `value`
/// expression (Java requires `for (Type name : value)` in that source
/// order) -- `child_by_kind("identifier")` unambiguously finds the loop
/// variable even when `value` is itself a bare identifier expression
/// (`for (Foo x : someList)`: `someList` is a second `identifier`, but it
/// always follows `x` in child order). `None` when `enclosing_method` is
/// unknown or the loop variable's name cannot be found at all
/// (malformed/error-recovery parse) -- never a guessed name/scope.
/// Missing type evidence (a shape `base_type_name` does not resolve, e.g.
/// a primitive/array type) records the empty-string sentinel rather than
/// omitting the record entirely -- the record's PRESENCE, not its type,
/// is what blocks `receiver::resolve_receiver_type`'s static-type-name
/// fallback from misresolving this name against an unrelated same-named
/// in-repo type (Bug #1898 P1-B).
pub(super) fn enhanced_for_typed_name(
    node: &OwnedNode,
    enclosing_method: Option<SymbolId>,
) -> Option<TypedNameRecord> {
    let enclosing_method = enclosing_method?;
    let name_node = node.child_by_kind("identifier")?;
    let declared_type = super::java_type_names::base_type_name(node).unwrap_or_default();
    Some(TypedNameRecord {
        name: name_node.text().to_string(),
        declared_type,
        scope: NameScope::Local { enclosing_method },
    })
}

/// P1-B: a try-with-resources `resource` node's own `TypedNameRecord`,
/// scoped to `enclosing_method`. Real grammar shape: `resource`'s `name`
/// and `type` fields are BOTH optional -- the "existing variable"
/// resource form (`try (alreadyDeclaredVar) { ... }`, Java 9+)
/// introduces no NEW binding at all (its sole named child is the bare
/// `identifier`/`field_access` expression itself, with no `name`/`type`
/// field), so this returns `None` for that shape rather than fabricating
/// a fresh scope for a name declared elsewhere -- discriminated by named-
/// child COUNT (the declaring form always has at least `type` + `name`,
/// so >= 2; the bare-reference form has exactly 1). When `name` IS
/// present (the declaring form, `try (Type var = init)`), `type` is
/// always present too under real javac-valid source.
pub(super) fn resource_typed_name(
    node: &OwnedNode,
    enclosing_method: Option<SymbolId>,
) -> Option<TypedNameRecord> {
    let enclosing_method = enclosing_method?;
    if node.named_children().len() < 2 {
        return None;
    }
    let name_node = node.child_by_kind("identifier")?;
    let declared_type = super::java_type_names::base_type_name(node).unwrap_or_default();
    Some(TypedNameRecord {
        name: name_node.text().to_string(),
        declared_type,
        scope: NameScope::Local { enclosing_method },
    })
}

/// P1-B: a `catch_formal_parameter`'s own `TypedNameRecord`, scoped to
/// `enclosing_method`. Verified real grammar shape: `name` (`identifier`)
/// is a direct field; the caught type(s) live under a `catch_type` child,
/// which itself carries one OR MORE `_unannotated_type` named children
/// (multiple for a multi-catch `catch (IOException | SQLException e)`).
/// A single caught type resolves via the SAME `resolve_type_node_base_
/// name` every other declared-type read in this module uses; a multi-
/// catch (or a `catch_type` this extractor cannot resolve) records the
/// empty-string sentinel -- genuinely ambiguous/missing type evidence,
/// never a guessed single type, but the record's presence still blocks
/// the static-type-name misresolution this fix exists for.
pub(super) fn catch_parameter_typed_name(
    node: &OwnedNode,
    enclosing_method: Option<SymbolId>,
) -> Option<TypedNameRecord> {
    let enclosing_method = enclosing_method?;
    let name_node = node.child_by_kind("identifier")?;
    let declared_type = node
        .child_by_kind("catch_type")
        .map(|catch_type| catch_type.named_children())
        .filter(|types| types.len() == 1)
        .and_then(|types| super::java_type_names::resolve_type_node_base_name(types[0]))
        .unwrap_or_default();
    Some(TypedNameRecord {
        name: name_node.text().to_string(),
        declared_type,
        scope: NameScope::Local { enclosing_method },
    })
}

/// Bug #1898 round 4 (epic #1906): an `instanceof_expression`'s TYPE
/// PATTERN binding (`if (o instanceof Svc handle) { handle.helper(); }`),
/// scoped to `enclosing_method`. Verified real tree-sitter-java 0.23.5
/// grammar shape: `named_children()` is `[left, type]` (2 elements) for a
/// plain instanceof test with no binding (`o instanceof Svc`), or
/// `[left, type, name]` (3 elements) when a pattern variable IS bound --
/// `name` is always the LAST named child, always an `identifier` for a
/// simple type-pattern binding. A `record_pattern` in the type position
/// (Java 21 deconstruction, e.g. `instanceof Wrapper(Target handle)`) has
/// no third named child at all (the component bindings live nested inside
/// the `record_pattern` itself) -- this function does not descend into
/// that shape and returns `None`, never a guessed/misattributed name
/// (Rule 2, anti-fallback; deliberately out of scope for this round, see
/// `bug_1898_round4_narrowing_regressions.rs`'s own "unknown binding form"
/// test for why leaving it uncovered is safe under the round-4 evidence-
/// tier fix). This binder is intentionally METHOD-granular, not flow-
/// sensitive (same simplification `local_variable_typed_names` already
/// makes): a NEGATED instanceof pattern (`if (!(o instanceof Svc handle))
/// { return; } handle.helper();`, where the binding is only in scope
/// AFTER the guard) is covered automatically by this same extraction --
/// the record's presence, not its precise flow position, is what matters
/// to `receiver::resolve_receiver_type`.
pub(super) fn instanceof_pattern_typed_name(
    node: &OwnedNode,
    enclosing_method: Option<SymbolId>,
) -> Option<TypedNameRecord> {
    let enclosing_method = enclosing_method?;
    let named = node.named_children();
    if named.len() < 3 {
        return None;
    }
    let type_node = named[named.len() - 2];
    let name_node = named[named.len() - 1];
    if name_node.kind != "identifier" {
        return None;
    }
    let declared_type = super::java_type_names::base_name_of_type_node(type_node);
    Some(TypedNameRecord {
        name: name_node.text().to_string(),
        declared_type,
        scope: NameScope::Local { enclosing_method },
    })
}

/// #1910 prerequisite 3 (round4-findings.md finding 3): a Java 21 switch
/// case PATTERN LABEL's `type_pattern` binding (`case Target handle ->`),
/// scoped to `enclosing_method`. Verified real tree-sitter-java 0.23.5
/// grammar shape: `type_pattern`'s named children are always exactly
/// `[_unannotated_type, identifier]`, in that order. This is deliberately
/// written against the GRAMMAR NODE KIND rather than "switch case
/// patterns" as a Java feature: `type_pattern` is the one production
/// Java's pattern-matching grammar uses for every "bind a name to a type
/// test" position, so extracting it generically here closes not just
/// today's switch case labels but any future context tree-sitter-java
/// reuses the same node for -- the exact structural fix the round-4
/// postmortem asked for in place of enumerating one more Java feature.
/// `None` when `enclosing_method` is unknown or the two expected named
/// children are not both present as `[type, identifier]` (malformed/
/// error-recovery parse) -- never a guessed name/scope.
pub(super) fn type_pattern_typed_name(
    node: &OwnedNode,
    enclosing_method: Option<SymbolId>,
) -> Option<TypedNameRecord> {
    let enclosing_method = enclosing_method?;
    let named = node.named_children();
    if named.len() != 2 || named[1].kind != "identifier" {
        return None;
    }
    let declared_type = super::java_type_names::base_name_of_type_node(named[0]);
    Some(TypedNameRecord {
        name: named[1].text().to_string(),
        declared_type,
        scope: NameScope::Local { enclosing_method },
    })
}

/// #1910 prerequisite 3 (round4-findings.md finding 3, and the round-4
/// "uncovered binding form" test's own former subject): one binding
/// introduced by a Java 21 RECORD PATTERN's own component (`Target
/// handle` inside `Wrapper(Target handle)`), reachable via an
/// `instanceof` pattern OR a switch case pattern, at ANY nesting depth --
/// a nested `record_pattern_body` contains either another `record_pattern`
/// (walked generically; `super::java`'s own stack-based tree walk already
/// visits every descendant regardless of depth, so no recursion is needed
/// HERE) or a leaf `record_pattern_component`, which is exactly this node.
/// Verified real grammar shape: named children are the declared type
/// followed by EITHER a bound `identifier` OR an `underscore_pattern`
/// (Java's `_`, explicitly UNNAMED -- introduces no binding at all).
/// Real tree-sitter-java 0.23.5 output (verified by parsing a live
/// fixture, not guessed from `node-types.json` alone): a bare `_` in this
/// position surfaces as an `identifier` node whose TEXT is `"_"`, not a
/// distinct `underscore_pattern` node -- both the node-kind check and the
/// literal-text check are therefore required to correctly treat it as
/// "no binding". `None` for either unnamed form, an unknown `enclosing_
/// method`, or an unexpected child shape (malformed/error-recovery
/// parse) -- never a guessed name/scope.
pub(super) fn record_pattern_component_typed_name(
    node: &OwnedNode,
    enclosing_method: Option<SymbolId>,
) -> Option<TypedNameRecord> {
    let enclosing_method = enclosing_method?;
    let named = node.named_children();
    if named.len() != 2 || named[1].kind != "identifier" || named[1].text() == "_" {
        return None;
    }
    let declared_type = super::java_type_names::base_name_of_type_node(named[0]);
    Some(TypedNameRecord {
        name: named[1].text().to_string(),
        declared_type,
        scope: NameScope::Local { enclosing_method },
    })
}

/// P1-B: every `TypedNameRecord` for a `lambda_expression`'s parameter(s),
/// scoped to `enclosing_method`. Three real grammar shapes for the
/// `parameters` field, always the lambda's FIRST named child (it precedes
/// `->` and the body in source order, so this is never ambiguous with a
/// bare-identifier BODY, e.g. `x -> y`): `formal_parameters` (explicitly
/// typed, `(Foo x) -> ...` -- delegates to `parameter_name_and_type`, the
/// SAME per-parameter extraction a method declaration's own formal
/// parameters already use, Rule 4 anti-duplication); a bare `identifier`
/// (a single untyped parameter with no parens, `x -> ...`); or
/// `inferred_parameters` (multiple untyped parameters, `(x, y) -> ...`).
/// The untyped shapes carry NO declared-type evidence at all (Java
/// infers it; this extractor performs no type inference) -- the empty-
/// string sentinel still registers the NAME as a genuine local binding,
/// which is exactly what blocks the static-type-name misresolution (Bug
/// #1898 P1-B). `Vec::new()` when `enclosing_method` is unknown or the
/// `parameters` field cannot be found at all.
pub(super) fn lambda_param_typed_names(
    node: &OwnedNode,
    enclosing_method: Option<SymbolId>,
) -> Vec<TypedNameRecord> {
    let Some(enclosing_method) = enclosing_method else {
        return Vec::new();
    };
    let Some(parameters) = node.named_children().into_iter().next() else {
        return Vec::new();
    };
    match parameters.kind.as_str() {
        "formal_parameters" => parameters
            .named_children()
            .into_iter()
            .filter_map(parameter_name_and_type)
            .map(|(name, declared_type)| TypedNameRecord {
                name,
                declared_type,
                scope: NameScope::Local { enclosing_method },
            })
            .collect(),
        "identifier" => vec![TypedNameRecord {
            name: parameters.text().to_string(),
            declared_type: String::new(),
            scope: NameScope::Local { enclosing_method },
        }],
        "inferred_parameters" => parameters
            .named_children()
            .into_iter()
            .filter(|c| c.kind == "identifier")
            .map(|c| TypedNameRecord {
                name: c.text().to_string(),
                declared_type: String::new(),
                scope: NameScope::Local { enclosing_method },
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// #1922: `collect_all_local_binding_names`'s own per-node-kind dispatch,
/// split out to stay under the function-length budget. Each arm mirrors
/// its `X_typed_name(s)` sibling's own verified grammar shape but reads
/// ONLY the name, never a declared type or scope.
fn push_binding_names_for(node: &OwnedNode, names: &mut Vec<String>) {
    match node.kind.as_str() {
        "formal_parameter" | "spread_parameter" => {
            if let Some((name, _)) = parameter_name_and_type(node) {
                names.push(name);
            }
        }
        "local_variable_declaration" => names.extend(
            node.children
                .iter()
                .filter(|c| c.kind == "variable_declarator")
                .filter_map(|d| d.child_by_kind("identifier"))
                .map(|n| n.text().to_string()),
        ),
        "catch_formal_parameter" | "enhanced_for_statement" => {
            if let Some(name) = node.child_by_kind("identifier") {
                names.push(name.text().to_string());
            }
        }
        "resource" => {
            // Mirrors `resource_typed_name`'s own "existing variable"
            // resource form discrimination (Java 9+ `try
            // (alreadyDeclaredVar) { ... }` introduces no new binding).
            if node.named_children().len() >= 2 {
                if let Some(name) = node.child_by_kind("identifier") {
                    names.push(name.text().to_string());
                }
            }
        }
        "instanceof_expression" => {
            // Mirrors `instanceof_pattern_typed_name`'s own shape: a
            // bound pattern variable is always the LAST named child, an
            // `identifier`, only when present at all.
            let named = node.named_children();
            if named.len() >= 3 && named[named.len() - 1].kind == "identifier" {
                names.push(named[named.len() - 1].text().to_string());
            }
        }
        "type_pattern" => {
            let named = node.named_children();
            if named.len() == 2 && named[1].kind == "identifier" {
                names.push(named[1].text().to_string());
            }
        }
        "record_pattern_component" => {
            let named = node.named_children();
            if named.len() == 2 && named[1].kind == "identifier" && named[1].text() != "_" {
                names.push(named[1].text().to_string());
            }
        }
        "lambda_expression" => push_lambda_param_names(node, names),
        _ => {}
    }
}

/// A lambda's UNTYPED parameter forms (a bare identifier with no parens,
/// or multiple comma-separated untyped identifiers) need separate
/// handling: neither produces a `formal_parameter` node at all, unlike
/// the explicitly-typed `(Worker Svc) -> ...` form, which the
/// `formal_parameter` arm above already covers (the grammar nests it
/// inside the SAME `formal_parameters` -> `formal_parameter` shape a
/// method's own parameters use).
fn push_lambda_param_names(node: &OwnedNode, names: &mut Vec<String>) {
    let Some(parameters) = node.named_children().into_iter().next() else {
        return;
    };
    match parameters.kind.as_str() {
        "identifier" => names.push(parameters.text().to_string()),
        "inferred_parameters" => names.extend(
            parameters
                .named_children()
                .into_iter()
                .filter(|c| c.kind == "identifier")
                .map(|c| c.text().to_string()),
        ),
        _ => {}
    }
}

/// #1922: every NAME this file binds as a local, parameter, or pattern
/// variable, regardless of whether the binding has an enclosing method --
/// a lambda parameter in a field initializer, an enum constant's
/// argument list, or a switch-expression pattern in a field initializer
/// are all still genuine Java local bindings, even though `NameScope::
/// Local` cannot represent them (it requires a real enclosing METHOD
/// `SymbolId`, and none of those contexts ever sets one). Sole consumer:
/// `receiver::FileTypedNames::has_any_local_binding`'s flat, context-
/// independent existence check. Extracts NAMES ONLY -- never a declared
/// type, never a scope -- and performs NO scope/flow resolution (this is
/// existence, not visibility; #1919 does not apply).
///
/// ONE explicit-stack pre-order walk over the WHOLE tree (mirroring
/// `OwnedNode::descendants_of_kind`'s own bounded-stack pattern, Rule 14:
/// total pushes equal the file's finite node count), matching each
/// node's kind inline via `push_binding_names_for` -- never N separate
/// per-kind tree walks.
///
/// A `record_declaration`'s own component list (`record Point(int x, int
/// y) {}`) shares the IDENTICAL `formal_parameters` -> `formal_parameter`
/// grammar shape a method's parameters use (this extractor's own record-
/// component extraction elsewhere already relies on that), but a
/// component is a FIELD (an implicit accessor), never a local/parameter
/// binding -- so this walk explicitly skips pushing a `record_
/// declaration`'s own DIRECT `formal_parameters` child onto the stack
/// (everything else about the record -- its body's methods, nested
/// types, and any lambdas/locals genuinely declared inside them -- is
/// still pushed and walked normally).
pub(super) fn collect_all_local_binding_names(root: &OwnedNode) -> Vec<String> {
    let mut names = Vec::new();
    let mut stack: Vec<&OwnedNode> = root.children.iter().rev().collect();
    while let Some(node) = stack.pop() {
        push_binding_names_for(node, &mut names);
        if node.kind == "record_declaration" {
            stack.extend(
                node.children
                    .iter()
                    .filter(|c| c.kind != "formal_parameters")
                    .rev(),
            );
        } else {
            stack.extend(node.children.iter().rev());
        }
    }
    names
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn parse_first_invocation(source: &str) -> OwnedNode {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("Sample.java");
        std::fs::write(&path, source).unwrap();
        let root = crate::scanner::parse_file(Path::new(&path)).unwrap();
        root.descendants_of_kind("method_invocation")
            .into_iter()
            .next()
            .unwrap()
            .clone()
    }

    /// AC1: `obj.doSomething()`'s object is a simple identifier receiver.
    #[test]
    fn build_receiver_expr_resolves_a_simple_identifier_receiver() {
        let invocation = parse_first_invocation(
            "class First {\n    void run() {\n        obj.doSomething();\n    }\n}\n",
        );
        let (object, _name) = invocation_object_and_name(&invocation);
        let receiver = build_receiver_expr(object.expect("obj.doSomething() has an object"));
        assert_eq!(receiver, ReceiverExpr::Identifier("obj".to_string()));
    }

    /// AC2: THE central discriminating case named in the story --
    /// `auth.realm().requireX()`'s outer call's receiver must be a
    /// CHAINED expression wrapping the inner `realm()` call's own
    /// (identifier) receiver, never collapsed to just the inner call's
    /// name or just the base identifier alone.
    #[test]
    fn build_receiver_expr_wraps_a_chained_method_invocation_receiver() {
        // The FIRST method_invocation descendant in source order is the
        // OUTER call (`auth.realm().requireX()` itself), since tree-sitter
        // pre-order DFS visits a parent before its children.
        let invocation = parse_first_invocation(
            "class First {\n    void run() {\n        auth.realm().requireX();\n    }\n}\n",
        );
        let (object, name) = invocation_object_and_name(&invocation);
        assert_eq!(name.unwrap().text(), "requireX");
        let receiver = build_receiver_expr(object.expect("requireX() has an object"));
        assert_eq!(
            receiver,
            ReceiverExpr::Chained {
                method_name: "realm".to_string(),
                receiver: Box::new(ReceiverExpr::Identifier("auth".to_string())),
            }
        );
    }

    fn parse_first_of_kind(source: &str, kind: &str) -> OwnedNode {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("Sample.java");
        std::fs::write(&path, source).unwrap();
        let root = crate::scanner::parse_file(Path::new(&path)).unwrap();
        root.descendants_of_kind(kind)
            .into_iter()
            .next()
            .unwrap()
            .clone()
    }

    /// AC2: `Foo getSomething()`'s declared return type is `"Foo"`; a
    /// `void` method's is the literal text `"void"` (never fabricated,
    /// never absent for a real declared `void_type` node).
    #[test]
    fn method_return_type_name_reads_the_declared_return_type() {
        let typed = parse_first_of_kind(
            "class First {\n    Foo getSomething() { return null; }\n}\n",
            "method_declaration",
        );
        assert_eq!(method_return_type_name(&typed), Some("Foo".to_string()));

        let void_method = parse_first_of_kind(
            "class First {\n    void run() {}\n}\n",
            "method_declaration",
        );
        assert_eq!(
            method_return_type_name(&void_method),
            Some("void".to_string())
        );
    }

    /// AC1: both `formal_parameter` (`String s`) and `spread_parameter`
    /// (`Bar... rest`) shapes yield their real `(name, type)` pair, and a
    /// modifier/annotation on a `formal_parameter` never shifts either.
    #[test]
    fn parameter_name_and_type_reads_both_formal_and_spread_parameters() {
        let formal_parameters = parse_first_of_kind(
            "class First {\n    void save(final String s, Bar... rest) {}\n}\n",
            "formal_parameters",
        );
        let params: Vec<&OwnedNode> = formal_parameters.named_children();
        assert_eq!(
            parameter_name_and_type(params[0]),
            Some(("s".to_string(), "String".to_string())),
            "a formal_parameter with a modifier must still yield its real name and type"
        );
        assert_eq!(
            parameter_name_and_type(params[1]),
            Some(("rest".to_string(), "Bar".to_string()))
        );
    }

    /// #1910 prerequisite 3 (round4-findings.md finding 3): a Java 21
    /// switch case PATTERN LABEL's `type_pattern` binding (`case Target
    /// handle -> ...`) must be extracted as a real `TypedNameRecord`,
    /// exactly like every other local-binding form -- this closes the
    /// exact shape that fell through to `resolve_receiver_type`'s
    /// open-world fallback substrate before this fix.
    #[test]
    fn type_pattern_typed_name_reads_a_switch_case_pattern_binding() {
        use crate::graph::identity::make_symbol_id;

        let typed = parse_first_of_kind(
            "class First {\n    void run(Object o) {\n        switch (o) {\n            case String s -> System.out.println(s);\n            default -> {}\n        }\n    }\n}\n",
            "type_pattern",
        );
        let enclosing_method = make_symbol_id(1, 0);
        let record = type_pattern_typed_name(&typed, Some(enclosing_method))
            .expect("a switch case type pattern must yield a typed-name record");
        assert_eq!(record.name, "s");
        assert_eq!(record.declared_type, "String");
        assert_eq!(record.scope, NameScope::Local { enclosing_method });
    }

    /// #1910 prerequisite 3: a Java 21 RECORD PATTERN's own component
    /// (`Target handle` inside `Wrapper(Target handle)`) must ALSO be
    /// extracted -- this is the SAME node kind whether reached via an
    /// `instanceof` pattern or a switch case pattern, and at any nesting
    /// depth, which is exactly why this is written against the grammar's
    /// own `record_pattern_component` node rather than re-derived per
    /// Java feature.
    #[test]
    fn record_pattern_component_typed_name_reads_a_bound_component() {
        use crate::graph::identity::make_symbol_id;

        let typed = parse_first_of_kind(
            "class First {\n    void run(Object o) {\n        if (o instanceof Wrapper(Target handle)) {\n            handle.run();\n        }\n    }\n}\n",
            "record_pattern_component",
        );
        let enclosing_method = make_symbol_id(1, 0);
        let record = record_pattern_component_typed_name(&typed, Some(enclosing_method))
            .expect("a bound record pattern component must yield a typed-name record");
        assert_eq!(record.name, "handle");
        assert_eq!(record.declared_type, "Target");
        assert_eq!(record.scope, NameScope::Local { enclosing_method });
    }

    /// Companion: Java's `_` (underscore pattern) inside a record pattern
    /// component explicitly introduces NO binding at all -- extracting a
    /// fabricated name for it would be worse than not extracting anything.
    #[test]
    fn record_pattern_component_typed_name_returns_none_for_an_underscore_component() {
        use crate::graph::identity::make_symbol_id;

        let typed = parse_first_of_kind(
            "class First {\n    void run(Object o) {\n        if (o instanceof Wrapper(Target _)) {\n            System.out.println(\"matched\");\n        }\n    }\n}\n",
            "record_pattern_component",
        );
        let enclosing_method = make_symbol_id(1, 0);
        assert_eq!(
            record_pattern_component_typed_name(&typed, Some(enclosing_method)),
            None,
            "an underscore-pattern component introduces no binding and must yield no record"
        );
    }

    /// AC1: `int a, b;` yields TWO `TypedNameRecord`s (one per
    /// comma-separated declarator), both sharing the same declared type
    /// and the same `Field` scope.
    #[test]
    fn field_typed_names_reads_multiple_comma_separated_declarators() {
        let field = parse_first_of_kind(
            "class First {\n    private int a, b;\n}\n",
            "field_declaration",
        );
        let records = field_typed_names(&field, Some("First"));
        assert_eq!(records.len(), 2);
        assert!(records.iter().all(|r| r.declared_type == "int"));
        assert!(records.iter().any(|r| r.name == "a"));
        assert!(records.iter().any(|r| r.name == "b"));
        assert!(records.iter().all(|r| r.scope
            == NameScope::Field {
                enclosing_type: "First".to_string()
            }));
    }

    /// AC1: `Foo local = new Foo();`'s declared type is read off the
    /// container even though its `variable_declarator` also carries an
    /// initializer expression -- the type lookup must not be confused by
    /// that trailing content.
    #[test]
    fn local_variable_typed_names_reads_the_declared_type() {
        use crate::graph::identity::make_symbol_id;

        let local = parse_first_of_kind(
            "class First {\n    void run() {\n        Foo local = new Foo();\n    }\n}\n",
            "local_variable_declaration",
        );
        let enclosing_method = make_symbol_id(1, 0);
        let records = local_variable_typed_names(&local, Some(enclosing_method));
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].name, "local");
        assert_eq!(records[0].declared_type, "Foo");
        assert_eq!(records[0].scope, NameScope::Local { enclosing_method });
    }
}
