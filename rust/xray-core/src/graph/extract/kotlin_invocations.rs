//! Kotlin invocation, construction, and operator-convention-call
//! extraction, split out of `kotlin.rs` (Messi Rule 6, anti-file-bloat --
//! issue #1936) mirroring `java_invocations.rs`'s own sibling-module
//! split. Owns every call-shaped extraction this extractor's single tree
//! walk dispatches to: ordinary `call_expression`s (plus the construction-
//! ambiguity over-binding heuristic -- see `kotlin.rs`'s module doc), the
//! `infix fun` convention, and every operator-convention call Bug #1917
//! added (binary/index/assignment/unary/range/`in`).

use super::local_index::{ArgShape, ConstructionSite, InvocationSite, LocalIndex, ReceiverExpr};
use crate::graph::identity::SymbolId;
use crate::owned_node::OwnedNode;
use std::collections::HashSet;

fn starts_with_uppercase(name: &str) -> bool {
    name.chars().next().is_some_and(|c| c.is_uppercase())
}

/// A `call_expression`'s own callee name and receiver, read directly off
/// its FIRST named child -- either a bare `identifier` (`helperFunc(...)`,
/// `Foo(...)`) or a `navigation_expression` (`obj.method(...)`,
/// `pkg.Type(...)`). Returns `None` for any other shape (an immediately-
/// invoked lambda, a parenthesized callee, ...) -- never a guess. Shared
/// by `extract_call_expression` and `arg_shape_for`'s nested-constructor-
/// argument detection (Rule 4, anti-duplication).
fn call_expression_callee(node: &OwnedNode) -> Option<(String, ReceiverExpr)> {
    let first = node.named_children().into_iter().next()?;
    match first.kind.as_str() {
        "identifier" => Some((first.text().to_string(), ReceiverExpr::None)),
        "navigation_expression" => {
            let named = first.named_children();
            let member = named.last()?;
            if member.kind != "identifier" {
                return None;
            }
            let receiver = super::kotlin_receiver::build_receiver_expr(named.first().copied());
            Some((member.text().to_string(), receiver))
        }
        _ => None,
    }
}

/// A call's argument list: `value_arguments` (`(a, b)`), a TRAILING lambda
/// (`annotated_lambda`, Kotlin's `list.forEach { ... }` syntax), or BOTH
/// together (`list.reduce(0) { acc, x -> acc + x }`). `None` only when
/// NEITHER is present (no `argument_list`-equivalent at all, e.g.
/// malformed/incomplete source under parse-error recovery) -- never
/// fabricated as `Some(0)` in that case, mirroring `JavaExtractor`'s own
/// contract for `InvocationSite::arg_count`.
pub(super) fn arg_count_and_shapes(node: &OwnedNode) -> (Option<usize>, Vec<ArgShape>) {
    let value_arguments = node.child_by_kind("value_arguments");
    let trailing_lambda = node.child_by_kind("annotated_lambda");
    if value_arguments.is_none() && trailing_lambda.is_none() {
        return (None, Vec::new());
    }
    let mut shapes: Vec<ArgShape> = value_arguments
        .map(|va| va.named_children().into_iter().map(arg_shape_for).collect())
        .unwrap_or_default();
    if trailing_lambda.is_some() {
        shapes.push(ArgShape::Lambda);
    }
    let count = shapes.len();
    (Some(count), shapes)
}

/// One `value_argument`'s coarse shape. A named argument (`name = value`)
/// wraps its actual value as the LAST named child (the name identifier
/// comes first) -- `.last()` picks the real value uniformly for both
/// positional and named arguments. `true`/`false`/`null` are lexed as
/// plain `identifier` nodes in this grammar (verified real dump, not a
/// dedicated literal kind), so they are matched by text, not kind.
fn arg_shape_for(arg: &OwnedNode) -> ArgShape {
    let Some(value) = arg.named_children().into_iter().last() else {
        return ArgShape::Other;
    };
    classify_expr_shape(value)
}

