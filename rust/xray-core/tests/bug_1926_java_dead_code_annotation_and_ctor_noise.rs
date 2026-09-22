//! Regression tests for Issue #1926 -- X-Ray graph mode's
//! `is_definitely_dead_code` on real Java code is dominated by two
//! mechanical, recognisable false-positive classes rather than genuine dead
//! code:
//!
//! 1. A `private Foo() {}` no-arg constructor that is the ONLY constructor
//!    its class declares is the standard Java idiom for an intentionally
//!    non-instantiable utility class -- "unreferenced" is the INTENDED
//!    state, not evidence of dead code.
//! 2. A private/package test method referenced only by NAME from a JUnit5
//!    `@MethodSource` annotation (invoked by reflection) is reachable, but
//!    the extractor never turned that string-literal reference into a
//!    graph edge.
//!
//! Every fixture below is realistically compilable Java (`com.example`
//! neutral names throughout, per this repo's public-disclosure rule) run
//! through the REAL `JavaExtractor` -> REAL `bind_with_budget` pipeline --
//! never a hand-built `LocalIndex` or a mocked graph -- then asserted
//! through `CodeGraph::is_definitely_dead_code` exactly as `analyze_graph`
//! consumes it.
//!
//! `is_definitely_dead_code` only ever reports `Some(true)` for a
//! declaration that is BOTH unreferenced AND `Visibility::Private`
//! (`code_graph.rs`); this whole test file is about widening that FIRST
//! condition, never the second, so every "must not be dead" assertion below
//! is discriminating specifically against `Some(true)`, not against
//! `Visibility`.
//!
//! Scope note on `@MethodSource("Class#method")`: only the SELF-qualified
//! form (`Class` textually equal to the annotated method's own enclosing
//! class) is resolved -- that is the one case a single file's own local
//! syntax can answer "does this class exist" for cheaply and unambiguously
//! (it is the class the annotation is already declared inside). A
//! DIFFERENT class name is deliberately left unresolved (no edge created):
//! proving that in-repo resolution genuinely requires a repo-wide type
//! index this extractor does not build, so guessing would risk exactly the
//! "bare-name guess across classes" the issue explicitly forbids. The
//! `method_source_cross_class_reference_...` test below asserts that safe
//! "no edge" outcome, not that cross-class resolution is implemented.

use xray_core::graph::bind::{bind_with_budget, FileForBind};
use xray_core::graph::budget::{AnalysisCompleteness, IndexBudget};
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::local_index::{Declaration, LocalIndex, Visibility};
use xray_core::graph::extract::LanguageExtractor;
use xray_core::graph::identity::SymbolId;

const FILE_ID: u32 = 1;

/// Parses `source` as a real temp `.java` file through the real scanner,
/// then runs it through the real `JavaExtractor` -- the exact production
/// path, never a hand-built `LocalIndex`. Mirrors
/// `bug_1873_java_method_reference_dead_code.rs`'s own harness verbatim.
fn extract_java(source: &str) -> LocalIndex {
    extract_java_as(source, FILE_ID)
}

/// Same as `extract_java`, but under an explicit `file_id` -- needed for a
/// fixture that binds more than one file together (see `bind_files`).
fn extract_java_as(source: &str, file_id: u32) -> LocalIndex {
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
    bind_files(vec![(FILE_ID, index)])
}

/// Same as `bind_single_file`, but for MORE than one already-extracted
/// file bound together -- needed to prove a decoy in a DIFFERENT file (in
/// the same package) is never reachable by `@MethodSource` resolution.
fn bind_files(files: Vec<(u32, LocalIndex)>) -> CodeGraph {
    let graph = bind_with_budget(
        files
            .into_iter()
            .map(|(file_id, index)| FileForBind {
                file_id,
                language: "java".to_string(),
                index,
            })
            .collect(),
        &IndexBudget::unlimited(),
    );
    assert_eq!(
        graph.completeness(),
        AnalysisCompleteness::Complete,
        "fixture sanity: an unlimited-budget bind must report Complete"
    );
    graph
}

