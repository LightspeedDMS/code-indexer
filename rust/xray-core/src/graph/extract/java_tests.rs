//! F5 (#1873/#1875 rework): `java.rs`'s unit tests, relocated verbatim
//! out of its `#[cfg(test)] mod tests { ... }` body to keep both files
//! under the project's line limit, mirroring the same split already
//! done for `bind/resolve.rs` -> `bind/resolve_tests.rs`.

use super::*;
use crate::graph::extract::local_index::{ArgShape, Visibility};
use std::path::Path;

fn extract_source(source: &str) -> LocalIndex {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("Sample.java");
    std::fs::write(&path, source).unwrap();
    let root = crate::scanner::parse_file(Path::new(&path)).unwrap();
    JavaExtractor.extract(&root, 7)
}

#[test]
fn extracts_package_declaration_with_a_signature() {
    let index = extract_source("package com.example;\nclass Foo {}\n");
    let pkg = index.declaration_named("com.example").unwrap();
    assert_eq!(pkg.kind, DeclarationKind::Package);
    assert_eq!(
        index.signatures.get(&pkg.symbol).unwrap(),
        "package com.example"
    );
}

#[test]
fn extracts_ordinary_static_and_wildcard_imports() {
    let index = extract_source(
        "import java.util.List;\nimport static java.lang.Math.max;\nimport java.util.*;\nclass Foo {}\n",
    );
    assert_eq!(index.imports.len(), 3);
    assert_eq!(index.imports[0].kind, ImportKind::Ordinary);
    assert_eq!(index.imports[1].kind, ImportKind::Static);
    assert_eq!(index.imports[2].kind, ImportKind::Wildcard);
}

/// Issue #1915: `import static pkg.Util.*;` is a STATIC-ON-DEMAND import
/// -- both wildcard (imports every member) AND static (only static
/// members) -- which is neither an ordinary `Wildcard` (a package-level
/// `import pkg.*;`, whose `path` is a bare package) nor a single-member
/// `Static` import (`import static pkg.Util.helper;`, whose `path` ends
/// in the MEMBER name). Before the fix, `extract_imports` tested
/// `is_wildcard` before `is_static` and classified this as plain
/// `Wildcard`, discarding the static-ness entirely. `path` must be the
/// declaring CLASS's own dotted path (`"pkg.Util"`, never truncated to
/// the bare package `"pkg"` and never including the trailing `*`) -- the
/// exact shape `import_reasons` (`bind/resolve.rs`) needs to resolve a
/// candidate's `(enclosing_type, package)` against it.
#[test]
fn extracts_static_on_demand_import_as_static_wildcard_kind_with_the_declaring_class_path() {
    let index = extract_source("import static pkg.Util.*;\nclass Foo {}\n");
    assert_eq!(index.imports.len(), 1);
    assert_eq!(index.imports[0].kind, ImportKind::StaticWildcard);
    assert_eq!(index.imports[0].path, "pkg.Util");
}

#[test]
fn extracts_class_interface_and_enum_declarations() {
    let index = extract_source("interface Shape {}\nenum Color { RED }\nclass First {}\n");
    assert!(index.declaration_named("Shape").is_some());
    assert!(index.declaration_named("Color").is_some());
    assert!(index.declaration_named("First").is_some());
}

#[test]
fn extracts_class_extends_and_implements_edges() {
    let index = extract_source("class First extends Base implements Runnable, Comparable {}\n");
    assert!(index
        .inheritance
        .iter()
        .any(|i| i.kind == InheritanceKind::Extends
            && i.subtype_name == "First"
            && i.supertype_name == "Base"));
    assert!(index
        .inheritance
        .iter()
        .any(|i| i.kind == InheritanceKind::Implements && i.supertype_name == "Runnable"));
    assert!(index
        .inheritance
        .iter()
        .any(|i| i.kind == InheritanceKind::Implements && i.supertype_name == "Comparable"));
}

/// AC2 correctness gap fixed in review: `class Foo extends Base<String>`
/// represents its supertype as `superclass -> generic_type ->
/// type_identifier`, not a bare `type_identifier` directly.
#[test]
fn extracts_generic_superclass_as_extends_edge() {
    let index = extract_source("class First extends Base<String> {}\n");
    assert!(index
        .inheritance
        .iter()
        .any(|i| i.kind == InheritanceKind::Extends
            && i.subtype_name == "First"
            && i.supertype_name == "Base"));
}

