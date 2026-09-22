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

/// Bug #1929 item 3: an anonymous object-literal type (`object : Base()
/// { ... }`) must synthesize a name a human can chase -- the enclosing
/// type's real name plus the object literal's own REAL source line --
/// never the previous opaque `<anon:{file_id}:{byte}>`, mirroring
/// `JavaExtractor`'s identical fix for an anonymous/enum-body class.
/// Braces deliberately span separate lines (idiomatic formatting) to
/// avoid the documented, unrelated Bug #1937 same-line parse-recovery
/// defect in the pinned tree-sitter-kotlin-ng grammar.
#[test]
fn anonymous_object_literal_synthesized_name_carries_the_enclosing_type_and_a_real_line() {
    let index = extract_source(
        "class Outer {\n    fun make() {\n        val x = object : Runnable {\n            override fun run() {}\n        }\n    }\n}\n",
    );
    let anon = index
        .declarations
        .iter()
        .find(|d| d.kind == DeclarationKind::Type && d.name.contains("$<anon@L"))
        .expect("the object literal must produce a synthesized anonymous Type declaration");
    assert!(
        anon.name.starts_with("Outer$<anon@L3:"),
        "anon name must start with the enclosing type's real name and its own real source \
         line (3, where `object : Runnable {{` begins): got {}",
        anon.name
    );
    assert!(
        !anon.name.contains("<anon:"),
        "the old opaque `<anon:file_id:byte>` shape must be gone: got {}",
        anon.name
    );
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

// ---------------------------------------------------------------------
// Bug #1917: operator-convention calls (`binary_expression`,
// `index_expression`)
// ---------------------------------------------------------------------

#[test]
fn extracts_a_binary_plus_expression_as_a_plus_invocation() {
    let index = extract_source(
        "class Vec(val x: Int) {\n    operator fun plus(o: Vec) = Vec(x + o.x)\n    fun sum(a: Vec, b: Vec) = a + b\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "plus" && i.receiver == ReceiverExpr::Identifier("a".to_string()))
        .expect("`a + b` must be extracted as a `plus` invocation on receiver `a`");
    assert_eq!(site.arg_count, Some(1));
}

#[test]
fn extracts_a_relational_binary_expression_as_a_compareto_invocation() {
    let index = extract_source(
        "class Money(val cents: Int) {\n    operator fun compareTo(o: Money) = cents - o.cents\n    fun bigger(a: Money, b: Money) = a > b\n}\n",
    );
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "compareTo" && i.receiver == ReceiverExpr::Identifier("a".to_string()))
        .expect("`a > b` must be extracted as a `compareTo` invocation on receiver `a`");
}

#[test]
fn extracts_a_structural_equality_binary_expression_as_an_equals_invocation() {
    let index = extract_source(
        "class Point(val x: Int) {\n    override fun equals(other: Any?) = other is Point && other.x == x\n    fun same(a: Point, b: Point) = a == b\n}\n",
    );
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "equals" && i.receiver == ReceiverExpr::Identifier("a".to_string()))
        .expect("`a == b` must be extracted as an `equals` invocation on receiver `a`");
}

/// `&&` is one of `binary_expression`'s own operator tokens in this
/// grammar, but Kotlin does NOT allow a user to overload it (no
/// corresponding `operator fun` convention exists) -- mapping it would
/// fabricate a callee that can never exist, so no invocation is emitted
/// for the top-level `&&` itself (its operands are still walked normally).
#[test]
fn a_logical_and_binary_expression_produces_no_operator_convention_invocation() {
    let index = extract_source("fun both(a: Boolean, b: Boolean) = a && b\n");
    assert!(
        index.invocations.iter().all(|i| i.callee_name != "and"),
        "&& has no user-overloadable convention and must never synthesize an invocation"
    );
}

