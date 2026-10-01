//! Regression tests for Issue #1873 -- X-Ray graph mode's Java extractor
//! emits no reference edge for a method reference expression (`this::name`,
//! `Type::name`, `expr::name`, `super::name`, `Type::new`). A private
//! method used only through a method reference therefore has zero inbound
//! edges and satisfies `is_definitely_dead_code` exactly, so `analyze_graph`
//! reports live code as definitely dead (confirmed live on a production
//! Java repository: 18 of 20 dead-code findings were method-reference call
//! sites like `.map(this::parseValue)`).
//!
//! Root cause: `rust/xray-core/src/graph/extract/java.rs`'s node-walk
//! `match` (`dispatch_node`) dispatches `"method_invocation"`,
//! `"object_creation_expression"`, and `"type_identifier"` into reference-
//! producing extraction functions, but never `"method_reference"` -- that
//! node kind is recognised elsewhere (`arg_shape_for` classifies it as
//! `ArgShape::MethodReference` for call-argument shape purposes) but that
//! path never emits a reference edge.
//!
//! Real tree-sitter-java 0.23.5 grammar shapes verified via a throwaway
//! diagnostic dump before writing these fixtures (not guessed):
//! - `this::parsePrice`  -> `method_reference( this, "::", identifier )`
//! - `super::greet`      -> `method_reference( super, "::", identifier )`
//! - `Worker::normalize` -> `method_reference( identifier, "::", identifier )`
//! - `Factory::new`      -> `method_reference( identifier, "::", new )`
//!
//! (the qualifying `object` child of an identifier-qualified reference is
//! plain `identifier`, never `type_identifier` -- so, pre-fix, none of
//! these forms are picked up incidentally by the EXISTING `"type_identifier"
//! => extract_type_reference` dispatch arm; every assertion below is
//! discriminating on the missing `"method_reference"` dispatch alone).
//!
//! Every fixture is realistically compilable Java, not merely
//! grammar-valid: `this::m`/`Formatter::format` reference private members
//! from WITHIN their own declaring class (legal); `super::greet` needs
//! `Base.greet` at least `protected` (a private member is not accessible
//! via `super` from a subclass at all); `h::transform` needs
//! `Helper.transform` at least package-private (a private member is not
//! accessible via an instance qualifier from a different class); and
//! `Factory::new` targets a RAW `java.util.function.Supplier` (no `<Factory>`
//! generic argument) specifically so "Factory" is never mentioned as a type
//! anywhere except the method reference itself -- `Supplier<Factory>` would
//! put a real `type_identifier` "Factory" node in the tree (verified in the
//! same diagnostic dump), which the EXISTING dispatch arm already turns into
//! a reference, contaminating the RED baseline for that one case.
//!
//! `CodeGraph::is_definitely_dead_code` only ever returns `Some(true)` for a
//! declaration that is BOTH unreferenced AND `Visibility::Private`; anything
//! else unreferenced comes back `None` (undecidable) instead, never a false
//! `Some(true)`. So the `protected`/package-private targets above are
//! `None` pre-fix rather than the headline `Some(true)` shape -- still a
//! real defect (a live method reported undecided instead of correctly
//! referenced) and still fully discriminating: `Some(false)` is reachable
//! ONLY through a real inbound edge regardless of visibility, so every
//! assertion below fails before the fix and passes only once that edge
//! exists.
//!
//! Each test extracts real Java source through the real `JavaExtractor`,
//! binds it through the real `bind_with_budget` pipeline (unlimited
//! budget, single file, so `AnalysisCompleteness::Complete` is the only
//! possible outcome and `is_definitely_dead_code`'s completeness gate never
//! interferes), then asserts `CodeGraph::is_definitely_dead_code` at the
//! level that matters to a caller.
//!
//! This file intentionally holds every case for the issue together (rather
//! than splitting across files): all seven share the exact same
//! extract -> bind -> assert harness (the five helpers below), and the
//! mission requires them cross-checked as one coherent discriminating-RED
//! set (verified together: red before the fix, green after, red again with
//! the new dispatch removed).

