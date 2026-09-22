//! Java method/constructor declaration extraction, split out of `java.rs`
//! (Messi Rule 6, anti-file-bloat -- `java.rs` crossed the project's
//! 1000-line limit once #1922 added `constant_declaration` support and
//! `java_fields.rs`'s own split alone was not enough) mirroring `java_
//! receiver.rs`/`java_invocations.rs`/`java_type_names.rs`/`java_fields.
//! rs`'s own sibling-module split. Owns `method_declaration`/`constructor_
//! declaration` metadata extraction: the declared parameter type list,
//! the method's own `Declaration`, its owner/return-type/parameter-typed-
//! name records.

use super::java::{formal_parameter_type_name, next_symbol, push_type_parameter_names};
use super::java_annotations::{extract_annotations_from_modifiers, push_method_source_invocations};
use super::local_index::{
    Declaration, DeclarationKind, LocalIndex, MethodOwnerRecord, MethodReturnTypeRecord,
    NameScope, SyntheticScopeRecord, TypedNameRecord, Visibility,
};
use crate::graph::identity::SymbolId;
use crate::owned_node::OwnedNode;
use std::collections::HashMap;

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
/// `java.rs::dispatch_node` threads it into `WalkContext.enclosing_
/// method` for this method's children (nested invocations, local
/// variable declarations). The symbol is allocated BEFORE the name
/// lookup so a malformed/nameless declaration (parse-error recovery)
/// still yields a valid symbol for its children's context -- no
/// `Declaration` is pushed for it (never fabricated), but the symbol
/// counter itself stays deterministic and every child still has SOME
/// enclosing-method handle.
///
/// Issue #1930: that handle is registered as a
/// `SyntheticScopeRecord` (same mechanism a static/instance initializer
/// block uses) carrying `enclosing_type_symbol`, so `bind::resolve::
/// enclosing_symbol_for_site` attributes a call inside this malformed
/// method to its REAL enclosing type -- never the debug-only "should be
/// impossible" fallback branch, which this exact shape used to reach.
pub(super) fn extract_method_declaration(
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
        index.synthetic_scopes.push(SyntheticScopeRecord {
            symbol,
            start_line: node.start_line,
            enclosing_type_symbol,
        });
        return symbol;
    };
    let name = name_node.text().to_string();

    extract_annotations_from_modifiers(node, &name, index);
    if node.kind == "constructor_declaration" {
        // Bug #1926: recorded for the end-of-file postprocess
        // (`mark_lone_private_no_arg_constructors` below) -- `Declaration`
        // itself cannot distinguish a constructor from an ordinary method
        // (both use `DeclarationKind::Method`), so this is the only place
        // that fact, paired with its owning type's OWN symbol, is ever
        // visible.
        index.constructor_owners.push((symbol, enclosing_type_symbol));
    } else if node.kind == "method_declaration" {
        // Bug #1926: owner-symbol record for the SAME reason
        // `constructor_owners` above needs one -- `resolve_method_source_
        // edges` must resolve a `@MethodSource` reference against the
        // annotated method's own owning TYPE, never a bare-name guess.
        index.method_owner_symbols.push((symbol, enclosing_type_symbol));
        // Bug #1926: `@MethodSource` only ever targets a test METHOD, never
        // a constructor.
        push_method_source_invocations(node, &name, symbol, enclosing_type, enclosing_type_symbol, index);
    }

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
        .insert(symbol, super::java_fields::visibility_of_modifiers(node));

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
    const JAVA_LANG_QUALIFIER: &str = "java.lang";
    for param in formal_parameters.named_children() {
        let Some((name, declared_type)) = super::java_receiver::parameter_name_and_type(param)
        else {
            continue;
        };
        // Recorded BEFORE `name` is moved into the `TypedNameRecord` below
        // -- see `LocalIndex::parameter_typed_names`'s own doc comment for
        // why this is the ONE site `RECEIVER_TYPE_MISMATCH` tagging trusts
        // to tell a real parameter apart from a same-method local
        // variable.
        index.parameter_typed_names.push((enclosing_method, name.clone()));
        // #1924 (p12): recorded the SAME way, for the SAME reason -- see
        // `LocalIndex::qualified_non_java_lang_parameter_types`'s own doc
        // comment.
        if let Some(prefix) = super::java_receiver::parameter_qualified_type_prefix(param) {
            if prefix != JAVA_LANG_QUALIFIER {
                index
                    .qualified_non_java_lang_parameter_types
                    .push((enclosing_method, name.clone()));
            }
        }
        index.typed_names.push(TypedNameRecord {
            name,
            declared_type,
            scope: NameScope::Local { enclosing_method },
        });
    }
}