#[test]
fn extracts_an_index_read_as_a_get_invocation() {
    let index = extract_source(
        "class Registry {\n    operator fun get(key: String) = key.length\n    fun lookup(k: String) = this[k]\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "get")
        .expect("`this[k]` must be extracted as a `get` invocation");
    assert_eq!(site.arg_count, Some(1));
    assert_eq!(site.receiver, ReceiverExpr::SelfOrSuper);
}

#[test]
fn extracts_a_multi_argument_index_read_as_a_get_invocation_with_arity_two() {
    let index = extract_source(
        "class Grid {\n    operator fun get(row: Int, col: Int) = row + col\n    fun at(g: Grid, r: Int, c: Int) = g[r, c]\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "get")
        .expect("`g[r, c]` must be extracted as a `get` invocation");
    assert_eq!(site.arg_count, Some(2));
}

/// The read/write discrimination the AC requires: `m[k] = v` must resolve
/// to `set`, and must NOT also emit a spurious `get` for the very same
/// index expression (that would be a double count at one source
/// position, not a second real evaluation).
#[test]
fn extracts_an_index_write_as_a_set_invocation_and_never_a_get_for_the_same_site() {
    let index = extract_source(
        "class Registry {\n    operator fun set(key: String, value: Int) {}\n    fun store(k: String, v: Int) { this[k] = v }\n}\n",
    );
    let set_site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "set")
        .expect("`this[k] = v` must be extracted as a `set` invocation");
    assert_eq!(set_site.arg_count, Some(2));
    assert_eq!(set_site.receiver, ReceiverExpr::SelfOrSuper);
    assert!(
        index.invocations.iter().all(|i| i.callee_name != "get"),
        "the assignment target `this[k]` must not ALSO be recorded as a `get` invocation"
    );
}

/// A plain (non-indexed) assignment target must never be mistaken for an
/// indexed write -- no `set` invocation is fabricated.
#[test]
fn a_plain_variable_assignment_never_produces_a_set_invocation() {
    let index = extract_source("fun run() { var x = 1; x = 2 }\n");
    assert!(
        index.invocations.iter().all(|i| i.callee_name != "set"),
        "a plain `x = 2` assignment must never synthesize a `set` invocation"
    );
}

/// Documented gap: a COMPOUND assignment onto an indexed target
/// (`m[k] += v`) is not disambiguated into its own `plusAssign`/`get`+`set`
/// desugaring -- it falls back to the safe `get` default (over-binding,
/// never silently dropped).
#[test]
fn a_compound_assignment_onto_an_indexed_target_falls_back_to_a_get_invocation() {
    let index = extract_source(
        "class Counters {\n    operator fun get(key: String) = 0\n    fun bump(k: String) { this[k] += 1 }\n}\n",
    );
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "get")
        .expect("a compound-assignment indexed target must still fall back to a `get` invocation");
}

// ---------------------------------------------------------------------
// Bug #1917 (second round): unary, range, containment, non-indexed
// compound assignment
// ---------------------------------------------------------------------

#[test]
fn extracts_a_prefix_not_expression_as_a_not_invocation() {
    let index = extract_source(
        "class Flag(val on: Boolean) {\n    operator fun not() = Flag(!on)\n    fun flip(f: Flag) = !f\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "not" && i.receiver == ReceiverExpr::Identifier("f".to_string()))
        .expect("`!f` must be extracted as a `not` invocation on receiver `f`");
    assert_eq!(site.arg_count, Some(0));
}

#[test]
fn extracts_a_prefix_unary_minus_as_a_unaryminus_invocation() {
    let index = extract_source(
        "class Vec(val x: Int) {\n    operator fun unaryMinus() = Vec(-x)\n    fun negate(v: Vec) = -v\n}\n",
    );
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "unaryMinus" && i.receiver == ReceiverExpr::Identifier("v".to_string()))
        .expect("`-v` must be extracted as a `unaryMinus` invocation on receiver `v`");
}

#[test]
fn extracts_a_postfix_increment_as_an_inc_invocation() {
    let index = extract_source(
        "class Counter(val n: Int) {\n    operator fun inc() = Counter(n + 1)\n    fun bump(c: Counter): Counter { var x = c; x++; return x }\n}\n",
    );
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "inc" && i.receiver == ReceiverExpr::Identifier("x".to_string()))
        .expect("`x++` must be extracted as an `inc` invocation on receiver `x`");
}

#[test]
fn extracts_a_prefix_decrement_as_a_dec_invocation() {
    let index = extract_source(
        "class Counter(val n: Int) {\n    operator fun dec() = Counter(n - 1)\n    fun bump(c: Counter): Counter { var x = c; --x; return x }\n}\n",
    );
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "dec" && i.receiver == ReceiverExpr::Identifier("x".to_string()))
        .expect("`--x` must be extracted as a `dec` invocation on receiver `x`");
}

