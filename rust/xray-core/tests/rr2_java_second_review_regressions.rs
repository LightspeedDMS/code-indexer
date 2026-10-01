//! Second code review of the #1873/#1875 rework found two NEW live->dead
//! regressions introduced by the F1-F6 fix itself (N1, N2), plus a
//! same-family qualified-constructor-reference gap (N3). This file holds
//! their discriminating regression tests, using the same
//! extract -> bind -> assert harness pattern `d3_java_super_and_constructor_
//! references.rs`/`bug_1873_java_method_reference_dead_code.rs` already
//! established. Probe labels (A1, A2, A3, ...) match the second reviewer's
//! `rr2_probes.rs` corpus so the mapping between that scratch material and
//! these real assertions is traceable.
//!
//! N1: `apply_super_class_narrowing` (bind/narrowing.rs) fell back to
//! "narrow to nothing changes" only when `supertypes_of(enclosing_type)` was
//! completely EMPTY -- never when the extractor's own supertype evidence for
//! that type was merely INCOMPLETE (a `superclass`/`implements` entry that
//! existed syntactically but could not be resolved to a name). When some
//! OTHER supertype edge for the same type WAS recorded, `allowed` was
//! non-empty but wrong, and the hard-filter narrowing deleted the real
//! target. Fixed two ways: (1) the extractor now resolves the previously-
//! unhandled shapes (`generic_type` wrapping a qualified name, an annotated
//! superclass/implements entry, a qualified name in an `implements` list);
//! (2) as a safety net for shapes still not specifically handled, any
//! superclass/type-list entry the extractor cannot resolve at all is
//! recorded as "incomplete supertype evidence" for that type, and
//! `apply_super_class_narrowing` skips narrowing entirely (never touches the
//! candidate set) whenever that evidence is incomplete -- narrowing only
//! ever runs when the supertype set is known to be COMPLETE.
//!
//! N2: `annotation_type_declaration` dispatches as a type declaration (F6),
//! so `@interface` types get a symbol -- but USING an annotation (`@Marker`)
//! produced no reference edge at all (annotation names are grammar
//! `identifier`/`scoped_identifier` nodes, never one of the three
//! reference-producing node kinds `method_invocation`/
//! `object_creation_expression`/`type_identifier`). A created symbol with no
//! possible inbound edge is automatically "definitely dead". Fixed by
//! emitting a real `TypeReferenceRecord` for every annotation usage
//! (`marker_annotation`/`annotation` node), resolved through the exact same
//! binder machinery a `type_identifier` reference already uses.
//!
//! N3: a qualified constructor-reference target (`Outer.Inner::new`) is
//! parsed by tree-sitter's GLR grammar as a `field_access` node (NOT
//! `scoped_type_identifier`, verified via a real grammar dump), and an
//! explicit-type-argument constructor reference (`G::<String>new`) has
//! `named_children() == [identifier, type_arguments]` -- the SAME shape the
//! extractor's `[object, name]` ordinary-method-reference arm already
//! matched, incorrectly capturing the type-argument node as the callee name.
//! Both are fixed in the shared type-name resolution helpers
//! (`java_type_names::resolve_type_node_base_name`) and the method-reference
//! dispatch's arm ordering.

use xray_core::graph::bind::{bind_with_budget, FileForBind};
use xray_core::graph::budget::{AnalysisCompleteness, IndexBudget};
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::local_index::{DeclarationKind, LocalIndex};
use xray_core::graph::extract::LanguageExtractor;
use xray_core::graph::identity::SymbolId;

const FILE_ID: u32 = 1;

fn extract_java(source: &str) -> LocalIndex {
    let dir = tempfile::tempdir().expect("create temp dir");
    let path = dir.path().join("Sample.java");
    std::fs::write(&path, source).expect("write fixture source");
    let root = xray_core::scanner::parse_file(&path).expect("fixture source must parse");
    JavaExtractor.extract(&root, FILE_ID)
}