/// The symbol of the single declaration named `name`. Panics loudly if the
/// name is missing or ambiguous -- use `declaration_symbol_owned_by` for a
/// fixture that legitimately declares more than one thing under the same
/// name.
fn declaration_symbol(index: &LocalIndex, name: &str) -> SymbolId {
    let matches: Vec<&Declaration> = index.declarations.iter().filter(|d| d.name == name).collect();
    match matches.as_slice() {
        [only] => only.symbol,
        [] => panic!("fixture bug: no declaration named {name:?}"),
        other => panic!(
            "fixture bug: {name:?} is ambiguous ({} declarations) -- narrow the fixture or use \
             declaration_symbol_owned_by",
            other.len()
        ),
    }
}

/// The symbol of the declaration named `name` and owned (via
/// `MethodOwnerRecord`) by `enclosing_type` -- for a fixture that
/// deliberately declares two same-named methods on different classes, or an
/// overload sharing a name with its own owner class (a constructor).
fn declaration_symbol_owned_by(index: &LocalIndex, name: &str, enclosing_type: &str) -> SymbolId {
    let owner_symbols: std::collections::HashSet<SymbolId> = index
        .method_owners
        .iter()
        .filter(|o| o.enclosing_type == enclosing_type)
        .map(|o| o.method_symbol)
        .collect();
    index
        .declarations
        .iter()
        .find(|d| d.name == name && owner_symbols.contains(&d.symbol))
        .unwrap_or_else(|| panic!("fixture bug: no {name:?} declaration owned by {enclosing_type:?}"))
        .symbol
}

fn dead(graph: &CodeGraph, symbol: SymbolId) -> Option<bool> {
    let dense = graph
        .dense_id_for(symbol)
        .expect("symbol must be interned in the bound graph");
    graph.is_definitely_dead_code(dense)
}

/// P2 rework (#1926): the fix must NEVER rewrite a constructor's own
/// declared `Visibility` -- `visibility_for` is documented as "declared
/// visibility" and also feeds the binder's private-candidate filter, an
/// unrelated concern. Mirrors `dead`'s exact shape.
fn visibility(graph: &CodeGraph, symbol: SymbolId) -> Visibility {
    let dense = graph
        .dense_id_for(symbol)
        .expect("symbol must be interned in the bound graph");
    graph.visibility_for(dense)
}

// ---------------------------------------------------------------------
// Class 1: lone private no-arg constructor (non-instantiable utility idiom)
// ---------------------------------------------------------------------

#[test]
fn lone_private_no_arg_constructor_is_not_reported_dead() {
    let source = r#"
package com.example;

final class StringUtils {
    private StringUtils() {}

    static String trim(String s) {
        return s.trim();
    }
}
"#;
    let index = extract_java(source);
    let ctor = declaration_symbol_owned_by(&index, "StringUtils", "StringUtils");
    let graph = bind_single_file(index);
    assert_eq!(
        visibility(&graph, ctor),
        Visibility::Private,
        "P2: the fix must never rewrite the constructor's OWN declared visibility -- it stays \
         truthfully Private even though its dead-code verdict changes"
    );
    assert_eq!(
        dead(&graph, ctor),
        None,
        "a class's sole private no-arg constructor is the standard non-instantiability idiom -- \
         it must be reported undecidable (None), never definitely dead"
    );
}

#[test]
fn private_constructor_with_a_public_sibling_constructor_unreferenced_is_still_dead() {
    let source = r#"
package com.example;

class Widget {
    public Widget(String name) {}

    private Widget() {}
}
"#;
    let index = extract_java(source);
    let private_no_arg_ctor = index
        .declarations
        .iter()
        .find(|d| d.name == "Widget" && d.param_count == Some(0))
        .expect("fixture bug: no no-arg Widget constructor")
        .symbol;
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, private_no_arg_ctor),
        Some(true),
        "Widget's private no-arg constructor has a real public sibling constructor -- it is NOT \
         the class's only constructor, so it must remain reported definitely dead when unreferenced"
    );
}

