//! Regression tests for D3 -- X-Ray graph mode's Java binder conflates
//! `this.foo()` and `super.foo()` into a single `ReceiverExpr::SelfOrSuper`
//! that resolves against the CALLING class's own enclosing type. For
//! `super.foo()` that is wrong: the call can only ever target the
//! superclass, never the enclosing type itself. Verified live (from the
//! mission): `BoundArrayListModel.add(E)` calls `super.add(o)`, whose real
//! target is the external `java.util.ArrayList.add` -- yet the graph
//! recorded a false self-loop on `BoundArrayListModel.add`.
//!
//! Root cause fixed this turn: `ReceiverExpr::Super` (a new variant,
//! distinct from `SelfOrSuper`) is produced by
//! `java_receiver::build_receiver_expr` for the literal `super` node, and
//! `resolve::apply_super_class_narrowing` (bind/resolve.rs) is a HARD
//! filter that resolves a `super` call only against the enclosing type's
//! transitive supertypes (`TypeIndex::supertypes_of`, which never includes
//! the enclosing type itself) -- clearing the candidate set entirely (no
//! edge at all) when the real superclass is external to the graph, rather
//! than falling back to the enclosing type or an unrelated same-named
//! candidate elsewhere in the repo.
//!
//! This file holds the D3 cases sharing that ONE fix (the `super.m()`
//! external-superclass case this turn; constructor-edge cases land here
//! next turn per the working agreement), using the same extract -> bind ->
//! assert harness pattern `bug_1873_java_method_reference_dead_code.rs`
//! already established.
//!
//! Constructor-reference / construction-site tests (D3/F4) that do not
//! involve `super.method()` narrowing were split out to the sibling file
//! `d3b_java_constructor_references.rs` once this file reached the
//! project's 1000-line-per-file limit -- see that file's own module doc.

use xray_core::graph::bind::{bind_with_budget, FileForBind};
use xray_core::graph::budget::{AnalysisCompleteness, IndexBudget};
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::extract::LanguageExtractor;
use xray_core::graph::identity::SymbolId;

const FILE_ID: u32 = 1;

/// Parses `source` as a real temp `.java` file through the real scanner,
/// then runs it through the real `JavaExtractor` -- the exact production
/// path, never a hand-built `LocalIndex`.
fn extract_java(source: &str) -> LocalIndex {
    extract_java_with_file_id(source, FILE_ID)
}

fn extract_java_with_file_id(source: &str, file_id: u32) -> LocalIndex {
    let dir = tempfile::tempdir().expect("create temp dir");
    let path = dir.path().join("Sample.java");
    std::fs::write(&path, source).expect("write fixture source");
    let root = xray_core::scanner::parse_file(&path).expect("fixture source must parse");
    JavaExtractor.extract(&root, file_id)
}

/// Binds one already-extracted file under an unlimited budget and asserts
/// the fixture-sanity precondition every test below relies on: a single
/// small file under an unlimited budget must report `Complete`.
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

