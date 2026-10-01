//! Kotlin extraction (Bug #1908): bind levels 0-2 (declarations, references,
//! imports, inheritance) at the SAME evidentiary tier `JavaExtractor`
//! (`super::java`) reaches for those levels. Levels 3-4 (Java-specific
//! inheritance-family expansion and overload discrimination) and the
//! receiver-type substrate (level 6, `LocalIndex::typed_names`) are
//! explicitly OUT of scope -- this extractor never populates `typed_names`.
//!
//! **Bug #1920 -- the missing receiver-type substrate costs EVIDENCE, not
//! an EDGE, for an ordinary qualified call.** Every `g.helper(x)`
//! (instance-qualified, a variable receiver) and `Type.helper(x)`
//! (type-qualified) call is extracted IDENTICALLY below: both reach
//! `extract_call_expression` -> `call_expression_callee`, which builds an
//! `InvocationSite{callee_name: "helper", receiver: ReceiverExpr::
//! Identifier(text), ..}` off the SAME `navigation_expression` shape
//! regardless of whether `text` happens to name a declared type or a local
//! variable -- the extractor never branches on that distinction. At bind
//! time, `resolve_identifier_receiver` (`bind::receiver`) resolves an
//! Identifier receiver Kotlin has no `typed_names` evidence for to
//! `ReceiverEvidence::None` (an instance variable, e.g. `g`) or
//! `ReceiverEvidence::Advisory` (a bare identifier that IS itself a known
//! in-repo type name, e.g. `JavaUtil`) -- but `apply_receiver_type_
//! narrowing` (`bind::narrowing`) is PERMANENTLY tag-only (epic #1906,
//! seven review rounds, see `docs/xray-architecture.md`'s
//! candidate-admission section): it can set `RECEIVER_TYPE_MATCH` on a
//! match, but it NEVER deletes a candidate on an empty or non-matching one,
//! under either evidence tier. So the two forms differ only in whether the
//! bound edge later carries `RECEIVER_TYPE_MATCH` (confidence ranking) --
//! candidate SET membership, and therefore `is_definitely_dead_code`, is
//! receiver-shape-agnostic by construction. Verified end-to-end via
//! `build_repo_graph` (not the extractor in isolation) across 15+
//! configurations in `bug_1920_kotlin_instance_qualified_calls.rs`:
//! top-level and in-class callers, Java and Kotlin targets, same/
//! different-package decoys, a constrained `IndexBudget` (which per Bug
//! #1833 cannot affect `is_definitely_dead_code` either, since
//! `ReferencedBits` marks from the pre-truncation candidate list), a
//! budget-truncated (`index_is_complete: false`) repo, wildcard imports,
//! and the `?.`/`!!` receiver forms -- an instance-qualified call to a
//! genuinely reachable target is never reported definitely dead in any of
//! them. The one live gap this investigation DID confirm is
//! receiver-agnostic, not specific to this call form: a `private` target
//! called from a DIFFERENT top-level Kotlin type is excluded by
//! `apply_private_visibility_filter` (D2, `bind::narrowing`) regardless of
//! whether the call is instance- or type-qualified -- that is a binder
//! concern outside this extractor's scope, tracked separately rather than
//! adjusted here.
//!
//! Node-kind names below were verified against the REAL
//! tree-sitter-kotlin-ng 1.1.0 grammar output (dumped from real parsed
//! sample files covering top-level/member/extension functions, classes,
//! interfaces, objects, companion objects, anonymous objects, primary and
//! secondary constructors, properties (including constructor-promoted
//! `val`/`var` parameters, custom getters/setters, `const val`), enum
//! entries, imports (ordinary/wildcard/aliased), inheritance (superclass
//! call, bare interface, `by`-delegation), qualified/bare call expressions
//! (including trailing-lambda syntax), and callable references -- not
//! guessed.
//!
//! **The construction ambiguity, and why it is resolved by over-binding.**
//! Kotlin has no dedicated "object creation" grammar node: `Foo()` and
//! `helperFunc()` are BOTH plain `call_expression`s with a bare `identifier`
//! callee -- there is no syntactic way to tell them apart the way Java's
//! `object_creation_expression` (`new Foo()`) always can. Per the epic's
//! over-binding-is-safe mandate (#1906, #1910: "over-binding is the SAFE
//! direction; under-binding is not"), a bare OR qualified call whose FINAL
//! callee segment starts with an uppercase letter (the universal
//! Kotlin/Java class-naming convention) is conservatively treated as BOTH
//! an ordinary invocation candidate AND a construction candidate --
//! mirroring exactly what `JavaExtractor::extract_construction` already
//! does for `new Foo()` (a `ConstructionSite` PLUS a parallel
//! `InvocationSite` for the same name, see `push_invocation_and_maybe_
//! construction` below). This can never under-bind a real constructor
//! call, at the cost of occasionally over-binding a same-named function to
//! a same-named class as harmless noise -- exactly the direction the
//! mandate requires. The identical heuristic applies to Kotlin's
//! constructor-reference syntax (`::Foo`), which is ALSO syntactically
//! identical to a bare top-level function reference (`::topLevelFn`).
//!
//! **Operator-convention calls (Bug #1917, closes the #1908 follow-up gap
//! below).** `binary_expression` (`a + b`, desugaring to `a.plus(b)`) and
//! `index_expression` (`m[k]`, desugaring to `m.get(k)`/`m.set(k, v)`) ARE
//! now extracted as invocation candidates, mapped through Kotlin's own
//! finite operator-convention table (verified against the real
//! tree-sitter-kotlin-ng 1.1.0 grammar dump -- see `extract_binary_
//! expression`/`extract_index_expression`/`extract_assignment_to_index`
//! below for the exact node shapes). A `private operator fun plus(...)`/
//! `get(...)`/`set(...)` called only through its operator syntax now binds
//! a real inbound edge instead of under-binding to zero callers.
//!
//! Within `binary_expression`, only the operators Kotlin actually allows a
//! user to overload are mapped: `+`/`-`/`*`/`/`/`%` -> `plus`/`minus`/
//! `times`/`div`/`rem`; `<`/`<=`/`>`/`>=` -> `compareTo`; `==`/`!=` ->
//! `equals`. The grammar's `binary_expression` also carries `&&`, `||`,
//! `?:`, `===`, and `!==` in the same `operator` token set, but NONE of
//! those five are user-overloadable Kotlin operators (they are fixed
//! language semantics with no corresponding `operator fun` convention) --
//! mapping them to a synthesized name would fabricate a callee that can
//! never exist, so they are deliberately left unmapped (no candidate
//! emitted; their operand subtrees are still walked normally for any real
//! calls nested inside them).
//!
//! For `index_expression`, read (`m[k]`) versus write (`m[k] = v`) is
//! resolved by structural assignment context, not guessed: `m[k] = v`
//! parses as an `assignment` node whose LEFT child is the `index_
//! expression` itself (verified via the real grammar dump) -- when a plain
//! `=` assignment's target is an `index_expression`, that occurrence is
//! recorded as a `set` call (receiver + index arguments + the assigned
//! value as the final argument) and is EXCLUDED from also producing a
//! spurious `get` at the same source position (`claimed_write_targets` in
//! `extract`, keyed by the node's own `start_byte`, which is unique within
//! one parsed file). Every other occurrence of `index_expression` --
//! including as the plain right-hand VALUE of an assignment, or as the
//! target of a COMPOUND assignment (`m[k] += v`, an `assignment` node with
//! a `+=`/`-=`/`*=`/`/=`/`%=` operator) -- is recorded as `get`: compound
//! index-assignment operator conventions (`plusAssign` and friends applied
//! through an indexed target) are a distinct, more complex desugaring this
//! extractor does not attempt to disambiguate, so it falls back to the
//! always-true fact that reading via `get` is at minimum part of what such
//! an expression evaluates -- over-binding, never under-binding, per the
//! module's governing mandate below.
//!
//! **Second round (Bug #1917 follow-up): unary, range, containment, and
//! non-indexed compound assignment.** `unary_expression` (`!f`, `-x`, `+x`,
//! `x++`, `--x` -- the SAME grammar node for both prefix and postfix,
//! discriminated only by whether the operator token is the first or
//! second child) maps `!`/`+`/`-`/`++`/`--` to `not`/`unaryPlus`/
//! `unaryMinus`/`inc`/`dec`; `!!` (not-null assertion) is left unmapped --
//! it is fixed language semantics with no `operator fun` convention, the
//! same reasoning as `&&`/`||`/`?:`/`===`/`!==` above. `range_expression`
//! (`a..b`, and `a..<b` -- verified as the SAME node with a `..`-vs-`..<`
//! operator token, both real Kotlin conventions) maps to `rangeTo`/
//! `rangeUntil`. `in_expression` (`x in y`, `x !in y`) maps BOTH keyword
//! forms to `contains` (Kotlin desugars `!in` to a negated `.contains(...)`
//! call, the same convention `in` uses).
//!
//! Non-indexed compound assignment (`x += y` etc., an `assignment` node
//! whose LEFT side is NOT an `index_expression`) is genuinely ambiguous in
//! a way the other conventions above are not: Kotlin resolves `+=` to
//! `plusAssign` when that member exists, but falls back to the plain
//! `x = x.plus(y)` desugaring when it does not (legal only when `x` is a
//! mutable `var`) -- and telling these apart requires knowing whether a
//! `plusAssign` overload exists on `x`'s real type, which is receiver-type
//! evidence (level 6) this extractor does not track. Per the over-binding
//! mandate, BOTH candidates are emitted for every non-indexed compound
//! assignment (`plusAssign`+`plus`, `minusAssign`+`minus`,
//! `timesAssign`+`times`, `divAssign`+`div`, `remAssign`+`rem`) rather than
//! guessing one -- guessing wrong would under-bind the real target exactly
//! as badly as not extracting it at all.
//!
//! **Still NOT extracted (explicit, deliberate gap, not silently absent):**
//! a COMPOUND assignment onto an INDEXED target (`m[k] += v`) is not
//! disambiguated into its own desugaring (see the `index_expression`
//! paragraph above -- it still falls back to a plain `get`), and the
//! `invoke` convention (`f(x)` where `f` is a value of a type with an
//! `operator fun invoke`) is indistinguishable from an ordinary
//! bare-identifier `call_expression` without receiver-type information
//! this extractor does not track (level 6, out of scope) -- inventing a
//! decision here would fabricate an edge from syntax that is genuinely
//! silent about which case it is, the same reasoning that keeps `&&`/`||`/
//! `?:`/`===`/`!==`/`!!` unmapped. A `private operator fun` reached ONLY
//! through one of these two remaining forms still under-binds today. Do
//! not extend the covered list above without also closing one of these.
//!
//! **Bug #1937 -- a same-line `object : Type { <function member> }` is a
//! tree-sitter-kotlin-ng PARSE-RECOVERY defect, not an extractor bug.**
//! When an object-literal expression's own opening `{`, a function-member
//! declaration inside it, and its closing `}` all sit on ONE physical
//! source line (`val o = object : Runnable { override fun run() {} }`,
//! with or without `override`, with or without a var binding), the
//! currently-pinned tree-sitter-kotlin-ng 1.1.0 grammar fails to recover:
//! the WHOLE enclosing scope (getter, setter, `init` block, or plain
//! function -- confirmed for all four) collapses into a single ERROR node
//! whose materialized children stop partway through, and everything after
//! that point is absent from the tree entirely (not even present as raw
//! ERROR-child tokens) -- there is nothing left for this extractor's walk
//! to see or emit a `Declaration` for. Writing the SAME object literal
//! with its own braces on separate lines (idiomatic Kotlin formatting)
//! parses cleanly with full extraction and correct #1930 synthetic-scope
//! call attribution -- see `bug_1937_kotlin_object_literal_parse_recovery
//! .rs` for the full investigation, both broken forms from the original
//! report, the setter/`init`-block variants, and the control fixture.
//! `tree.root_node().has_error()` (`scanner::parse_file_with_error_flag`)
//! is already `true` for every broken variant, and the existing
//! language-agnostic `has_syntax_error` -> `files_with_parse_errors` ->
//! `fact_graph_complete = false` pipeline (`repo_index.rs`) already
//! surfaces this as loud degradation, never silent loss -- confirmed by
//! that same test file. Do not "fix" this by adding extractor logic: the
//! data genuinely does not exist in the parse tree.
//!
//! **Sibling-module split (issue #1936, Messi Rule 6, anti-file-bloat):**
//! this file crossed the project's 1000-line limit and was split along
//! the same seams `java.rs` already was, plus one more `kotlin.rs` itself
//! needed (its own module doc, above, is far larger than java.rs's) --
//! `kotlin_declarations.rs` (package/import handling and type-declaration
//! extraction), `kotlin_fields.rs` (property/enum-entry extraction and
//! visibility resolution), `kotlin_functions.rs` (function/secondary-
//! constructor extraction, their `WalkContext`-mutating dispatch
//! wrappers, and delegation calls), `kotlin_invocations.rs` (call/
//! construction/operator-convention extraction), `kotlin_receiver.rs`
//! (receiver-expression construction and callable references), and
//! `kotlin_type_names.rs` (type-declaration and type-reference name
//! resolution). Pure move, no behavior change: this file keeps the single
//! stack-walk entry point (`extract`), `WalkContext` itself, and its
//! dispatch table (`dispatch_node`). `dispatch_node` routes every node
//! kind whose extraction has the uniform `(node, enclosing_type,
//! enclosing_method, index)` call shape through one function-pointer
//! lookup (`uniform_expr_extractor`); every remaining node kind is routed
//! through its own small `dispatch_*` helper, so `dispatch_node` itself
//! stays a short table of one-line match arms.