#[test]
fn lone_private_constructor_that_takes_parameters_is_still_reported_dead() {
    let source = r#"
package com.example;

class Repository {
    private Repository(String connectionString) {}
}
"#;
    let index = extract_java(source);
    let ctor = index
        .declarations
        .iter()
        .find(|d| d.name == "Repository" && d.param_count == Some(1))
        .expect("fixture bug: no one-arg Repository constructor")
        .symbol;
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, ctor),
        Some(true),
        "a private constructor that takes parameters is not the non-instantiability idiom -- it \
         must remain reported definitely dead when unreferenced, even if it is the only constructor"
    );
}

#[test]
fn constructor_exception_never_leaks_onto_an_ordinary_unreferenced_private_method() {
    // Sanity/scope guard: the constructor exception must never leak onto an
    // ordinary unreferenced private METHOD (only `DeclarationKind::Method`
    // declarations shaped like a CONSTRUCTOR qualify).
    let source = r#"
package com.example;

final class Holder {
    private Holder() {}

    private void neverCalled() {}
}
"#;
    let index = extract_java(source);
    let ctor = declaration_symbol_owned_by(&index, "Holder", "Holder");
    let method = declaration_symbol(&index, "neverCalled");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, ctor),
        None,
        "Holder's lone private no-arg constructor must be undecidable, not definitely dead"
    );
    assert_eq!(
        dead(&graph, method),
        Some(true),
        "an ordinary unreferenced private method is NOT a constructor and must remain reported \
         definitely dead -- the constructor exception must never leak onto it"
    );
}

/// Bug #1926 owner-identity regression: `OuterA.Inner` and `OuterB.Inner`
/// are two DIFFERENT nested classes that
/// happen to share the bare name `Inner`. Each is independently the sole
/// constructor of ITS OWN class, so BOTH must qualify for the exception --
/// a bare-name-keyed count would wrongly merge them into "2 constructors
/// named Inner" and exclude both, a real regression this test pins against.
#[test]
fn lone_ctor_exception_counts_constructors_per_owning_type_not_bare_name() {
    let source = r#"
package com.example;

class OuterA {
    static class Inner {
        private Inner() {}
    }
}

class OuterB {
    static class Inner {
        private Inner() {}
    }
}
"#;
    let index = extract_java(source);
    let inner_ctors: Vec<SymbolId> = index
        .declarations
        .iter()
        .filter(|d| d.name == "Inner" && d.param_count == Some(0))
        .map(|d| d.symbol)
        .collect();
    assert_eq!(inner_ctors.len(), 2, "fixture bug: expected exactly two Inner() constructors");
    let graph = bind_single_file(index);
    for &ctor in &inner_ctors {
        assert_eq!(
            dead(&graph, ctor),
            None,
            "each Inner() is the sole constructor of ITS OWN owning type -- bare-name collision \
             with the unrelated sibling Inner class must never merge their counts"
        );
    }
}

// ---------------------------------------------------------------------
// Class 2: JUnit5 @MethodSource reflection references
// ---------------------------------------------------------------------

#[test]
fn method_source_bare_string_argument_marks_the_named_provider_as_not_dead() {
    let source = r#"
package com.example;

class ParserSelectionTest {
    @ParameterizedTest
    @MethodSource("supportedParsers")
    private void testParsing(String parser) {}

    private static java.util.stream.Stream<String> supportedParsers() {
        return java.util.stream.Stream.of("a", "b");
    }
}
"#;
    let index = extract_java(source);
    let provider = declaration_symbol(&index, "supportedParsers");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, provider),
        Some(false),
        "@MethodSource(\"supportedParsers\") must reference supportedParsers's declaration -- it must \
         never be reported definitely dead"
    );
}