use xray_core::graph::bind::{bind_with_budget, FileForBind};
use xray_core::graph::budget::{AnalysisCompleteness, IndexBudget};
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::local_index::{Declaration, DeclarationKind, LocalIndex};
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
/// small file under an unlimited budget must report `Complete`, so a
/// `None` result from `is_definitely_dead_code` in these tests can only
/// mean "unknown declaration kind or non-private visibility", never
/// completeness suppression.
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

/// The symbol of the single declaration named `name`. Panics loudly (never
/// silently picks a wrong candidate) if the name is missing or ambiguous --
/// use `declaration_symbols` for a fixture that legitimately declares more
/// than one thing under the same name (overloads).
fn declaration_symbol(index: &LocalIndex, name: &str) -> SymbolId {
    let matches: Vec<&Declaration> = index
        .declarations
        .iter()
        .filter(|d| d.name == name)
        .collect();
    match matches.as_slice() {
        [only] => only.symbol,
        [] => panic!("fixture bug: no declaration named {name:?}"),
        other => panic!(
            "fixture bug: {name:?} is ambiguous ({} declarations) -- narrow the fixture or filter by kind",
            other.len()
        ),
    }
}

/// Every declaration's symbol named `name` -- for overload fixtures.
fn declaration_symbols(index: &LocalIndex, name: &str) -> Vec<SymbolId> {
    index
        .declarations
        .iter()
        .filter(|d| d.name == name)
        .map(|d| d.symbol)
        .collect()
}

/// D3: the symbol of the declaration named `name` whose `MethodOwnerRecord`
/// names `enclosing_type` -- for a fixture that deliberately declares the
/// SAME method name on two different classes (e.g. a superclass method and
/// its subclass override), where plain `declaration_symbol` would panic on
/// the ambiguity by design. Panics loudly if no declaration named `name`
/// is owned by `enclosing_type` (never silently picks a wrong candidate).
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
        .unwrap_or_else(|| {
            panic!("fixture bug: no {name:?} declaration owned by {enclosing_type:?}")
        })
        .symbol
}

fn dead(graph: &CodeGraph, symbol: SymbolId) -> Option<bool> {
    let dense = graph
        .dense_id_for(symbol)
        .expect("symbol must be interned in the bound graph");
    graph.is_definitely_dead_code(dense)
}

#[test]
fn this_method_reference_marks_the_referenced_private_method_as_not_dead() {
    let source = r#"
import java.util.List;
import java.util.stream.Collectors;

class Worker {
    void run(List<String> items) {
        items.stream().map(this::parsePrice).collect(Collectors.toList());
    }

    private String parsePrice(String raw) {
        return raw.trim();
    }
}
"#;
    let index = extract_java(source);
    let parse_price = declaration_symbol(&index, "parsePrice");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, parse_price),
        Some(false),
        "this::parsePrice must reference parsePrice's declaration -- it must never be reported definitely dead"
    );
}

#[test]
fn type_static_method_reference_marks_the_referenced_static_method_as_not_dead() {
    let source = r#"
import java.util.List;
import java.util.stream.Collectors;

class Worker {
    void run(List<String> items) {
        items.stream().map(Worker::normalize).collect(Collectors.toList());
    }

    private static String normalize(String raw) {
        return raw.trim();
    }
}
"#;
    let index = extract_java(source);
    let normalize = declaration_symbol(&index, "normalize");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, normalize),
        Some(false),
        "Worker::normalize must reference normalize's declaration -- it must never be reported definitely dead"
    );
}

#[test]
fn expr_method_reference_marks_the_referenced_instance_method_as_not_dead() {
    let source = r#"
import java.util.List;
import java.util.stream.Collectors;

class Helper {
    String transform(String s) {
        return s.trim();
    }
}

class Worker {
    void run(List<String> items) {
        Helper h = new Helper();
        items.stream().map(h::transform).collect(Collectors.toList());
    }
}
"#;
    let index = extract_java(source);
    let transform = declaration_symbol(&index, "transform");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, transform),
        Some(false),
        "h::transform must reference Helper.transform's declaration -- it must never be reported definitely dead"
    );
}

