//! Kotlin extraction (Bug #1908): bind levels 0-2 (declarations, references,
//! imports, inheritance) at the SAME evidentiary tier `JavaExtractor`
//! (`super::java`) reaches for those levels. Levels 3-4 (Java-specific
//! inheritance-family expansion and overload discrimination) and the
//! receiver-type substrate (level 6, `LocalIndex::typed_names`) are
//! explicitly OUT of scope -- this extractor never populates `typed_names`.
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
//! **Known, deliberate gap (Bug #1908 follow-up, reviewer finding B):**
//! operator-convention calls (`a + b` desugaring to `a.plus(b)`, `m[k]`
//! desugaring to `m.get(k)`, and every other `operator fun` convention --
//! `binary_expression`, `index_expression`, unary/compound-assignment
//! operators) are NOT extracted as invocations. Only `infix_expression`
//! (a genuine `infix fun` called via `a fn b` syntax) is handled below.
//! A `private operator fun plus(...)`/`get(...)` called only through its
//! operator syntax therefore still under-binds today -- tracked as a
//! separate, explicitly-acknowledged gap rather than silently absent.
//! Under-binding here is the same unsafe direction the rest of this
//! module goes out of its way to avoid; do not extend this list to
//! new callers without also closing this one.

use super::local_index::{
    ArgShape, ConstructionSite, Declaration, DeclarationKind, ImportKind, ImportRecord,
    InheritanceKind, InheritanceRecord, InvocationSite, LocalIndex, MethodOwnerRecord,
    ReceiverExpr, TypeNestingRecord, TypeReferenceRecord, Visibility,
};
use super::LanguageExtractor;
use crate::graph::identity::{make_symbol_id, SymbolId};
use crate::owned_node::OwnedNode;
use std::collections::HashMap;

pub struct KotlinExtractor;

/// Per-node resolution context threaded through `extract`'s stack walk --
/// mirrors `super::java::WalkContext` (same fields, same threading rules):
/// a type declaration resets `enclosing_method` to `None` for its own
/// children; a function/constructor/accessor keeps `enclosing_type` but
/// sets `enclosing_method` to ITS OWN symbol.
#[derive(Clone)]
struct WalkContext {
    enclosing_type: Option<std::rc::Rc<str>>,
    top_level_type: Option<std::rc::Rc<str>>,
    enclosing_method: Option<SymbolId>,
}

impl WalkContext {
    fn root() -> Self {
        WalkContext {
            enclosing_type: None,
            top_level_type: None,
            enclosing_method: None,
        }
    }
}

