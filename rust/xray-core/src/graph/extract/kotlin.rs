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

use super::local_index::{
    ArgShape, ConstructionSite, Declaration, DeclarationKind, ImportKind, ImportRecord,
    InheritanceKind, InheritanceRecord, InvocationSite, LocalIndex, MethodOwnerRecord,
    ReceiverExpr, SyntheticScopeRecord, TypeNestingRecord, TypeReferenceRecord, Visibility,
};
use super::LanguageExtractor;
use crate::graph::identity::{make_symbol_id, SymbolId};
use crate::owned_node::OwnedNode;
use std::collections::{HashMap, HashSet};

pub struct KotlinExtractor;

/// Per-node resolution context threaded through `extract`'s stack walk --
/// mirrors `super::java::WalkContext` (same fields, same threading rules):
/// a type declaration resets `enclosing_method` to `None` for its own
/// children; a function/constructor/accessor keeps `enclosing_type` but
/// sets `enclosing_method` to ITS OWN symbol.
///
/// `enclosing_type_symbol` (Issue #1930 rework, item 1): the CURRENT
/// enclosing type's own interned symbol -- set by `dispatch_type_
/// declaration` for EVERY type kind this match handles, including an
/// `object_literal` (Kotlin gives every type a real `Declaration`+symbol,
/// unlike Java's anonymous classes, which get neither -- see `java.rs`'s
/// `WalkContext::enclosing_type_symbol` and its own doc comment on why
/// that one case stays `None`). Sole consumer: `LocalIndex::synthetic_
/// scopes`, recorded when a `"getter" | "setter" | "anonymous_
/// initializer"` scope is allocated.
#[derive(Clone)]
struct WalkContext {
    enclosing_type: Option<std::rc::Rc<str>>,
    top_level_type: Option<std::rc::Rc<str>>,
    enclosing_method: Option<SymbolId>,
    enclosing_type_symbol: Option<SymbolId>,
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

        extract_package(root, file_id, &mut next_local, &mut index);
        let aliases = extract_imports(root, &mut index);

        // Bug #1917: `start_byte` values of every `index_expression` node
        // already claimed as an indexed-assignment WRITE target (`m[k] =
        // v`) by its enclosing `assignment` node -- see `extract_
        // assignment_to_index`. Checked when the walk later reaches that
        // SAME node via the generic stack traversal below, so it is
        // recorded once as `set` and never a second time as a spurious
        // `get`. `start_byte` is unique per node within one parsed file
        // (no two distinct nodes share a byte span), and this set is fresh
        // per `extract()` call, so there is no cross-file leakage.
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
            apply_import_aliases(&aliases, &mut index);
        }

        index
    }
}

fn next_symbol(file_id: u32, next_local: &mut u32) -> SymbolId {
    let symbol = make_symbol_id(file_id, *next_local);
    *next_local += 1;
    symbol
}

