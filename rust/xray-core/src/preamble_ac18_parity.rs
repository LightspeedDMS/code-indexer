//! AC18 (Story #1787 / ADR-002): fails `cargo test` loudly, naming what
//! diverged, whenever `compiler::PREAMBLE`'s mirrored `OwnedNode`/
//! `EvalFinding` structurally diverges from the REAL types in
//! `owned_node.rs`/`finding.rs`.
//!
//! Bug #1795 is why "structurally" matters: the bug was a `.rev()`
//! PLACEMENT difference between the real `descendants_of_kind` and its
//! PREAMBLE mirror -- both copies contained the identical set of
//! substrings/tokens, so no substring-presence check (see the pre-existing,
//! deliberately-kept `test_preamble_types_match_crate_types` in
//! compiler.rs, which only proves "these tokens appear somewhere") could
//! ever have caught it. This module instead parses both sides with `syn`
//! and compares the actual AST (field lists, method signatures, method
//! BODIES) for structural equality, ignoring only the divergences that are
//! documented as intentional (see `owned_node_mirror_has_no_drop_impl`
//! below).
//!
//! Every comparison here panics with a message naming the exact struct,
//! field, or method that diverged (Anti-Silent-Failure, Messi Rule 13) --
//! never a bare `assert!(a == b)` that would force a developer to diff two
//! multi-hundred-line strings by hand to find the one line that changed.

use syn::{Fields, File, ImplItem, ImplItemFn, Item, ItemStruct, Type, Visibility};

const OWNED_NODE_SRC: &str = include_str!("owned_node.rs");
const FINDING_SRC: &str = include_str!("finding.rs");

/// Parses `src` as a sequence of top-level Rust items. Panics naming `label`
/// on a parse failure -- this helper is only ever fed known-good Rust source
/// (real crate files, or the PREAMBLE text, both of which must already be
/// valid Rust for the crate/evaluator to compile at all), so a parse
/// failure here indicates the source text itself is broken, not a normal
/// test outcome.
fn parse_items(src: &str, label: &str) -> File {
    syn::parse_str(src).unwrap_or_else(|e| panic!("failed to parse {label} as Rust items: {e}"))
}

/// Finds a top-level `struct <name> { ... }` item in `file`. Panics naming
/// `name`/`label` when absent -- an absent struct is itself a fatal
/// divergence (the mirror or the real type was renamed or removed on one
/// side only).
fn find_struct<'a>(file: &'a File, name: &str, label: &str) -> &'a ItemStruct {
    file.items
        .iter()
        .find_map(|item| match item {
            Item::Struct(s) if s.ident == name => Some(s),
            _ => None,
        })
        .unwrap_or_else(|| panic!("struct {name} not found in {label}"))
}

/// Extracts `(field_name, is_public, field_type)` for every field of `s`, in
/// DECLARATION ORDER. Deliberately excludes `attrs` (doc comments) -- the
/// real type and the mirror legitimately carry different doc comments, and
/// that must never register as a divergence. Panics if `s` uses tuple/unit
/// fields; both `OwnedNode` and `EvalFinding` use named fields, and a
/// switch away from that shape on either side is itself worth a loud
/// failure rather than a silently-empty comparison.
fn struct_field_signature(s: &ItemStruct) -> Vec<(String, bool, Type)> {
    match &s.fields {
        Fields::Named(named) => named
            .named
            .iter()
            .map(|f| {
                let name = f
                    .ident
                    .as_ref()
                    .unwrap_or_else(|| panic!("named field with no ident in struct {}", s.ident))
                    .to_string();
                let is_pub = matches!(f.vis, Visibility::Public(_));
                (name, is_pub, f.ty.clone())
            })
            .collect(),
        other => panic!(
            "struct {} does not use named fields (got {:?}) -- parity check assumes named fields",
            s.ident, other
        ),
    }
}

