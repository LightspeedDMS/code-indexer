//! Java extraction (Story #1787, S2, AC2): the only language IMPLEMENTED
//! in this slice (the epic's primary validation corpus is
//! Keycloak/Elasticsearch, both Java). A single explicit-stack walk of the
//! parsed tree (mirrors `crate::owned_node`'s own iterative traversal style
//! -- Rule 14, anti-unbounded-loop, requires a provable termination bound
//! on every loop) that fully populates a `LocalIndex` before returning.
//! `crate::graph::fused` is what then calls `collect_facts` on the
//! completed index -- this module never calls into that seam itself.
//!
//! Node-kind names below were verified against the REAL tree-sitter-java
//! 0.23.5 grammar output (dumped from real parsed sample files covering
//! classes, interfaces, enums, constructors, class extends/implements,
//! interface extends (multiple), imports, static imports, wildcard
//! imports, annotations, qualified and bare method invocations, and object
//! creation), not guessed.

use super::java_type_names::{
    base_name_of_type_node, base_type_name, is_plausible_java_identifier, last_named_child_of_kind,
    type_names_in_type_list,
};
use super::local_index::{
    AnnotationRecord, ConstructionSite, Declaration, DeclarationKind, ImportKind, ImportRecord,
    InheritanceKind, InheritanceRecord, InvocationSite, LocalIndex, MethodOwnerRecord,
    MethodReturnTypeRecord, NameScope, TypeNestingRecord, TypeReferenceRecord, TypedNameRecord,
    Visibility,
};
use super::LanguageExtractor;
use crate::graph::identity::{make_symbol_id, SymbolId};
use crate::owned_node::OwnedNode;

pub struct JavaExtractor;

/// AC1/AC2/AC3 (Story #1806, S2b): per-node resolution context threaded
/// through `extract`'s stack walk -- extends the pre-existing
/// `enclosing_type` (Story #1793, S4) with `enclosing_method`, the symbol
/// of the CURRENT immediately enclosing method (`None` outside any method
/// body, e.g. a field initializer). `Rc<str>` for `enclosing_type`:
/// cloned on every child push, and an `Rc` clone is a refcount bump, never
/// a fresh heap allocation; `enclosing_method` is `Copy` (`SymbolId` is a
/// plain `u64`), so cloning `WalkContext` itself stays cheap.
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

/// Dispatches ONE node to its extraction function (if any) and returns the
/// context its CHILDREN should see. A type declaration establishes a NEW
/// context (its own bare name, AND resets `enclosing_method` to `None` --
/// a nested type's own methods start their own method context) for its
/// own children; a method/constructor declaration keeps `enclosing_type`
/// but sets `enclosing_method` to ITS OWN symbol; every other node kind
/// simply inherits the context unchanged. This is why a nested class's
/// methods are attributed to the INNER type: the inner `class_declaration`
/// node overwrites the context before its own children (including its
/// methods) are pushed. Split out of `extract` to keep that function
/// under the per-function line budget.
/// Split out of `dispatch_node` (F5/F6, #1873/#1875 rework) to keep that
/// function under the per-function line budget: constructs the NEW
/// context a type declaration's own children (including a nested type's
/// methods) must see -- its own bare name, the inherited-or-newly-rooted
/// top-level type, and a reset `enclosing_method`.
fn dispatch_type_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: &WalkContext,
    index: &mut LocalIndex,
) -> WalkContext {
    extract_type_declaration(node, file_id, next_local, index);
    let enclosing_type = node
        .child_by_kind("identifier")
        .map(|n| std::rc::Rc::from(n.text()));
    let top_level_type = ctx
        .top_level_type
        .clone()
        .or_else(|| enclosing_type.clone());
    if let (Some(type_name), Some(top_level_type)) = (&enclosing_type, &top_level_type) {
        index.type_nesting.push(TypeNestingRecord {
            type_name: type_name.to_string(),
            top_level_type: top_level_type.to_string(),
        });
    }
    WalkContext {
        enclosing_type,
        top_level_type,
        enclosing_method: None,
    }
}