fn bind_single_file(index: LocalIndex) -> CodeGraph {
    let graph = bind_with_budget(
        vec![FileForBind {
            file_id: FILE_ID,
            language: "java".to_string(),
            index,
        }],
        &IndexBudget::unlimited(),
    );
    assert_eq!(
        graph.completeness(),
        AnalysisCompleteness::Complete,
        "fixture sanity: an unlimited-budget single-file bind must report Complete"
    );
    graph
}

/// The symbol of the single declaration named `name`. Panics loudly if the
/// name is missing or ambiguous.
fn declaration_symbol(index: &LocalIndex, name: &str) -> SymbolId {
    let matches: Vec<_> = index
        .declarations
        .iter()
        .filter(|d| d.name == name)
        .collect();
    match matches.as_slice() {
        [only] => only.symbol,
        [] => panic!("fixture bug: no declaration named {name:?}"),
        other => panic!(
            "fixture bug: {name:?} is ambiguous ({} declarations)",
            other.len()
        ),
    }
}

/// A Java constructor shares its bare name with its own enclosing class's
/// TYPE declaration, so a plain by-name lookup like `declaration_symbol` is
/// ambiguous for it. This filters to `DeclarationKind::Method` specifically
/// (constructors are extracted as `Method` declarations, per
/// `extract_method_declaration`) and returns the first match.
fn constructor_symbol(index: &LocalIndex, class_name: &str) -> SymbolId {
    index
        .declarations
        .iter()
        .find(|d| d.name == class_name && d.kind == DeclarationKind::Method)
        .unwrap_or_else(|| panic!("fixture bug: no constructor named {class_name:?}"))
        .symbol
}

/// Disambiguates a same-named declaration by its recorded
/// `MethodOwnerRecord.enclosing_type` -- needed when a fixture declares the
/// same method name twice in different types (e.g. `Base.f` and an
/// unrelated decoy `Zoo.f`), mirroring the identical helper in
/// `d3_java_super_and_constructor_references.rs`.
fn declaration_symbol_owned_by(index: &LocalIndex, name: &str, owner: &str) -> SymbolId {
    let matches: Vec<SymbolId> = index
        .declarations
        .iter()
        .filter(|d| d.name == name)
        .filter_map(|d| {
            let is_owned_by = index
                .method_owners
                .iter()
                .any(|o| o.method_symbol == d.symbol && o.enclosing_type == owner);
            is_owned_by.then_some(d.symbol)
        })
        .collect();
    match matches.as_slice() {
        [only] => *only,
        [] => panic!("fixture bug: no declaration named {name:?} owned by {owner:?}"),
        other => panic!(
            "fixture bug: {name:?} owned by {owner:?} is ambiguous ({} declarations)",
            other.len()
        ),
    }
}

// ---------------------------------------------------------------------
// N1: super-class narrowing must never act on incomplete supertype
// evidence -- neither directly via a still-unhandled shape (the safety
// net) nor via the specific shapes the extractor now resolves correctly.
// ---------------------------------------------------------------------

/// A1 (javac-valid): `Sub extends Outer.Base<String> implements Marker`
/// combines a GENERIC superclass wrapping a QUALIFIED name with a real
/// `implements` edge. Before the fix, `base_type_name` could not resolve
/// `Outer.Base<String>` at all (its `generic_type` branch only looked for a
/// bare `type_identifier` inside, never a `scoped_type_identifier`), so no
/// Extends edge was ever recorded for `Sub` -- but the recorded `implements
/// Marker` edge alone made `supertypes_of("Sub")` NON-EMPTY (`{"Marker"}`),
/// so the F1 "skip narrowing only when allowed is empty" fallback did not
/// engage, and the hard filter deleted `Base.f` entirely.
#[test]
fn super_call_with_generic_qualified_superclass_and_interface_reaches_private_base_method() {
    let source = r#"
class Outer {
    interface Marker {}
    static class Base<T> { private void f() {} }
    static class Sub extends Outer.Base<String> implements Marker {
        void g() { super.f(); }
    }
}
"#;
    let index = extract_java(source);
    let f = declaration_symbol(&index, "f");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(f).expect("f must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "super.f() from Sub (extends the GENERIC-QUALIFIED Outer.Base<String>, implements Marker) \
         must reach Base's private f() -- a recorded implements edge must never let narrowing \
         run on an incomplete/unresolved superclass"
    );
}