use super::local_index::{LocalIndex, SyntheticScopeRecord};
use super::LanguageExtractor;
use crate::graph::identity::{make_symbol_id, SymbolId};
use crate::owned_node::OwnedNode;
use std::collections::HashSet;

pub struct KotlinExtractor;

/// Per-node resolution context threaded through `extract`'s stack walk --
/// mirrors `super::java::WalkContext` (same fields, same threading rules):
/// a type declaration resets `enclosing_method` to `None` for its own
/// children; a function/constructor/accessor keeps `enclosing_type` but
/// sets `enclosing_method` to ITS OWN symbol.
///
/// `enclosing_type_symbol` (Issue #1930 rework, item 1): the CURRENT
/// enclosing type's own interned symbol -- set by `kotlin_declarations::
/// dispatch_type_declaration` for EVERY type kind this match handles,
/// including an `object_literal` (Kotlin gives every type a real
/// `Declaration`+symbol, unlike Java's anonymous classes, which get
/// neither -- see `java.rs`'s `WalkContext::enclosing_type_symbol` and
/// its own doc comment on why that one case stays `None`). Sole
/// consumer: `LocalIndex::synthetic_scopes`, recorded when a `"getter" |
/// "setter" | "anonymous_initializer"` scope is allocated.
///
/// `pub(super)` (unlike Java's private equivalent): `kotlin_
/// declarations.rs`'s `dispatch_type_declaration` and `kotlin_
/// functions.rs`'s `dispatch_function_declaration`/`dispatch_secondary_
/// constructor` also construct/consume this type, since all three mutate
/// `WalkContext` for their own children.
#[derive(Clone)]
pub(super) struct WalkContext {
    pub(super) enclosing_type: Option<std::rc::Rc<str>>,
    pub(super) top_level_type: Option<std::rc::Rc<str>>,
    pub(super) enclosing_method: Option<SymbolId>,
    pub(super) enclosing_type_symbol: Option<SymbolId>,
}