/// Split out of `dispatch_node` for the same reason: a method/constructor
/// declaration keeps the surrounding `enclosing_type`/`top_level_type`
/// unchanged but sets `enclosing_method` to ITS OWN symbol for its
/// children's context.
fn dispatch_method_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: &WalkContext,
    index: &mut LocalIndex,
) -> WalkContext {
    let symbol = extract_method_declaration(
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

fn dispatch_node(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: WalkContext,
    index: &mut LocalIndex,
) -> WalkContext {
    match node.kind.as_str() {
        "class_declaration"
        | "interface_declaration"
        | "annotation_type_declaration"
        | "enum_declaration"
        | "record_declaration" => dispatch_type_declaration(node, file_id, next_local, &ctx, index),
        "method_declaration" | "constructor_declaration" => {
            dispatch_method_declaration(node, file_id, next_local, &ctx, index)
        }
        "field_declaration" => {
            extract_field_declaration(
                node,
                file_id,
                next_local,
                ctx.enclosing_type.as_deref(),
                index,
            );
            ctx
        }
        "local_variable_declaration" => {
            index
                .typed_names
                .extend(super::java_receiver::local_variable_typed_names(
                    node,
                    ctx.enclosing_method,
                ));
            ctx
        }
        "method_invocation" => {
            super::java_invocations::extract_invocation(
                node,
                ctx.enclosing_type.as_deref(),
                ctx.enclosing_method,
                index,
            );
            ctx
        }
        "object_creation_expression" => {
            super::java_invocations::extract_construction(
                node,
                ctx.enclosing_type.as_deref(),
                ctx.enclosing_method,
                index,
            );
            ctx
        }
        "explicit_constructor_invocation" => {
            super::java_invocations::extract_explicit_constructor_invocation(
                node,
                ctx.enclosing_type.as_deref(),
                ctx.enclosing_method,
                index,
            );
            ctx
        }
        "type_identifier" => {
            super::java_invocations::extract_type_reference(node, index);
            ctx
        }
        "method_reference" => {
            extract_method_reference(
                node,
                ctx.enclosing_type.as_deref(),
                ctx.enclosing_method,
                index,
            );
            ctx
        }
        "marker_annotation" | "annotation" => {
            extract_annotation_usage_reference(node, index);
            ctx
        }
        _ => ctx,
    }
}

/// N2 (#1873/#1875 second-review rework): using an annotation (`@Marker`,
/// `@Marker(...)`, or a qualified `@Outer.Marker`) is a real reference to
/// the annotation TYPE's own declaration -- F6 made
/// `annotation_type_declaration` dispatch as a type declaration (so
/// `@interface` types get a symbol), but nothing emitted the matching
/// reference edge, so a created symbol with no possible inbound edge was
/// automatically reported dead. Grammar (`marker_annotation`/`annotation`):
/// `field('name', $._name)` is always either a bare `identifier` or a
/// qualified `scoped_identifier`.
///
/// N1 (#1873/#1875 third-review rework): the qualified case used to be
/// resolved via a raw-text split (`last_dot_segment`), which captures any
/// whitespace/line-break/comment token that legally sits between the dot
/// and the final identifier (e.g. `@Outer. Marker`) as part of the
/// "name" -- garbage that can never match the real declaration. Fixed by
/// taking the qualified name's LAST named `identifier` child structurally
/// (`last_named_child_of_kind`), the same fix applied to
/// `java_type_names::resolve_type_node_base_name`'s qualified-type arms,
/// validated by the same `is_plausible_java_identifier` backstop. Fires on
/// EVERY annotation usage in the file (including ones with no in-repo
/// declaration, e.g. `@Override`), which is harmless: an unresolvable type
/// reference simply resolves to an empty candidate pool downstream, a
/// no-op.
fn extract_annotation_usage_reference(node: &OwnedNode, index: &mut LocalIndex) {
    let Some(name_node) = node
        .named_children()
        .into_iter()
        .find(|c| c.kind == "identifier" || c.kind == "scoped_identifier")
    else {
        return;
    };
    let type_name = match name_node.kind.as_str() {
        "scoped_identifier" => last_named_child_of_kind(name_node, "identifier"),
        _ => Some(name_node.text().to_string()),
    };
    let Some(type_name) = type_name.filter(|name| is_plausible_java_identifier(name)) else {
        return;
    };
    index.type_references.push(TypeReferenceRecord {
        type_name,
        line: node.start_line,
    });
}

/// Issue #1873: a `method_reference` (`this::name`, `Type::name`,
/// `expr::name`, `super::name`, `Type::new`) contributes a reference edge
/// through the EXACT SAME machinery `method_invocation`/
/// `object_creation_expression` already use -- never a parallel resolver.
/// Verified real tree-sitter-java 0.23.5 grammar shape (dumped from real
/// parsed samples, not guessed): `named_children()` is `[object, name]` for
/// an ordinary reference (`this::parsePrice`, `Worker::normalize`,
/// `h::transform`, `super::greet` -- `object` is directly the `this`/
/// `super`/`identifier` node itself, exactly like `method_invocation`'s own
/// `object` field, so `receiver_expr_for` is reusable unchanged) and just
/// `[object]` for a constructor reference (`Factory::new` -- the `new`
/// keyword is an ANONYMOUS token, absent from `named_children()`).
///
/// A constructor reference is recorded as a `ConstructionSite` (`base_name_
/// of_type_node`, the single-node counterpart to `object_creation_
/// expression`'s container-search `base_type_name`), resolving against the
/// TYPE declaration exactly like `new Type()` does -- "treated consistently
/// with `object_creation_expression`" per the issue. An ordinary reference
/// is recorded as an `InvocationSite` with `arg_count: None` and empty
/// `arg_shapes` (a method reference carries no argument list at all, so
/// there is no arity evidence to narrow candidates on) -- this is exactly
/// what keeps an overloaded target's every candidate looking referenced
/// (the binder's own under-report-not-over-report contract), never
/// collapsing to one arbitrarily chosen overload.
///
/// `node`/`index` are plain Rust references (`&OwnedNode`/`&mut
/// LocalIndex`), which the language itself guarantees are never null --
/// unlike a raw pointer, there is no null state to validate, and no sibling
/// extraction function in this file (`extract_invocation`,
/// `extract_construction`, `extract_type_reference` immediately above)
/// performs such a check either.
/// Shared by `extract_method_reference`'s two constructor-reference arms
/// (`Type::new` with and without explicit type arguments): records both a
/// `ConstructionSite` and an `InvocationSite` for `object`'s resolved base
/// type name, mirroring `object_creation_expression`'s own construction
/// extraction.
fn push_constructor_reference(
    object: &OwnedNode,
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let type_name = base_name_of_type_node(object);
    index.constructions.push(ConstructionSite {
        type_name: type_name.clone(),
        line: node.start_line,
    });
    index.invocations.push(InvocationSite {
        callee_name: type_name,
        line: node.start_line,
        arg_count: None,
        arg_shapes: Vec::new(),
        receiver: crate::graph::extract::local_index::ReceiverExpr::Other,
        enclosing_type: enclosing_type.map(|t| t.to_string()),
        enclosing_method,
    });
}

fn extract_method_reference(
    node: &OwnedNode,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    match node.named_children().as_slice() {
        [object] => {
            push_constructor_reference(object, node, enclosing_type, enclosing_method, index)
        }
        // N3 (#1873/#1875 second-review rework), A15: `G::<String>new` --
        // an explicit-type-argument CONSTRUCTOR reference has the exact
        // same 2-element `[object, type_arguments]` `named_children()`
        // shape as an ordinary `[object, name]` method reference (`new` is
        // an anonymous grammar token, absent from `named_children()`
        // either way). The discriminator is the second element's kind:
        // only a real method reference's second element is the method-name
        // `identifier`.
        [object, second] if second.kind == "type_arguments" => {
            push_constructor_reference(object, node, enclosing_type, enclosing_method, index)
        }
        // Real tree-sitter-java grammar for `this::<String>m`: explicit
        // type arguments (when present, `this::<String>m`) are a named
        // child between the receiver and method name -- they constrain
        // Java's generic inference but do not alter the graph's
        // conservative named-method target set, so both shapes share one
        // arm (an or-pattern binding the SAME `object`/`name` names is
        // valid even though the two slice patterns differ in length).
        [object, name] | [object, _, name] => {
            index.invocations.push(InvocationSite {
                callee_name: name.text().to_string(),
                line: node.start_line,
                arg_count: None,
                arg_shapes: Vec::new(),
                receiver: super::java_invocations::receiver_expr_for(Some(object)),
                enclosing_type: enclosing_type.map(|t| t.to_string()),
                enclosing_method,
            });
        }
        _ => {}
    }
}

impl LanguageExtractor for JavaExtractor {
    fn extract(&self, root: &OwnedNode, file_id: u32) -> LocalIndex {
        let mut index = LocalIndex::new();
        let mut next_local: u32 = 0;

        extract_package(root, file_id, &mut next_local, &mut index);
        extract_imports(root, &mut index);

        let mut stack: Vec<(&OwnedNode, WalkContext)> = vec![(root, WalkContext::root())];
        // Bounded: each iteration pops one node from `stack` and pushes its
        // (finite) children; total pushes across the walk equal the tree's
        // finite node count -- the same bound `OwnedNode`'s own traversals
        // use (see owned_node.rs). This is the ONLY traversal of the tree
        // besides the two direct (non-recursive) top-level lookups above.
        while let Some((node, ctx)) = stack.pop() {
            let child_context = dispatch_node(node, file_id, &mut next_local, ctx, &mut index);
            for child in &node.children {
                let ctx_for_child =
                    anonymous_body_context(node, child, &child_context, file_id, &mut index)
                        .unwrap_or_else(|| child_context.clone());
                stack.push((child, ctx_for_child));
            }
        }

        index
    }
}

fn next_symbol(file_id: u32, next_local: &mut u32) -> SymbolId {
    let symbol = make_symbol_id(file_id, *next_local);
    *next_local += 1;
    symbol
}

/// F1 (#1873/#1875 rework, HIGH regression fix): the `class_body` of `new
/// Base() { ... }` or an enum constant's own `PLUS { ... }` override block
/// is itself a distinct (anonymous) type -- its methods must never be
/// attributed to the type that merely happens to syntactically surround
/// the `new`/enum-constant expression, and a `super` call inside it must
/// resolve against the REAL supertype (the constructed type, or the
/// enclosing enum), not against whatever `enclosing_type` the outer
/// context left in place. Called once per `(node, child)` pair from the
/// main walk in `extract` below, immediately before pushing `child` onto
/// the stack; returns `None` for every other child, in which case the
/// caller keeps using `dispatch_node`'s own `child_context` unchanged.
///
/// Records a synthetic `Extends` edge (subtype = a synthesized,
/// non-colliding name unique to this `(file_id, node.start_byte)` pair;
/// Java identifiers can never contain `<`/`:`/`>`) so `TypeIndex::
/// supertypes_of` sees real evidence for this anonymous type -- exactly
/// the evidence `apply_super_class_narrowing` (resolve.rs) needs to narrow
/// correctly instead of emptying the candidate set. Also records a
/// `TypeNestingRecord` mapping the anonymous type to the SAME top-level
/// type as its surrounding context, mirroring `dispatch_node`'s own
/// `class_declaration` handling -- Java's private-access domain already
/// treats a nested/anonymous class as part of its enclosing top-level
/// type's domain, so D2 (`apply_private_visibility_filter`) must see that
/// too.
fn anonymous_body_context(
    node: &OwnedNode,
    child: &OwnedNode,
    parent_context: &WalkContext,
    file_id: u32,
    index: &mut LocalIndex,
) -> Option<WalkContext> {
    if child.kind != "class_body" {
        return None;
    }
    let raw_supertype: Option<String> = match node.kind.as_str() {
        "object_creation_expression" => base_type_name(node),
        "enum_constant" => parent_context
            .enclosing_type
            .as_ref()
            .map(|t| t.to_string()),
        _ => return None,
    };
    let anon_name: std::rc::Rc<str> =
        std::rc::Rc::from(format!("<anon:{file_id}:{}>", node.start_byte));
    // N1 (#1873/#1875 second-review rework): a genuinely unparseable
    // anonymous/enum-constant supertype must still get its OWN distinct anon
    // context (never silently fall back to the syntactically-surrounding
    // type via an early `None` return here -- that is exactly the
    // misattribution bug F1 already fixed for the resolvable case) and must
    // be recorded as having incomplete supertype evidence, so downstream
    // `super`-call narrowing on it skips narrowing entirely rather than
    // trusting a possibly-missing real supertype.
    match raw_supertype {
        Some(supertype) => {
            index.inheritance.push(InheritanceRecord {
                kind: InheritanceKind::Extends,
                subtype_name: anon_name.to_string(),
                supertype_name: supertype,
                line: node.start_line,
            });
        }
        None => {
            index.incomplete_supertypes.push(anon_name.to_string());
        }
    }
    let top_level_type = parent_context
        .top_level_type
        .clone()
        .unwrap_or_else(|| anon_name.clone());
    index.type_nesting.push(TypeNestingRecord {
        type_name: anon_name.to_string(),
        top_level_type: top_level_type.to_string(),
    });
    Some(WalkContext {
        enclosing_type: Some(anon_name),
        top_level_type: Some(top_level_type),
        enclosing_method: None,
    })
}

fn extract_package(root: &OwnedNode, file_id: u32, next_local: &mut u32, index: &mut LocalIndex) {
    let Some(decl) = root.child_by_kind("package_declaration") else {
        return;
    };
    let Some(name_node) = decl
        .named_children()
        .into_iter()
        .find(|c| c.kind == "scoped_identifier" || c.kind == "identifier")
    else {
        return;
    };
    let name = name_node.text().to_string();
    let symbol = next_symbol(file_id, next_local);
    index.signatures.insert(symbol, format!("package {name}"));
    index.declarations.push(Declaration {
        kind: DeclarationKind::Package,
        name,
        line: decl.start_line,
        symbol,
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
    });
}

fn extract_imports(root: &OwnedNode, index: &mut LocalIndex) {
    for child in root
        .children
        .iter()
        .filter(|c| c.kind == "import_declaration")
    {
        // Verified real grammar output: a wildcard import's `*` is wrapped
        // in a NAMED direct child of kind "asterisk"; the anonymous "*"
        // check is defensive in case a future grammar bump flattens that
        // wrapper node away.
        let is_wildcard =
            child.child_by_kind("asterisk").is_some() || child.child_by_kind("*").is_some();
        let is_static = child.child_by_kind("static").is_some();
        let kind = if is_wildcard {
            ImportKind::Wildcard
        } else if is_static {
            ImportKind::Static
        } else {
            ImportKind::Ordinary
        };
        let path = child
            .named_children()
            .into_iter()
            .find(|c| c.kind == "scoped_identifier" || c.kind == "identifier")
            .map(|n| n.text().to_string())
            .unwrap_or_default();
        index.imports.push(ImportRecord {
            kind,
            path,
            line: child.start_line,
        });
    }
}

fn type_keyword(kind: &str) -> &'static str {
    match kind {
        "interface_declaration" | "annotation_type_declaration" => "interface",
        "enum_declaration" => "enum",
        "record_declaration" => "record",
        _ => "class",
    }
}

fn extract_type_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    index: &mut LocalIndex,
) {
    let Some(name_node) = node.child_by_kind("identifier") else {
        return;
    };
    let name = name_node.text().to_string();
    let symbol = next_symbol(file_id, next_local);

    extract_annotations_from_modifiers(node, &name, index);
    extract_inheritance(node, &name, index);
    if matches!(
        node.kind.as_str(),
        "interface_declaration" | "annotation_type_declaration"
    ) {
        index.interface_names.push(name.clone());
    }

    let signature = format!("{} {}", type_keyword(&node.kind), name);
    index.signatures.insert(symbol, signature);
    index
        .visibilities
        .insert(symbol, visibility_of_modifiers(node));
    index.declarations.push(Declaration {
        kind: DeclarationKind::Type,
        name,
        line: node.start_line,
        symbol,
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
    });
}