/// A2 (javac-valid, non-private target): `Mid extends Outer.Base<String>
/// implements Marker {}`, `Sub extends Mid`. Once `Outer.Base<String>`
/// resolves correctly, `supertypes_of("Sub")` transitively includes `Base`
/// via `Mid`. `f` is package-private (`Visibility::Unknown`), so
/// `is_definitely_dead_code` cannot assert a definite verdict on it -- the
/// direct, visibility-independent proof is the graph's own caller set.
#[test]
fn super_call_transitively_through_generic_qualified_mid_reaches_nonprivate_base_method() {
    let source = r#"
class Outer {
    interface Marker {}
    static class Base<T> { void f() {} }
    static class Mid extends Outer.Base<String> implements Marker {}
    static class Sub extends Mid {
        void g() { super.f(); }
    }
}
"#;
    let index = extract_java(source);
    let f = declaration_symbol(&index, "f");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(f).expect("f must be interned");
    assert!(
        !graph.callers_index(dense).is_empty(),
        "super.f() from Sub (extending Mid, which extends the GENERIC-QUALIFIED \
         Outer.Base<String>) must retain a real caller edge to Base.f transitively"
    );
}

/// A3 (javac-valid): the same generic-qualified-superclass-plus-interface
/// shape as A1, but for an ANONYMOUS class body (`new Outer.Base<String>()
/// { ... super.f(); ... }`), which resolves the supertype via
/// `anonymous_body_context`'s own `base_type_name(node)` call on the
/// `object_creation_expression` node directly (a different code path from
/// `extract_inheritance`'s `superclass` container).
#[test]
fn super_call_inside_anonymous_body_extending_generic_qualified_type_reaches_private_base_method() {
    let source = r#"
class Outer implements Runnable {
    public void run() {}
    static class Base<T> { private void f() {} }
    void make() { new Outer.Base<String>() { void g() { super.f(); } }.g(); }
}
"#;
    let index = extract_java(source);
    let f = declaration_symbol(&index, "f");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(f).expect("f must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "super.f() from inside an anonymous subclass of the GENERIC-QUALIFIED Outer.Base<String> \
         (Outer itself implements Runnable) must reach Base's private f()"
    );
}

/// A20 (javac-valid, non-private target): `Base implements Outer.Iface`
/// (a QUALIFIED name in an `implements` list) then `Sub extends Base` calls
/// `super.dm()`, the interface's default method. Before the fix,
/// `type_names_in_type_list` only handled bare/generic `type_identifier`
/// entries, so `implements Outer.Iface` recorded NO edge for `Base` at all
/// -- `supertypes_of("Sub")` was then just `{"Base"}` (non-empty, from the
/// `extends Base` edge alone), so the hard filter deleted `dm` entirely.
/// `dm` carries no explicit modifier (`Visibility::Unknown`), so the direct
/// proof is the caller set.
#[test]
fn super_call_via_qualified_interface_default_method_reaches_it() {
    let source = r#"
class Outer {
    interface Iface { default void dm() {} }
    static class Base implements Outer.Iface {}
    static class Sub extends Base {
        void g() { super.dm(); }
    }
}
"#;
    let index = extract_java(source);
    let dm = declaration_symbol(&index, "dm");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(dm).expect("dm must be interned");
    assert!(
        !graph.callers_index(dense).is_empty(),
        "super.dm() from Sub (whose supertype Base implements the QUALIFIED Outer.Iface) must \
         retain a real caller edge to the interface's default method"
    );
}