/// Interfaces can extend MULTIPLE other interfaces via their OWN
/// `extends_interfaces` grammar node (distinct from a class's single
/// `superclass`) -- both must produce `Extends` edges.
#[test]
fn extracts_interface_extends_multiple_interfaces_as_extends_edges() {
    let index = extract_source("interface Foo extends Bar, Baz {}\n");
    assert!(index
        .inheritance
        .iter()
        .any(|i| i.kind == InheritanceKind::Extends
            && i.subtype_name == "Foo"
            && i.supertype_name == "Bar"));
    assert!(index
        .inheritance
        .iter()
        .any(|i| i.kind == InheritanceKind::Extends
            && i.subtype_name == "Foo"
            && i.supertype_name == "Baz"));
}

#[test]
fn extracts_annotations_on_types_and_methods() {
    let index = extract_source("@Deprecated\nclass First {\n    @Override\n    void run() {}\n}\n");
    assert!(index
        .annotations
        .iter()
        .any(|a| a.name == "Deprecated" && a.target_name == "First"));
    assert!(index
        .annotations
        .iter()
        .any(|a| a.name == "Override" && a.target_name == "run"));
}

#[test]
fn extracts_fields_and_constants_distinctly() {
    let index = extract_source(
        "class First {\n    private int count;\n    public static final int MAX = 10;\n}\n",
    );
    let field = index.declaration_named("count").unwrap();
    assert_eq!(field.kind, DeclarationKind::Field);
    let constant = index.declaration_named("MAX").unwrap();
    assert_eq!(constant.kind, DeclarationKind::Constant);
}

/// Story #1835 AC1 (RED against unmodified code -- `index.visibilities`
/// exists but nothing populates it yet, so every lookup here falls back
/// to `Unknown` and the `Private`/`Public` assertions fail): explicit
/// `private`/`public` modifiers on a method, a field, and a type
/// declaration must be recorded faithfully, and a package declaration
/// (no visibility concept at all) plus an UNMARKED method (no explicit
/// modifier keyword) must both read back as `Unknown` -- never silently
/// defaulted to a restricted visibility (AC1's explicit requirement;
/// see `Visibility`'s doc comment on why absent-modifier package-default
/// semantics are deliberately not inferred).
#[test]
fn extracts_visibility_from_explicit_modifiers_and_unknown_when_absent() {
    let index = extract_source(
        "package com.example;\n\
         public class First {\n\
         \x20   private void hidden() {}\n\
         \x20   public void exposed() {}\n\
         \x20   void packageScoped() {}\n\
         \x20   private int secretField;\n\
         \x20   public int openField;\n\
         }\n",
    );

    let visibility_of = |name: &str| -> Visibility {
        let decl = index.declaration_named(name).unwrap();
        index
            .visibilities
            .get(&decl.symbol)
            .copied()
            .unwrap_or(Visibility::Unknown)
    };

    assert_eq!(visibility_of("First"), Visibility::Public);
    assert_eq!(visibility_of("hidden"), Visibility::Private);
    assert_eq!(visibility_of("exposed"), Visibility::Public);
    assert_eq!(
        visibility_of("packageScoped"),
        Visibility::Unknown,
        "an unmarked method must be Unknown, never silently inferred as a restricted visibility"
    );
    assert_eq!(visibility_of("secretField"), Visibility::Private);
    assert_eq!(visibility_of("openField"), Visibility::Public);

    let pkg = index.declaration_named("com.example").unwrap();
    assert_eq!(
        index
            .visibilities
            .get(&pkg.symbol)
            .copied()
            .unwrap_or(Visibility::Unknown),
        Visibility::Unknown,
        "a package declaration has no visibility concept and must read back Unknown"
    );
}

#[test]
fn extracts_multiple_comma_separated_field_declarators() {
    let index = extract_source("class First {\n    private int a, b;\n}\n");
    assert!(index.declaration_named("a").is_some());
    assert!(index.declaration_named("b").is_some());
}

#[test]
fn extracts_qualified_and_bare_invocations() {
    let index = extract_source(
        "class First {\n    void run() {\n        obj.doSomething();\n        bareCall();\n    }\n}\n",
    );
    assert!(index
        .invocations
        .iter()
        .any(|i| i.callee_name == "doSomething"));
    assert!(index
        .invocations
        .iter()
        .any(|i| i.callee_name == "bareCall"));
}