fn dispatch_node(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: WalkContext,
    claimed_write_targets: &mut HashSet<usize>,
    index: &mut LocalIndex,
) -> WalkContext {
    match node.kind.as_str() {
        "class_declaration" | "object_declaration" | "companion_object" | "object_literal" => {
            dispatch_type_declaration(node, file_id, next_local, &ctx, index)
        }
        "function_declaration" => dispatch_function_declaration(node, file_id, next_local, &ctx, index),
        "secondary_constructor" => {
            dispatch_secondary_constructor(node, file_id, next_local, &ctx, index)
        }
        // A custom getter/setter body and a class's `init { }` block are
        // all method-shaped for context-threading purposes (a fresh
        // synthetic symbol, mirroring `JavaExtractor`'s own "block with no
        // enclosing_method yet" synthetic-scope rule) even though none of
        // the three ever gets its own `Declaration` pushed.
        "getter" | "setter" | "anonymous_initializer" => {
            let symbol = next_symbol(file_id, next_local);
            // Issue #1930 (rework, items 1/3): records this scope's
            // lexically enclosing type (and start line, the narrow
            // fallback) -- see `LocalIndex::synthetic_scopes`'s own doc
            // comment.
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
        "property_declaration" => {
            extract_property_declaration(node, file_id, next_local, index);
            ctx
        }
        "enum_entry" => {
            extract_enum_entry(node, file_id, next_local, ctx.enclosing_type.as_deref(), index);
            ctx
        }
        "call_expression" => {
            extract_call_expression(node, ctx.enclosing_type.as_deref(), ctx.enclosing_method, index);
            ctx
        }
        // Bug #1908 follow-up (reviewer finding B): an infix call
        // (`a matches b`, invoking an `infix fun matches`) is its own
        // distinct grammar node, never a `call_expression` -- omitting it
        // meant every infix-only-called function under-bound to zero
        // callers. See the module doc's "known, deliberate gap" note for
        // what is still NOT covered (operator-convention calls).
        "infix_expression" => {
            extract_infix_expression(node, ctx.enclosing_type.as_deref(), ctx.enclosing_method, index);
            ctx
        }
        // Bug #1917: an operator-convention binary call (`a + b`, `a ==
        // b`, ...). See the module doc for exactly which `binary_
        // expression` operators map to a convention name and which five
        // (`&&`, `||`, `?:`, `===`, `!==`) are deliberately left unmapped
        // (not user-overloadable in Kotlin).
        "binary_expression" => {
            extract_binary_expression(node, ctx.enclosing_type.as_deref(), ctx.enclosing_method, index);
            ctx
        }
        // Bug #1917: `m[k]` read via the `[]` index convention -- UNLESS
        // this exact node was already claimed as an indexed-assignment
        // WRITE target by its enclosing `assignment` (see `extract_
        // assignment_to_index`, which inserts into `claimed_write_
        // targets` on the way down the stack BEFORE this node is popped).
        "index_expression" => {
            if !claimed_write_targets.contains(&node.start_byte) {
                extract_index_expression(node, ctx.enclosing_type.as_deref(), ctx.enclosing_method, index);
            }
            ctx
        }
        // Bug #1917: `m[k] = v` (plain `=` on an indexed target, desugars
        // to `m.set(k, v)`) and `x += y`/etc. (compound assignment on a
        // NON-indexed target, desugars to BOTH `plusAssign`-family AND the
        // plain `plus`-family form -- see the module doc for why both are
        // emitted). A compound operator on an INDEXED target (`m[k] += v`)
        // is a no-op here and falls through to the plain `index_expression`
        // arm above as a `get`, the documented remaining gap.
        "assignment" => {
            extract_assignment(
                node,
                ctx.enclosing_type.as_deref(),
                ctx.enclosing_method,
                claimed_write_targets,
                index,
            );
            ctx
        }
        // Bug #1917: a unary/postfix operator-convention call (`!f`, `-x`,
        // `+x`, `x++`, `--x`). See the module doc for the full mapping and
        // why `!!` (not-null assertion) is deliberately left unmapped.
        "unary_expression" => {
            extract_unary_expression(node, ctx.enclosing_type.as_deref(), ctx.enclosing_method, index);
            ctx
        }
        // Bug #1917: `a..b` / `a..<b` -- the range-convention call.
        "range_expression" => {
            extract_range_expression(node, ctx.enclosing_type.as_deref(), ctx.enclosing_method, index);
            ctx
        }
        // Bug #1917: `x in y` / `x !in y` -- both map to `contains`.
        "in_expression" => {
            extract_in_expression(node, ctx.enclosing_type.as_deref(), ctx.enclosing_method, index);
            ctx
        }
        // A qualified callable reference (`Foo::method`, `f::method`) uses
        // the SAME `navigation_expression` node a `.`/`?.` member access
        // does, discriminated only by carrying a `::` operator token
        // instead -- see the module doc for why this can never collide
        // with a real call (a `::`-navigation is never itself the direct
        // callee child of a `call_expression` in valid Kotlin syntax).
        "navigation_expression" if node.child_by_kind("::").is_some() => {
            extract_navigation_callable_reference(
                node,
                ctx.enclosing_type.as_deref(),
                ctx.enclosing_method,
                index,
            );
            ctx
        }
        // A BARE callable reference (`::topLevelFn`, or `::Foo` -- a
        // constructor reference, per the module doc's ambiguity note).
        "callable_reference" => {
            extract_bare_callable_reference(
                node,
                ctx.enclosing_type.as_deref(),
                ctx.enclosing_method,
                index,
            );
            ctx
        }
        "constructor_delegation_call" => {
            extract_constructor_delegation_call(
                node,
                ctx.enclosing_type.as_deref(),
                ctx.enclosing_method,
                index,
            );
            ctx
        }
        // Every declared-type mention (parameter/return/property types,
        // superclass/`by`-delegate types, cast/`is`/`as` targets, generic
        // type arguments) shares this ONE grammar node kind -- unlike
        // Java, which needs several (`type_identifier`, `generic_type`,
        // `scoped_type_identifier`, ...). Fires unconditionally for EVERY
        // `user_type` node the walk reaches, including ones a more
        // specific extraction (inheritance, primary-constructor property
        // types) already consumed -- harmless double-coverage, exactly
        // mirroring `JavaExtractor`'s own `"type_identifier"` dispatch arm.
        "user_type" => {
            extract_type_reference(node, ctx.enclosing_method, index);
            ctx
        }
        _ => ctx,
    }
}

// ---------------------------------------------------------------------
// Package / imports
// ---------------------------------------------------------------------

fn extract_package(root: &OwnedNode, file_id: u32, next_local: &mut u32, index: &mut LocalIndex) {
    let Some(header) = root.child_by_kind("package_header") else {
        return;
    };
    let Some(name_node) = header.child_by_kind("qualified_identifier") else {
        return;
    };
    let name = name_node.text().to_string();
    let symbol = next_symbol(file_id, next_local);
    index.signatures.insert(symbol, format!("package {name}"));
    index.declarations.push(Declaration {
        kind: DeclarationKind::Package,
        name,
        line: header.start_line,
        symbol,
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    });
}

/// Kotlin has no static/static-wildcard import distinction (unlike Java --
/// `ImportKind::Static`/`StaticWildcard` are never produced here): any
/// import can bring in a class, a top-level function, or a top-level
/// property uniformly. `path` is always the `qualified_identifier` text
/// preceding an optional `.*`/`as alias` suffix.
///
/// Bug #1908 follow-up (P1): `as alias` is recognized by the grammar as a
/// direct `identifier` child of the `import` node itself -- verified
/// against a real tree-sitter-kotlin-ng 1.1.0 parse dump: an ordinary
/// import's direct children are `[import, qualified_identifier]`, a
/// wildcard's are `[import, qualified_identifier, ., *]`, and an ALIASED
/// import's are `[import, qualified_identifier, as, identifier]` -- the
/// alias `identifier` is never nested inside `qualified_identifier`
/// itself (that node's own `identifier` children are its dotted path
/// segments), so `child.child_by_kind("identifier")` on the `import` node
/// unambiguously means "this import is aliased" and can never
/// false-positive on an ordinary or wildcard import. A wildcard import
/// can never carry an alias in valid Kotlin syntax, so alias detection is
/// skipped entirely for that shape. Returns a bare-name alias map
/// (`alias -> real declared name`, e.g. `"alias" -> "target"`) consumed
/// by `apply_import_aliases` once the whole-file walk below has finished,
/// so every invocation/construction/type-reference written through the
/// alias resolves to the SAME declaration an unaliased call would.
fn extract_imports(root: &OwnedNode, index: &mut LocalIndex) -> HashMap<String, String> {
    let mut aliases = HashMap::new();
    for child in root.children.iter().filter(|c| c.kind == "import") {
        let is_wildcard = child.child_by_kind("*").is_some();
        let qualified = child.child_by_kind("qualified_identifier");
        let path = qualified.map(|n| n.text().to_string()).unwrap_or_default();
        let kind = if is_wildcard {
            ImportKind::Wildcard
        } else {
            ImportKind::Ordinary
        };
        if !is_wildcard {
            if let (Some(alias_node), Some(qualified)) = (child.child_by_kind("identifier"), qualified) {
                if let Some(real_name) = last_identifier_text(qualified) {
                    aliases.insert(alias_node.text().to_string(), real_name);
                }
            }
        }
        index.imports.push(ImportRecord {
            kind,
            path,
            line: child.start_line,
        });
    }
    aliases
}

/// Rewrites every invocation/construction/type-reference site whose bare
/// name is a local import ALIAS to the REAL declared name it stands for --
/// Bug #1908 follow-up (P1). A deliberate POST-pass over the whole walk's
/// output rather than a per-site lookup threaded through `WalkContext`: a
/// Kotlin import's alias is valid for the ENTIRE file (imports must
/// precede all other declarations/uses), so a single end-of-extraction
/// rewrite is equivalent to, and far simpler than, resolving each call
/// site against the alias map at the point of use. Only sites whose name
/// is an actual key are touched -- an unrelated name that happens to
/// collide with some OTHER file's alias is never affected, since this map
/// is built fresh per file.
fn apply_import_aliases(aliases: &HashMap<String, String>, index: &mut LocalIndex) {
    for site in &mut index.invocations {
        if let Some(real_name) = aliases.get(&site.callee_name) {
            site.callee_name = real_name.clone();
        }
    }
    for site in &mut index.constructions {
        if let Some(real_name) = aliases.get(&site.type_name) {
            site.type_name = real_name.clone();
        }
    }
    for site in &mut index.type_references {
        if let Some(real_name) = aliases.get(&site.type_name) {
            site.type_name = real_name.clone();
        }
    }
}

// ---------------------------------------------------------------------
// Type declarations (class/interface/enum/object/companion/anonymous)
// ---------------------------------------------------------------------

/// A declared type's bare name: its own `identifier` child when present
/// (class/interface/enum/object declarations, and a NAMED companion
/// object), `"Companion"` for an unnamed companion object (Kotlin's own
/// real default name -- accessible as `Type.Companion`, and at most one
/// per enclosing class so this can never collide within one nesting), or
/// a synthesized human-chaseable name (Bug #1929 item 3, shared with
/// `JavaExtractor`'s identical F1 anonymous-class scheme via
/// `super::synthesize_anon_type_name`) for an anonymous object expression
/// (`object : Base() { ... }`).
fn type_declaration_name(node: &OwnedNode, file_id: u32, enclosing_type: Option<&str>) -> std::rc::Rc<str> {
    if let Some(id) = node.child_by_kind("identifier") {
        return std::rc::Rc::from(id.text());
    }
    if node.kind == "companion_object" {
        return std::rc::Rc::from("Companion");
    }
    super::synthesize_anon_type_name(enclosing_type, file_id, node.start_line, node.start_byte)
}

fn is_interface(node: &OwnedNode) -> bool {
    node.child_by_kind("interface").is_some()
}

fn type_keyword(node: &OwnedNode) -> &'static str {
    match node.kind.as_str() {
        "object_declaration" | "companion_object" | "object_literal" => "object",
        _ if is_interface(node) => "interface",
        _ => "class",
    }
}