/// Bug #1926: a `private Foo() {}` no-arg
/// constructor that is the ONLY constructor its class declares is the
/// standard Java idiom for an intentionally non-instantiable utility class
/// (`Collections`-style holders) -- "unreferenced" is the INTENDED state,
/// not evidence of dead code. `CodeGraph::is_definitely_dead_code`
/// (`csr/code_graph.rs`) has no way to see "is this the class's only
/// constructor" on its own: that fact lives only in THIS file's own
/// extraction and never survives past `LocalIndex` on its own. So this
/// runs once per file, called from `JavaExtractor::extract` AFTER the main
/// stack walk has populated every constructor's `Declaration`,
/// `Visibility`, and `constructor_owners` entry, and records exactly the
/// qualifying constructors' symbols into `index.non_instantiable_
/// constructors` -- a SEPARATE carried fact, never a rewrite of the
/// constructor's own `Visibility` (see that field's doc comment on
/// `LocalIndex` for why: `visibility_of()` is documented as "declared
/// visibility" and also feeds `bind::narrowing::apply_private_visibility_
/// filter`'s candidate admission, an unrelated concern this fix must not
/// perturb). A private constructor with ANY sibling constructor (a
/// public/protected overload, or another private one) is left untouched
/// -- only a class's genuinely LONE, no-arg constructor qualifies -- and a
/// lone private constructor that takes parameters is left untouched too
/// (it is not the non-instantiability idiom).
///
/// Counts constructors per OWNING TYPE SYMBOL (`constructor_owners`'
/// second element), never the bare `MethodOwnerRecord.enclosing_type`
/// name: two distinct nested classes can share a bare name (e.g. two
/// different `Inner` types under two different outer classes), and
/// counting by name alone would wrongly merge their constructors into one
/// pool. A constructor whose owning type symbol is unknown (`None`) is
/// conservatively never marked (matches every other "no evidence, stay
/// undecided" default in this predicate).
pub(super) fn mark_lone_private_no_arg_constructors(index: &mut LocalIndex) {
    if index.constructor_owners.is_empty() {
        return;
    }
    let mut ctors_per_type: HashMap<SymbolId, u32> = HashMap::new();
    for &(_, owner) in &index.constructor_owners {
        if let Some(owner) = owner {
            *ctors_per_type.entry(owner).or_insert(0) += 1;
        }
    }
    let param_count_of: HashMap<SymbolId, Option<usize>> = index
        .declarations
        .iter()
        .map(|d| (d.symbol, d.param_count))
        .collect();
    for &(symbol, owner) in &index.constructor_owners {
        let Some(owner) = owner else {
            continue;
        };
        if ctors_per_type.get(&owner).copied().unwrap_or(0) != 1 {
            continue;
        }
        if param_count_of.get(&symbol).copied().flatten() != Some(0) {
            continue;
        }
        if index.visibilities.get(&symbol) != Some(&Visibility::Private) {
            continue;
        }
        index.non_instantiable_constructors.push(symbol);
    }
}

/// Bug #1926 (final round): resolves every recorded `@MethodSource`
/// request DIRECTLY against its own annotated method's owning type --
/// never through the generic name-based binder. Runs once per file, called
/// from `JavaExtractor::extract` AFTER the main stack walk has populated
/// every method's `Declaration` and `method_owner_symbols` entry (a
/// same-owner sibling method declared LATER in the source is only visible
/// once the whole file has been walked).
///
/// For each request, builds the owner's own `(name -> zero-arg method
/// symbol)` map from `declarations`/`method_owner_symbols` restricted to
/// `owner_type_symbol` -- an outer class, a sibling nested class, or a
/// method in another file can never even be considered, since they are
/// simply absent from this exact-owner map. A target name with NO match in
/// the owner emits NOTHING (no cross-owner guess). A target name whose
/// ONLY match is the annotated method's own symbol ALSO emits nothing --
/// JUnit5's same-name default always names a DIFFERENT factory method, so
/// a self-match here means no real target exists, never a self-reference
/// that would hide a genuinely dead annotated method.
pub(super) fn resolve_method_source_edges(index: &mut LocalIndex) {
    if index.method_source_requests.is_empty() {
        return;
    }
    let owner_of_method: HashMap<SymbolId, Option<SymbolId>> =
        index.method_owner_symbols.iter().copied().collect();
    let mut zero_arg_by_owner_and_name: HashMap<(SymbolId, &str), SymbolId> = HashMap::new();
    for declaration in &index.declarations {
        if declaration.kind != DeclarationKind::Method || declaration.param_count != Some(0) {
            continue;
        }
        let Some(Some(owner)) = owner_of_method.get(&declaration.symbol).copied() else {
            continue;
        };
        zero_arg_by_owner_and_name.insert((owner, declaration.name.as_str()), declaration.symbol);
    }
    for request in &index.method_source_requests {
        let Some(owner) = request.owner_type_symbol else {
            continue;
        };
        for name in &request.target_names {
            let Some(&target) = zero_arg_by_owner_and_name.get(&(owner, name.as_str())) else {
                continue;
            };
            if target == request.from_method {
                continue;
            }
            index.method_source_edges.push(target);
        }
    }
}
