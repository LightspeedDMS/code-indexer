//! `kotlin.rs`'s unit tests -- relocated into their own file, mirroring
//! `java.rs` -> `java_tests.rs` (same split, same rationale: keeps both
//! files under this project's per-file line budget).

use super::*;
use std::path::Path;

fn extract_source(source: &str) -> LocalIndex {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("Sample.kt");
    std::fs::write(&path, source).unwrap();
    let root = crate::scanner::parse_file(Path::new(&path)).unwrap();
    KotlinExtractor.extract(&root, 7)
}

#[test]
fn extracts_package_declaration_with_a_signature() {
    let index = extract_source("package com.example.app\n\nclass Foo\n");
    let pkg = index.declaration_named("com.example.app").unwrap();
    assert_eq!(pkg.kind, DeclarationKind::Package);
    assert_eq!(
        index.signatures.get(&pkg.symbol).unwrap(),
        "package com.example.app"
    );
}

#[test]
fn extracts_ordinary_wildcard_and_aliased_imports() {
    let index = extract_source(
        "package com.example.app\n\nimport com.example.app.util.Helper\nimport com.example.app.util.*\nimport com.example.app.util.Helper as H2\n\nclass Foo\n",
    );
    assert_eq!(index.imports.len(), 3);
    assert_eq!(index.imports[0].kind, ImportKind::Ordinary);
    assert_eq!(index.imports[0].path, "com.example.app.util.Helper");
    assert_eq!(index.imports[1].kind, ImportKind::Wildcard);
    assert_eq!(index.imports[1].path, "com.example.app.util");
    assert_eq!(index.imports[2].kind, ImportKind::Ordinary);
    assert_eq!(index.imports[2].path, "com.example.app.util.Helper");
}

#[test]
fn extracts_a_class_declaration_as_a_type() {
    let index = extract_source("class Greeter\n");
    let decl = index.declaration_named("Greeter").unwrap();
    assert_eq!(decl.kind, DeclarationKind::Type);
    assert_eq!(index.signatures.get(&decl.symbol).unwrap(), "class Greeter");
}

#[test]
fn extracts_an_interface_declaration_and_records_it_as_an_interface_name() {
    let index = extract_source("interface Marker {\n    fun run()\n}\n");
    let decl = index.declaration_named("Marker").unwrap();
    assert_eq!(decl.kind, DeclarationKind::Type);
    assert_eq!(index.signatures.get(&decl.symbol).unwrap(), "interface Marker");
    assert!(index.interface_names.contains(&"Marker".to_string()));
}

#[test]
fn extracts_a_top_level_object_declaration_as_a_type() {
    let index = extract_source(
        "object Singleton {\n    fun ping(): String = \"pong\"\n}\n",
    );
    let decl = index.declaration_named("Singleton").unwrap();
    assert_eq!(decl.kind, DeclarationKind::Type);
    assert_eq!(index.signatures.get(&decl.symbol).unwrap(), "object Singleton");
}

#[test]
fn extracts_an_unnamed_companion_object_as_companion() {
    let index = extract_source(
        "class Greeter {\n    companion object {\n        fun create(): Greeter = Greeter()\n    }\n}\n",
    );
    let decl = index.declaration_named("Companion").unwrap();
    assert_eq!(decl.kind, DeclarationKind::Type);
}

#[test]
fn extracts_a_member_function_owned_by_its_enclosing_type() {
    let index = extract_source(
        "class Greeter {\n    fun greet(): String {\n        return \"hi\"\n    }\n}\n",
    );
    let decl = index.declaration_named("greet").unwrap();
    assert_eq!(decl.kind, DeclarationKind::Method);
    assert_eq!(decl.param_count, Some(0));
    assert!(index
        .method_owners
        .iter()
        .any(|o| o.method_symbol == decl.symbol && o.enclosing_type == "Greeter"));
}

#[test]
fn extracts_a_top_level_function_with_parameter_count_and_types() {
    let index = extract_source(
        "fun process(a: String, b: Int): String {\n    return a\n}\n",
    );
    let decl = index.declaration_named("process").unwrap();
    assert_eq!(decl.kind, DeclarationKind::Method);
    assert_eq!(decl.param_count, Some(2));
    assert_eq!(decl.param_types, vec!["String".to_string(), "Int".to_string()]);
}

#[test]
fn extracts_an_extension_function_by_its_own_bare_name_never_the_receiver_type() {
    let index = extract_source(
        "fun String.shout(): String {\n    return this\n}\n",
    );
    let decl = index.declaration_named("shout").unwrap();
    assert_eq!(decl.kind, DeclarationKind::Method);
    assert!(index.declaration_named("String").is_none());
}