/// AC1 (Story #1806, S2b): end-to-end through the real `JavaExtractor`
/// pipeline (not just `java_receiver`'s own isolated unit tests) -- a
/// qualified call's `InvocationSite.receiver` is a simple identifier,
/// and a bare call's stays `ReceiverExpr::None`.
#[test]
fn extracts_receiver_expression_on_qualified_invocations() {
    use crate::graph::extract::local_index::ReceiverExpr;

    let index = extract_source(
        "class First {\n    void run() {\n        obj.doSomething();\n        bareCall();\n    }\n}\n",
    );
    let qualified = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "doSomething")
        .unwrap();
    assert_eq!(
        qualified.receiver,
        ReceiverExpr::Identifier("obj".to_string())
    );

    let bare = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "bareCall")
        .unwrap();
    assert_eq!(bare.receiver, ReceiverExpr::None);
}

/// AC1/AC3 (Story #1806, S2b): a call's `enclosing_type`/
/// `enclosing_method` reflect the REAL innermost type/method around
/// it -- the discriminating case is a call inside a NESTED class's
/// method, which must be attributed to the INNER type, mirroring
/// `attributes_methods_to_their_immediately_enclosing_type_including_nested_classes`'s
/// own discriminating fixture shape for `method_owners`.
#[test]
fn extracts_enclosing_type_and_method_on_invocation_sites() {
    let index = extract_source(
        "class Outer {\n    void outerMethod() {\n        outerCall();\n    }\n    class Inner {\n        void innerMethod() {\n            innerCall();\n        }\n    }\n}\n",
    );
    let outer_call = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "outerCall")
        .unwrap();
    assert_eq!(outer_call.enclosing_type.as_deref(), Some("Outer"));
    let outer_method = index.declaration_named("outerMethod").unwrap();
    assert_eq!(outer_call.enclosing_method, Some(outer_method.symbol));

    let inner_call = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "innerCall")
        .unwrap();
    assert_eq!(
        inner_call.enclosing_type.as_deref(),
        Some("Inner"),
        "a call inside a nested class's method must be attributed to the INNER type"
    );
    let inner_method = index.declaration_named("innerMethod").unwrap();
    assert_eq!(inner_call.enclosing_method, Some(inner_method.symbol));
}

/// AC2 (Story #1806, S2b): a real method declaration's return type
/// and its parameters' declared types are captured end-to-end,
/// scoped to the method's own symbol.
#[test]
fn extracts_method_return_type_and_parameter_typed_names() {
    use crate::graph::extract::local_index::NameScope;

    let index = extract_source("class First {\n    Foo save(String s) { return null; }\n}\n");
    let method = index.declaration_named("save").unwrap();

    let return_type = index
        .method_return_types
        .iter()
        .find(|r| r.method_symbol == method.symbol)
        .unwrap();
    assert_eq!(return_type.return_type, "Foo");

    let param = index.typed_names.iter().find(|t| t.name == "s").unwrap();
    assert_eq!(param.declared_type, "String");
    assert_eq!(
        param.scope,
        NameScope::Local {
            enclosing_method: method.symbol
        }
    );
}

/// AC1 (Story #1806, S2b): a FIELD's declared type is scoped to its
/// enclosing TYPE, and a LOCAL VARIABLE's is scoped to its enclosing
/// METHOD -- both captured end-to-end through the real `JavaExtractor`
/// pipeline.
#[test]
fn extracts_field_and_local_variable_typed_names() {
    use crate::graph::extract::local_index::NameScope;

    let index = extract_source(
        "class First {\n    private Foo field1;\n    void run() {\n        Bar local = null;\n    }\n}\n",
    );

    let field = index
        .typed_names
        .iter()
        .find(|t| t.name == "field1")
        .unwrap();
    assert_eq!(field.declared_type, "Foo");
    assert_eq!(
        field.scope,
        NameScope::Field {
            enclosing_type: "First".to_string()
        }
    );

    let run_method = index.declaration_named("run").unwrap();
    let local = index
        .typed_names
        .iter()
        .find(|t| t.name == "local")
        .unwrap();
    assert_eq!(local.declared_type, "Bar");
    assert_eq!(
        local.scope,
        NameScope::Local {
            enclosing_method: run_method.symbol
        }
    );
}