#[test]
fn method_source_array_form_marks_every_named_provider_as_not_dead() {
    let source = r#"
package com.example;

class MultiSourceTest {
    @ParameterizedTest
    @MethodSource({"providerA", "providerB"})
    private void testBoth(String value) {}

    private static java.util.stream.Stream<String> providerA() {
        return java.util.stream.Stream.of("a");
    }

    private static java.util.stream.Stream<String> providerB() {
        return java.util.stream.Stream.of("b");
    }
}
"#;
    let index = extract_java(source);
    let provider_a = declaration_symbol(&index, "providerA");
    let provider_b = declaration_symbol(&index, "providerB");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, provider_a),
        Some(false),
        "@MethodSource({{\"providerA\", ...}}) must reference providerA -- it must never be dead"
    );
    assert_eq!(
        dead(&graph, provider_b),
        Some(false),
        "@MethodSource({{..., \"providerB\"}}) must reference providerB -- it must never be dead"
    );
}

#[test]
fn method_source_named_value_form_marks_the_named_provider_as_not_dead() {
    let source = r#"
package com.example;

class NamedValueTest {
    @ParameterizedTest
    @MethodSource(value = "namedProvider")
    private void testNamed(String value) {}

    private static java.util.stream.Stream<String> namedProvider() {
        return java.util.stream.Stream.of("x");
    }
}
"#;
    let index = extract_java(source);
    let provider = declaration_symbol(&index, "namedProvider");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, provider),
        Some(false),
        "@MethodSource(value = \"namedProvider\") must reference namedProvider -- it must never be dead"
    );
}

#[test]
fn method_source_empty_value_defaults_to_the_same_named_zero_arg_provider() {
    let source = r#"
package com.example;

class DefaultNameTest {
    @ParameterizedTest
    @MethodSource
    private void supply(String value) {}

    private static java.util.stream.Stream<String> supply() {
        return java.util.stream.Stream.of("y");
    }
}
"#;
    let index = extract_java(source);
    let provider = index
        .declarations
        .iter()
        .find(|d| d.name == "supply" && d.param_count == Some(0))
        .expect("fixture bug: no zero-arg supply() declaration")
        .symbol;
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, provider),
        Some(false),
        "a bare @MethodSource with no explicit value defaults to a same-named, no-arg provider \
         (JUnit5's own documented default) -- it must never be reported definitely dead"
    );
}

/// Bug #1926 (final round): JUnit5's same-name default ALWAYS names a
/// DIFFERENT factory method -- a zero-arg method and a parameterized test
/// method cannot legally share the exact same name AND signature in real
/// Java. Here `supply()` is annotated on ITSELF (a zero-arg method with a
/// bare `@MethodSource`), so the only same-name, zero-arg candidate in its
/// owning type IS the annotated method itself; `resolve_method_source_
/// edges` must exclude that self-match and resolve to nothing, never
/// fabricating a self-edge that would hide a genuinely dead method.
#[test]
fn method_source_empty_value_default_never_self_references_a_zero_arg_annotated_method() {
    let source = r#"
package com.example;

class SelfOnlyTest {
    @ParameterizedTest
    @MethodSource
    private void supply() {}
}
"#;
    let index = extract_java(source);
    let supply = declaration_symbol(&index, "supply");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, supply),
        Some(true),
        "a bare @MethodSource on a zero-arg method whose only same-name candidate is itself must \
         resolve to nothing -- a self-edge would hide a genuinely dead annotated method"
    );
}

/// Bug #1926 (final round): JUnit5 treats a BLANK `@MethodSource("")`
/// identically to no value at all, applying the same-name default.
#[test]
fn method_source_blank_string_value_defaults_like_the_empty_value() {
    let source = r#"
package com.example;

class BlankValueTest {
    @ParameterizedTest
    @MethodSource("")
    private void supply(String value) {}

    private static java.util.stream.Stream<String> supply() {
        return java.util.stream.Stream.of("y");
    }
}
"#;
    let index = extract_java(source);
    let provider = index
        .declarations
        .iter()
        .find(|d| d.name == "supply" && d.param_count == Some(0))
        .expect("fixture bug: no zero-arg supply() declaration")
        .symbol;
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, provider),
        Some(false),
        "@MethodSource(\"\") (a blank string) must be treated exactly like the no-argument \
         default and reference the same-named zero-arg provider"
    );
}