/// `!!` (not-null assertion) is fixed language semantics with no
/// corresponding `operator fun` convention -- mapping it would fabricate a
/// nonexistent callee, so no invocation is emitted for it.
#[test]
fn a_not_null_assertion_produces_no_operator_convention_invocation() {
    let index = extract_source("fun demo(s: String?): Int = s!!.length\n");
    assert!(
        index.invocations.iter().all(|i| i.callee_name != "not!!" && i.callee_name != "assertNotNull"),
        "`!!` has no user-overloadable convention and must never synthesize an invocation"
    );
}

#[test]
fn extracts_a_range_to_expression_as_a_rangeto_invocation() {
    let index = extract_source(
        "class Span(val a: Int, val b: Int) {\n    operator fun rangeTo(o: Span) = b - o.a\n    fun gap(x: Span, y: Span) = x..y\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "rangeTo" && i.receiver == ReceiverExpr::Identifier("x".to_string()))
        .expect("`x..y` must be extracted as a `rangeTo` invocation on receiver `x`");
    assert_eq!(site.arg_count, Some(1));
}

#[test]
fn extracts_a_range_until_expression_as_a_rangeuntil_invocation() {
    let index = extract_source("fun demo(x: Int, y: Int) = x..<y\n");
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "rangeUntil")
        .expect("`x..<y` must be extracted as a `rangeUntil` invocation");
}

#[test]
fn extracts_an_in_expression_as_a_contains_invocation_on_the_right_operand() {
    let index = extract_source(
        "class Bag {\n    operator fun contains(s: String) = s.isNotEmpty()\n    fun has(b: Bag, s: String) = s in b\n}\n",
    );
    let site = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "contains")
        .expect("`s in b` must be extracted as a `contains` invocation");
    assert_eq!(
        site.receiver,
        ReceiverExpr::Identifier("b".to_string()),
        "`x in y` calls `y.contains(x)` -- the receiver is the RIGHT operand, not the left"
    );
}

#[test]
fn extracts_a_not_in_expression_as_a_contains_invocation_too() {
    let index = extract_source(
        "class Bag {\n    operator fun contains(s: String) = s.isNotEmpty()\n    fun hasNot(b: Bag, s: String) = s !in b\n}\n",
    );
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "contains" && i.receiver == ReceiverExpr::Identifier("b".to_string()))
        .expect("`s !in b` must ALSO be extracted as a `contains` invocation (same convention as `in`)");
}

/// The over-binding fallback requirement: a non-indexed `+=` with no
/// `plusAssign` in scope must ALSO surface a `plus` candidate, since the
/// extractor cannot determine which desugaring Kotlin actually chose
/// without receiver-type/mutability evidence it does not track.
#[test]
fn a_non_indexed_compound_assignment_emits_both_the_assign_and_plain_convention_names() {
    let index = extract_source(
        "class Counter(val n: Int) {\n    operator fun plusAssign(k: Int) {}\n    operator fun plus(k: Int) = Counter(n)\n    fun bump(c: Counter) { var x = c; x += 1 }\n}\n",
    );
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "plusAssign" && i.receiver == ReceiverExpr::Identifier("x".to_string()))
        .expect("`x += 1` must emit a `plusAssign` invocation on receiver `x`");
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "plus" && i.receiver == ReceiverExpr::Identifier("x".to_string()))
        .expect("`x += 1` must ALSO emit a `plus` invocation on receiver `x` (over-binding, \
                 since the extractor cannot know whether `plusAssign` really applies here)");
}

/// A plain (non-compound) assignment to a non-indexed target must never
/// fabricate an operator-convention invocation.
#[test]
fn a_plain_non_indexed_assignment_never_produces_an_operator_convention_invocation() {
    let index = extract_source("fun run() { var x = 1; x = 2 }\n");
    assert!(
        index
            .invocations
            .iter()
            .all(|i| !["plusAssign", "plus", "minusAssign", "minus"].contains(&i.callee_name.as_str())),
        "a plain `x = 2` assignment must never synthesize any operator-convention invocation"
    );
}