/// The coarse-shape classification shared by `arg_shape_for` (a
/// `value_argument`'s already-unwrapped value node) and
/// `extract_infix_expression` (a bare right-operand expression node,
/// never wrapped in `value_argument` -- an infix call has no argument
/// list at all). Factored out rather than duplicated (Rule 4,
/// anti-duplication): both call sites already have the actual expression
/// node in hand, they differ only in HOW they got there.
fn classify_expr_shape(value: &OwnedNode) -> ArgShape {
    match value.kind.as_str() {
        "string_literal" => ArgShape::StringLiteral,
        "number_literal" | "float_literal" => ArgShape::NumericLiteral,
        "identifier" if value.text() == "true" || value.text() == "false" => ArgShape::BooleanLiteral,
        "identifier" if value.text() == "null" => ArgShape::NullLiteral,
        "lambda_literal" => ArgShape::Lambda,
        "callable_reference" => ArgShape::MethodReference,
        "call_expression" => call_expression_callee(value)
            .filter(|(name, _)| starts_with_uppercase(name))
            .map(|(name, _)| ArgShape::Constructor(name))
            .unwrap_or(ArgShape::Other),
        _ => ArgShape::Other,
    }
}

/// Pushes an `InvocationSite`, and -- per the module doc's construction-
/// ambiguity note -- ALSO a `ConstructionSite` when `callee_name` starts
/// with an uppercase letter. The SOLE place this heuristic is applied
/// (Rule 4, anti-duplication): every call site below routes through here.
#[allow(clippy::too_many_arguments)]
pub(super) fn push_invocation_and_maybe_construction(
    callee_name: String,
    line: usize,
    arg_count: Option<usize>,
    arg_shapes: Vec<ArgShape>,
    receiver: ReceiverExpr,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    if starts_with_uppercase(&callee_name) {
        index.constructions.push(ConstructionSite {
            type_name: callee_name.clone(),
            line,
            enclosing_method,
        });
    }
    index.invocations.push(InvocationSite {
        callee_name,
        line,
        arg_count,
        arg_shapes,
        receiver,
        enclosing_type: enclosing_type.map(str::to_string),
        enclosing_method,
    });
}

pub(super) fn extract_call_expression(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let Some((callee_name, receiver)) = call_expression_callee(node) else {
        return;
    };
    let (arg_count, arg_shapes) = arg_count_and_shapes(node);
    push_invocation_and_maybe_construction(
        callee_name,
        node.start_line,
        arg_count,
        arg_shapes,
        receiver,
        enclosing_type,
        enclosing_method,
        index,
    );
}

/// `a matches b` -- an INFIX call (`infix fun matches`). Grammar shape
/// verified against a real tree-sitter-kotlin-ng 1.1.0 parse dump:
/// `infix_expression` has exactly three direct children in source order
/// -- the left/receiver expression, the function-name `identifier`, and
/// the right/sole-argument expression. Always exactly one argument (an
/// infix function takes exactly one parameter by Kotlin's own grammar
/// rule), so `arg_count` is always `Some(1)` here, never fabricated for
/// any other shape.
pub(super) fn extract_infix_expression(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let children = &node.children;
    let Some((left, name_node, right)) = (match children.as_slice() {
        [left, name_node, right] if name_node.kind == "identifier" => Some((left, name_node, right)),
        _ => None,
    }) else {
        return;
    };
    let receiver = super::kotlin_receiver::build_receiver_expr(Some(left));
    let arg_shape = classify_expr_shape(right);
    push_invocation_and_maybe_construction(
        name_node.text().to_string(),
        node.start_line,
        Some(1),
        vec![arg_shape],
        receiver,
        enclosing_type,
        enclosing_method,
        index,
    );
}