/// A21 (javac-valid): `Sub extends @Ann Base implements Marker` -- an
/// ANNOTATED superclass type. Before the fix, `base_type_name`'s search for
/// a direct `type_identifier`/`generic_type`/`scoped_type_identifier` child
/// of `superclass` never found one (the actual child is `annotated_type`,
/// wrapping the real type), so no Extends edge was recorded for `Sub` -- but
/// the recorded `implements Marker` edge alone made `supertypes_of` non-empty,
/// so the hard filter deleted `Base.f`.
#[test]
fn super_call_with_annotated_superclass_and_interface_reaches_private_base_method() {
    let source = r#"
import java.lang.annotation.ElementType;
import java.lang.annotation.Target;
class Outer {
    @Target(ElementType.TYPE_USE) @interface Ann {}
    interface Marker {}
    static class Base { private void f() {} }
    static class Sub extends @Ann Base implements Marker {
        void g() { super.f(); }
    }
}
"#;
    let index = extract_java(source);
    let f = declaration_symbol(&index, "f");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(f).expect("f must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "super.f() from Sub (extends the ANNOTATED @Ann Base, implements Marker) must reach \
         Base's private f() -- an annotated superclass type must still resolve, and even if it \
         did not, a recorded implements edge must never let narrowing run on incomplete evidence"
    );
}

/// Discriminating-power companion to A1: a mutation-testing pass proved the
/// plain A1 assertion above is NOT independently discriminating for the
/// generic-wrapping-qualified-name fix -- it stays green even with that fix
/// reverted, rescued entirely by the N1 safety net (which sees `Sub`'s
/// superclass as unparseable and skips narrowing altogether, leaving every
/// same-named candidate referenced). Adding an unrelated `Zoo.f` sharing
/// `Sub`'s own top-level type (`Outer`, so D2 cannot filter it out on a
/// top-level mismatch) makes the two defenses observably different: the
/// safety net ALONE would leave BOTH `Base.f` and `Zoo.f` referenced
/// (no real narrowing evidence at all); the CORRECT fix resolves
/// `Outer.Base<String>` properly and narrows `Sub`'s candidates to `Base`
/// only, correctly excluding the never-called `Zoo.f`.
#[test]
fn super_call_with_generic_qualified_superclass_does_not_falsely_reference_an_unrelated_same_named_decoy(
) {
    let source = r#"
class Outer {
    interface Marker {}
    static class Base<T> { private void f() {} }
    static class Zoo { private void f() {} }
    static class Sub extends Outer.Base<String> implements Marker {
        void g() { super.f(); }
    }
}
"#;
    let index = extract_java(source);
    let zoo_f = declaration_symbol_owned_by(&index, "f", "Zoo");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(zoo_f).expect("Zoo.f must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "super.f() from Sub (extends the GENERIC-QUALIFIED Outer.Base<String>) must never reach \
         the unrelated Zoo.f -- the real generic/scoped-type-identifier Extends edge must exclude \
         it, not just conservatively leave every same-named candidate referenced via the safety net"
    );
}

/// Discriminating-power companion to A3, mirroring the A1 decoy above for
/// the anonymous-class-body path (`anonymous_body_context`'s own
/// `base_type_name(node)` call, a separate code path from
/// `extract_inheritance`'s `superclass` container).
#[test]
fn super_call_inside_anonymous_body_extending_generic_qualified_type_does_not_falsely_reference_an_unrelated_same_named_decoy(
) {
    let source = r#"
class Outer implements Runnable {
    public void run() {}
    static class Base<T> { private void f() {} }
    static class Zoo { private void f() {} }
    void make() { new Outer.Base<String>() { void g() { super.f(); } }.g(); }
}
"#;
    let index = extract_java(source);
    let zoo_f = declaration_symbol_owned_by(&index, "f", "Zoo");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(zoo_f).expect("Zoo.f must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "super.f() inside the anonymous subclass of the GENERIC-QUALIFIED Outer.Base<String> \
         must never reach the unrelated Zoo.f -- real superclass evidence for the anonymous type \
         must exclude it, not just conservatively leave every same-named candidate referenced"
    );
}