/// Shared assertion pair for the three same-arity-decoy tests below: the
/// real provider must be referenced, and the decoy (wherever it lives)
/// must stay reported dead.
fn assert_provider_live_decoy_dead(graph: &CodeGraph, provider: SymbolId, decoy: SymbolId, decoy_description: &str) {
    assert_eq!(
        dead(graph, provider),
        Some(false),
        "the real zero-arg supply() provider must be referenced by the default @MethodSource rule"
    );
    assert_eq!(
        dead(graph, decoy),
        Some(true),
        "{decoy_description} must NEVER be bound by an @MethodSource default outside its own \
         owning type, even though it shares the exact same name AND arity"
    );
}

/// Bug #1926 (final round): JUnit5 resolves an `@MethodSource` factory in
/// the test class ONLY -- never an outer class merely lexically
/// surrounding a nested test class. `OuterContainer` declares its OWN
/// same-NAME, same-ARITY (zero-arg) `supply()` -- deliberately identical
/// in every way a name-based lookup could match, so arity cannot be what
/// excludes it. `resolve_method_source_edges` (`java_methods.rs`) resolves
/// PURELY by `(owner_type_symbol, name)`: `OuterContainer`'s own `supply()`
/// is keyed under `OuterContainer`'s symbol, `SupplyTest`'s `@MethodSource`
/// request is scoped to `SupplyTest`'s symbol only, so the outer decoy is
/// never even a candidate -- this never reaches the generic binder at all.
#[test]
fn method_source_empty_value_default_never_binds_a_same_arity_outer_class_decoy() {
    let source = r#"
package com.example;

class OuterContainer {
    private static java.util.stream.Stream<String> supply() {
        return java.util.stream.Stream.of("outer");
    }

    static class SupplyTest {
        @ParameterizedTest
        @MethodSource
        private void supply(String value) {}

        private static java.util.stream.Stream<String> supply() {
            return java.util.stream.Stream.of("y");
        }
    }
}
"#;
    let index = extract_java(source);
    let outer_decoy = declaration_symbol_owned_by(&index, "supply", "OuterContainer");
    let provider = declaration_symbol_owned_by(&index, "supply", "SupplyTest");
    let graph = bind_single_file(index);
    assert_provider_live_decoy_dead(&graph, provider, outer_decoy, "OuterContainer.supply()");
}

/// Bug #1926 (final round): mirrors the outer-class decoy test immediately
/// above, but the same-arity decoy is instead a SIBLING nested class under
/// the SAME top-level outer as the test class -- the other position that
/// must never be reachable, for the identical owner-symbol-scoping reason.
#[test]
fn method_source_empty_value_default_never_binds_a_same_arity_sibling_nested_class_decoy() {
    let source = r#"
package com.example;

class SiblingContainer {
    static class Sibling {
        private static java.util.stream.Stream<String> supply() {
            return java.util.stream.Stream.of("sibling");
        }
    }

    static class SupplyTest {
        @ParameterizedTest
        @MethodSource
        private void supply(String value) {}

        private static java.util.stream.Stream<String> supply() {
            return java.util.stream.Stream.of("y");
        }
    }
}
"#;
    let index = extract_java(source);
    let sibling_decoy = declaration_symbol_owned_by(&index, "supply", "Sibling");
    let provider = declaration_symbol_owned_by(&index, "supply", "SupplyTest");
    let graph = bind_single_file(index);
    assert_provider_live_decoy_dead(&graph, provider, sibling_decoy, "Sibling.supply()");
}