/// D3 tightening: `Derived` also declares an unrelated same-named `greet`
/// (different arity, so per JLS there is no override relationship -- an
/// `int`-arity private "override" attempt would collide on visibility and
/// fail to compile, verified against real javac). `super::greet` must
/// resolve to `Base.greet` specifically and must never fall back to
/// `Derived`'s own method -- the exact shape of D3's self-loop bug via
/// method-reference syntax. Plain "not dead" alone could not tell "resolved
/// to Base.greet" apart from "silently self-referenced Derived's method".
#[test]
fn super_method_reference_marks_the_referenced_base_method_as_not_dead() {
    let source = r#"
import java.util.function.Supplier;

class Base {
    protected String greet() {
        return "hi";
    }
}

class Derived extends Base {
    private String greet(int unused) {
        return "unrelated";
    }

    Supplier<String> useSuperGreeting() {
        return super::greet;
    }
}
"#;
    let index = extract_java(source);
    let base_greet = declaration_symbol_owned_by(&index, "greet", "Base");
    let derived_greet = declaration_symbol_owned_by(&index, "greet", "Derived");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, base_greet),
        Some(false),
        "super::greet must reference Base.greet's declaration -- it must never be reported definitely dead"
    );
    assert_eq!(
        dead(&graph, derived_greet),
        Some(true),
        "super::greet must NEVER resolve to Derived's own unrelated greet -- it is unreferenced elsewhere \
         and must be reported definitely dead, proving no false self-loop"
    );
}

#[test]
fn type_new_constructor_reference_marks_the_type_as_not_dead_consistent_with_object_creation() {
    // `Factory` is a private static NESTED class -- a private top-level
    // class is not real Java, and `is_definitely_dead_code` only ever
    // returns `Some(true)` for a `Visibility::Private` declaration, so a
    // package-private top-level `Factory` would merely be `None`
    // (undecidable) pre-fix. `make()` returns a RAW `Supplier` (no
    // `<Factory>` generic argument) so "Factory" is mentioned nowhere else
    // as a type -- see the module docs for why `Supplier<Factory>` would
    // contaminate this specific fixture's RED baseline.
    let source = r#"
class Container {
    private static class Factory {
        private Factory() {}

        static java.util.function.Supplier make() {
            return Factory::new;
        }
    }
}
"#;
    let index = extract_java(source);
    let factory_type = index
        .declarations
        .iter()
        .find(|d| d.name == "Factory" && d.kind == DeclarationKind::Type)
        .expect("fixture bug: Factory's Type declaration must be present")
        .symbol;
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, factory_type),
        Some(false),
        "Factory::new must reference the Factory type the same way `new Factory()` does -- \
         it must never be reported definitely dead"
    );
}

#[test]
fn method_reference_to_an_overloaded_name_never_reports_a_false_dead_verdict_for_any_overload() {
    let source = r#"
class Formatter {
    private static String format(String s) {
        return s;
    }

    private static String format(int i) {
        return String.valueOf(i);
    }

    void run() {
        java.util.function.Function<String, String> f1 = Formatter::format;
        java.util.function.Function<Integer, String> f2 = Formatter::format;
    }
}
"#;
    let index = extract_java(source);
    let overloads = declaration_symbols(&index, "format");
    assert_eq!(
        overloads.len(),
        2,
        "fixture bug: expected exactly two format overloads"
    );
    let graph = bind_single_file(index);
    for symbol in overloads {
        assert_eq!(
            dead(&graph, symbol),
            Some(false),
            "Formatter::format must never leave any overload it could target looking definitely dead -- \
             under-reporting (marking every overload referenced) is acceptable, over-reporting is not"
        );
    }
}

#[test]
fn an_unreferenced_private_method_alongside_a_method_reference_is_still_reported_dead() {
    let source = r#"
import java.util.function.Function;

class Utility {
    private String combineLines(String a, String b) {
        return a + b;
    }

    private String used(String s) {
        return s;
    }

    void run() {
        Function<String, String> f = this::used;
    }
}
"#;
    let index = extract_java(source);
    let combine_lines = declaration_symbol(&index, "combineLines");
    let used = declaration_symbol(&index, "used");
    let graph = bind_single_file(index);
    assert_eq!(
        dead(&graph, used),
        Some(false),
        "this::used must reference used's declaration"
    );
    assert_eq!(
        dead(&graph, combine_lines),
        Some(true),
        "combineLines is never referenced anywhere (not even via a method reference) and must \
         still be reported definitely dead -- the true-positive shape must survive the fix"
    );
}