/// Discriminating-power companion to A21: a mutation-testing pass proved
/// the plain A21 assertion above is NOT independently discriminating for
/// the annotated-superclass-type fix -- it stays green even with that fix
/// reverted, rescued entirely by the N1 safety net. Same decoy technique as
/// A1's companion above.
#[test]
fn super_call_with_annotated_superclass_does_not_falsely_reference_an_unrelated_same_named_decoy() {
    let source = r#"
import java.lang.annotation.ElementType;
import java.lang.annotation.Target;
class Outer {
    @Target(ElementType.TYPE_USE) @interface Ann {}
    interface Marker {}
    static class Base { private void f() {} }
    static class Zoo { private void f() {} }
    static class Sub extends @Ann Base implements Marker {
        void g() { super.f(); }
    }
}
"#;
    let index = extract_java(source);
    let zoo_f = declaration_symbol_owned_by(&index, "f", "Zoo");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(zoo_f).expect("Zoo.f must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "super.f() from Sub (extends the ANNOTATED @Ann Base) must never reach the unrelated \
         Zoo.f -- the real annotated-type Extends edge must exclude it, not just conservatively \
         leave every same-named candidate referenced via the safety net"
    );
}

/// Safety-net proof (N1 requirement 2): a superclass shape the extractor
/// deliberately does NOT special-case (`extends int` -- syntactically valid
/// per the tree-sitter-java grammar, which performs no semantic type
/// checking, though not valid real Java) must still leave a genuinely
/// referenced candidate alone. `Sub extends int implements Marker`: the
/// `implements Marker` edge alone makes `supertypes_of("Sub")` NON-EMPTY
/// (`{"Marker"}`), so a fix that only checks "is `allowed` empty" would
/// still wrongly narrow `super.f()`'s candidates down to nothing (`Base.f`'s
/// enclosing type "Base" is not in `{"Marker"}`) and delete the real,
/// private target. The fix must instead recognise that `Sub`'s superclass
/// evidence is INCOMPLETE (the `extends int` clause could not be resolved
/// to any name) and skip narrowing entirely, regardless of what `allowed`
/// contains.
#[test]
fn super_class_narrowing_skips_entirely_when_supertype_evidence_is_unparseable() {
    let source = r#"
class Outer {
    interface Marker {}
    static class Base { private void f() {} }
    static class Sub extends int implements Marker {
        void g() { super.f(); }
    }
}
"#;
    let index = extract_java(source);
    assert!(
        index.incomplete_supertypes.iter().any(|t| t == "Sub"),
        "fixture precondition: extraction must record Sub's superclass evidence as incomplete \
         (the `extends int` clause cannot be resolved to any type name)"
    );
    let f = declaration_symbol(&index, "f");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(f).expect("f must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "super.f() must still reach Base's private f() even though Sub ALSO has a real, \
         unrelated implements edge (Marker) that would otherwise make the candidate set look \
         non-empty and wrongly narrowable -- incomplete supertype evidence must skip narrowing \
         entirely, not just when the whole allowed-set happens to be empty"
    );
}

// ---------------------------------------------------------------------
// N2: a private annotation TYPE used only as an annotation must never be
// reported definitely dead -- using it is a real reference.
// ---------------------------------------------------------------------

/// A22: `@Marker` annotates `run()`. `Marker` is a private `@interface`
/// (gets a symbol via F6's `annotation_type_declaration` dispatch) with no
/// possible inbound edge before this fix, since annotation usages produced
/// no reference at all.
#[test]
fn private_annotation_type_used_only_as_annotation_is_never_reported_dead() {
    let source = r#"
class Outer {
    private @interface Marker {}
    @Marker void run() {}
}
"#;
    let index = extract_java(source);
    let marker = declaration_symbol(&index, "Marker");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(marker).expect("Marker must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "@Marker's usage on run() must be a real reference to the Marker annotation type's own \
         declaration -- a created symbol with no possible inbound edge must never be reported dead"
    );
    assert!(
        !graph.callers_index(dense).is_empty(),
        "Marker must have a real inbound edge from its own @Marker usage"
    );
}