impl WalkContext {
    fn root() -> Self {
        WalkContext {
            enclosing_type: None,
            top_level_type: None,
            enclosing_method: None,
            enclosing_type_symbol: None,
        }
    }
}

impl LanguageExtractor for KotlinExtractor {
    fn extract(&self, root: &OwnedNode, file_id: u32) -> LocalIndex {
        let mut index = LocalIndex::new();
        let mut next_local: u32 = 0;

        super::kotlin_declarations::extract_package(root, file_id, &mut next_local, &mut index);
        let aliases = super::kotlin_declarations::extract_imports(root, &mut index);

        // Bug #1917: `start_byte` values of every `index_expression` node
        // already claimed as an indexed-assignment WRITE target (`m[k] =
        // v`) by its enclosing `assignment` node -- see `dispatch_
        // assignment` below. Checked when the walk later reaches that SAME
        // node via the generic stack traversal below, so it is recorded
        // once as `set` and never a second time as a spurious `get`.
        // `start_byte` is unique per node within one parsed file (no two
        // distinct nodes share a byte span), and this set is fresh per
        // `extract()` call, so there is no cross-file leakage.
        let mut claimed_write_targets: HashSet<usize> = HashSet::new();

        let mut stack: Vec<(&OwnedNode, WalkContext)> = vec![(root, WalkContext::root())];
        // Bounded: each iteration pops one node from `stack` and pushes its
        // (finite) children; total pushes across the walk equal the tree's
        // finite node count -- mirrors `JavaExtractor::extract`'s identical
        // bound.
        while let Some((node, ctx)) = stack.pop() {
            let child_context = dispatch_node(
                node,
                file_id,
                &mut next_local,
                ctx,
                &mut claimed_write_targets,
                &mut index,
            );
            for child in &node.children {
                stack.push((child, child_context.clone()));
            }
        }

        if !aliases.is_empty() {
            super::kotlin_declarations::apply_import_aliases(&aliases, &mut index);
        }

        index
    }
}