/// Compares two field-signature lists (see `struct_field_signature`) and
/// returns `Some(message)` naming exactly what diverged -- a missing field,
/// an extra field, a visibility change, or a type change -- or `None` when
/// they are identical. Order-sensitive: swapping two same-typed fields IS a
/// divergence, since it changes the struct's memory layout.
fn diff_struct_fields(real: &ItemStruct, mirror: &ItemStruct, label: &str) -> Option<String> {
    let real_fields = struct_field_signature(real);
    let mirror_fields = struct_field_signature(mirror);

    if real_fields.len() != mirror_fields.len() {
        return Some(format!(
            "{label}: field COUNT diverged -- real has {} field(s) {:?}, mirror has {} field(s) {:?}",
            real_fields.len(),
            real_fields.iter().map(|(n, ..)| n.as_str()).collect::<Vec<_>>(),
            mirror_fields.len(),
            mirror_fields.iter().map(|(n, ..)| n.as_str()).collect::<Vec<_>>(),
        ));
    }

    for (index, (real_field, mirror_field)) in
        real_fields.iter().zip(mirror_fields.iter()).enumerate()
    {
        let (real_name, real_pub, real_ty) = real_field;
        let (mirror_name, mirror_pub, mirror_ty) = mirror_field;
        if real_name != mirror_name {
            return Some(format!(
                "{label}: field #{index} NAME diverged -- real='{real_name}', mirror='{mirror_name}'"
            ));
        }
        if real_pub != mirror_pub {
            return Some(format!(
                "{label}.{real_name}: VISIBILITY diverged -- real pub={real_pub}, mirror pub={mirror_pub}"
            ));
        }
        if real_ty != mirror_ty {
            return Some(format!(
                "{label}.{real_name}: TYPE diverged -- real={:#?}, mirror={:#?}",
                real_ty, mirror_ty
            ));
        }
    }

    None
}

/// Returns the trailing path-segment identifier of an `impl` block's
/// `self_ty` (e.g. `"OwnedNode"` for `impl OwnedNode { .. }` or
/// `impl some::path::OwnedNode { .. }`). `None` for a `self_ty` that isn't a
/// simple path (defensive: neither `owned_node.rs` nor PREAMBLE has any
/// reason to `impl` for a non-path type).
fn impl_self_type_name(item_impl: &syn::ItemImpl) -> Option<String> {
    match &*item_impl.self_ty {
        Type::Path(type_path) => type_path
            .path
            .segments
            .last()
            .map(|seg| seg.ident.to_string()),
        _ => None,
    }
}

/// Finds the INHERENT (non-trait) method named `method` on `impl <self_ty>`
/// in `file`. Panics naming `self_ty`/`method`/`label` when absent -- a
/// missing method is itself a fatal divergence (one side lost or renamed a
/// method the other still exposes to evaluator code).
fn find_impl_method<'a>(
    file: &'a File,
    self_ty: &str,
    method: &str,
    label: &str,
) -> &'a ImplItemFn {
    file.items
        .iter()
        .filter_map(|item| match item {
            Item::Impl(item_impl) if item_impl.trait_.is_none() => Some(item_impl),
            _ => None,
        })
        .filter(|item_impl| impl_self_type_name(item_impl).as_deref() == Some(self_ty))
        .find_map(|item_impl| {
            item_impl
                .items
                .iter()
                .find_map(|impl_item| match impl_item {
                    ImplItem::Fn(f) if f.sig.ident == method => Some(f),
                    _ => None,
                })
        })
        .unwrap_or_else(|| panic!("method {self_ty}::{method} not found in {label}"))
}

/// Compares a method's SIGNATURE (name, params, return type -- via
/// `syn::Signature`'s `PartialEq`) and BODY (via `syn::Block`'s
/// `PartialEq`, gated on the `extra-traits` dev-dependency feature).
/// Deliberately excludes `attrs` (doc comments) and `vis` (both are always
/// `pub` on the methods this check targets, and a `pub`-ness mismatch would
/// surface as a compile error in the mirror anyway, not a silent one).
///
/// Comparing `Block` -- not the method's source text -- is exactly what
/// catches a `.rev()`-PLACEMENT divergence: two bodies with an identical
/// token multiset produce DIFFERENT `Block` ASTs when a call moves from one
/// call site to another, because the position of that call in the
/// statement tree changed even though no token was added or removed.
fn diff_method(real: &ImplItemFn, mirror: &ImplItemFn, label: &str) -> Option<String> {
    if real.sig != mirror.sig {
        return Some(format!(
            "{label}: SIGNATURE diverged -- real={:#?}, mirror={:#?}",
            real.sig, mirror.sig
        ));
    }
    if real.block != mirror.block {
        return Some(format!(
            "{label}: BODY diverged (same signature, different implementation) -- real={:#?}\nmirror={:#?}",
            real.block, mirror.block
        ));
    }
    None
}

