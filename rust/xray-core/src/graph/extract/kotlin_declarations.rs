//! Kotlin package/import handling and type-declaration extraction, split
//! out of `kotlin.rs` (Messi Rule 6, anti-file-bloat -- issue #1936).
//! Owns package/import parsing (including import-alias detection and the
//! end-of-file alias rewrite pass) and everything about a type
//! declaration (class/interface/enum/object/companion/anonymous):
//! its own `Declaration`, inheritance evidence, primary-constructor
//! property promotion, and the `WalkContext` it hands to its children.
//! `dispatch_type_declaration` is the one function here that needs
//! `kotlin::WalkContext` (it mutates context for its own children), so it
//! stays paired with its own siblings rather than moving to a
//! `WalkContext`-free module.

use super::kotlin::WalkContext;
use super::local_index::{
    Declaration, DeclarationKind, ImportKind, ImportRecord, InheritanceKind, InheritanceRecord,
    LocalIndex, TypeNestingRecord,
};
use crate::owned_node::OwnedNode;
use std::collections::HashMap;

pub(super) fn extract_package(root: &OwnedNode, file_id: u32, next_local: &mut u32, index: &mut LocalIndex) {
    let Some(header) = root.child_by_kind("package_header") else {
        return;
    };
    let Some(name_node) = header.child_by_kind("qualified_identifier") else {
        return;
    };
    let name = name_node.text().to_string();
    let symbol = super::kotlin::next_symbol(file_id, next_local);
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
/// by `apply_import_aliases` once the whole-file walk has finished, so
/// every invocation/construction/type-reference written through the
/// alias resolves to the SAME declaration an unaliased call would.
pub(super) fn extract_imports(root: &OwnedNode, index: &mut LocalIndex) -> HashMap<String, String> {
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
                if let Some(real_name) = super::kotlin_type_names::last_identifier_text(qualified) {
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
pub(super) fn apply_import_aliases(aliases: &HashMap<String, String>, index: &mut LocalIndex) {
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

pub(super) fn dispatch_type_declaration(
    node: &OwnedNode,
    file_id: u32,
    next_local: &mut u32,
    ctx: &WalkContext,
    index: &mut LocalIndex,
) -> WalkContext {
    let name = super::kotlin_type_names::type_declaration_name(node, file_id, ctx.enclosing_type.as_deref());
    let symbol = super::kotlin::next_symbol(file_id, next_local);
    let keyword = type_keyword(node);
    index.signatures.insert(symbol, format!("{keyword} {name}"));
    index
        .visibilities
        .insert(symbol, super::kotlin_fields::visibility_of_modifiers(node));
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

pub(super) fn push_type_parameter_names(node: &OwnedNode, index: &mut LocalIndex) {
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
        match type_node.and_then(super::kotlin_type_names::last_identifier_text) {
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
        let symbol = super::kotlin::next_symbol(file_id, next_local);
        index.signatures.insert(symbol, format!("property {name}"));
        index
            .visibilities
            .insert(symbol, super::kotlin_fields::visibility_of_modifiers(param));
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