#[test]
fn extracts_construction_sites() {
    let index = extract_source(
        "class First {\n    void run() {\n        Object x = new Last();\n    }\n}\n",
    );
    assert!(index.constructions.iter().any(|c| c.type_name == "Last"));
}

#[test]
fn extracts_type_references() {
    let index =
        extract_source("class First {\n    void run() {\n        Last x = null;\n    }\n}\n");
    assert!(index.type_references.iter().any(|t| t.type_name == "Last"));
}

#[test]
fn assigns_a_cached_signature_line_per_symbol() {
    let index = extract_source("class First {\n    void run() {}\n}\n");
    let decl = index.declaration_named("run").unwrap();
    let signature = index.signatures.get(&decl.symbol).unwrap();
    assert_eq!(signature, "run(0 params)");
}

/// AC4 (Story #1787, S2) Level-1 "+arity" narrowing needs a genuine
/// call-site argument count and a genuine declared parameter count --
/// neither existed on `InvocationSite`/`Declaration` before this slice
/// (the declared count was previously only baked into the opaque
/// `signatures` string, unusable for a structural comparison). Both are
/// captured during the SAME single walk `extract` already performs
/// (`method_invocation`'s `argument_list`, `method_declaration`'s
/// `formal_parameters`) -- no second tree walk is introduced.
#[test]
fn extracts_argument_count_at_call_sites_and_param_count_on_method_declarations() {
    let index = extract_source(
        "class First {\n    void run(int a, int b) {}\n    void go() {\n        run(1, 2);\n        bare();\n    }\n}\n",
    );
    let call = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "run")
        .unwrap();
    assert_eq!(call.arg_count, Some(2));
    let bare_call = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "bare")
        .unwrap();
    assert_eq!(bare_call.arg_count, Some(0));

    let run_decl = index.declaration_named("run").unwrap();
    assert_eq!(run_decl.param_count, Some(2));
    let go_decl = index.declaration_named("go").unwrap();
    assert_eq!(go_decl.param_count, Some(0));

    // Non-method declarations never carry a param_count.
    let type_decl = index.declaration_named("First").unwrap();
    assert_eq!(type_decl.param_count, None);
}

/// AC2 (Story #1793, S4): declared parameter TYPE names (beyond the
/// existing `param_count`) and the varargs flag, verified against the
/// real tree-sitter-java 0.23.5 grammar shapes for `formal_parameter`
/// (`type: (T) name: (identifier)`) and `spread_parameter`
/// (`(T) (variable_declarator name: (identifier))`).
#[test]
fn extracts_declared_parameter_type_names_and_varargs_flag() {
    let index = extract_source(
        "class First {\n    void save(String s, int x) {}\n    void tail(String... parts) {}\n}\n",
    );
    let save_decl = index.declaration_named("save").unwrap();
    assert_eq!(
        save_decl.param_types,
        vec!["String".to_string(), "int".to_string()]
    );
    assert!(!save_decl.is_varargs);

    let tail_decl = index.declaration_named("tail").unwrap();
    assert_eq!(tail_decl.param_types, vec!["String".to_string()]);
    assert!(
        tail_decl.is_varargs,
        "a spread_parameter must set is_varargs"
    );
}

/// Discriminating regression for a code-review-caught defect: a
/// PARAMETER with a modifier (`final`) or annotation (`@NonNull`) puts
/// a `modifiers` node BEFORE the type in the real grammar shape -- a
/// naive "first named child is always the type" implementation would
/// wrongly record the modifiers node's text instead of the real type.
#[test]
fn parameter_modifiers_and_annotations_do_not_shift_the_declared_type() {
    let index =
        extract_source("class First {\n    void save(final String s, @NonNull int x) {}\n}\n");
    let save_decl = index.declaration_named("save").unwrap();
    assert_eq!(
        save_decl.param_types,
        vec!["String".to_string(), "int".to_string()]
    );
}

