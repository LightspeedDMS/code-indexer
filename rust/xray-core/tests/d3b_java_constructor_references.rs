//! Constructor-reference / construction-site regression tests split out of
//! `d3_java_super_and_constructor_references.rs` (which hit the project's
//! 1000-line-per-file limit exactly) to give both files headroom for new
//! regression coverage. This file holds every D3/F4 case that exercises
//! `new Type(...)`, `Type::new`, `this(...)`/`super(...)` constructor
//! invocations, and method-reference construction sites -- i.e. everything
//! that is NOT about `super.method()` call narrowing (which stays in the
//! sibling file). Same extract -> bind -> assert harness pattern as
//! `bug_1873_java_method_reference_dead_code.rs`/
//! `d3_java_super_and_constructor_references.rs`.

use xray_core::graph::bind::{bind_with_budget, FileForBind};
use xray_core::graph::budget::{AnalysisCompleteness, IndexBudget};
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::local_index::{DeclarationKind, LocalIndex};
use xray_core::graph::extract::LanguageExtractor;
use xray_core::graph::identity::SymbolId;

const FILE_ID: u32 = 1;

/// Parses `source` as a real temp `.java` file through the real scanner,
/// then runs it through the real `JavaExtractor` -- the exact production
/// path, never a hand-built `LocalIndex`.
fn extract_java(source: &str) -> LocalIndex {
    let dir = tempfile::tempdir().expect("create temp dir");
    let path = dir.path().join("Sample.java");
    std::fs::write(&path, source).expect("write fixture source");
    let root = xray_core::scanner::parse_file(&path).expect("fixture source must parse");
    JavaExtractor.extract(&root, FILE_ID)
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

fn declaration_symbols(index: &LocalIndex, name: &str) -> Vec<SymbolId> {
    index
        .declarations
        .iter()
        .filter(|d| d.name == name && d.kind == DeclarationKind::Method)
        .map(|d| d.symbol)
        .collect()
}

/// Turn 7 review finding: the original combined fixture (`new Target()` +
/// `new Target(1)` + `Target::new` all in one test) was NOT discriminating
/// for the `new X(args)` construction-site fix -- `Target::new` alone
/// (arg_count: None, so no arity narrowing at all) already keeps BOTH
/// constructor overloads looking referenced regardless of whether
/// `extract_construction` pushes an `InvocationSite`. Isolated-removal
/// proof: stripping only that `InvocationSite` push left this exact
/// combined assertion GREEN. Split into two isolated tests below so each
/// path is proven on its own.
#[test]
fn new_x_args_calls_keep_both_constructor_overloads_live() {
    let source = r#"
class Target {
    private Target() {}
    private Target(int value) {}

    static void make() {
        new Target();
        new Target(1);
    }
}
"#;
    let index = extract_java(source);
    let constructors = declaration_symbols(&index, "Target");
    assert_eq!(
        constructors.len(),
        2,
        "fixture must contain exactly two Target constructors"
    );
    let graph = bind_single_file(index);
    for symbol in constructors {
        let dense = graph
            .dense_id_for(symbol)
            .expect("constructor must be interned");
        assert_eq!(
            graph.is_definitely_dead_code(dense),
            Some(false),
            "new Target()/new Target(1) alone (no method reference anywhere) must keep both overloads live"
        );
    }
}

#[test]
fn constructor_reference_alone_keeps_both_overloads_live() {
    let source = r#"
import java.util.function.Supplier;

class Target {
    private Target() {}
    private Target(int value) {}

    static Supplier<Target> factory() {
        return Target::new;
    }
}
"#;
    let index = extract_java(source);
    let constructors = declaration_symbols(&index, "Target");
    assert_eq!(
        constructors.len(),
        2,
        "fixture must contain exactly two Target constructors"
    );
    let graph = bind_single_file(index);
    for symbol in constructors {
        let dense = graph
            .dense_id_for(symbol)
            .expect("constructor must be interned");
        assert_eq!(
            graph.is_definitely_dead_code(dense),
            Some(false),
            "Target::new alone (no new Target(args) call anywhere) must keep both constructor \
             declarations live -- distinct from the #1873 test, which only checks the TYPE"
        );
    }
}

#[test]
fn explicit_this_constructor_invocation_reaches_target_overload() {
    let source = r#"
class Target {
    private Target() { this(1); }
    private Target(int value) {}
}
"#;
    let index = extract_java(source);
    let constructors = declaration_symbols(&index, "Target");
    assert_eq!(constructors.len(), 2);
    let target = constructors
        .iter()
        .find(|symbol| {
            index
                .declarations
                .iter()
                .any(|d| d.symbol == **symbol && d.param_count == Some(1))
        })
        .copied()
        .expect("one-argument constructor must exist");
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(target)
        .expect("constructor must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "this(1) must reference the one-argument constructor"
    );
}

#[test]
fn explicit_super_constructor_invocation_reaches_superclass_constructor() {
    let source = r#"
class Base {
    private Base() {}
    protected Base(int value) {}
}

class Child extends Base {
    private Child() { super(1); }
}
"#;
    let index = extract_java(source);
    let base_constructors = declaration_symbols(&index, "Base");
    assert_eq!(base_constructors.len(), 2);
    let graph = bind_single_file(index);
    let live: Vec<_> = base_constructors
        .iter()
        .filter(|symbol| {
            let dense = graph
                .dense_id_for(**symbol)
                .expect("Base constructor must be interned");
            graph.is_definitely_dead_code(dense) == Some(false)
        })
        .collect();
    assert_eq!(
        live.len(),
        1,
        "super(1) must reach only the one-argument Base constructor"
    );
}

/// `Target(int)` and `Target(String)` are both arity-1, so
/// `apply_arity_narrowing` alone cannot discriminate between them.
/// `this(value)` passes a bare identifier naming a constructor
/// PARAMETER whose declared type is `String`. Named-type (identifier/
/// `this`) argument evidence is TAG-ONLY: it decides whether
/// `OVERLOAD_ARG_TYPE_MATCH` is set, but never excludes a candidate --
/// so `Target(int)` stays referenced, while `OVERLOAD_ARG_TYPE_MATCH`
/// is tagged on `Target(String)` alone (a `String` argument is
/// closed-world provably not assignable to an `int` parameter).
#[test]
fn same_arity_constructor_call_keeps_both_overloads_live_and_tags_only_the_compatible_one() {
    let source = r#"
class Target {
    private Target(int a) {}
    private Target(String s) {}

    private Target(Object o, String value) {
        this(value);
    }
}
"#;
    let index = extract_java(source);
    let constructors = declaration_symbols(&index, "Target");
    assert_eq!(
        constructors.len(),
        3,
        "fixture must contain exactly three Target constructors"
    );
    let find_by_params = |param_types: &[&str]| {
        let wanted: Vec<String> = param_types.iter().map(|s| s.to_string()).collect();
        constructors
            .iter()
            .copied()
            .find(|symbol| {
                index.declarations.iter().any(|d| d.symbol == *symbol && d.param_types == wanted)
            })
            .unwrap_or_else(|| panic!("fixture bug: no Target{param_types:?} declaration"))
    };
    let int_constructor = find_by_params(&["int"]);
    let string_constructor = find_by_params(&["String"]);
    let caller_constructor = find_by_params(&["Object", "String"]);
    let graph = bind_single_file(index);

    let int_dense = graph.dense_id_for(int_constructor).expect("Target(int) must be interned");
    let string_dense = graph
        .dense_id_for(string_constructor)
        .expect("Target(String) must be interned");
    let caller_dense = graph
        .dense_id_for(caller_constructor)
        .expect("Target(Object, String) must be interned");

    // Liveness: named-type evidence is tag-only and must never exclude a
    // candidate, so Target(int) stays referenced even though it is
    // provably the wrong overload.
    assert_eq!(
        graph.is_definitely_dead_code(int_dense),
        Some(false),
        "Target(int) must never be reported definitely dead -- named-type evidence is tag-only"
    );
    assert_eq!(
        graph.is_definitely_dead_code(string_dense),
        Some(false),
        "Target(String) must be reached by this(value)"
    );

    // Tag accuracy: only Target(String) may carry OVERLOAD_ARG_TYPE_MATCH.
    use xray_core::graph::reasons::OVERLOAD_ARG_TYPE_MATCH;
    let int_bits = graph.edge_evidence(caller_dense, int_dense);
    let string_bits = graph
        .edge_evidence(caller_dense, string_dense)
        .expect("Target(Object, String) -> Target(String) edge must exist");
    assert_ne!(
        string_bits & OVERLOAD_ARG_TYPE_MATCH,
        0,
        "Target(String) must carry OVERLOAD_ARG_TYPE_MATCH: the argument matches exactly"
    );
    if let Some(bits) = int_bits {
        assert_eq!(
            bits & OVERLOAD_ARG_TYPE_MATCH,
            0,
            "Target(int) must never carry OVERLOAD_ARG_TYPE_MATCH: \
             a String argument is closed-world provably not assignable to int"
        );
    }
}

/// Mission REQUIRED-ordering step 4: a varargs constructor
/// `Target(int, String...)` must stay referenced for EVERY call arity at
/// or above its fixed parameter count -- `param_count_matches_arity`
/// (resolve.rs) already implements `arg_count >= param_count - 1` for a
/// varargs declaration, and this test proves that holds end-to-end
/// through the real Java extractor for the CONSTRUCTOR path specifically
/// (the pre-existing `varargs_declaration_matches_any_arg_count_at_or_
/// above_its_minimum` unit test in resolve.rs only proves the resolver
/// logic in isolation via a hand-built `DeclInfo`, never through
/// `extract_construction`'s real arg_count/arg_shapes extraction).
#[test]
fn varargs_constructor_stays_live_for_every_arity_at_or_above_its_fixed_parameters() {
    let source = r#"
class Target {
    private Target(int a, String... rest) {}

    static void make() {
        new Target(1);
        new Target(1, "a");
        new Target(1, "a", "b");
    }
}
"#;
    let index = extract_java(source);
    let constructors = declaration_symbols(&index, "Target");
    assert_eq!(
        constructors.len(),
        1,
        "fixture must contain exactly one Target constructor"
    );
    let constructor = constructors[0];
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(constructor)
        .expect("constructor must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "a varargs constructor must stay referenced across every call arity at or above its fixed parameters"
    );
}

/// #1873/#1875 rework, F4 (P12): a qualified constructed type is still a
/// real constructor call. `Outer.Inner` is represented by tree-sitter as a
/// `scoped_type_identifier`; the extractor must retain its final type name
/// when it records the construction site.
#[test]
fn qualified_new_expression_reaches_private_inner_constructor() {
    let source = r#"
class Outer {
    static class Inner { private Inner() {} }
    Object make() { return new Outer.Inner(); }
}
"#;
    let index = extract_java(source);
    let inner_constructor = declaration_symbols(&index, "Inner")
        .into_iter()
        .next()
        .expect("Inner constructor must be extracted");
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(inner_constructor)
        .expect("Inner constructor must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "new Outer.Inner() must retain a caller edge to Inner's private constructor"
    );
}

/// #1873/#1875 rework, F4 (P8): explicit method-reference type arguments
/// are a named tree-sitter child between the receiver and method name. They
/// must not cause an otherwise ordinary `this::m` reference to be ignored.
#[test]
fn generic_this_method_reference_reaches_private_method() {
    let source = r#"
import java.util.function.Consumer;
class GenericReferences {
    private <T> void m(T value) {}
    Consumer<String> make() { return this::<String>m; }
}
"#;
    let index = extract_java(source);
    let method = declaration_symbol(&index, "m");
    let graph = bind_single_file(index);
    let dense = graph.dense_id_for(method).expect("m must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "this::<String>m must retain a caller edge to m"
    );
}

/// KNOWN LIMITATION (F4 P6), pinned deliberately -- do not "fix" by making
/// this assertion match reality.
///
/// The CORRECT answer for `constructor` here is `Some(false)`: `Status`'s
/// private constructor IS called, implicitly, by the enum constant
/// `READY("ready")`. This test asserts the current WRONG answer,
/// `Some(true)` (definitely dead), on purpose -- the extractor has no
/// enum-constant extraction path at all (`enum_constant` is not one of the
/// three reference-producing node kinds: `method_invocation`,
/// `object_creation_expression`, `explicit_constructor_invocation`), so an
/// enum constant's implicit call to its enum's own constructor is
/// structurally invisible to the graph today. See "Enum-constant
/// construction" under "What is invisible to the graph" in
/// `docs/xray-architecture.md`.
///
/// If a future change teaches the extractor to model enum-constant
/// construction, THIS TEST WILL FAIL -- that failure is the fix working
/// correctly and should be embraced, not treated as a regression: update
/// the assertion to `Some(false)`, delete this test's KNOWN LIMITATION
/// framing, and remove the corresponding bullet from
/// `docs/xray-architecture.md`.
#[test]
fn known_limitation_enum_constant_constructor_not_referenced() {
    let source = r#"
enum Status {
    READY("ready");
    private Status(String label) {}
}
"#;
    let index = extract_java(source);
    let constructor = declaration_symbols(&index, "Status")
        .into_iter()
        .next()
        .expect("Status constructor must be extracted");
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(constructor)
        .expect("Status constructor must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "KNOWN LIMITATION: the correct answer is Some(false) -- READY(\"ready\") \
         does call this private constructor, but enum-constant construction has \
         no extraction path yet. See docs/xray-architecture.md, \
         \"What is invisible to the graph\" -> \"Enum-constant construction\"."
    );
}

/// KNOWN LIMITATION (F4 P7), pinned deliberately -- do not "fix" by making
/// this assertion match reality.
///
/// The CORRECT answer for `constructor` (`Base`'s private, no-arg
/// constructor) is `Some(false)`: `Child extends Base` with `Child()`
/// having no explicit constructor-invocation statement means Java inserts
/// an implicit `super()` call, which DOES reach `Base()`. This test asserts
/// the current WRONG answer, `Some(true)` (definitely dead), on purpose --
/// D3a's explicit-constructor-invocation handling only fires for a real
/// `this(...)`/`super(...)` AST node, and tree-sitter-java's grammar has NO
/// node at all for an implicit `super()` (there is nothing to walk), so
/// this edge is structurally invisible to the graph today. See "Implicit
/// superclass-constructor calls" under "What is invisible to the graph" in
/// `docs/xray-architecture.md`.
///
/// If a future change synthesizes an implicit-super-constructor edge for
/// every constructor with no explicit invocation, THIS TEST WILL FAIL --
/// that failure is the fix working correctly and should be embraced, not
/// treated as a regression: update the assertion to `Some(false)`, delete
/// this test's KNOWN LIMITATION framing, and remove the corresponding
/// bullet from `docs/xray-architecture.md`.
///
/// Bug #1926 addendum: `Base` deliberately declares a SECOND, unrelated
/// constructor overload (`Base(int)`) purely so this fixture is not ALSO
/// the class's ONLY constructor -- otherwise Bug #1926's lone-private-
/// no-arg-constructor non-instantiability exception (a real, independent
/// fix: `java_methods::suppress_lone_private_no_arg_constructor_dead_
/// signal`) would suppress the `Some(true)` verdict here for an unrelated
/// reason, masking the specific implicit-super-call limitation this test
/// exists to pin. `Base(int)` is never called by anything either, so it
/// changes nothing else about what this fixture demonstrates.
#[test]
fn known_limitation_implicit_super_constructor_not_referenced() {
    let source = r#"
class Outer {
    static class Base {
        private Base() {}
        private Base(int unused) {}
    }
    static class Child extends Base { Child() {} }
}
"#;
    let index = extract_java(source);
    let constructor = declaration_symbols(&index, "Base")
        .into_iter()
        .find(|&symbol| {
            index
                .declarations
                .iter()
                .any(|d| d.symbol == symbol && d.param_count == Some(0))
        })
        .expect("Base's no-arg constructor must be extracted");
    let graph = bind_single_file(index);
    let dense = graph
        .dense_id_for(constructor)
        .expect("Base constructor must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "KNOWN LIMITATION: the correct answer is Some(false) -- Child()'s \
         implicit super() call does reach Base()'s private constructor, but \
         Java's implicit super() insertion has no AST node to extract and no \
         synthesized edge yet. See docs/xray-architecture.md, \"What is \
         invisible to the graph\" -> \"Implicit superclass-constructor calls\"."
    );
}
