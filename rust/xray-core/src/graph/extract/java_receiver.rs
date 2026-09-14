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
pub(super) fn invocation_object_and_name(node: &OwnedNode) -> (Option<&OwnedNode>, Option<&OwnedNode>) {
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
            "this" | "super" => break ReceiverExpr::SelfOrSuper,
            "method_invocation" => {
                let (object, name) = invocation_object_and_name(node);
                let Some(name_node) = name else { return ReceiverExpr::Other };
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
        .fold(base, |acc, method_name| ReceiverExpr::Chained { method_name, receiver: Box::new(acc) })
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
    let type_node = node.named_children().into_iter().find(|c| c.kind != "modifiers" && c.kind != "type_parameters")?;
    Some(super::java::base_name_of_type_node(type_node))
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
        param_node.child_by_kind("variable_declarator")?.child_by_kind("identifier")?
    } else {
        param_node.named_children().into_iter().find(|c| c.kind == "identifier")?
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
        .map(super::java::base_name_of_type_node)
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
pub(super) fn field_typed_names(node: &OwnedNode, enclosing_type: Option<&str>) -> Vec<TypedNameRecord> {
    let Some(enclosing_type) = enclosing_type else { return Vec::new() };
    typed_names_from_declarators(node, NameScope::Field { enclosing_type: enclosing_type.to_string() })
}

/// AC1: every LOCAL VARIABLE `TypedNameRecord` declared by one
/// `local_variable_declaration` node, scoped to `enclosing_method`.
/// `Vec::new()` when `enclosing_method` is unknown (e.g. a declaration
/// outside any method body) -- never a guessed scope.
pub(super) fn local_variable_typed_names(node: &OwnedNode, enclosing_method: Option<SymbolId>) -> Vec<TypedNameRecord> {
    let Some(enclosing_method) = enclosing_method else { return Vec::new() };
    typed_names_from_declarators(node, NameScope::Local { enclosing_method })
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
        root.descendants_of_kind("method_invocation").into_iter().next().unwrap().clone()
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
        root.descendants_of_kind(kind).into_iter().next().unwrap().clone()
    }

    /// AC2: `Foo getSomething()`'s declared return type is `"Foo"`; a
    /// `void` method's is the literal text `"void"` (never fabricated,
    /// never absent for a real declared `void_type` node).
    #[test]
    fn method_return_type_name_reads_the_declared_return_type() {
        let typed = parse_first_of_kind("class First {\n    Foo getSomething() { return null; }\n}\n", "method_declaration");
        assert_eq!(method_return_type_name(&typed), Some("Foo".to_string()));

        let void_method = parse_first_of_kind("class First {\n    void run() {}\n}\n", "method_declaration");
        assert_eq!(method_return_type_name(&void_method), Some("void".to_string()));
    }

    /// AC1: both `formal_parameter` (`String s`) and `spread_parameter`
    /// (`Bar... rest`) shapes yield their real `(name, type)` pair, and a
    /// modifier/annotation on a `formal_parameter` never shifts either.
    #[test]
    fn parameter_name_and_type_reads_both_formal_and_spread_parameters() {
        let formal_parameters =
            parse_first_of_kind("class First {\n    void save(final String s, Bar... rest) {}\n}\n", "formal_parameters");
        let params: Vec<&OwnedNode> = formal_parameters.named_children();
        assert_eq!(
            parameter_name_and_type(params[0]),
            Some(("s".to_string(), "String".to_string())),
            "a formal_parameter with a modifier must still yield its real name and type"
        );
        assert_eq!(parameter_name_and_type(params[1]), Some(("rest".to_string(), "Bar".to_string())));
    }

    /// AC1: `int a, b;` yields TWO `TypedNameRecord`s (one per
    /// comma-separated declarator), both sharing the same declared type
    /// and the same `Field` scope.
    #[test]
    fn field_typed_names_reads_multiple_comma_separated_declarators() {
        let field = parse_first_of_kind("class First {\n    private int a, b;\n}\n", "field_declaration");
        let records = field_typed_names(&field, Some("First"));
        assert_eq!(records.len(), 2);
        assert!(records.iter().all(|r| r.declared_type == "int"));
        assert!(records.iter().any(|r| r.name == "a"));
        assert!(records.iter().any(|r| r.name == "b"));
        assert!(records
            .iter()
            .all(|r| r.scope == NameScope::Field { enclosing_type: "First".to_string() }));
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