/// Pushes one `InheritanceRecord` of `edge_kind` for every type name found
/// in `container_kind`'s own `type_list` child (used for both a class's
/// `super_interfaces` and an interface's `extends_interfaces`, which share
/// the identical `-> type_list -> type_identifier|generic_type` shape). N1:
/// records `subtype_name` into `index.incomplete_supertypes` when the type
/// list carried an entry `type_names_in_type_list` could not resolve.
fn extract_type_list_edges(
    node: &OwnedNode,
    container_kind: &str,
    edge_kind: InheritanceKind,
    subtype_name: &str,
    index: &mut LocalIndex,
) {
    let Some(container) = node.child_by_kind(container_kind) else {
        return;
    };
    let Some(type_list) = container.child_by_kind("type_list") else {
        return;
    };
    let (names, incomplete) = type_names_in_type_list(type_list);
    for supertype_name in names {
        index.inheritance.push(InheritanceRecord {
            kind: edge_kind,
            subtype_name: subtype_name.to_string(),
            supertype_name,
            line: container.start_line,
        });
    }
    if incomplete {
        index.incomplete_supertypes.push(subtype_name.to_string());
    }
}

fn extract_inheritance(node: &OwnedNode, subtype_name: &str, index: &mut LocalIndex) {
    // Class: `class Foo extends Base` or `class Foo extends Base<String>`
    // -- single supertype, its OWN grammar shape (`superclass ->
    // type_identifier` OR `superclass -> generic_type -> type_identifier`
    // directly, no `type_list`). N1 (#1873/#1875 second-review rework): a
    // `superclass` clause that EXISTS syntactically but cannot be resolved
    // (e.g. an unhandled shape) records `subtype_name` as having incomplete
    // supertype evidence, so narrowing on it downstream never trusts a
    // possibly-missing real supertype.
    //
    // Third-review rework: this extractor's type-name model tracks only
    // BARE (unqualified) names -- `Holder.Foo` and `Outer.Foo` are two
    // genuinely distinct declared types that both resolve to the bare name
    // "Foo". When the resolved supertype's bare name textually equals the
    // subtype's OWN bare name, this extractor has no way to tell "a type
    // extends a DIFFERENT same-named type nested elsewhere" apart from "a
    // type extends itself" using only local, syntactic information -- and
    // recording it as a normal edge would create a same-name
    // self-referencing entry in `TypeIndex::direct_parents` that collides
    // with `supertypes_of`'s own self-seeded cycle guard (which seeds
    // `visited` with the subtype's own name before walking up), silently
    // dropping the real supertype relationship. Recording this case as
    // incomplete evidence instead makes `apply_super_class_narrowing` skip
    // narrowing entirely for it, which is the safe, conservative outcome.
    if let Some(superclass) = node.child_by_kind("superclass") {
        match base_type_name(superclass) {
            Some(supertype_name) if supertype_name == subtype_name => {
                index.incomplete_supertypes.push(subtype_name.to_string());
            }
            Some(supertype_name) => {
                index.inheritance.push(InheritanceRecord {
                    kind: InheritanceKind::Extends,
                    subtype_name: subtype_name.to_string(),
                    supertype_name,
                    line: superclass.start_line,
                });
            }
            None => {
                index.incomplete_supertypes.push(subtype_name.to_string());
            }
        }
    }
    // Class: `class Foo implements A, B`.
    extract_type_list_edges(
        node,
        "super_interfaces",
        InheritanceKind::Implements,
        subtype_name,
        index,
    );
    // Interface: `interface Foo extends A, B` -- unlike a class, an
    // interface can extend MULTIPLE other interfaces, hence its own
    // `extends_interfaces -> type_list` shape (verified via real grammar
    // dump), distinct from a class's single-supertype `superclass`.
    extract_type_list_edges(
        node,
        "extends_interfaces",
        InheritanceKind::Extends,
        subtype_name,
        index,
    );
}