fn dispatch_type_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: &WalkContext,
    index: &mut LocalIndex,
) -> WalkContext {
    let name = type_declaration_name(node, file_id, ctx.enclosing_type.as_deref());
    let symbol = next_symbol(file_id, next_local);
    let keyword = type_keyword(node);
    index.signatures.insert(symbol, format!("{keyword} {name}"));
    index.visibilities.insert(symbol, visibility_of_modifiers(node));
    if is_interface(node) {
        index.interface_names.push(name.to_string());
    }
    index.declarations.push(Declaration {
        kind: DeclarationKind::Type,
        name: name.to_string(),
        line: node.start_line,
        symbol,
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    });
    push_type_parameter_names(node, index);

    let top_level_type = ctx.top_level_type.clone().unwrap_or_else(|| name.clone());
    index.type_nesting.push(TypeNestingRecord {
        type_name: name.to_string(),
        top_level_type: top_level_type.to_string(),
    });

    extract_inheritance(node, &name, index);
    extract_primary_constructor_properties(node, file_id, next_local, index);

    WalkContext {
        enclosing_type: Some(name),
        top_level_type: Some(top_level_type),
        enclosing_method: None,
        enclosing_type_symbol: Some(symbol),
    }
}

fn push_type_parameter_names(node: &OwnedNode, index: &mut LocalIndex) {
    let Some(type_parameters) = node.child_by_kind("type_parameters") else {
        return;
    };
    for param in type_parameters.named_children() {
        if param.kind != "type_parameter" {
            continue;
        }
        if let Some(name_node) = param.child_by_kind("identifier") {
            index.type_parameter_names.push(name_node.text().to_string());
        }
    }
}