pub(super) fn next_symbol(file_id: u32, next_local: &mut u32) -> SymbolId {
    let symbol = make_symbol_id(file_id, *next_local);
    *next_local += 1;
    symbol
}

/// A node-kind-to-extractor function pointer, for every node kind whose
/// extraction shares the uniform `(node, enclosing_type, enclosing_method,
/// index)` call shape and never mutates `WalkContext`.
type UniformExtractor = fn(&OwnedNode, Option<&str>, Option<SymbolId>, &mut LocalIndex);

/// Looks up the extraction function for a uniform-shaped node kind, so
/// `dispatch_node` can route through ONE function-pointer call instead of
/// repeating that call shape at every match arm. Node kinds needing
/// bespoke handling (a guard condition, an extra parameter, a narrower
/// call shape, or a `WalkContext` mutation) are NOT listed here -- they
/// route through their own small `dispatch_*` helper below instead.
fn uniform_expr_extractor(kind: &str) -> Option<UniformExtractor> {
    match kind {
        "call_expression" => Some(super::kotlin_invocations::extract_call_expression),
        // Bug #1908 follow-up (reviewer finding B): an infix call
        // (`a matches b`, invoking an `infix fun matches`) is its own
        // distinct grammar node, never a `call_expression` -- omitting it
        // meant every infix-only-called function under-bound to zero
        // callers. See the module doc's "known, deliberate gap" note for
        // what is still NOT covered (operator-convention calls).
        "infix_expression" => Some(super::kotlin_invocations::extract_infix_expression),
        // Bug #1917: an operator-convention binary call (`a + b`, `a ==
        // b`, ...). See the module doc for exactly which `binary_
        // expression` operators map to a convention name and which five
        // (`&&`, `||`, `?:`, `===`, `!==`) are deliberately left unmapped
        // (not user-overloadable in Kotlin).
        "binary_expression" => Some(super::kotlin_invocations::extract_binary_expression),
        // Bug #1917: a unary/postfix operator-convention call (`!f`, `-x`,
        // `+x`, `x++`, `--x`). See the module doc for the full mapping and
        // why `!!` (not-null assertion) is deliberately left unmapped.
        "unary_expression" => Some(super::kotlin_invocations::extract_unary_expression),
        // Bug #1917: `a..b` / `a..<b` -- the range-convention call.
        "range_expression" => Some(super::kotlin_invocations::extract_range_expression),
        // Bug #1917: `x in y` / `x !in y` -- both map to `contains`.
        "in_expression" => Some(super::kotlin_invocations::extract_in_expression),
        // A BARE callable reference (`::topLevelFn`, or `::Foo` -- a
        // constructor reference, per the module doc's ambiguity note).
        "callable_reference" => Some(super::kotlin_receiver::extract_bare_callable_reference),
        // `constructor(x: Int) : this(...)`/`: super(...)` -- a secondary
        // constructor's own delegation call.
        "constructor_delegation_call" => Some(super::kotlin_functions::extract_constructor_delegation_call),
        _ => None,
    }
}