/// Bug #1926 (final round): the same-arity decoy this time lives in a
/// DIFFERENT FILE in the SAME PACKAGE -- `resolve_method_source_edges`
/// resolves purely within its OWN file's `LocalIndex` (built and resolved
/// before any file is ever bound to another), so a same-package sibling
/// file's same-named, same-arity method is structurally impossible to
/// reach, independent of anything the generic binder's SAME_PACKAGE
/// evidence would otherwise admit.
#[test]
fn method_source_empty_value_default_never_binds_a_same_arity_decoy_in_another_file_in_the_same_package() {
    const TEST_FILE_ID: u32 = 1;
    const DECOY_FILE_ID: u32 = 2;
    let test_source = r#"
package com.example;

class PackageFileTest {
    @ParameterizedTest
    @MethodSource
    private void supply(String value) {}

    private static java.util.stream.Stream<String> supply() {
        return java.util.stream.Stream.of("y");
    }
}
"#;
    let decoy_source = r#"
package com.example;

class PackageSibling {
    private static java.util.stream.Stream<String> supply() {
        return java.util.stream.Stream.of("other-file");
    }
}
"#;
    let test_index = extract_java_as(test_source, TEST_FILE_ID);
    let decoy_index = extract_java_as(decoy_source, DECOY_FILE_ID);
    let provider = declaration_symbol_owned_by(&test_index, "supply", "PackageFileTest");
    let decoy = declaration_symbol_owned_by(&decoy_index, "supply", "PackageSibling");
    let graph = bind_files(vec![(TEST_FILE_ID, test_index), (DECOY_FILE_ID, decoy_index)]);
    assert_provider_live_decoy_dead(&graph, provider, decoy, "PackageSibling.supply() (a different file)");
}

#[test]
fn method_source_naming_a_method_that_does_not_exist_creates_no_edge_and_does_not_panic() {
    let source = r#"
package com.example;

class GhostProviderTest {
    @ParameterizedTest
    @MethodSource("doesNotExist")
    private void testGhost(String value) {}

    private String unrelatedNeverCalled() {
        return "x";
    }
}
"#;
    let index = extract_java(source);
    let unrelated = declaration_symbol(&index, "unrelatedNeverCalled");
    // Must not panic while binding a reference whose name resolves to zero
    // candidates anywhere in the repository.
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, unrelated),
        Some(true),
        "a @MethodSource string naming a method that does not exist must create no candidate -- it \
         must never accidentally mark an unrelated private method as referenced"
    );
}

/// Bug #1926 regression: an EXPLICIT
/// `@MethodSource` argument that resolves to zero local targets (here, a
/// `Class#method` form naming a class other than the annotation's own
/// enclosing type) must NEVER fall back to the JUnit5 same-name default.
/// The annotated method's OWN name is deliberately `test` -- the same
/// name as its private zero-arg overload `test()` -- so a wrong
/// implementation that ignores the unresolvable explicit argument and
/// falls back to the default would fabricate a false edge to `test()`
/// exactly the way it would for a genuinely empty `@MethodSource`.
#[test]
fn method_source_explicit_unresolvable_reference_never_falls_back_to_the_default_name() {
    let source = r#"
package com.example;

class ExternalReferenceTest {
    @ParameterizedTest
    @MethodSource("com.example.Other#provider")
    private void test(String value) {}

    private static java.util.stream.Stream<String> test() {
        return java.util.stream.Stream.of("x");
    }
}
"#;
    let index = extract_java(source);
    let zero_arg_test = index
        .declarations
        .iter()
        .find(|d| d.name == "test" && d.param_count == Some(0))
        .expect("fixture bug: no zero-arg test() declaration")
        .symbol;
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, zero_arg_test),
        Some(true),
        "@MethodSource(\"com.example.Other#provider\") is an EXPLICIT argument that resolves to no \
         local target -- it must never fall back to the same-name default and fabricate an edge to \
         the overloaded test() method"
    );
}