fn bind_files(indexes: Vec<LocalIndex>) -> CodeGraph {
    let files = indexes
        .into_iter()
        .enumerate()
        .map(|(offset, index)| FileForBind {
            file_id: offset as u32 + 1,
            language: "java".to_string(),
            index,
        })
        .collect();
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    assert_eq!(
        graph.completeness(),
        AnalysisCompleteness::Complete,
        "fixture sanity: unlimited multi-file binding must report Complete"
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

/// Like `declaration_symbol`, but disambiguates by the declaration's
/// recorded `MethodOwnerRecord.enclosing_type` -- needed when the same
/// method name is declared more than once in a fixture (e.g. an anonymous
/// subclass overriding the same name its supertype declares).
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

/// D3: `BoundListLike.add` overrides `java.util.ArrayList.add` and calls
/// `super.add(o)`. `ArrayList` is a REAL JDK class (real, compilable Java,
/// mirroring the production `BoundArrayListModel`/`ArrayList` case in the
/// mission) but is NOT itself declared anywhere in this single-file
/// extraction, so it is genuinely external to the graph -- there is no
/// `ArrayList.add` declaration for `super.add(o)` to resolve to. Before
/// this fix, `super.add(o)`'s `ReceiverExpr::SelfOrSuper` resolved against
/// the ENCLOSING type (`BoundListLike`), and since `add` is the only
/// `add` declared anywhere in this file, matched BoundListLike's own `add`
/// -- a false self-loop. `add` is `public` (required: overriding a JDK
/// public method), so `is_definitely_dead_code` cannot assert a definite
/// verdict on it (that call is gated to `Visibility::Private`); the direct,
/// visibility-independent proof is the graph's own caller set.
#[test]
fn super_call_with_external_superclass_produces_no_self_loop() {
    let source = r#"
import java.util.ArrayList;

class BoundListLike extends ArrayList<Object> {
    @Override
    public boolean add(Object o) {
        return super.add(o);
    }
}
"#;
    let index = extract_java(source);
    let add = declaration_symbol(&index, "add");
    let graph = bind_single_file(index);
    let dense_id = graph
        .dense_id_for(add)
        .expect("add must be interned in the bound graph");
    assert!(
        graph.callers_index(dense_id).is_empty(),
        "super.add(o) must never resolve to BoundListLike's own add -- ArrayList.add is external \
         to this single-file graph, so this call must produce NO edge at all, not a self-loop"
    );
    assert!(
        !graph.is_symbol_referenced(dense_id),
        "super.add(o) with an external superclass must leave BoundListLike.add completely \
         unreferenced -- no self-loop, no fallback to an unrelated same-named candidate"
    );
}

/// #1873/#1875 rework, F1 (HIGH, REGRESSION). `apply_super_class_narrowing`
/// unconditionally replaced the candidate set with matches against
/// `type_index.supertypes_of(enclosing_type)` -- but an anonymous class
/// body's own methods were never attributed to a distinct enclosing type,
/// so `enclosing_type` stayed the SYNTACTICALLY surrounding type (`Outer`),
/// which has no recorded relationship to `Base` at all. That emptied the
/// candidate set for `super.f()`, wrongly marking the private `Base.f()`
/// definitely dead even though HEAD correctly resolved it. Reviewer probe
/// P1 (javac-validated).
#[test]
fn super_call_inside_anonymous_class_body_reaches_private_nested_base_method() {
    let source = r#"
class Outer {
    static class Base { private void f() {} }
    void run() {
        new Base() { void g() { super.f(); } }.g();
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
        "super.f() from inside the anonymous Base subclass's body must reach the real, private \
         Base.f() -- the anonymous class body's own methods must be attributed to a distinct \
         enclosing type whose recorded supertype is Base, never to the syntactically surrounding Outer"
    );
}

/// #1873/#1875 rework, F1. Same root cause as the anonymous-class-body
/// case above, but for an enum CONSTANT's own body (`PLUS { ... }`), which
/// is likewise never given a distinct enclosing type today -- `super.
/// helper(a)` stayed attributed to the enum's own type `Op`, and since `Op`
/// has no recorded supertype, the hard filter emptied the pool. Reviewer
/// probe P1e (javac-validated).
#[test]
fn super_call_inside_enum_constant_body_reaches_private_enum_method() {
    let source = r#"
enum Op {
    PLUS { int apply(int a) { return super.helper(a) + 1; } };
    private int helper(int a) { return a; }
    int apply(int a) { return a; }
}
"#;
    let index = extract_java(source);
    let helper = declaration_symbol(&index, "helper");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(helper).expect("helper must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "super.helper(a) from inside PLUS's enum-constant body must reach the real, private \
         Op.helper -- an enum constant body's own methods must be attributed to a distinct \
         enclosing type whose recorded supertype is the enum itself"
    );
}

/// #1873/#1875 rework, F1. Distinct root cause from the two tests above:
/// `Sub extends Outer.Base` is a QUALIFIED (`scoped_type_identifier`)
/// superclass, which `base_type_name` (java.rs) did not handle at all (only
/// bare `type_identifier`/`generic_type`) -- so `extract_inheritance` never
/// recorded an Extends edge for `Sub` in the first place, regardless of any
/// anonymous/enum-constant type-context fix. Reviewer probe P13
/// (javac-validated).
#[test]
fn super_call_with_qualified_scoped_superclass_reaches_private_base_method() {
    let source = r#"
class Outer {
    static class Base { private void f() {} }
    static class Sub extends Outer.Base {
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
        "super.f() from Sub (which extends the QUALIFIED type Outer.Base) must reach Base's \
         private f() -- extract_inheritance's base_type_name must record an Extends edge for a \
         scoped_type_identifier superclass (Outer.Base), not silently record no edge at all"
    );
}

/// #1873/#1875 rework, F1's explicit acceptance-bar note: "for NON-private
/// targets the same bug removed real caller edges (hurting reachability/
/// blast-radius)". `Adapter.onX` is package-private (no explicit
/// modifier), so `is_definitely_dead_code` cannot assert a definite verdict
/// on it (gated to `Visibility::Private`, same rationale as
/// `super_call_with_external_superclass_produces_no_self_loop` above) --
/// the direct, visibility-independent proof is the graph's own caller set.
#[test]
fn super_call_inside_anonymous_class_body_keeps_non_private_target_referenced() {
    let source = r#"
class Adapter {
    void onX() {}
}
class Client {
    void run() {
        new Adapter() { void trigger() { super.onX(); } }.trigger();
    }
}
"#;
    let index = extract_java(source);
    let on_x = declaration_symbol_owned_by(&index, "onX", "Adapter");
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(on_x)
        .expect("Adapter.onX must be interned");
    assert!(
        !graph.callers_index(dense).is_empty(),
        "super.onX() from inside the anonymous Adapter subclass's body must reach Adapter's own \
         onX -- a NON-private target must not lose its real caller edge either"
    );
    assert!(
        graph.is_symbol_referenced(dense),
        "Adapter.onX must be considered referenced through the anonymous subclass's super call"
    );
}

/// #1873/#1875 rework, F1's third required behaviour, independent of the
/// anonymous/enum-constant type-context fixes above: `apply_super_class_
/// narrowing` must never wipe the WHOLE candidate set just because
/// `enclosing_type` has no recorded supertype at all (e.g. a class with no
/// explicit `extends`, whose real implicit superclass is `java.lang.
/// Object`, external to this graph). `super.toString()` is legal Java in
/// ANY class (every class implicitly extends Object); under the pre-#1873
/// HEAD behaviour this used the softer SelfOrSuper narrowing, which skips
/// narrowing entirely on an empty match, so Bar's unrelated `toString()`
/// stayed referenced through Foo's `super.toString()` call. The
/// unconditional hard-filter regression instead always empties the pool
/// when `supertypes_of` is empty, wrongly zeroing out Bar.toString's only
/// caller edge. Not one of the reviewer's named probes -- added to give
/// the "no evidence -> conservative fallback" requirement its own
/// independent, isolated proof, since P1/P1e/P13 above are all satisfiable
/// via real (non-empty) superclass evidence alone.
///
/// N4 (#1873/#1875 second-review, LOW): NOT a real-Java-semantics claim --
/// Foo's real superclass is `Object`, so `javac` never reaches `Bar.
/// toString()`. Proves the deliberately IMPRECISE conservative fallback
/// instead. See `docs/xray-architecture.md`/`analyze_graph.md` (corrected).
#[test]
fn super_call_with_no_recorded_superclass_falls_back_to_leaving_the_sole_candidate_referenced() {
    let source = r#"
class Bar {
    public String toString() { return "decoy"; }
}
class Foo {
    void run() {
        super.toString();
    }
}
"#;
    let index = extract_java(source);
    let to_string = declaration_symbol(&index, "toString");
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(to_string)
        .expect("toString must be interned");
    assert!(
        !graph.callers_index(dense).is_empty(),
        "with no recorded supertype for Foo at all, the super-class hard filter must fall back \
         to leaving Bar.toString's only reachable candidate referenced, exactly like the pre-fix \
         SelfOrSuper narrowing (which always skips narrowing on an empty match) -- never wipe the \
         whole pool just because there is no superclass evidence"
    );
}

/// #1873/#1875 rework, F2. Import-context narrowing must not run before a
/// genuine `super.foo()` is restricted to its recorded superclass chain:
/// this cross-package override's same-file `Sub.foo` otherwise wins the
/// import-context pass, leaving the later super filter no `Base.foo` to keep.
#[test]
fn cross_package_super_call_reaches_imported_base_despite_same_named_override() {
    let base = extract_java_with_file_id(
        r#"
package p1;
public class Base { protected void foo() {} }
"#,
        1,
    );
    let base_foo = declaration_symbol(&base, "foo");
    let sub = extract_java_with_file_id(
        r#"
package p2;
import p1.Base;
public class Sub extends Base {
    @Override protected void foo() { super.foo(); }
}
"#,
        2,
    );
    let graph = bind_files(vec![base, sub]);
    let dense = graph
        .dense_id_for(base_foo)
        .expect("Base.foo must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "cross-package super.foo() must retain Base.foo even when Sub.foo is a same-file override"
    );
    assert!(graph.is_symbol_referenced(dense));
}

/// Control for F2: the same-package case already has package-context
/// evidence for Base.foo, but must remain live after the ordering change.
#[test]
fn same_package_super_call_reaches_base_method() {
    let base = extract_java_with_file_id(
        r#"
package p1;
public class Base { protected void foo() {} }
"#,
        1,
    );
    let base_foo = declaration_symbol(&base, "foo");
    let sub = extract_java_with_file_id(
        r#"
package p1;
public class Sub extends Base {
    @Override protected void foo() { super.foo(); }
}
"#,
        2,
    );
    let graph = bind_files(vec![base, sub]);
    let dense = graph
        .dense_id_for(base_foo)
        .expect("Base.foo must be interned");
    assert_eq!(graph.is_definitely_dead_code(dense), Some(false));
}

/// F2's non-override variant: an unrelated same-package sibling must not
/// cause import-context narrowing to discard the imported superclass method
/// before the `super` filter sees it.
#[test]
fn cross_package_super_call_reaches_base_despite_same_package_sibling() {
    let base = extract_java_with_file_id(
        r#"
package p1;
public class Base { protected void foo() {} }
"#,
        1,
    );
    let base_foo = declaration_symbol(&base, "foo");
    let sub_and_sibling = extract_java_with_file_id(
        r#"
package p2;
import p1.Base;
public class Sub extends Base {
    void run() { super.foo(); }
}
class Sibling { void foo() {} }
"#,
        2,
    );
    let graph = bind_files(vec![base, sub_and_sibling]);
    let dense = graph
        .dense_id_for(base_foo)
        .expect("Base.foo must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "an imported superclass target must survive a same-package sibling's foo declaration"
    );
}

/// #1873/#1875 rework, F1 -- an independent discriminator for the
/// `scoped_type_identifier` fix specifically, distinct from
/// `super_call_with_qualified_scoped_superclass_reaches_private_base_
/// method` above. That test's pool for `f` has only ONE declaration, so
/// it is ALSO satisfied by the conservative-fallback fix alone. Adding an
/// unrelated `Zoo.f` sharing Sub's own top-level type (`Outer`, so D2
/// cannot filter it out on a top-level mismatch) makes the two fixes
/// observably different: WITHOUT the `scoped_type_identifier` fix, `Sub`
/// gets NO recorded Extends edge at all, so the conservative fallback
/// alone leaves BOTH `Base.f` and `Zoo.f` in the pool, incorrectly marking
/// the never-called `Zoo.f` as referenced; WITH it, the real `Sub extends
/// Outer.Base` edge correctly excludes `Zoo.f`.
#[test]
fn super_call_with_qualified_scoped_superclass_does_not_falsely_reference_an_unrelated_same_named_decoy(
) {
    let source = r#"
class Outer {
    static class Base { private void f() {} }
    static class Zoo { private void f() {} }
    static class Sub extends Outer.Base {
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
        "super.f() from Sub (which extends the qualified Outer.Base) must never reach the \
         unrelated Zoo.f -- the real scoped_type_identifier Extends edge must exclude it, not \
         just conservatively leave every same-named candidate referenced"
    );
}

/// #1873/#1875 rework, F1 -- an independent discriminator for the
/// anonymous-class-body type-context fix specifically, distinct from
/// `super_call_inside_anonymous_class_body_reaches_private_nested_base_
/// method` above. That test's pool for `f` has only ONE declaration, so it
/// is ALSO satisfied by the conservative-fallback fix alone (which simply
/// leaves a single-candidate pool untouched, without any real narrowing
/// evidence). Adding an unrelated `Zoo.f` sharing Base's own top-level type
/// (`Outer`, so D2 cannot filter it out on a top-level mismatch) makes the
/// two fixes observably different: WITHOUT the anonymous-class-body
/// type-context fix, the conservative fallback alone leaves BOTH `Base.f`
/// and `Zoo.f` in the pool (no real evidence to narrow on), incorrectly
/// marking the never-called `Zoo.f` as referenced; WITH it, real
/// superclass evidence (the anonymous type's recorded `Extends Base` edge)
/// correctly excludes `Zoo.f`.
#[test]
fn super_call_inside_anonymous_class_body_does_not_falsely_reference_an_unrelated_same_named_decoy()
{
    let source = r#"
class Outer {
    static class Base { private void f() {} }
    static class Zoo { private void f() {} }
    void run() {
        new Base() { void g() { super.f(); } }.g();
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
        "super.f() inside the anonymous Base subclass must never reach the unrelated Zoo.f -- \
         real superclass evidence for the anonymous type must exclude it, not just \
         conservatively leave every same-named candidate referenced"
    );
}

/// #1873/#1875 rework, F1 -- the same independent discriminator as above,
/// for the enum-constant-body type-context fix. `Zoo` is nested INSIDE the
/// enum `Op` so it shares Op's own top-level type (D2 cannot filter it out
/// on a top-level mismatch either).
#[test]
fn super_call_inside_enum_constant_body_does_not_falsely_reference_an_unrelated_same_named_decoy() {
    let source = r#"
enum Op {
    PLUS { int apply(int a) { return super.helper(a) + 1; } };
    private int helper(int a) { return a; }
    int apply(int a) { return a; }
    static class Zoo {
        private int helper(int a) { return a; }
    }
}
"#;
    let index = extract_java(source);
    let zoo_helper = declaration_symbol_owned_by(&index, "helper", "Zoo");
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(zoo_helper)
        .expect("Zoo.helper must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "super.helper(a) inside PLUS's enum-constant body must never reach the unrelated \
         Zoo.helper -- real superclass evidence for the constant's anonymous type must exclude \
         it, not just conservatively leave every same-named candidate referenced"
    );
}

/// #1873/#1875 rework, F3 (MEDIUM): the D2 visibility filter ran AFTER all
/// narrowing, so an inaccessible private candidate from an UNRELATED
/// top-level type could knock the real, accessible target out of the
/// candidate set during overload-shape narrowing, then get removed itself
/// by D2 -- leaving BOTH candidates unreferenced. `A.run` makes a bare
/// (same-class) call to `log((Foo) f)`; `A.log(Object)` is the only
/// legitimately reachable declaration (an unqualified call from A can never
/// resolve to B's unrelated private `log(Foo)`), but overload-shape
/// narrowing's named-type preference step exactly matches the cast's `Foo`
/// argument against B.log's declared `Foo` parameter and narrows to B.log
/// ALONE before D2 ever runs -- D2 then removes B.log (cross-top-level
/// private), leaving A.log with zero caller edges even though this is its
/// own real (and only) call site. Reviewer probe P4 (javac-validated).
#[test]
fn overload_shape_narrowing_before_d2_does_not_falsely_kill_the_real_target() {
    let source = r#"
class Foo {}
class A {
    private void log(Object o) {}
    void run(Object f) { log((Foo) f); }
}

class B {
    private void log(Foo f) {}
}
"#;
    let index = extract_java(source);
    let a_log = declaration_symbol_owned_by(&index, "log", "A");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(a_log).expect("A.log must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "A.log(Object) is the only call `log((Foo) f)` can legitimately reach from inside A -- \
         D2 must filter out the cross-top-level private B.log BEFORE overload-shape narrowing \
         gets a chance to wrongly prefer it and knock A.log out of the candidate set"
    );
    assert_eq!(
        graph.callers_index(dense).len(),
        1,
        "A.log must have exactly its own real call site as a caller"
    );
}

// ---------------------------------------------------------------------
// Second-round-review regressions (N1 implementation defect): a
// `scoped_type_identifier`/`field_access` superclass whose RAW SOURCE TEXT
// contains whitespace, a line break, a type annotation, or a comment
// between the dot and the final identifier used to be resolved by
// `last_dot_segment` doing `text.rsplit('.').next()` on the node's raw
// text span -- garbage like `" Base"`/`"\n    Base"`/`"@Ann Base"`/
// `"/*c*/Base"` (a `Some(garbage)`, never `None`), which silently defeats
// the N1 incomplete-supertype-evidence safety net (it only engages on
// `None`) and poisons the recorded supertype set with a name that can
// never match anything. Fixed by resolving from the PARSE TREE structure
// (the last named `type_identifier`/`identifier` child) instead of
// splitting raw text.
// ---------------------------------------------------------------------

/// P01: a stray space after the dot in a qualified superclass
/// (`extends Outer. Base`).
#[test]
fn qualified_superclass_with_whitespace_after_dot_reaches_private_base_method() {
    let source = r#"
class Outer {
    interface Marker {}
    static class Base { private void f() {} }
    static class Sub extends Outer. Base implements Marker {
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
        "super.f() from Sub (extends Outer. Base, with a stray space after the dot) must reach \
         Base's private f() -- resolving the qualified superclass name must never split raw \
         source text, which would capture the leading whitespace as part of the name and poison \
         the recorded supertype set with a name that can never match anything"
    );
}

/// P02: a line break after the dot in a qualified superclass.
#[test]
fn qualified_superclass_with_newline_after_dot_reaches_private_base_method() {
    let source = "
class Outer {
    interface Marker {}
    static class Base { private void f() {} }
    static class Sub extends Outer.
            Base implements Marker {
        void g() { super.f(); }
    }
}
";
    let index = extract_java(source);
    let f = declaration_symbol(&index, "f");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(f).expect("f must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "super.f() from Sub (extends Outer.\\n    Base, a line break after the dot) must reach \
         Base's private f() -- same raw-text-splitting defect as the whitespace case, just with \
         a newline instead of a space"
    );
}

/// P03: a type annotation between the dot and the final identifier in a
/// qualified superclass (`extends Outer.@Ann Base`).
#[test]
fn qualified_superclass_with_type_annotation_reaches_private_base_method() {
    let source = r#"
import java.lang.annotation.ElementType;
import java.lang.annotation.Target;
class Outer {
    @Target(ElementType.TYPE_USE) @interface Ann {}
    interface Marker {}
    static class Base { private void f() {} }
    static class Sub extends Outer.@Ann Base implements Marker {
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
        "super.f() from Sub (extends Outer.@Ann Base, a type annotation between the dot and the \
         final identifier) must reach Base's private f() -- the marker_annotation node sits as a \
         real named child inside the scoped_type_identifier, so a raw-text split captures \
         `\"@Ann Base\"` verbatim as the (unmatchable) supertype name"
    );
}

/// P04: a block comment between the dot and the final identifier in a
/// qualified superclass (`extends Outer./*c*/Base`).
#[test]
fn qualified_superclass_with_comment_reaches_private_base_method() {
    let source = r#"
class Outer {
    interface Marker {}
    static class Base { private void f() {} }
    static class Sub extends Outer./*c*/Base implements Marker {
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
        "super.f() from Sub (extends Outer./*c*/Base, a comment between the dot and the final \
         identifier) must reach Base's private f() -- tree-sitter-java represents a comment as a \
         real named child, so a raw-text split captures `\"/*c*/Base\"` verbatim as the \
         (unmatchable) supertype name"
    );
}

/// P07: the same whitespace-after-dot defect, but for an ANONYMOUS class's
/// synthesized supertype (`new Outer. Base() { ... }`) rather than a real
/// `class ... extends` clause -- `anonymous_body_context` (java.rs) also
/// resolves its supertype via `base_type_name`/`resolve_type_node_base_name`,
/// so it shares the exact same raw-text-splitting defect.
#[test]
fn anonymous_class_with_whitespace_after_dot_in_qualified_supertype_reaches_private_base_method() {
    let source = r#"
class Outer implements Runnable {
    public void run() {}
    static class Base { private void f() {} }
    void make() { new Outer. Base() { void g() { super.f(); } }.g(); }
}
"#;
    let index = extract_java(source);
    let f = declaration_symbol(&index, "f");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(f).expect("f must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "super.f() from the anonymous subclass of Outer. Base (a stray space after the dot) must \
         reach Base's private f() -- anonymous_body_context's supertype resolution shares the \
         same raw-text-splitting defect as a real extends clause"
    );
}

// ---------------------------------------------------------------------
// Second-round-review regression (pre-existing `supertypes_of` self-seeding
// defect, independent of the text-splitting root cause above): a subtype
// and its (textually) resolved supertype share the same BARE simple name
// but are genuinely different declared types nested in different enclosing
// scopes. The type-name model in this extractor tracks only bare names, so
// `extract_inheritance` would record a same-name self-referencing edge
// (`direct_parents["Foo"] = ["Foo"]`), and `TypeIndex::supertypes_of` seeds
// its `visited` set with the subtype's own bare name before walking up --
// so the self-named parent is treated as "already visited" and silently
// dropped, leaving `supertypes_of("Foo")` empty even though a real (if
// ambiguous) inheritance edge was recorded for it. Fixed by having the
// EXTRACTOR flag this specific case as incomplete supertype evidence
// (never resolvable from purely local, syntactic information) rather than
// letting the resolved edge collide with the walker's own self-seeding.
// ---------------------------------------------------------------------

/// P05: `Holder.Foo extends Outer.Foo` -- two distinct declared types both
/// named `Foo` in different enclosing scopes. `Outer.Foo` is the only
/// declaration named `f` anywhere in the fixture.
#[test]
fn subclass_and_superclass_sharing_the_same_bare_name_reaches_private_base_method() {
    let source = r#"
class Outer {
    interface Marker {}
    static class Foo { private void f() {} }
    static class Holder {
        static class Foo extends Outer.Foo implements Marker {
            void g() { super.f(); }
        }
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
        "super.f() from Holder.Foo (which extends the DIFFERENT, same-bare-named Outer.Foo) must \
         reach Outer.Foo's private f() -- the extractor cannot safely disambiguate two distinct \
         types sharing one bare name from its own local syntactic view, so it must record this as \
         incomplete supertype evidence (skipping super-class narrowing entirely) rather than \
         let the resolved same-name edge collide with supertypes_of's own self-seeded cycle guard \
         and silently vanish"
    );
}