/// Returns true if `file` contains `impl Drop for <self_ty> { .. }`.
///
/// Used to encode the ADR-002 INTENTIONAL divergence: the mirror must never
/// declare Drop for `OwnedNode` (a second Drop over data the real type
/// already frees risks a double-free across the FFI boundary, since the
/// evaluator only ever borrows via `fn(&OwnedNode)`), while the real type
/// must have one (Bug #1795's explicit-stack fix). This is a presence
/// check, not a body comparison -- there is nothing to structurally
/// reconcile between "has a Drop impl" and "does not".
fn mirror_declares_drop_impl(file: &File, self_ty: &str) -> bool {
    file.items.iter().any(|item| match item {
        Item::Impl(item_impl) => {
            let is_drop_trait = item_impl
                .trait_
                .as_ref()
                .and_then(|(_, path, _)| path.segments.last())
                .is_some_and(|seg| seg.ident == "Drop");
            is_drop_trait && impl_self_type_name(item_impl).as_deref() == Some(self_ty)
        }
        _ => false,
    })
}

/// Method names on `OwnedNode` that are mirrored into PREAMBLE and must
/// stay structurally identical to the real implementation.
const MIRRORED_OWNED_NODE_METHODS: &[&str] = &[
    "text",
    "named_children",
    "child_by_kind",
    "has_descendant_of_kind",
    "descendants_of_kind",
];

