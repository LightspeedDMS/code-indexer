//! Shared Java type-name resolution helpers, split out of `java.rs` (N1/N3,
//! #1873/#1875 second-review rework) to keep both files under the project's
//! 1000-line-per-file limit. These are the ONLY functions in the extractor
//! that turn a raw tree-sitter TYPE node (or a container holding one) into
//! its simple, generic-stripped, qualification-stripped base name -- every
//! call site that needs "the type name a node stands for" (superclass
//! extraction, implements/extends_interfaces lists, constructor-reference
//! targets, cast/constructor argument shapes) goes through here so the
//! qualified-name (`Outer.Inner`, the `field_access` shape
//! `Outer.Inner::new` parses to) and annotated-type (`@Ann Base`) handling
//! never drifts between call sites.
//!
//! Third-review regression fix (N1 implementation defect, #1873/#1875):
//! the qualified-name arms below used to resolve via `text.rsplit('.')`
//! against the node's RAW SOURCE TEXT. That is wrong whenever whitespace, a
//! line break, a type annotation, or a comment token falls between the dot
//! and the final identifier -- all of those are legal Java syntax inside a
//! `scoped_type_identifier`/`field_access` node's byte span, and none of
//! them are consumed by a plain text split. The result was `Some(garbage)`
//! (e.g. `" Base"`, `"\n    Base"`, `"@Ann Base"`, `"/*c*/Base"`) rather
//! than `None`, which silently defeated the N1 "incomplete supertype
//! evidence" safety net below (it only engages on `None`) and poisoned the
//! recorded supertype/reference name with a string that can never match
//! any real declaration. Fixed by resolving from the PARSE TREE structure
//! (the last relevant named child) instead of splitting raw text, plus an
//! identifier-shape validation backstop for defense-in-depth against any
//! other shape not specifically handled here.

use crate::owned_node::OwnedNode;

/// Returns the text of the LAST direct named child of `node` whose kind is
/// `child_kind`, ignoring every other named child (annotations, comments,
/// nested qualifiers) that may sit between the meaningful segments of a
/// qualified name. This is the structural replacement for splitting a
/// node's raw source text on `.`: whitespace and line breaks are never
/// nodes at all, and a comment/annotation token that legally appears
/// between the dot and the final identifier IS a real named child, so
/// filtering by kind and taking the last match naturally skips it without
/// needing to know about it specifically.
pub(super) fn last_named_child_of_kind(node: &OwnedNode, child_kind: &str) -> Option<String> {
    node.named_children()
        .into_iter()
        .rfind(|c| c.kind == child_kind)
        .map(|c| c.text().to_string())
}

/// N1 (#1873/#1875 third-review rework) backstop: does `name` look like a
/// real Java identifier (first character a letter/`_`/`$`, every
/// subsequent character alphanumeric/`_`/`$`, non-empty)? This is
/// deliberately loose (not the full JLS identifier grammar) -- it exists
/// only to catch garbage that slipped through as `Some(...)` from a shape
/// this module does not yet handle correctly, so that callers treat it as
/// `None` (incomplete evidence) instead of trusting an unmatchable name.
/// A genuine tree-sitter `type_identifier`/`identifier` leaf always passes
/// this check; only a raw-text artifact (leading whitespace, an embedded
/// comment, stray punctuation) can fail it.
pub(super) fn is_plausible_java_identifier(name: &str) -> bool {
    let mut chars = name.chars();
    match chars.next() {
        Some(c) if c.is_alphabetic() || c == '_' || c == '$' => {}
        _ => return false,
    }
    chars.all(|c| c.is_alphanumeric() || c == '_' || c == '$')
}