fn extract_annotations_from_modifiers(node: &OwnedNode, target_name: &str, index: &mut LocalIndex) {
    let Some(modifiers) = node.child_by_kind("modifiers") else {
        return;
    };
    for annotation_node in modifiers
        .children
        .iter()
        .filter(|c| c.kind == "marker_annotation" || c.kind == "annotation")
    {
        let Some(name_node) = annotation_node
            .named_children()
            .into_iter()
            .find(|c| c.kind == "identifier" || c.kind == "scoped_identifier")
        else {
            continue;
        };
        index.annotations.push(AnnotationRecord {
            name: name_node.text().to_string(),
            target_name: target_name.to_string(),
            line: annotation_node.start_line,
        });
    }
}

/// Reads one `formal_parameter`/`spread_parameter` node's declared type.
/// Verified real tree-sitter-java 0.23.5 grammar shapes: `(formal_parameter
/// [modifiers]? type: (T) name: (identifier))` and `(spread_parameter
/// [modifiers]? (T) (variable_declarator name: (identifier)))` -- an
/// optional leading `modifiers` node (present for e.g. `final String s` or
/// `@NonNull int x`) is skipped explicitly rather than assumed absent, so
/// the type is "the first named child that isn't `modifiers`", never a
/// fixed position.
pub(super) fn formal_parameter_type_name(param_node: &OwnedNode) -> Option<String> {
    let type_node = param_node
        .named_children()
        .into_iter()
        .find(|c| c.kind != "modifiers")?;
    Some(base_name_of_type_node(type_node))
}

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
/// `dispatch_node` threads it into `WalkContext.enclosing_method` for
/// this method's children (nested invocations, local variable
/// declarations). The symbol is allocated BEFORE the name lookup so a
/// malformed/nameless declaration (parse-error recovery) still yields a
/// valid symbol for its children's context -- no `Declaration` is pushed
/// for it (never fabricated), but the symbol counter itself stays
/// deterministic and every child still has SOME enclosing-method handle.
fn extract_method_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    index: &mut LocalIndex,
) -> SymbolId {
    let symbol = next_symbol(file_id, next_local);
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
        .insert(symbol, visibility_of_modifiers(node));

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

fn has_modifier(modifiers: &OwnedNode, keyword: &str) -> bool {
    modifiers.children.iter().any(|c| c.kind == keyword)
}

/// Story #1835 AC1: resolves a declaration's explicit Java access
/// modifier off its own `modifiers` node, reusing the exact
/// child-node-kind check `field_declaration_kind` already relies on for
/// `static`/`final`. Absent modifiers (no `modifiers` node at all, or one
/// present but carrying none of `public`/`protected`/`private`) map to
/// `Visibility::Unknown`, NEVER to a restricted default -- see
/// `Visibility`'s own doc comment (`local_index.rs`) for why: an
/// interface/annotation-type member with no explicit modifier is
/// implicitly `public`, and this extractor does not track "is the
/// enclosing type an interface" context to tell that apart from a real
/// class member's package-private default.
fn visibility_of_modifiers(node: &OwnedNode) -> Visibility {
    let Some(modifiers) = node.child_by_kind("modifiers") else {
        return Visibility::Unknown;
    };
    if has_modifier(modifiers, "private") {
        Visibility::Private
    } else if has_modifier(modifiers, "protected") {
        Visibility::Protected
    } else if has_modifier(modifiers, "public") {
        Visibility::Public
    } else {
        Visibility::Unknown
    }
}

fn field_declaration_kind(node: &OwnedNode) -> DeclarationKind {
    let is_constant = node
        .child_by_kind("modifiers")
        .map(|m| has_modifier(m, "static") && has_modifier(m, "final"))
        .unwrap_or(false);
    if is_constant {
        DeclarationKind::Constant
    } else {
        DeclarationKind::Field
    }
}

/// Reads `field_declaration`'s own `variable_declarator` children directly
/// (verified real grammar output: they are DIRECT children, one per
/// comma-separated variable in the declaration, never nested deeper) so
/// this stays within the single bounded stack-walk in `extract` above,
/// rather than opening a second, separate traversal per field.
fn extract_field_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    enclosing_type: Option<&str>,
    index: &mut LocalIndex,
) {
    let kind = field_declaration_kind(node);
    let keyword = if matches!(kind, DeclarationKind::Constant) {
        "constant"
    } else {
        "field"
    };
    index
        .typed_names
        .extend(super::java_receiver::field_typed_names(
            node,
            enclosing_type,
        ));

    for declarator in node
        .children
        .iter()
        .filter(|c| c.kind == "variable_declarator")
    {
        let Some(name_node) = declarator.child_by_kind("identifier") else {
            continue;
        };
        let name = name_node.text().to_string();
        let symbol = next_symbol(file_id, next_local);
        index.signatures.insert(symbol, format!("{keyword} {name}"));
        index
            .visibilities
            .insert(symbol, visibility_of_modifiers(node));
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
}

#[cfg(test)]
#[path = "java_tests.rs"]
mod tests;