/// Kotlin's user-overloadable `binary_expression` operators mapped to
/// their `operator fun` convention name -- `None` for any operator this
/// grammar's `binary_expression` also carries (`&&`, `||`, `?:`, `===`,
/// `!==`) that Kotlin does NOT allow a user to overload (see the module
/// doc). `==`/`!=` both map to `equals`: Kotlin desugars structural
/// (in)equality to a null-safe `.equals(...)` call for BOTH operators
/// (`!=` is `!(a.equals(b))`), so both share the one real convention
/// function a user can actually override.
fn operator_convention_name(operator: &str) -> Option<&'static str> {
    match operator {
        "+" => Some("plus"),
        "-" => Some("minus"),
        "*" => Some("times"),
        "/" => Some("div"),
        "%" => Some("rem"),
        "<" | "<=" | ">" | ">=" => Some("compareTo"),
        "==" | "!=" => Some("equals"),
        _ => None,
    }
}

/// `a + b` / `a == b` / ... -- an operator-convention BINARY call. Grammar
/// shape verified against a real tree-sitter-kotlin-ng 1.1.0 parse dump:
/// `binary_expression` has exactly three direct children in source order
/// -- the left operand, the operator token (an UNNAMED leaf whose `kind`
/// is the literal operator text, e.g. `"+"` -- unlike `infix_expression`'s
/// middle child, which is a NAMED `identifier`), and the right operand.
/// Always exactly one argument (the right operand), mirroring `extract_
/// infix_expression`'s identical arity contract. A no-op (no invocation
/// emitted) when the operator has no convention mapping -- see `operator_
/// convention_name` -- but the walk still reaches `left`/`right`'s own
/// children normally via the generic stack traversal, so any real call
/// nested inside either operand (e.g. `f() + g()`) is never missed.
pub(super) fn extract_binary_expression(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let children = &node.children;
    let Some((left, operator_node, right)) = (match children.as_slice() {
        [left, operator_node, right] if !operator_node.is_named => Some((left, operator_node, right)),
        _ => None,
    }) else {
        return;
    };
    let Some(convention_name) = operator_convention_name(operator_node.text()) else {
        return;
    };
    let receiver = super::kotlin_receiver::build_receiver_expr(Some(left));
    let arg_shape = classify_expr_shape(right);
    push_invocation_and_maybe_construction(
        convention_name.to_string(),
        node.start_line,
        Some(1),
        vec![arg_shape],
        receiver,
        enclosing_type,
        enclosing_method,
        index,
    );
}

/// `m[k]` -- an operator-convention index READ (`m.get(k)`). Grammar shape
/// verified against a real tree-sitter-kotlin-ng 1.1.0 parse dump:
/// `index_expression`'s named children are the receiver expression
/// followed by one or more index-argument expressions (the `[`, `]`, and
/// any `,` separators are unnamed punctuation, never named children) --
/// `m[a, b]` (a multi-parameter `get` overload) is supported uniformly by
/// treating every named child after the first as an index argument.
pub(super) fn extract_index_expression(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let named = node.named_children();
    let Some((receiver, index_args)) = named.split_first() else {
        return;
    };
    let receiver_expr = super::kotlin_receiver::build_receiver_expr(Some(*receiver));
    let arg_shapes: Vec<ArgShape> = index_args.iter().map(|arg| classify_expr_shape(arg)).collect();
    let arg_count = arg_shapes.len();
    push_invocation_and_maybe_construction(
        "get".to_string(),
        node.start_line,
        Some(arg_count),
        arg_shapes,
        receiver_expr,
        enclosing_type,
        enclosing_method,
        index,
    );
}

/// Non-indexed compound-assignment operators mapped to their `(*Assign,
/// plain)` convention name PAIR -- see the module doc's second-round
/// paragraph for why BOTH are emitted rather than choosing one: the real
/// desugaring depends on whether a `*Assign` overload exists on the
/// target's real type, receiver-type evidence (level 6) this extractor
/// does not track.
fn compound_assign_convention_names(operator: &str) -> Option<(&'static str, &'static str)> {
    match operator {
        "+=" => Some(("plusAssign", "plus")),
        "-=" => Some(("minusAssign", "minus")),
        "*=" => Some(("timesAssign", "times")),
        "/=" => Some(("divAssign", "div")),
        "%=" => Some(("remAssign", "rem")),
        _ => None,
    }
}