/// A custom getter/setter body and a class's `init { }` block are all
/// method-shaped for context-threading purposes (a fresh synthetic
/// symbol, mirroring `JavaExtractor`'s own "block with no enclosing_
/// method yet" synthetic-scope rule) even though none of the three ever
/// gets its own `Declaration` pushed. Issue #1930 (rework, items 1/3):
/// records this scope's lexically enclosing type (and start line, the
/// narrow fallback) -- see `LocalIndex::synthetic_scopes`'s own doc
/// comment.
fn dispatch_synthetic_scope(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: &WalkContext,
    index: &mut LocalIndex,
) -> WalkContext {
    let symbol = next_symbol(file_id, next_local);
    index.synthetic_scopes.push(SyntheticScopeRecord {
        symbol,
        start_line: node.start_line,
        enclosing_type_symbol: ctx.enclosing_type_symbol,
    });
    WalkContext {
        enclosing_type: ctx.enclosing_type.clone(),
        top_level_type: ctx.top_level_type.clone(),
        enclosing_method: Some(symbol),
        enclosing_type_symbol: ctx.enclosing_type_symbol,
    }
}

/// Bug #1917: `m[k]` read via the `[]` index convention -- UNLESS this
/// exact node was already claimed as an indexed-assignment WRITE target by
/// its enclosing `assignment` (see `dispatch_assignment`, which inserts
/// into `claimed_write_targets` on the way down the stack BEFORE this node
/// is popped).
fn dispatch_index_expression(
    node: &OwnedNode,
    ctx: &WalkContext,
    claimed_write_targets: &HashSet<usize>,
    index: &mut LocalIndex,
) {
    if !claimed_write_targets.contains(&node.start_byte) {
        super::kotlin_invocations::extract_index_expression(
            node,
            ctx.enclosing_type.as_deref(),
            ctx.enclosing_method,
            index,
        );
    }
}