/// Extends/implements/`by`-delegation evidence off a type declaration's own
/// `delegation_specifiers` (shared by `class_declaration`/`object_literal`;
/// a no-op when absent, e.g. `object_declaration`/`companion_object` with
/// no supertype clause). Three shapes, discriminated structurally:
/// - `constructor_invocation` (a real superclass call, `Base()`) -> Extends
///   -- the ONLY shape that unambiguously means "this is the superclass".
/// - `explicit_delegation` (`Type by delegate`) -> Implements.
/// - a bare `user_type` with neither -> Implements. This is the common
///   interface-implementation shape, but it is ALSO what a superclass with
///   NO primary constructor produces (Kotlin then requires the actual
///   super-call to live in a secondary constructor's own delegation
///   instead) -- an unresolvable ambiguity from local syntax alone.
///   Defaulting to Implements never affects level 0-2 correctness (this
///   extractor's scope): `InheritanceKind` only feeds Java-specific
///   inheritance-family expansion (level 3), explicitly out of scope here.
fn extract_inheritance(node: &OwnedNode, subtype_name: &str, index: &mut LocalIndex) {
    let Some(specs) = node.child_by_kind("delegation_specifiers") else {
        return;
    };
    for spec in specs.named_children() {
        if spec.kind != "delegation_specifier" {
            continue;
        }
        let (kind, type_node) = if let Some(ctor) = spec.child_by_kind("constructor_invocation") {
            (InheritanceKind::Extends, ctor.child_by_kind("user_type"))
        } else if let Some(deleg) = spec.child_by_kind("explicit_delegation") {
            (InheritanceKind::Implements, deleg.child_by_kind("user_type"))
        } else {
            (InheritanceKind::Implements, spec.child_by_kind("user_type"))
        };
        // A same-named supertype cannot be distinguished from a genuine
        // self-reference using only bare, generic-stripped names (two
        // distinct nested types sharing one simple name) -- recorded as
        // incomplete evidence, mirroring `JavaExtractor::extract_
        // inheritance`'s identical N1 guard, rather than risking a
        // same-name self-loop entry.
        match type_node.and_then(last_identifier_text) {
            Some(supertype_name) if supertype_name != subtype_name => {
                index.inheritance.push(InheritanceRecord {
                    kind,
                    subtype_name: subtype_name.to_string(),
                    supertype_name,
                    line: spec.start_line,
                });
            }
            _ => {
                index.incomplete_supertypes.push(subtype_name.to_string());
            }
        }
    }
}