/// `m[k] = v` (indexed WRITE, desugars to `m.set(k, v)`) and `x += y`/etc.
/// (non-indexed COMPOUND assignment, desugars to `x.plusAssign(y)` OR
/// `x = x.plus(y)` -- both candidates emitted, see `compound_assign_
/// convention_names`). A no-op for a plain `=` on a non-indexed target
/// (no operator-convention evidence at all) and for a COMPOUND operator on
/// an INDEXED target (`m[k] += v` -- see the module doc for why that
/// combination is left as a documented remaining gap). Grammar shape
/// verified against a real tree-sitter-kotlin-ng 1.1.0 parse dump:
/// `assignment` has exactly three direct children in source order -- the
/// left (target) expression, the operator token (an UNNAMED leaf, the same
/// positional shape `extract_binary_expression` destructures), and the
/// right (value) expression.
///
/// On an indexed-write match, marks the target `index_expression` node's
/// own `start_byte` in `claimed_write_targets` so the generic `"index_
/// expression"` dispatch arm -- which will still reach this SAME node
/// moments later via the ordinary stack walk, since `assignment`'s
/// children are pushed unconditionally like any other node's -- skips
/// emitting a second, spurious `get` for it.
pub(super) fn extract_assignment(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    claimed_write_targets: &mut HashSet<usize>,
    index: &mut LocalIndex,
) {
    let children = &node.children;
    let Some((target, operator_node, value)) = (match children.as_slice() {
        [target, operator_node, value] if !operator_node.is_named => Some((target, operator_node, value)),
        _ => None,
    }) else {
        return;
    };
    if target.kind == "index_expression" && operator_node.kind == "=" {
        let named = target.named_children();
        let Some((receiver, index_args)) = named.split_first() else {
            return;
        };
        claimed_write_targets.insert(target.start_byte);
        let receiver_expr = super::kotlin_receiver::build_receiver_expr(Some(*receiver));
        let mut arg_shapes: Vec<ArgShape> = index_args.iter().map(|arg| classify_expr_shape(arg)).collect();
        arg_shapes.push(classify_expr_shape(value));
        let arg_count = arg_shapes.len();
        push_invocation_and_maybe_construction(
            "set".to_string(),
            node.start_line,
            Some(arg_count),
            arg_shapes,
            receiver_expr,
            enclosing_type,
            enclosing_method,
            index,
        );
        return;
    }
    // A compound operator on an indexed target (`m[k] += v`) is the
    // documented remaining gap: fall through without emitting anything
    // here (the plain `index_expression` arm still fires normally as a
    // `get`, since this node was never claimed above).
    if target.kind == "index_expression" {
        return;
    }
    let Some((assign_name, plain_name)) = compound_assign_convention_names(operator_node.kind.as_str())
    else {
        return;
    };
    let receiver_expr = super::kotlin_receiver::build_receiver_expr(Some(target));
    let arg_shape = classify_expr_shape(value);
    push_invocation_and_maybe_construction(
        assign_name.to_string(),
        node.start_line,
        Some(1),
        vec![arg_shape.clone()],
        receiver_expr.clone(),
        enclosing_type,
        enclosing_method,
        index,
    );
    push_invocation_and_maybe_construction(
        plain_name.to_string(),
        node.start_line,
        Some(1),
        vec![arg_shape],
        receiver_expr,
        enclosing_type,
        enclosing_method,
        index,
    );
}

/// Kotlin's unary/postfix operator-convention tokens mapped to their
/// `operator fun` convention name. `!!` (not-null assertion) is fixed
/// language semantics with no convention function and is deliberately left
/// unmapped, the same reasoning `operator_convention_name` applies to
/// `&&`/`||`/`?:`/`===`/`!==`.
fn unary_convention_name(operator: &str) -> Option<&'static str> {
    match operator {
        "!" => Some("not"),
        "+" => Some("unaryPlus"),
        "-" => Some("unaryMinus"),
        "++" => Some("inc"),
        "--" => Some("dec"),
        _ => None,
    }
}