#[test]
fn extracts_a_secondary_constructor_named_after_the_enclosing_type() {
    let index = extract_source(
        "class Outer {\n    constructor(n: Int)\n}\n",
    );
    let ctor = index
        .declarations
        .iter()
        .find(|d| d.name == "Outer" && d.kind == DeclarationKind::Method)
        .expect("secondary constructor must be recorded as a Method declaration named Outer");
    assert_eq!(ctor.param_count, Some(1));
    assert!(index
        .method_owners
        .iter()
        .any(|o| o.method_symbol == ctor.symbol && o.enclosing_type == "Outer"));
}

#[test]
fn primary_constructor_val_parameters_become_field_properties() {
    let index = extract_source("class Point(val x: Int, val y: Int)\n");
    let x = index.declaration_named("x").unwrap();
    assert_eq!(x.kind, DeclarationKind::Field);
    let y = index.declaration_named("y").unwrap();
    assert_eq!(y.kind, DeclarationKind::Field);
}

#[test]
fn primary_constructor_plain_parameters_are_not_recorded_as_declarations() {
    let index = extract_source("class WithBy(base: Base)\n\nopen class Base\n");
    assert!(index.declaration_named("base").is_none());
}

#[test]
fn extracts_a_property_declaration_as_a_field() {
    let index = extract_source("val greeting: String = \"hi\"\n");
    let decl = index.declaration_named("greeting").unwrap();
    assert_eq!(decl.kind, DeclarationKind::Field);
}

#[test]
fn extracts_a_const_val_as_a_constant() {
    let index = extract_source("const val LIMIT: Int = 7\n");
    let decl = index.declaration_named("LIMIT").unwrap();
    assert_eq!(decl.kind, DeclarationKind::Constant);
}

#[test]
fn extracts_enum_entries_as_constants_owned_by_the_enum_type() {
    let index = extract_source("enum class Color {\n    RED,\n    GREEN\n}\n");
    let red = index.declaration_named("RED").unwrap();
    assert_eq!(red.kind, DeclarationKind::Constant);
    let green = index.declaration_named("GREEN").unwrap();
    assert_eq!(green.kind, DeclarationKind::Constant);
}