#[test]
fn method_source_cross_class_reference_is_never_a_bare_name_guess_across_classes() {
    // `Sibling` is nested under the SAME outer class as `CrossClassTest`
    // (both share the top-level type `CrossClassContainer`) and declares
    // its OWN unrelated `helper()` -- a decoy with the exact simple method
    // name referenced by the fully-qualified `@MethodSource` on `Sibling`
    // (a class other than `CrossClassTest`'s own enclosing type). This
    // test's pass/fail hinges entirely on `java_annotations.rs`'s own
    // `Class#method` self-qualification check inside `method_source_
    // target_names`: since `Sibling` is not `CrossClassTest`'s own
    // enclosing type, no target name is even recorded, so `resolve_
    // method_source_edges` never gets a chance to look anything up --
    // `@MethodSource` is resolved entirely at extraction time and never
    // reaches the generic binder at all.
    let source = r#"
package com.example;

class CrossClassContainer {
    static class CrossClassTest {
        @ParameterizedTest
        @MethodSource("com.example.Sibling#helper")
        private void testCross(String value) {}
    }

    static class Sibling {
        private static java.util.stream.Stream<String> helper() {
            return java.util.stream.Stream.of("z");
        }
    }
}
"#;
    let index = extract_java(source);
    let decoy = declaration_symbol_owned_by(&index, "helper", "Sibling");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, decoy),
        Some(true),
        "@MethodSource(\"com.example.Sibling#helper\") names a class that is neither \
         CrossClassTest's own enclosing type nor resolvable in this file -- it must never fall \
         back to a same-named method on a sibling nested class, even one sharing the SAME \
         top-level outer class"
    );
}

#[test]
fn method_source_self_qualified_class_hash_method_form_marks_the_provider_as_not_dead() {
    let source = r#"
package com.example;

class SelfQualifiedTest {
    @ParameterizedTest
    @MethodSource("SelfQualifiedTest#provider")
    private void testSelf(String value) {}

    private static java.util.stream.Stream<String> provider() {
        return java.util.stream.Stream.of("w");
    }
}
"#;
    let index = extract_java(source);
    let provider = declaration_symbol(&index, "provider");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, provider),
        Some(false),
        "@MethodSource(\"SelfQualifiedTest#provider\") qualifies the SAME class the annotation is \
         declared in -- this is cheap and unambiguous to resolve, and must reference provider()"
    );
}

// ---------------------------------------------------------------------
// Dead-set comparison: a small multi-symbol corpus proving the fix only
// ever REMOVES false Some(true) verdicts, never adds one.
// ---------------------------------------------------------------------

#[test]
fn dead_set_comparison_corpus_only_removes_false_positives_never_adds_one() {
    let source = r#"
package com.example;

final class UtilityHolder {
    private UtilityHolder() {}

    static int identity(int x) {
        return x;
    }
}

class ParityTest {
    @ParameterizedTest
    @MethodSource("cases")
    private void testParity(int n, boolean expected) {}

    private static java.util.stream.Stream<Object[]> cases() {
        return java.util.stream.Stream.of(new Object[] {2, true});
    }

    private String genuinelyDeadHelper() {
        return "never called";
    }
}
"#;
    let index = extract_java(source);
    let ctor = declaration_symbol_owned_by(&index, "UtilityHolder", "UtilityHolder");
    let provider = declaration_symbol(&index, "cases");
    let genuinely_dead = declaration_symbol(&index, "genuinelyDeadHelper");
    let graph = bind_single_file(index);

    // Before this fix, all three of these would have reported `Some(true)`.
    // After: only the genuinely dead helper still does.
    assert_eq!(
        dead(&graph, ctor),
        None,
        "the lone private no-arg constructor must move OUT of the dead set (Some(true) -> None)"
    );
    assert_eq!(
        dead(&graph, provider),
        Some(false),
        "the @MethodSource-referenced provider must move OUT of the dead set (Some(true) -> Some(false))"
    );
    assert_eq!(
        dead(&graph, genuinely_dead),
        Some(true),
        "the one genuinely dead, unreferenced private method must STAY in the dead set -- the fix \
         must never remove a true positive"
    );
}