/// A primary constructor's `val`/`var`-marked parameters are real
/// PROPERTIES of the declared type (`class Point(val x: Int)` makes `x`
/// referenceable as `Point.x`); a plain parameter with neither keyword is
/// constructor-scoped only and is deliberately NOT recorded as a
/// declaration here.
fn extract_primary_constructor_properties(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    index: &mut LocalIndex,
) {
    let Some(params) = node
        .child_by_kind("primary_constructor")
        .and_then(|pc| pc.child_by_kind("class_parameters"))
    else {
        return;
    };
    for param in params.named_children() {
        if param.kind != "class_parameter" {
            continue;
        }
        let is_property = param.child_by_kind("val").is_some() || param.child_by_kind("var").is_some();
        if !is_property {
            continue;
        }
        let Some(name_node) = param.child_by_kind("identifier") else {
            continue;
        };
        let name = name_node.text().to_string();
        let symbol = next_symbol(file_id, next_local);
        index.signatures.insert(symbol, format!("property {name}"));
        index.visibilities.insert(symbol, visibility_of_modifiers(param));
        index.declarations.push(Declaration {
            kind: DeclarationKind::Field,
            name,
            line: param.start_line,
            symbol,
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
            vararg_index: None,
        });
    }
}

// ---------------------------------------------------------------------
// Functions / constructors
// ---------------------------------------------------------------------

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
                let pushed_type = child.child_by_kind("user_type").and_then(last_identifier_text);
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
/// declaration`'s own extraction and `dispatch_function_declaration`'s
/// context threading, mirroring `JavaExtractor::extract_method_
/// declaration`'s split. Works uniformly for a member function, a
/// top-level function, AND an extension function (`fun Point.dist()`):
/// the extension receiver's own `user_type` never collides with the
/// direct-child `identifier` lookup below, since it is nested one level
/// deeper (inside `user_type`, not a direct child of `function_
/// declaration` itself) -- verified against the real grammar dump.
fn extract_function_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    enclosing_type_symbol: Option<SymbolId>,
    index: &mut LocalIndex,
) -> SymbolId {
    let symbol = next_symbol(file_id, next_local);
    push_type_parameter_names(node, index);
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
    index.visibilities.insert(symbol, visibility_of_modifiers(node));
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

fn dispatch_function_declaration(
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
fn extract_secondary_constructor(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    enclosing_type_symbol: Option<SymbolId>,
    index: &mut LocalIndex,
) -> SymbolId {
    let symbol = next_symbol(file_id, next_local);
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
    index.visibilities.insert(symbol, visibility_of_modifiers(node));
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

fn dispatch_secondary_constructor(
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
/// own ambiguity default classifies as `Implements` (see `extract_
/// inheritance`'s doc comment: bare local syntax cannot tell a superclass
/// from an interface). So every real `super(...)` call site missed its
/// only recorded edge -- a dead branch reachable by no legal Kotlin input.
/// Fixed by accepting ANY recorded supertype for the enclosing type
/// (regardless of `InheritanceKind`) and emitting one candidate per
/// DISTINCT name: `super(...)` can target only the true superclass, but
/// when a bare specifier list also contains implemented interfaces this
/// extractor cannot always tell which entry is which from local syntax
/// alone -- per the over-binding-is-safe mandate, emitting all of them is
/// the safe direction (a spurious interface edge is harmless noise; a
/// missing superclass edge is a false dead-code verdict).
fn extract_constructor_delegation_call(
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
    let (arg_count, arg_shapes) = arg_count_and_shapes(node);
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

// ---------------------------------------------------------------------
// Properties / enum entries
// ---------------------------------------------------------------------

fn has_property_modifier(modifiers: &OwnedNode, keyword: &str) -> bool {
    modifiers
        .children
        .iter()
        .any(|c| c.kind == "property_modifier" && c.child_by_kind(keyword).is_some())
}

fn extract_property_declaration(
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
    let symbol = next_symbol(file_id, next_local);
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
fn extract_enum_entry(
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
    let symbol = next_symbol(file_id, next_local);
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

// ---------------------------------------------------------------------
// Calls / references
// ---------------------------------------------------------------------

fn starts_with_uppercase(name: &str) -> bool {
    name.chars().next().is_some_and(|c| c.is_uppercase())
}

/// A simple identifier receiver, `this`/`super`, or `Other` for anything
/// this extractor does not attempt to type (a chained call, a
/// parenthesized/cast/null-asserted expression, ...) -- never fabricated.
/// Coarser than `JavaExtractor`'s own receiver classification (no
/// `Chained` variant) because, absent `typed_names` (out of scope, level
/// 6), a richer receiver shape here would carry no resolvable evidence
/// anyway.
fn build_receiver_expr(node: Option<&OwnedNode>) -> ReceiverExpr {
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
            let receiver = build_receiver_expr(named.first().copied());
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
fn arg_count_and_shapes(node: &OwnedNode) -> (Option<usize>, Vec<ArgShape>) {
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
fn push_invocation_and_maybe_construction(
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

fn extract_call_expression(
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
fn extract_infix_expression(
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
    let receiver = build_receiver_expr(Some(left));
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
fn extract_binary_expression(
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
    let receiver = build_receiver_expr(Some(left));
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
fn extract_index_expression(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let named = node.named_children();
    let Some((receiver, index_args)) = named.split_first() else {
        return;
    };
    let receiver_expr = build_receiver_expr(Some(*receiver));
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
fn extract_assignment(
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
        let receiver_expr = build_receiver_expr(Some(*receiver));
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
    let receiver_expr = build_receiver_expr(Some(target));
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
fn extract_unary_expression(
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
    let receiver = build_receiver_expr(Some(operand));
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
fn extract_range_expression(
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
    let receiver = build_receiver_expr(Some(left));
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
fn extract_in_expression(
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
    let receiver = build_receiver_expr(Some(right));
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

/// `Foo::instanceMethod` / `f::instanceMethod` -- a QUALIFIED callable
/// reference, used as a value (never itself a call). No argument-list
/// evidence exists for a bare reference (`arg_count: None`, empty
/// `arg_shapes`), mirroring `JavaExtractor::extract_method_reference`.
fn extract_navigation_callable_reference(
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
    push_invocation_and_maybe_construction(
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
fn extract_bare_callable_reference(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let Some(name_node) = node.child_by_kind("identifier") else {
        return;
    };
    push_invocation_and_maybe_construction(
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

/// A `user_type`'s bare, generics-stripped, rightmost segment name: its
/// LAST direct `identifier` child. Works uniformly for a simple name
/// (`Foo`), a qualified/dotted name (`com.example.Foo` -- the grammar
/// decomposes this into multiple sibling `identifier` children joined by
/// anonymous `.` tokens, unlike Java's single-node `scoped_type_
/// identifier`), and a generic name (`List<String>` -- `String` lives two
/// levels deeper, inside `type_arguments -> type_projection -> user_type`,
/// never a DIRECT child of the outer `user_type`, so it is never picked
/// up here by accident).
fn last_identifier_text(node: &OwnedNode) -> Option<String> {
    node.children
        .iter()
        .rev()
        .find(|c| c.kind == "identifier")
        .map(|c| c.text().to_string())
}

fn extract_type_reference(
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

// ---------------------------------------------------------------------
// Visibility
// ---------------------------------------------------------------------

/// Unlike Java (whose no-modifier default is context-dependent --
/// `Visibility::Unknown` because a class member's real default,
/// package-private, differs from an interface member's implicit public),
/// Kotlin's no-explicit-modifier default is UNAMBIGUOUSLY `public` in
/// every declaration position this extractor covers -- so absent evidence
/// here safely maps to `Public` directly, not `Unknown`. `internal`
/// (module-scoped) conservatively maps to `Unknown`: this repo-only
/// analysis cannot prove it is unreachable from outside the compiled
/// module, so it must never be treated as `Private`-equivalent.
fn visibility_of_modifiers(node: &OwnedNode) -> Visibility {
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

#[cfg(test)]
#[path = "kotlin_tests.rs"]
mod tests;