/// Bug #1917: `m[k] = v` (plain `=` on an indexed target, desugars to
/// `m.set(k, v)`) and `x += y`/etc. (compound assignment on a NON-indexed
/// target, desugars to BOTH `plusAssign`-family AND the plain `plus`-
/// family form -- see the module doc for why both are emitted). A
/// compound operator on an INDEXED target (`m[k] += v`) is a no-op here
/// and falls through to `dispatch_index_expression` as a `get`, the
/// documented remaining gap.
fn dispatch_assignment(
    node: &OwnedNode,
    ctx: &WalkContext,
    claimed_write_targets: &mut HashSet<usize>,
    index: &mut LocalIndex,
) {
    super::kotlin_invocations::extract_assignment(
        node,
        ctx.enclosing_type.as_deref(),
        ctx.enclosing_method,
        claimed_write_targets,
        index,
    );
}

/// A qualified callable reference (`Foo::method`, `f::method`) uses the
/// SAME `navigation_expression` node a `.`/`?.` member access does,
/// discriminated only by carrying a `::` operator token instead -- see the
/// module doc for why this can never collide with a real call (a
/// `::`-navigation is never itself the direct callee child of a
/// `call_expression` in valid Kotlin syntax). A no-op for a plain
/// (non-`::`) `navigation_expression`, mirroring `dispatch_node`'s prior
/// inline guard.
fn dispatch_navigation_expression(node: &OwnedNode, ctx: &WalkContext, index: &mut LocalIndex) {
    if node.child_by_kind("::").is_some() {
        super::kotlin_receiver::extract_navigation_callable_reference(
            node,
            ctx.enclosing_type.as_deref(),
            ctx.enclosing_method,
            index,
        );
    }
}

fn dispatch_node(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: WalkContext,
    claimed_write_targets: &mut HashSet<usize>,
    index: &mut LocalIndex,
) -> WalkContext {
    if let Some(extractor) = uniform_expr_extractor(node.kind.as_str()) {
        extractor(node, ctx.enclosing_type.as_deref(), ctx.enclosing_method, index);
        return ctx;
    }
    match node.kind.as_str() {
        "class_declaration" | "object_declaration" | "companion_object" | "object_literal" => {
            return super::kotlin_declarations::dispatch_type_declaration(node, file_id, next_local, &ctx, index);
        }
        "function_declaration" => {
            return super::kotlin_functions::dispatch_function_declaration(node, file_id, next_local, &ctx, index);
        }
        "secondary_constructor" => {
            return super::kotlin_functions::dispatch_secondary_constructor(node, file_id, next_local, &ctx, index);
        }
        "getter" | "setter" | "anonymous_initializer" => {
            return dispatch_synthetic_scope(node, file_id, next_local, &ctx, index);
        }
        "property_declaration" => {
            super::kotlin_fields::extract_property_declaration(node, file_id, next_local, index)
        }
        "enum_entry" => super::kotlin_fields::extract_enum_entry(
            node,
            file_id,
            next_local,
            ctx.enclosing_type.as_deref(),
            index,
        ),
        "index_expression" => dispatch_index_expression(node, &ctx, claimed_write_targets, index),
        "assignment" => dispatch_assignment(node, &ctx, claimed_write_targets, index),
        "navigation_expression" => dispatch_navigation_expression(node, &ctx, index),
        // Every declared-type mention (parameter/return/property types,
        // superclass/`by`-delegate types, cast/`is`/`as` targets, generic
        // type arguments) shares this ONE grammar node kind -- unlike
        // Java, which needs several (`type_identifier`, `generic_type`,
        // `scoped_type_identifier`, ...). Fires unconditionally for EVERY
        // `user_type` node the walk reaches, including ones a more
        // specific extraction (inheritance, primary-constructor property
        // types) already consumed -- harmless double-coverage, exactly
        // mirroring `JavaExtractor`'s own `"type_identifier"` dispatch arm.
        "user_type" => super::kotlin_type_names::extract_type_reference(node, ctx.enclosing_method, index),
        _ => {}
    }
    ctx
}

#[cfg(test)]
#[path = "kotlin_tests.rs"]
mod tests;