/// AC2 (Story #1793, S4): every documented `ArgShape` category is read
/// off real call-site argument nodes -- string/numeric/boolean/null
/// literals, an explicit cast, an inline constructor, a lambda, and a
/// method reference -- verified against real tree-sitter-java 0.23.5
/// grammar output for each argument kind.
#[test]
fn extracts_per_argument_shapes_at_call_sites() {
    let index = extract_source(
        "class First {\n    void go() {\n        save(\"a\", 1, true, null, (Foo) obj, new Foo(), x -> x, Foo::bar);\n    }\n}\n",
    );
    let call = index
        .invocations
        .iter()
        .find(|i| i.callee_name == "save")
        .unwrap();
    assert_eq!(
        call.arg_shapes,
        vec![
            ArgShape::StringLiteral,
            ArgShape::NumericLiteral,
            ArgShape::BooleanLiteral,
            ArgShape::NullLiteral,
            ArgShape::Cast("Foo".to_string()),
            ArgShape::Constructor("Foo".to_string()),
            ArgShape::Lambda,
            ArgShape::MethodReference,
        ]
    );
}

/// AC1 (Story #1793, S4): the family binder's `is_interface` check
/// reads `LocalIndex.interface_names` -- populated ONLY for
/// `interface_declaration` nodes, never for `class`/`enum`/`record`.
#[test]
fn extracts_interface_names_only_for_interface_declarations() {
    let index = extract_source("interface Shape {}\nclass Foo {}\nenum Color { RED }\n");
    assert_eq!(index.interface_names, vec!["Shape".to_string()]);
}

/// AC1: the enclosing-type context threaded through the stack walk
/// must attribute each method to its IMMEDIATELY enclosing type --
/// the discriminating case is a NESTED class, whose own method must
/// be attributed to the inner type, never to the outer one a naive
/// "last type seen" implementation might wrongly keep using.
#[test]
fn attributes_methods_to_their_immediately_enclosing_type_including_nested_classes() {
    let index = extract_source(
        "class Outer {\n    void outerMethod() {}\n    class Inner {\n        void innerMethod() {}\n    }\n}\n",
    );
    let outer_decl = index.declaration_named("outerMethod").unwrap();
    let inner_decl = index.declaration_named("innerMethod").unwrap();
    let owner_of = |symbol: SymbolId| {
        index
            .method_owners
            .iter()
            .find(|o| o.method_symbol == symbol)
            .map(|o| o.enclosing_type.clone())
    };
    assert_eq!(owner_of(outer_decl.symbol), Some("Outer".to_string()));
    assert_eq!(
        owner_of(inner_decl.symbol),
        Some("Inner".to_string()),
        "a nested class's method must be attributed to the INNER type, not Outer"
    );
}

/// Bug #1929 item 3: an anonymous/enum-body class's method owner must
/// render as something a human can chase -- the enclosing type's real
/// name plus the anonymous body's own REAL source line -- never the
/// previous opaque `<anon:{file_id_hash}:{byte_offset}>` (two large
/// numbers with zero human meaning, reported live as e.g.
/// `<anon:918273645:42>.read(...)` against a real-world enum whose every
/// constant overrides `read()` in its own anonymous body -- exactly this
/// fixture's shape).
#[test]
fn anonymous_enum_constant_body_method_owner_renders_the_enclosing_type_and_a_real_line() {
    let index = extract_source(
        "enum LexerState {\n    Data {\n        void read() {}\n    };\n}\n",
    );
    let read_decl = index.declaration_named("read").unwrap();
    let owner = index
        .method_owners
        .iter()
        .find(|o| o.method_symbol == read_decl.symbol)
        .map(|o| o.enclosing_type.clone())
        .expect("anonymous enum-constant body method must still carry an owner record");
    assert!(
        owner.starts_with("LexerState$<anon@L2:"),
        "owner must start with the enclosing type's real name and the anon body's own real \
         source line (2, where `Data {{` begins): got {owner}"
    );
    assert!(
        !owner.contains("<anon:"),
        "the old opaque `<anon:hash:byte>` shape must be gone: got {owner}"
    );
}