/// Resolves a TYPE NODE ITSELF (not a container search -- see
/// `base_type_name` below for that) to its base name. Handles every real
/// tree-sitter-java shape a Java type/annotation-usage/method-reference-
/// object position can take: a bare `type_identifier`; a `generic_type`
/// wrapping either a plain `type_identifier` or a qualified
/// `scoped_type_identifier` (e.g. `Outer.Base<String>`); a qualified
/// `scoped_type_identifier` on its own (e.g. `Outer.Base`, `implements
/// Outer.Iface`) -- resolved via its last named `type_identifier` child,
/// never a raw-text split, so a comment/annotation/whitespace token
/// legally sitting between the dot and the final identifier is never
/// mistaken for part of the name; an `annotated_type` (`@Ann Base`) --
/// recurses into its own LAST named child, which the grammar guarantees is
/// always the underlying `_unannotated_type` (`repeat1($._annotation)
/// $._unannotated_type`), never an annotation; and `field_access` (e.g.
/// `Outer.Inner` as parsed on the LEFT of a constructor method-reference
/// `Outer.Inner::new` -- tree-sitter's GLR parser resolves that position as
/// `primary_expression` -> `field_access`, not `scoped_type_identifier`,
/// verified via a real grammar dump), resolved the same structural way via
/// its last named `identifier` child. `None` for a node kind this
/// extractor genuinely does not understand as a type, OR when the
/// resolved candidate fails the `is_plausible_java_identifier` backstop --
/// NEVER a fabricated or raw-text-contaminated name.
pub(super) fn resolve_type_node_base_name(type_node: &OwnedNode) -> Option<String> {
    let candidate = match type_node.kind.as_str() {
        "type_identifier" => Some(type_node.text().to_string()),
        "generic_type" => type_node
            .child_by_kind("type_identifier")
            .map(|t| t.text().to_string())
            .or_else(|| {
                type_node
                    .child_by_kind("scoped_type_identifier")
                    .and_then(resolve_type_node_base_name)
            }),
        "scoped_type_identifier" => last_named_child_of_kind(type_node, "type_identifier"),
        "field_access" => last_named_child_of_kind(type_node, "identifier"),
        "annotated_type" => type_node
            .named_children()
            .into_iter()
            .last()
            .and_then(resolve_type_node_base_name),
        _ => None,
    };
    candidate.filter(|name| is_plausible_java_identifier(name))
}

/// Resolves a CONTAINER's (`superclass`, an `object_creation_expression`)
/// direct child type node to its base name -- searches for the FIRST of
/// `type_identifier`/`generic_type`/`scoped_type_identifier`/`annotated_type`
/// present as a direct child, then delegates to `resolve_type_node_base_name`.
/// `None` when no such child is present, or the one found still could not be
/// resolved (e.g. `extends int` -- syntactically valid per the grammar,
/// which performs no semantic type checking, but not a type shape this
/// extractor understands) -- callers (`extract_inheritance`,
/// `anonymous_body_context`) must treat that as INCOMPLETE supertype
/// evidence, never as "this type has no supertype at all".
pub(super) fn base_type_name(container: &OwnedNode) -> Option<String> {
    for kind in [
        "type_identifier",
        "generic_type",
        "scoped_type_identifier",
        "annotated_type",
    ] {
        if let Some(child) = container.child_by_kind(kind) {
            if let Some(name) = resolve_type_node_base_name(child) {
                return Some(name);
            }
        }
    }
    None
}

/// Collects the type names out of a `type_list` node (the shared grammar
/// shape for BOTH `super_interfaces -> type_list` and `extends_interfaces ->
/// type_list`): each entry IS a type node directly (never a container), so
/// `resolve_type_node_base_name` applies unchanged -- handling bare/generic
/// `type_identifier` entries, a qualified `scoped_type_identifier` entry
/// (e.g. `implements Outer.Iface`), and an annotated entry. Returns the
/// names alongside whether ANY entry could not be resolved -- N1
/// (#1873/#1875 second-review rework): an unparseable entry means the
/// caller's declared supertype set is INCOMPLETE, not merely "fewer
/// entries than the syntactic count" -- callers must record that
/// incompleteness rather than silently treating the parsed subset as the
/// whole truth.
pub(super) fn type_names_in_type_list(type_list: &OwnedNode) -> (Vec<String>, bool) {
    let mut names = Vec::new();
    let mut incomplete = false;
    for child in type_list.named_children() {
        match resolve_type_node_base_name(child) {
            Some(name) => names.push(name),
            None => incomplete = true,
        }
    }
    (names, incomplete)
}

/// AC2 (Story #1793, S4): resolves a TYPE NODE's local-syntactic base name,
/// falling back to the node's raw text VERBATIM (never `None`) when
/// `resolve_type_node_base_name` does not recognise its kind (or the
/// resolved candidate failed the identifier-shape backstop) -- callers
/// (`formal_parameter_type_name`, an ordinary bare constructor-reference
/// target like `Widget::new`) require a guaranteed `String`, and a
/// primitive/array-type node (which has no children to search) is
/// genuinely fine returned as its own literal text. N3 (#1873/#1875
/// second-review rework): this also resolves a qualified
/// constructor-reference target's `field_access` shape (`Outer.Inner::new`)
/// to its LAST segment, previously returned verbatim (`"Outer.Inner"`,
/// which can never match any declared type name).
pub(super) fn base_name_of_type_node(type_node: &OwnedNode) -> String {
    resolve_type_node_base_name(type_node).unwrap_or_else(|| type_node.text().to_string())
}