/// Runs every AC18 structural comparison (OwnedNode fields, EvalFinding
/// fields, each mirrored method, and the ADR-002 Drop-impl asymmetry) and
/// returns every divergence found -- collecting all of them, not stopping
/// at the first, per Anti-Silent-Failure (Messi Rule 13).
fn collect_ac18_divergences(
    owned_node_file: &File,
    finding_file: &File,
    preamble_file: &File,
) -> Vec<String> {
    let mut divergences: Vec<String> = Vec::new();

    let real_owned_node = find_struct(owned_node_file, "OwnedNode", "owned_node.rs");
    let mirror_owned_node = find_struct(preamble_file, "OwnedNode", "PREAMBLE");
    divergences.extend(diff_struct_fields(
        real_owned_node,
        mirror_owned_node,
        "OwnedNode",
    ));

    let real_finding = find_struct(finding_file, "EvalFinding", "finding.rs");
    let mirror_finding = find_struct(preamble_file, "EvalFinding", "PREAMBLE");
    divergences.extend(diff_struct_fields(
        real_finding,
        mirror_finding,
        "EvalFinding",
    ));

    for method in MIRRORED_OWNED_NODE_METHODS {
        let real_method = find_impl_method(owned_node_file, "OwnedNode", method, "owned_node.rs");
        let mirror_method = find_impl_method(preamble_file, "OwnedNode", method, "PREAMBLE");
        divergences.extend(diff_method(
            real_method,
            mirror_method,
            &format!("OwnedNode::{method}"),
        ));
    }

    // ADR-002 intentional divergence: see mirror_declares_drop_impl's doc
    // comment. Both directions are checked explicitly rather than silently
    // assumed, per the task's instruction to "encode the divergences that
    // are INTENTIONAL and catch the ones that are not".
    if !mirror_declares_drop_impl(owned_node_file, "OwnedNode") {
        divergences.push(
            "OwnedNode: the REAL type no longer has `impl Drop for OwnedNode` -- Bug #1795's \
             explicit-stack Drop fix appears to have been reverted or renamed"
                .to_string(),
        );
    }
    if mirror_declares_drop_impl(preamble_file, "OwnedNode") {
        divergences.push(
            "OwnedNode: the PREAMBLE mirror now declares `impl Drop for OwnedNode` -- FORBIDDEN \
             by ADR-002: the evaluator only ever borrows the tree (&OwnedNode, see dynlib.rs's \
             EvaluateNodeFn), so a second Drop over the same data risks a double-free across \
             the FFI boundary"
                .to_string(),
        );
    }

    divergences
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_a_field_type_divergence_between_two_synthetic_structs() {
        let real_src = "struct Foo { pub a: usize, pub b: String }";
        let mirror_src = "struct Foo { pub a: usize, pub b: u32 }";
        let real_file = parse_items(real_src, "real");
        let mirror_file = parse_items(mirror_src, "mirror");
        let real_struct = find_struct(&real_file, "Foo", "real");
        let mirror_struct = find_struct(&mirror_file, "Foo", "mirror");

        let diff = diff_struct_fields(real_struct, mirror_struct, "Foo");
        assert!(
            diff.is_some(),
            "a genuine field-type divergence must be detected"
        );
        let msg = diff.unwrap();
        assert!(
            msg.contains('b'),
            "divergence message must name the diverging field: {msg}"
        );
    }

    /// This is the discriminating test the task calls for: it reproduces the
    /// EXACT shape of Bug #1795 -- a `.rev()` PLACEMENT difference between
    /// two method bodies that contain the IDENTICAL token multiset (one
    /// `rev`, one `collect`, one `extend`, two `iter`) and differ ONLY in
    /// which call site `.rev()` is attached to. A substring-presence check
    /// (like the pre-existing `test_preamble_types_match_crate_types`)
    /// cannot distinguish these two bodies -- every token it could check
    /// for is present in both. Only an AST/structural comparison can.
    #[test]
    fn detects_a_method_body_divergence_in_rev_call_placement() {
        let real_src = r#"
impl Foo {
    pub fn walk(&self) -> Vec<i32> {
        let mut stack: Vec<i32> = self.children.iter().rev().collect();
        stack.extend(self.other.iter());
        stack
    }
}
"#;
        let mirror_src = r#"
impl Foo {
    pub fn walk(&self) -> Vec<i32> {
        let mut stack: Vec<i32> = self.children.iter().collect();
        stack.extend(self.other.iter().rev());
        stack
    }
}
"#;
        let real_file = parse_items(real_src, "real");
        let mirror_file = parse_items(mirror_src, "mirror");
        let real_method = find_impl_method(&real_file, "Foo", "walk", "real");
        let mirror_method = find_impl_method(&mirror_file, "Foo", "walk", "mirror");

        let diff = diff_method(real_method, mirror_method, "Foo::walk");
        assert!(
            diff.is_some(),
            "a `.rev()`-placement body divergence (the exact Bug #1795 shape) must be detected"
        );
    }

    #[test]
    fn reports_no_divergence_for_byte_identical_methods() {
        let src = r#"
impl Foo {
    pub fn walk(&self) -> Vec<i32> {
        let mut stack: Vec<i32> = self.children.iter().rev().collect();
        stack.extend(self.other.iter());
        stack
    }
}
"#;
        let real_file = parse_items(src, "real");
        let mirror_file = parse_items(src, "mirror");
        let real_method = find_impl_method(&real_file, "Foo", "walk", "real");
        let mirror_method = find_impl_method(&mirror_file, "Foo", "walk", "mirror");

        assert!(
            diff_method(real_method, mirror_method, "Foo::walk").is_none(),
            "identical method bodies must never be reported as diverging"
        );
    }

    /// Detection for the ADR-002 intentional-divergence check: the mirror
    /// must NEVER declare `impl Drop for OwnedNode` (a second Drop over
    /// data the real type already frees risks a double-free across the FFI
    /// boundary), while the real type MUST have one (Bug #1795's fix).
    #[test]
    fn detects_drop_impl_presence_and_absence() {
        let with_drop = "impl Drop for Foo { fn drop(&mut self) {} }";
        let without_drop = "struct Foo;";
        let with_file = parse_items(with_drop, "with_drop");
        let without_file = parse_items(without_drop, "without_drop");
        assert!(
            mirror_declares_drop_impl(&with_file, "Foo"),
            "a genuine `impl Drop for Foo` must be detected"
        );
        assert!(
            !mirror_declares_drop_impl(&without_file, "Foo"),
            "a source with no Drop impl must not be falsely flagged"
        );
    }

    /// THE AC18 GATE: parses the REAL `owned_node.rs`/`finding.rs` and the
    /// ACTUAL `compiler::PREAMBLE` text compiled into every evaluator
    /// artifact, and asserts field-for-field / method-for-method structural
    /// parity via `collect_ac18_divergences`. Currently mirrored surface
    /// (per AC18's explicit scope): `OwnedNode`'s fields plus its 5
    /// evaluator-facing methods, `EvalFinding`'s fields, and the ADR-002
    /// Drop-impl asymmetry. `XRAY_ABI_VERSION` is deliberately NOT
    /// re-checked here: Bug #1784 already replaced that duplication with a
    /// single-source-of-truth substitution
    /// (`ABI_VERSION_PLACEHOLDER` -> `compiler::XRAY_ABI_VERSION`), so
    /// there is no second copy left that could drift -- `dynlib.rs`'s
    /// `test_compiled_evaluator_exports_abi_version_matching_single_source_of_truth`
    /// and `compiler.rs`'s `test_compile_evaluator_cache_miss_when_preamble_changes`
    /// already guard that mechanism end-to-end.
    #[test]
    fn preamble_mirror_matches_real_types_structurally() {
        let owned_node_file = parse_items(OWNED_NODE_SRC, "owned_node.rs");
        let finding_file = parse_items(FINDING_SRC, "finding.rs");
        let preamble_file = parse_items(crate::compiler::PREAMBLE, "compiler::PREAMBLE");

        let divergences = collect_ac18_divergences(&owned_node_file, &finding_file, &preamble_file);

        assert!(
            divergences.is_empty(),
            "AC18: PREAMBLE mirror diverged from the real OwnedNode/EvalFinding types:\n\n{}",
            divergences.join("\n\n")
        );
    }
}