// ---------------------------------------------------------------------
// N3: qualified and type-argument-carrying constructor-reference targets.
// ---------------------------------------------------------------------

/// A14: `Outer.Inner::new` -- tree-sitter's GLR grammar parses the receiver
/// position before `::new` as a `field_access` node (`Outer.Inner`), not a
/// `scoped_type_identifier` (verified via a real grammar dump) -- distinct
/// from `new Outer.Inner()`'s `object_creation_expression`, which DOES use
/// `scoped_type_identifier` and was already fixed by P12.
#[test]
fn qualified_constructor_reference_reaches_private_inner_constructor() {
    let source = r#"
import java.util.function.Supplier;
class Outer {
    static class Inner { private Inner() {} }
    Supplier<Inner> ref() { return Outer.Inner::new; }
}
"#;
    let index = extract_java(source);
    let inner_ctor = constructor_symbol(&index, "Inner");
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(inner_ctor)
        .expect("Inner constructor must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "Outer.Inner::new must retain a caller edge to Inner's private constructor -- the \
         qualified name's LAST segment (Inner) must be resolved, not the whole raw text \
         (Outer.Inner), which can never match any declared type name"
    );
}

/// A15: `G::<String>new` -- an explicit-type-argument CONSTRUCTOR reference
/// has `named_children() == [identifier(G), type_arguments]`, the exact
/// same 2-element shape the extractor's ordinary `[object, name]`
/// method-reference arm already matches -- without a dedicated arm, the
/// `type_arguments` node itself gets incorrectly captured as the callee
/// name (`"<String>"`), which can never resolve to anything.
#[test]
fn constructor_reference_with_explicit_type_arguments_reaches_private_constructor() {
    let source = r#"
import java.util.function.Function;
class Outer {
    static class G { private <T> G(T t) {} }
    Function<String, G> ref() { return G::<String>new; }
}
"#;
    let index = extract_java(source);
    let g_ctor = constructor_symbol(&index, "G");
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(g_ctor)
        .expect("G constructor must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "G::<String>new must retain a caller edge to G's private constructor -- the explicit \
         type-argument node must never be mistaken for the reference's callee name"
    );
}

// ---------------------------------------------------------------------
// Second-round-review regression (N1 implementation defect, same root
// cause as the qualified-superclass tests in
// `d3_java_super_and_constructor_references.rs`): a qualified annotation
// usage (`@Outer.Marker`) is a `scoped_identifier` node whose name was
// resolved via `last_dot_segment` doing a raw-text split -- a stray space
// after the dot (`@Outer. Marker`) makes that split return `" Marker"`
// (garbage, never `None`), which can never match the real `Marker`
// declaration. Fixed by resolving from the parse tree's last named
// `identifier` child instead of splitting the node's raw text.
// ---------------------------------------------------------------------

/// P09: `@Outer. Marker` -- a stray space after the dot in a qualified
/// annotation usage.
#[test]
fn qualified_annotation_usage_with_whitespace_after_dot_is_never_reported_dead() {
    let source = r#"
class Outer {
    private @interface Marker {}
    @Outer. Marker void run() {}
}
"#;
    let index = extract_java(source);
    let marker = declaration_symbol(&index, "Marker");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(marker).expect("Marker must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "@Outer. Marker's usage on run() (a stray space after the dot) must still be a real \
         reference to the Marker annotation type's own declaration -- resolving a qualified \
         annotation name must never split raw source text, which would capture the leading \
         whitespace as part of the name and never match anything"
    );
    assert!(
        !graph.callers_index(dense).is_empty(),
        "Marker must have a real inbound edge from its own qualified @Outer. Marker usage"
    );
}