impl LanguageExtractor for KotlinExtractor {
    fn extract(&self, root: &OwnedNode, file_id: u32) -> LocalIndex {
        let mut index = LocalIndex::new();
        let mut next_local: u32 = 0;

        extract_package(root, file_id, &mut next_local, &mut index);
        let aliases = extract_imports(root, &mut index);

        let mut stack: Vec<(&OwnedNode, WalkContext)> = vec![(root, WalkContext::root())];
        // Bounded: each iteration pops one node from `stack` and pushes its
        // (finite) children; total pushes across the walk equal the tree's
        // finite node count -- mirrors `JavaExtractor::extract`'s identical
        // bound.
        while let Some((node, ctx)) = stack.pop() {
            let child_context = dispatch_node(node, file_id, &mut next_local, ctx, &mut index);
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
            WalkContext {
                enclosing_type: ctx.enclosing_type.clone(),
                top_level_type: ctx.top_level_type.clone(),
                enclosing_method: Some(symbol),
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
            extract_type_reference(node, index);
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
/// a synthesized `<anon:file:byte>` name for an anonymous object
/// expression (`object : Base() { ... }`) -- mirrors `JavaExtractor`'s own
/// F1 anonymous-class naming scheme exactly.
fn type_declaration_name(node: &OwnedNode, file_id: u32) -> std::rc::Rc<str> {
    if let Some(id) = node.child_by_kind("identifier") {
        return std::rc::Rc::from(id.text());
    }
    if node.kind == "companion_object" {
        return std::rc::Rc::from("Companion");
    }
    std::rc::Rc::from(format!("<anon:{file_id}:{}>", node.start_byte))
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
    let name = type_declaration_name(node, file_id);
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
/// fabricated) and whether ANY parameter carries the `vararg` modifier.
/// `vararg` is a SIBLING `parameter_modifiers` node immediately preceding
/// the parameter it modifies (verified real grammar shape), not nested
/// inside the `parameter` node itself -- and since only the LAST
/// parameter can legally be vararg in Kotlin, "any vararg modifier
/// present anywhere in this list" is equivalent to "the last parameter is
/// variable-arity", the exact property `Declaration::is_varargs` records.
fn extract_param_types_and_varargs(params: &OwnedNode) -> (Vec<String>, bool) {
    let mut types = Vec::new();
    let mut is_varargs = false;
    for child in params.named_children() {
        match child.kind.as_str() {
            "parameter_modifiers" => {
                if child.named_children().iter().any(|m| m.child_by_kind("vararg").is_some()) {
                    is_varargs = true;
                }
            }
            "parameter" => {
                if let Some(user_type) = child.child_by_kind("user_type") {
                    types.extend(last_identifier_text(user_type));
                }
            }
            _ => {}
        }
    }
    (types, is_varargs)
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
    index: &mut LocalIndex,
) -> SymbolId {
    let symbol = next_symbol(file_id, next_local);
    push_type_parameter_names(node, index);
    let Some(name_node) = node.child_by_kind("identifier") else {
        return symbol;
    };
    let name = name_node.text().to_string();
    let params = node.child_by_kind("function_value_parameters");
    let param_count = params.map(count_parameters).unwrap_or(0);
    let (param_types, is_varargs) = params.map(extract_param_types_and_varargs).unwrap_or_default();
    index.signatures.insert(symbol, format!("{name}({param_count} params)"));
    index.visibilities.insert(symbol, visibility_of_modifiers(node));
    index.declarations.push(Declaration {
        kind: DeclarationKind::Method,
        name,
        line: node.start_line,
        symbol,
        param_count: Some(param_count),
        param_types,
        is_varargs,
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
    let symbol =
        extract_function_declaration(node, file_id, next_local, ctx.enclosing_type.as_deref(), index);
    WalkContext {
        enclosing_type: ctx.enclosing_type.clone(),
        top_level_type: ctx.top_level_type.clone(),
        enclosing_method: Some(symbol),
    }
}

/// A `secondary_constructor` has no `identifier` child of its own (just
/// the `constructor` keyword) -- its declared NAME is the enclosing
/// type's own bare name, mirroring `JavaExtractor::extract_method_
/// declaration`'s identical convention for a Java `constructor_
/// declaration`. A constructor with no known enclosing type (malformed
/// input) still gets a symbol for its children's context, but no
/// `Declaration` is pushed -- never fabricated.
fn extract_secondary_constructor(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    index: &mut LocalIndex,
) -> SymbolId {
    let symbol = next_symbol(file_id, next_local);
    let Some(enclosing_type) = enclosing_type else {
        return symbol;
    };
    let params = node.child_by_kind("function_value_parameters");
    let param_count = params.map(count_parameters).unwrap_or(0);
    let (param_types, is_varargs) = params.map(extract_param_types_and_varargs).unwrap_or_default();
    index.signatures.insert(symbol, format!("{enclosing_type}({param_count} params)"));
    index.visibilities.insert(symbol, visibility_of_modifiers(node));
    index.declarations.push(Declaration {
        kind: DeclarationKind::Method,
        name: enclosing_type.to_string(),
        line: node.start_line,
        symbol,
        param_count: Some(param_count),
        param_types,
        is_varargs,
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
        index,
    );
    WalkContext {
        enclosing_type: ctx.enclosing_type.clone(),
        top_level_type: ctx.top_level_type.clone(),
        enclosing_method: Some(symbol),
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

fn extract_type_reference(node: &OwnedNode, index: &mut LocalIndex) {
    if let Some(name) = last_identifier_text(node) {
        index.type_references.push(TypeReferenceRecord {
            type_name: name,
            line: node.start_line,
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