#[test]
fn extracts_a_bare_call_as_an_invocation_with_arity() {
    let index = extract_source(
        "fun helper(x: String): String {\n    return x\n}\n\nfun demo() {\n    helper(\"a\")\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "helper")
        .expect("helper(...) must be extracted as an invocation");
    assert_eq!(site.arg_count, Some(1));
    assert_eq!(site.receiver, ReceiverExpr::None);
}

#[test]
fn extracts_a_qualified_call_with_an_identifier_receiver() {
    let index = extract_source(
        "class Greeter {\n    fun greet() {}\n}\n\nfun demo() {\n    val g = Greeter()\n    g.greet()\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "greet")
        .expect("g.greet() must be extracted as an invocation");
    assert_eq!(site.receiver, ReceiverExpr::Identifier("g".to_string()));
}

/// The module's central design decision: `Foo()` is indistinguishable
/// from a function call at the grammar level, so a bare uppercase-led
/// call site must produce BOTH an `InvocationSite` (arity/import-context
/// evidence) AND a `ConstructionSite` (so the TYPE itself is never
/// reported as unreferenced), exactly like `JavaExtractor::extract_
/// construction` does for `new Foo()`.
#[test]
fn a_bare_uppercase_call_produces_both_an_invocation_and_a_construction_site() {
    let index = extract_source(
        "class Greeter\n\nfun demo() {\n    val g = Greeter()\n}\n",
    );
    assert!(index.invocations.iter().any(|i| i.callee_name == "Greeter"));
    assert!(index
        .constructions
        .iter()
        .any(|c| c.type_name == "Greeter"));
}

#[test]
fn a_bare_lowercase_call_never_produces_a_construction_site() {
    let index = extract_source("fun helper() {}\n\nfun demo() {\n    helper()\n}\n");
    assert!(!index.constructions.iter().any(|c| c.type_name == "helper"));
}

#[test]
fn extracts_a_trailing_lambda_call_with_arity_one() {
    let index = extract_source(
        "fun demo(items: List<String>) {\n    items.forEach { item -> println(item) }\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "forEach")
        .expect("forEach { ... } must be extracted as an invocation");
    assert_eq!(site.arg_count, Some(1));
}

#[test]
fn extracts_a_bare_callable_reference_to_a_top_level_function() {
    let index = extract_source(
        "fun topLevelFn() {}\n\nfun demo() {\n    val ref1 = ::topLevelFn\n}\n",
    );
    assert!(index
        .invocations
        .iter()
        .any(|i| i.callee_name == "topLevelFn"));
}

#[test]
fn extracts_a_qualified_callable_reference_to_a_member_method() {
    let index = extract_source(
        "class Foo {\n    fun instanceMethod() {}\n}\n\nfun demo() {\n    val ref1 = Foo::instanceMethod\n}\n",
    );
    assert!(index
        .invocations
        .iter()
        .any(|i| i.callee_name == "instanceMethod"));
}

#[test]
fn extracts_class_extends_via_a_constructor_invocation_delegation_specifier() {
    let index = extract_source(
        "open class Base\n\nclass Sub : Base()\n",
    );
    assert!(index.inheritance.iter().any(|r| r.kind == InheritanceKind::Extends
        && r.subtype_name == "Sub"
        && r.supertype_name == "Base"));
}

#[test]
fn extracts_interface_implementation_via_a_bare_delegation_specifier() {
    let index = extract_source(
        "interface Marker\n\nclass Impl : Marker\n",
    );
    assert!(index.inheritance.iter().any(|r| r.kind == InheritanceKind::Implements
        && r.subtype_name == "Impl"
        && r.supertype_name == "Marker"));
}

#[test]
fn extracts_by_delegation_as_implements() {
    let index = extract_source(
        "interface Base\n\nclass WithBy(base: Base) : Base by base\n",
    );
    assert!(index.inheritance.iter().any(|r| r.kind == InheritanceKind::Implements
        && r.subtype_name == "WithBy"
        && r.supertype_name == "Base"));
}

#[test]
fn constructor_delegation_this_resolves_to_the_enclosing_type() {
    let index = extract_source(
        "class Outer {\n    constructor() : this(0)\n    constructor(n: Int)\n}\n",
    );
    assert!(index
        .invocations
        .iter()
        .any(|i| i.callee_name == "Outer" && i.receiver == ReceiverExpr::SelfOrSuper));
}

/// Bug #1908 follow-up (second reviewer, finding A): `class Sub : Base`
/// (bare, no parens) is the ONLY legal supertype-specifier shape whenever
/// `Base` has no primary constructor -- Kotlin then REQUIRES every
/// secondary constructor of `Sub` to delegate via `super(...)`, never
/// `this(...)`. That bare specifier is recorded as `Implements` (this
/// extractor's own ambiguity default, since bare syntax alone cannot
/// distinguish a superclass from an interface). The previous version of
/// this test (`constructor_delegation_super_resolves_via_the_extends_
/// edge`) asserted the ABSENCE of this edge and called it "safe" -- that
/// was backwards: a missing edge is the dangerous under-binding direction
/// this whole epic exists to close, and it made the extractor's ONLY
/// support for `super(...)` unreachable by any real input (a class WITH a
/// primary constructor never uses `super(...)` in a secondary
/// constructor at all -- it uses `this(...)`).
#[test]
fn constructor_delegation_super_resolves_via_a_bare_supertype_specifier() {
    let index = extract_source(
        "open class Base(x: Int)\n\nclass Sub : Base {\n    constructor(x: Int) : super(x)\n}\n",
    );
    assert!(
        index
            .invocations
            .iter()
            .any(|i| i.callee_name == "Base" && i.receiver == ReceiverExpr::Other),
        "super(x) must resolve to Base via the bare (Implements-classified) supertype \
         specifier -- this is the ONLY shape `super(...)` can legally appear in"
    );
}

#[test]
fn extracts_a_qualified_type_reference_by_its_rightmost_segment() {
    let index = extract_source(
        "fun demo(x: com.example.other.Foo) {}\n",
    );
    assert!(index.type_references.iter().any(|t| t.type_name == "Foo"));
}

#[test]
fn extracts_a_generic_type_reference_without_the_type_argument_leaking_into_the_base_name() {
    let index = extract_source(
        "fun demo(items: List<String>) {}\n",
    );
    assert!(index.type_references.iter().any(|t| t.type_name == "List"));
    assert!(index.type_references.iter().any(|t| t.type_name == "String"));
}

#[test]
fn private_visibility_is_recorded_for_an_explicit_private_modifier() {
    let index = extract_source(
        "class Outer {\n    private fun hidden() {}\n}\n",
    );
    let decl = index.declaration_named("hidden").unwrap();
    assert_eq!(index.visibilities.get(&decl.symbol), Some(&Visibility::Private));
}

#[test]
fn absent_modifier_is_recorded_as_public_never_unknown() {
    let index = extract_source(
        "class Outer {\n    fun visible() {}\n}\n",
    );
    let decl = index.declaration_named("visible").unwrap();
    assert_eq!(index.visibilities.get(&decl.symbol), Some(&Visibility::Public));
}

#[test]
fn internal_visibility_is_recorded_as_unknown_never_private() {
    let index = extract_source(
        "class Outer {\n    internal fun scoped() {}\n}\n",
    );
    let decl = index.declaration_named("scoped").unwrap();
    assert_eq!(index.visibilities.get(&decl.symbol), Some(&Visibility::Unknown));
}

#[test]
fn extracts_an_anonymous_object_expression_and_scopes_its_member_to_it() {
    let index = extract_source(
        "interface Runnable {\n    fun run()\n}\n\nfun demo() {\n    val r = object : Runnable {\n        override fun run() {\n            helper()\n        }\n    }\n}\n\nfun helper() {}\n",
    );
    // The anonymous object's own `run` override must be a distinct
    // declaration from the interface's abstract `run` -- proven by there
    // being exactly two `run` declarations (interface + anonymous
    // override), never merged into one.
    let runs: Vec<_> = index.declarations.iter().filter(|d| d.name == "run").collect();
    assert_eq!(runs.len(), 2);
}

// ---------------------------------------------------------------------
// Bug #1908 follow-up: aliased imports, `super(...)` under a bare
// supertype specifier, infix call sites.
// ---------------------------------------------------------------------

/// P1: `import com.example.app.target as alias` recognizes the alias in
/// the grammar and then discards it, so a call site written as `alias()`
/// is recorded under the LOCAL name the source never declares anywhere
/// -- it can never match `target`'s real `Declaration`. Under-binding
/// (the dangerous direction): the real callee gets zero inbound edges.
#[test]
fn a_call_through_an_aliased_import_binds_to_the_real_target_name() {
    let index = extract_source(
        "package com.example.client\n\nimport com.example.app.target as alias\n\nfun run() = alias()\n",
    );
    assert!(
        index.invocations.iter().any(|i| i.callee_name == "target"),
        "a call through an aliased import must be recorded under the REAL declared name \
         (\"target\"), not the local alias -- otherwise cross-file binding can never find \
         the real declaration"
    );
    assert!(
        !index.invocations.iter().any(|i| i.callee_name == "alias"),
        "the bare alias name must never leak into the graph as a callee name"
    );
}

/// Same failure mode for an aliased CLASS import: `Widget` constructed as
/// `W()` must resolve to `Widget`, not vanish into an unmatched `W`.
#[test]
fn a_construction_through_an_aliased_class_import_binds_to_the_real_type_name() {
    let index = extract_source(
        "package com.example.client\n\nimport com.example.app.Widget as W\n\nfun demo() {\n    W()\n}\n",
    );
    assert!(
        index.constructions.iter().any(|c| c.type_name == "Widget"),
        "constructing an aliased class must record the REAL declared type name (\"Widget\")"
    );
    assert!(
        !index.constructions.iter().any(|c| c.type_name == "W"),
        "the bare alias name must never leak into the graph as a constructed type name"
    );
}

/// A plain (non-aliased) import must be entirely unaffected -- proves the
/// alias rewrite is scoped to genuinely aliased names, never a blanket
/// rename of anything that happens to share a name with an import.
#[test]
fn an_ordinary_unaliased_import_never_triggers_a_rewrite() {
    let index = extract_source(
        "package com.example.client\n\nimport com.example.app.target\n\nfun run() = target()\n",
    );
    assert!(index.invocations.iter().any(|i| i.callee_name == "target"));
}

/// Reviewer finding B: `infix_expression` (`this matches s`) was entirely
/// absent from `dispatch_node`'s match arms, so an infix call produced no
/// `InvocationSite` at all -- a private infix function called only via
/// infix syntax reports zero inbound edges and a false definitely-dead
/// verdict.
#[test]
fn extracts_an_infix_call_as_an_invocation_with_its_receiver() {
    let index = extract_source(
        "class Matcher {\n    fun matches(other: String): Boolean = true\n    fun run(s: String) = this matches s\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "matches")
        .expect("an infix call (`this matches s`) must be extracted as an invocation");
    assert_eq!(site.arg_count, Some(1));
    assert_eq!(site.receiver, ReceiverExpr::SelfOrSuper);
}

#[test]
fn extracts_a_top_level_infix_call_with_an_identifier_receiver() {
    let index = extract_source(
        "class Box(val n: Int)\n\ninfix fun Box.plusN(x: Int): Int = n + x\n\nfun demo(b: Box) {\n    val r = b plusN 3\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "plusN")
        .expect("an infix call (`b plusN 3`) must be extracted as an invocation");
    assert_eq!(site.arg_count, Some(1));
    assert_eq!(site.receiver, ReceiverExpr::Identifier("b".to_string()));
}