/// Bug #1929 rework item 5: two anonymous bodies on the SAME source
/// line (`new Object() { void run() {} }; new Object() { void run()
/// {} };`, differing only by byte offset) must synthesize DISTINCT
/// names -- a collision here would silently merge two unrelated
/// anonymous types' supertype evidence in the repo-wide `TypeIndex`
/// (`graph::bind::families::supertypes_of`). Java never pushes a real
/// `Declaration` for an anonymous body itself (only Kotlin's `object_
/// literal` does; see `anonymous_body_context`'s own doc comment), so
/// this observes the marker via `method_owners`' `enclosing_type` on
/// each anon body's own `run` method -- the same observation point
/// `anonymous_enum_constant_body_method_owner_renders_the_enclosing_
/// type_and_a_real_line` above already uses.
#[test]
fn two_anonymous_bodies_on_the_same_source_line_synthesize_distinct_names() {
    let index = extract_source(
        "class Outer {\n    void m() {\n        Object x = new Object() { void run() {} }; Object y = new Object() { void run() {} };\n    }\n}\n",
    );
    let run_decls: Vec<&Declaration> = index
        .declarations
        .iter()
        .filter(|d| d.kind == DeclarationKind::Method && d.name == "run")
        .collect();
    assert_eq!(
        run_decls.len(),
        2,
        "fixture sanity: two anonymous bodies' `run` methods must both extract: got {} \
         declarations",
        run_decls.len()
    );
    let owner_of = |symbol: SymbolId| {
        index
            .method_owners
            .iter()
            .find(|o| o.method_symbol == symbol)
            .map(|o| o.enclosing_type.clone())
            .expect("each anonymous body's method must carry an owner record")
    };
    let owner_a = owner_of(run_decls[0].symbol);
    let owner_b = owner_of(run_decls[1].symbol);
    assert_ne!(
        owner_a, owner_b,
        "two anonymous bodies on the SAME line must synthesize DISTINCT owner names (the byte \
         offset must disambiguate them): got {owner_a:?} vs {owner_b:?}"
    );
}

/// Bug #1929 rework item 5: the SAME source (same line number) parsed
/// under two DIFFERENT `file_id`s must synthesize DISTINCT anon names --
/// the whole point of keeping `file_id` in the marker after the
/// human-readable prefix (global uniqueness across the whole analysed
/// repo, never just "unique within one file").
#[test]
fn the_same_line_number_in_two_different_files_synthesizes_distinct_anon_names() {
    let source = "class Outer {\n    void m() {\n        Object x = new Object() { void run() {} };\n    }\n}\n";
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("Sample.java");
    std::fs::write(&path, source).unwrap();
    let root = crate::scanner::parse_file(&path).unwrap();

    let index_a = JavaExtractor.extract(&root, 1);
    let index_b = JavaExtractor.extract(&root, 2);

    let owner_of_run = |index: &LocalIndex| -> String {
        let run_decl = index
            .declarations
            .iter()
            .find(|d| d.kind == DeclarationKind::Method && d.name == "run")
            .expect("anonymous body's run method must be extracted");
        index
            .method_owners
            .iter()
            .find(|o| o.method_symbol == run_decl.symbol)
            .map(|o| o.enclosing_type.clone())
            .expect("anonymous body's method must carry an owner record")
    };
    let owner_a = owner_of_run(&index_a);
    let owner_b = owner_of_run(&index_b);
    assert_ne!(
        owner_a, owner_b,
        "the SAME line number extracted under two different file_ids must synthesize DISTINCT \
         anon owner names (file_id must disambiguate across files): got {owner_a:?} vs {owner_b:?}"
    );
}

#[test]
fn symbol_ids_carry_the_given_file_id() {
    let index = extract_source("class First {}\n");
    let decl = index.declaration_named("First").unwrap();
    assert_eq!((decl.symbol >> 32) as u32, 7);
}

/// #1910 prerequisite 3 (round4-findings.md finding 3): a Java 21 switch
/// case pattern binding (`case Target handle ->`) must be recorded as a
/// real local `TypedNameRecord`, wired through `dispatch_node`'s
/// `"type_pattern"` arm -- the exact shape that fell through to
/// `resolve_receiver_type`'s open-world fallback substrate before this
/// fix.
#[test]
fn extracts_switch_case_type_pattern_as_a_typed_name() {
    use crate::graph::extract::local_index::NameScope;

    let index = extract_source(
        "class First {\n    void run(Object o) {\n        switch (o) {\n            case Target handle -> handle.helper();\n            default -> {}\n        }\n    }\n}\n",
    );
    let run_method = index.declaration_named("run").unwrap();
    let record = index
        .typed_names
        .iter()
        .find(|t| t.name == "handle")
        .expect("switch case type-pattern binding must be recorded as a typed name");
    assert_eq!(record.declared_type, "Target");
    assert_eq!(
        record.scope,
        NameScope::Local {
            enclosing_method: run_method.symbol
        }
    );
}