/// `!f` / `-x` / `+x` / `x++` / `--x` -- a unary or postfix
/// operator-convention call. Grammar shape verified against a real
/// tree-sitter-kotlin-ng 1.1.0 parse dump: `unary_expression` has exactly
/// two direct children, one the operand and the other an UNNAMED operator
/// token -- PREFIX forms (`!f`, `-x`, `--c`) place the operator FIRST,
/// POSTFIX forms (`c++`) place it LAST. Kotlin's `inc`/`dec` conventions
/// apply identically whether written prefix or postfix, so the two shapes
/// are handled uniformly here by simply locating whichever child is the
/// (unnamed) operator versus the (named) operand, without needing to know
/// which position it came from.
pub(super) fn extract_unary_expression(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let children = &node.children;
    let Some((operand, operator_node)) = (match children.as_slice() {
        [a, b] if !a.is_named && b.is_named => Some((b, a)),
        [a, b] if a.is_named && !b.is_named => Some((a, b)),
        _ => None,
    }) else {
        return;
    };
    let Some(convention_name) = unary_convention_name(operator_node.text()) else {
        return;
    };
    let receiver = super::kotlin_receiver::build_receiver_expr(Some(operand));
    push_invocation_and_maybe_construction(
        convention_name.to_string(),
        node.start_line,
        Some(0),
        Vec::new(),
        receiver,
        enclosing_type,
        enclosing_method,
        index,
    );
}

/// `a..b` -> `rangeTo`, `a..<b` -> `rangeUntil` -- the range-convention
/// call. Grammar shape verified against a real tree-sitter-kotlin-ng 1.1.0
/// parse dump: `range_expression` has exactly three direct children in
/// source order -- left operand, the operator token (an UNNAMED leaf,
/// either `..` or `..<`), and right operand -- the same positional shape
/// `extract_binary_expression` destructures.
pub(super) fn extract_range_expression(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let children = &node.children;
    let Some((left, operator_node, right)) = (match children.as_slice() {
        [left, operator_node, right] if !operator_node.is_named => Some((left, operator_node, right)),
        _ => None,
    }) else {
        return;
    };
    let convention_name = match operator_node.kind.as_str() {
        ".." => "rangeTo",
        "..<" => "rangeUntil",
        _ => return,
    };
    let receiver = super::kotlin_receiver::build_receiver_expr(Some(left));
    let arg_shape = classify_expr_shape(right);
    push_invocation_and_maybe_construction(
        convention_name.to_string(),
        node.start_line,
        Some(1),
        vec![arg_shape],
        receiver,
        enclosing_type,
        enclosing_method,
        index,
    );
}

/// `x in y` / `x !in y` -- both keywords desugar to the SAME `contains`
/// convention (`!in` is a negated `.contains(...)` call, not a distinct
/// convention function). Grammar shape verified against a real
/// tree-sitter-kotlin-ng 1.1.0 parse dump: `in_expression` has exactly
/// three direct children in source order -- left operand, the keyword
/// token (an UNNAMED leaf, either `in` or `!in`), and right operand. The
/// CONTAINER is the right operand (`y` in `x in y` calls `y.contains(x)`),
/// unlike every other operator-convention call above where the receiver is
/// the LEFT operand -- this is Kotlin's own convention, not a choice made
/// here.
pub(super) fn extract_in_expression(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let children = &node.children;
    let Some((left, operator_node, right)) = (match children.as_slice() {
        [left, operator_node, right] if !operator_node.is_named => Some((left, operator_node, right)),
        _ => None,
    }) else {
        return;
    };
    if operator_node.kind != "in" && operator_node.kind != "!in" {
        return;
    }
    let receiver = super::kotlin_receiver::build_receiver_expr(Some(right));
    let arg_shape = classify_expr_shape(left);
    push_invocation_and_maybe_construction(
        "contains".to_string(),
        node.start_line,
        Some(1),
        vec![arg_shape],
        receiver,
        enclosing_type,
        enclosing_method,
        index,
    );
}
