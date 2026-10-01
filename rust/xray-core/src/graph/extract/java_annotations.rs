//! Java annotation extraction, split out of `java.rs` (Messi Rule 6,
//! anti-file-bloat -- `java.rs` was at the project's 1000-line limit)
//! mirroring `java_receiver.rs`/`java_invocations.rs`/`java_type_names.
//! rs`/`java_fields.rs`/`java_methods.rs`'s own sibling-module splits. Owns
//! plain annotation-presence extraction (`extract_annotations_from_
//! modifiers`) and, since Bug #1926 (epic #1906), JUnit5 `@MethodSource`
//! reflection-reference extraction.

use super::java_type_names::is_plausible_java_identifier;
use super::local_index::{AnnotationRecord, LocalIndex, MethodSourceRequest};
use crate::graph::identity::SymbolId;
use crate::owned_node::OwnedNode;

pub(super) fn extract_annotations_from_modifiers(node: &OwnedNode, target_name: &str, index: &mut LocalIndex) {
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

/// True when `annotation_node` (a `marker_annotation` or `annotation`
/// node) is a `@MethodSource` annotation, comparing only the LAST
/// dot-separated segment of its name so a fully-qualified usage
/// (`@org.junit.jupiter.params.provider.MethodSource(...)`) matches too.
fn is_method_source_annotation(annotation_node: &OwnedNode) -> bool {
    let Some(name_node) = annotation_node
        .named_children()
        .into_iter()
        .find(|c| c.kind == "identifier" || c.kind == "scoped_identifier")
    else {
        return false;
    };
    let text = name_node.text();
    text.rsplit('.').next().unwrap_or(text) == "MethodSource"
}

/// Bug #1926: a JUnit5 `@MethodSource("providerName")` annotation is a
/// reflection-invoked reference to a factory method -- invisible to the
/// ordinary invocation-site walk (`dispatch_node` never visits an
/// annotation's own argument list as a call). Without this, a private
/// `@MethodSource` provider method has zero inbound edges and satisfies
/// `is_definitely_dead_code` exactly (~16 of ~42 false `Some(true)` hits on
/// a real Java library were exactly this shape).
///
/// Handles every real grammar shape a `@MethodSource` argument list can
/// take: a bare string (`@MethodSource("p")`), an array initializer
/// (`@MethodSource({"a", "b"})`), and a named element-value pair
/// (`@MethodSource(value = "p")`) -- `method_source_target_names` collects
/// every `string_literal` DESCENDANT of the argument list regardless of
/// which of these three shapes wraps it. NO argument was written at all
/// (`has_explicit_argument` is false -- a bare `marker_annotation`, or an
/// `annotation` node whose argument list is syntactically empty,
/// `@MethodSource()`), or the single argument is a BLANK string
/// (`@MethodSource("")`, JUnit5's own documented same-name-default rule
/// for an empty value), both apply the same-name default: the SAME name as
/// the annotated test method itself. An argument WAS written and is
/// non-blank but resolves to no local target (e.g. every string names an
/// unrelated class) records NO target name at all -- it never falls back
/// to the default, which would fabricate a self-reference the source never
/// wrote.
///
/// `method_source_target_names`'s `Class#method` handling only resolves a
/// `Class` that textually equals the annotation's OWN enclosing class
/// (self-reference, cheap and unambiguous from purely local syntax); a
/// fully-qualified reference naming any OTHER class is left unresolved.
/// TestNG's `@DataProvider`/`dataProvider = "x"` pairing is NOT handled
/// here: unlike `@MethodSource`, it requires matching an attribute VALUE
/// across two potentially different methods in the class (not merely
/// reading one annotation's own argument), a materially different, more
/// expensive lookup this fix's scope does not cover.
///
/// Records a `MethodSourceRequest` per `@MethodSource` annotation found --
/// resolution is deliberately DEFERRED to an end-of-file postprocess
/// (`java_methods::resolve_method_source_edges`), never handed to the
/// generic name-based bind pipeline: that pipeline's candidate pool starts
/// from EVERY same-named declaration in the whole repo, and its same-
/// class-or-super narrowing is permanently soft (tag-only, documented,
/// never a hard filter -- see `bind::narrowing::apply_same_class_or_
/// super_narrowing`'s own doc comment), so a same-named, same-arity method
/// in an outer class, a sibling nested class, or another file in the same
/// package would fabricate a false edge, hiding genuinely dead code. JUnit5
/// itself resolves the factory ONLY within the test class's own declared
/// methods (its superclasses too, in principle -- NOT modelled here,
/// conservatively: an inherited factory is simply not referenced by this
/// pass, under-report rather than a guess). `owner_type_symbol` is exactly
/// the annotated method's own immediately enclosing type, so resolution
/// can never reach outside it.
pub(super) fn push_method_source_invocations(
    node: &OwnedNode,
    method_name: &str,
    method_symbol: SymbolId,
    enclosing_type: Option<&str>,
    enclosing_type_symbol: Option<SymbolId>,
    index: &mut LocalIndex,
) {
    let Some(modifiers) = node.child_by_kind("modifiers") else {
        return;
    };
    for annotation_node in modifiers
        .children
        .iter()
        .filter(|c| c.kind == "marker_annotation" || c.kind == "annotation")
        .filter(|c| is_method_source_annotation(c))
    {
        let uses_default =
            !has_explicit_argument(annotation_node) || is_blank_single_string_argument(annotation_node);
        let target_names: Vec<String> = if uses_default {
            vec![method_name.to_string()]
        } else {
            method_source_target_names(annotation_node, enclosing_type)
        };
        index.method_source_requests.push(MethodSourceRequest {
            from_method: method_symbol,
            owner_type_symbol: enclosing_type_symbol,
            target_names,
        });
    }
}

/// True when `annotation_node` carries EXACTLY one string-literal argument
/// whose trimmed content is empty (`@MethodSource("")`) -- JUnit5 treats a
/// blank value identically to no value at all, applying the same-name
/// default. Deliberately narrow (a single blank string only, not an array
/// containing one): the broader case is not part of this fix's scope.
fn is_blank_single_string_argument(annotation_node: &OwnedNode) -> bool {
    let Some(args) = annotation_node.child_by_kind("annotation_argument_list") else {
        return false;
    };
    let literals = args.descendants_of_kind("string_literal");
    let [only] = literals.as_slice() else {
        return false;
    };
    only
        .child_by_kind("string_fragment")
        .map(|fragment| fragment.text().trim().is_empty())
        .unwrap_or(true)
}

/// True when `annotation_node` carries a real, explicitly-written argument
/// -- false for a bare `marker_annotation` (`@MethodSource`) and for an
/// `annotation` node whose own `annotation_argument_list` is syntactically
/// present but empty (`@MethodSource()`), both of which fall under JUnit5's
/// documented same-name default rather than an explicit value.
fn has_explicit_argument(annotation_node: &OwnedNode) -> bool {
    annotation_node
        .child_by_kind("annotation_argument_list")
        .is_some_and(|args| !args.named_children().is_empty())
}

/// Collects and normalizes every string-literal argument of
/// `annotation_node`'s own `annotation_argument_list` -- see
/// `push_method_source_invocations`'s doc comment above for the grammar
/// shapes this covers uniformly and the `Class#method` scope decision this
/// enforces (a cross-class qualifier is silently dropped, never guessed).
fn method_source_target_names(annotation_node: &OwnedNode, enclosing_type: Option<&str>) -> Vec<String> {
    let Some(args) = annotation_node.child_by_kind("annotation_argument_list") else {
        return Vec::new();
    };
    args.descendants_of_kind("string_literal")
        .into_iter()
        .filter_map(|literal| literal.child_by_kind("string_fragment"))
        .filter_map(|fragment| {
            let raw = fragment.text();
            let name = match raw.split_once('#') {
                None => raw,
                Some((class_part, method_part)) => {
                    let simple_class = class_part.rsplit('.').next().unwrap_or(class_part);
                    if Some(simple_class) != enclosing_type {
                        return None;
                    }
                    method_part
                }
            }
            .trim();
            if is_plausible_java_identifier(name) {
                Some(name.to_string())
            } else {
                None
            }
        })
        .collect()
}