/// #1910 prerequisite 3: a Java 21 record-pattern deconstruction
/// component (`Target handle` inside `Wrapper(Target handle)`) must ALSO
/// be recorded, wired through `dispatch_node`'s `"record_pattern_
/// component"` arm -- at any nesting depth, with no per-level special
/// casing, since the generic tree walk visits every descendant
/// regardless of depth.
#[test]
fn extracts_record_pattern_component_as_a_typed_name() {
    use crate::graph::extract::local_index::NameScope;

    let index = extract_source(
        "class First {\n    void run(Object o) {\n        if (o instanceof Wrapper(Target handle)) {\n            handle.helper();\n        }\n    }\n}\n",
    );
    let run_method = index.declaration_named("run").unwrap();
    let record = index
        .typed_names
        .iter()
        .find(|t| t.name == "handle")
        .expect("record pattern component binding must be recorded as a typed name");
    assert_eq!(record.declared_type, "Target");
    assert_eq!(
        record.scope,
        NameScope::Local {
            enclosing_method: run_method.symbol
        }
    );
}

/// #1910 prerequisite 3: a record's own COMPONENTS (`record Wrapper(Target
/// handle) {}`) behave as implicit fields visible throughout every one of
/// the record's own methods (including its compact constructor) -- they
/// must be recorded as FIELD-scope typed names on the record's own type,
/// exactly like an ordinary `field_declaration`, closing the gap where a
/// compact constructor referencing its own component previously had NO
/// typed-name evidence at all.
#[test]
fn extracts_record_components_as_field_scope_typed_names() {
    use crate::graph::extract::local_index::NameScope;

    let index = extract_source("record Wrapper(Target handle) {\n}\n");
    let record = index
        .typed_names
        .iter()
        .find(|t| t.name == "handle")
        .expect("a record's own component must be recorded as a typed name");
    assert_eq!(record.declared_type, "Target");
    assert_eq!(
        record.scope,
        NameScope::Field {
            enclosing_type: "Wrapper".to_string()
        }
    );
}

/// #1910 prerequisite 3: an enum's own CONSTANTS (`enum Color { RED }`)
/// are effectively `public static final` instances of the enum type
/// itself, referenceable as a bare name from any of the enum's own
/// methods (and, via a static import, from anywhere) -- they must be
/// recorded as FIELD-scope typed names on the enum's own type so a bare
/// reference to one never falls through to the open-world static-type-
/// name fallback and misresolves against an unrelated in-repo type that
/// coincidentally shares the constant's name.
#[test]
fn extracts_enum_constants_as_field_scope_typed_names() {
    use crate::graph::extract::local_index::NameScope;

    let index = extract_source("enum Color {\n    RED, GREEN\n}\n");
    let red = index
        .typed_names
        .iter()
        .find(|t| t.name == "RED")
        .expect("an enum constant must be recorded as a typed name");
    assert_eq!(red.declared_type, "Color");
    assert_eq!(
        red.scope,
        NameScope::Field {
            enclosing_type: "Color".to_string()
        }
    );
}

/// #1922: a lambda parameter bound in a STATIC field initializer has NO
/// enclosing method at all, so it is absent from `typed_names` entirely
/// -- `all_local_binding_names` is the flat, context-independent
/// substrate that still records it.
#[test]
fn all_local_binding_names_records_a_lambda_parameter_bound_in_a_field_initializer() {
    let index = extract_source(
        "import java.util.function.Consumer;\nclass First {\n    static final Consumer<String> C = Svc -> Svc.trim();\n}\n",
    );
    assert!(
        index
            .all_local_binding_names
            .iter()
            .any(|n| n == "Svc"),
        "the lambda parameter Svc, bound in a static field initializer, must be recorded"
    );
}

/// #1922: a record's own component list shares the IDENTICAL
/// `formal_parameters` -> `formal_parameter` grammar shape a method's
/// parameters use, but a component is a FIELD (an implicit accessor),
/// never a local/parameter binding -- `all_local_binding_names` must
/// never record it.
#[test]
fn all_local_binding_names_excludes_a_records_own_component_list() {
    let index = extract_source("record Point(int Svc, int y) {}\n");
    assert!(
        !index.all_local_binding_names.iter().any(|n| n == "Svc"),
        "a record component is a field, never a local/parameter binding"
    );
}
