//! Regression/measurement coverage for #1899 (epic #1906, P1) --
//! `strongly_connected_components()` cycle precision -- re-grounded in
//! measurement after #1898 (`ed65c3a8`) split arity narrowing (now
//! narrows to EMPTY on known evidence) from receiver-type narrowing (now
//! TAG-ONLY, never deletes).
//!
//! #1899's production report (`repo-A`, `["*.java"]`) found two false
//! multi-node components:
//!
//! - a 3-node component closed by a misbound 0-arg `close()` call binding
//!   to a 1-param `close` declaration -- a WRONG-ARITY edge;
//! - a 15-node component spanning three sibling parser classes, formed
//!   because every `parse(1 params)` call site bound to every other
//!   class's same-arity `parse` -- a SAME-ARITY magnet, which arity
//!   narrowing is structurally incapable of discriminating (that is
//!   #1910's job, not this fix's).
//!
//! Both shapes are reproduced below as minimal, javac-17-valid fixtures
//! (3-node analogues of the production 3-node/15-node components -- the
//! mechanism is what is being pinned, not the exact node count) and
//! driven through the REAL front door, `build_repo_graph` (never `bind()`
//! directly, never a hand-built `LocalIndex`), exactly as
//! `bug_1898_round2_narrowing_regressions.rs` does.
//!
//! `wrong_arity_three_node_component_no_longer_forms_a_cycle_after_1898`
//! asserts ed65c3a8's win: NO multi-node component survives.
//!
//! `same_arity_statically_qualified_calls_no_longer_fabricate_a_false_cycle_after_1922`
//! (formerly `..._still_fabricate_a_false_cycle_pinned_for_1910`, an
//! ACCEPTED-REGRESSION PIN) is INVERTED per its own documented
//! instructions ("when #1910 lands ... delete this test (or flip its
//! assertion and rename it), do not loosen it"): #1910's own inferred-
//! local-type approach was closed without shipping; #1922 instead
//! delivers the SAME statically-qualified-call capability through a
//! narrower, provably-safe mechanism (`narrowing::apply_type_qualifier_
//! narrowing`, gated on the qualifier being a literal type-shaped
//! identifier with zero local-variable-scope analysis). The false 3-node
//! component this fixture reproduced is now GONE: each `ParserX.parse`'s
//! `TimeUtil.parse(input)` call binds exclusively to `TimeUtil.parse`,
//! never to a same-arity sibling.
//!
//! `same_arity_overload_self_loop_is_unaffected_by_1898` reproduces the
//! shipped template's 20-self-loop-singleton shape: a generated-accessor-
//! style setter that a human reading the source can see cannot recurse,
//! but which the heuristic binder still self-loops because it cannot
//! discriminate two SAME-ARITY overloads of the same name by parameter
//! TYPE. Arity narrowing only discriminates by argument COUNT, so this
//! shape is untouched by ed65c3a8 -- the test measures that, it does not
//! assume it (see the accompanying report for the A/B evidence across the
//! pre-ed65c3a8 tree).
//!
//! Out of scope, deliberately NOT implemented here: #1899's third AC
//! ("mark an edge whose candidate window had more than one surviving
//! target, exposed on `GraphHandle`") -- the issue itself says this
//! overlaps #1900's `edge_reason` accessor ("ship one mechanism, not
//! two"). This file only measures precision, it does not add an
//! ambiguity-marking mechanism.

use std::path::Path;
use tempfile::TempDir;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::extract::LanguageExtractor;
use xray_core::graph::identity::{file_id, SymbolId};
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, UserFact};
use xray_core::owned_node::OwnedNode;

struct NoOpCollector;
impl FactCollector for NoOpCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        Vec::new()
    }
}

fn write_java(dir: &Path, relative_path: &str, source: &str) {
    std::fs::write(dir.join(relative_path), source).unwrap();
}

/// Mirrors `bug_1898_round2_narrowing_regressions.rs`'s own helper
/// verbatim: the symbol of the declaration named `name` whose
/// `MethodOwnerRecord` names `enclosing_type`.
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

fn extract_index(dir: &Path, relative_path: &str) -> LocalIndex {
    let full_path = dir.join(relative_path);
    let root = xray_core::scanner::parse_file(&full_path).expect("fixture source must parse");
    JavaExtractor.extract(&root, file_id(relative_path))
}

/// Runs `build_repo_graph` -- the REAL front-door entry point
/// `analyze_graph` uses -- over every listed file, unbounded budget.
fn build_graph_over(dir: &Path, relative_paths: &[&str]) -> CodeGraph {
    let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
    let paths: Vec<String> = relative_paths.iter().map(|p| p.to_string()).collect();
    let result = build_repo_graph(dir, &paths, &options, &NoOpCollector)
        .expect("no file_id collision in this fixture");
    result.graph
}

/// The dense id of the declaration named `name` owned by `owner` in
/// `relative_path`, as interned by `graph`.
fn dense_id_of(graph: &CodeGraph, dir: &Path, relative_path: &str, name: &str, owner: &str) -> u32 {
    let index = extract_index(dir, relative_path);
    let symbol = declaration_symbol_owned_by(&index, name, owner);
    graph
        .dense_id_for(symbol)
        .unwrap_or_else(|| panic!("{owner}.{name} must be interned"))
}

/// The size of the strongly-connected component containing `dense_id`,
/// via the SAME primitive `strongly_connected_components()` the
/// production `find-reference-cycles.rs` template calls.
fn component_size_containing(graph: &CodeGraph, dense_id: u32) -> usize {
    graph
        .strongly_connected_components()
        .into_iter()
        .find(|component| component.contains(&dense_id))
        .map(|component| component.len())
        .unwrap_or(0)
}

/// Every strongly-connected component of `graph` with more than one node.
fn multi_node_components(graph: &CodeGraph) -> Vec<Vec<u32>> {
    graph.strongly_connected_components().into_iter().filter(|c| c.len() > 1).collect()
}

// ---------------------------------------------------------------------
// Shape 1: wrong-arity 3-node component (#1899's Component 1, "close").
// ---------------------------------------------------------------------

/// Mirrors #1899's production Component 1 exactly: `ConnectionHolder`
/// exposes a 1-param `close(String key)` that calls `manageConnection`,
/// which calls `releaseHandle`, which makes a 0-arg call
/// `connection.close()` on a variable typed by a class NOT present in
/// this repo (`java.sql.Connection` stands in for any external JDK/
/// third-party type -- the extractor never sees its declarations at
/// all). Before #1898, the bare-name candidate pool for that 0-arg call
/// (`{ConnectionHolder.close(String)}`, the only in-repo "close") had no
/// narrowing pass find a match by any evidence, so the pre-fix "no match
/// -> keep the whole pool" fallback fabricated an edge from
/// `releaseHandle` back to `ConnectionHolder.close(String)` -- closing a
/// 3-node cycle that does not exist in the real program.
fn write_wrong_arity_fixture(dir: &Path) {
    write_java(
        dir,
        "ConnectionHolder.java",
        r#"package p;
public class ConnectionHolder {
    public void close(String key) {
        manageConnection(false, key);
    }
    private void manageConnection(boolean force, String key) {
        releaseHandle(key);
    }
    private void releaseHandle(String key) {
        java.sql.Connection connection = lookup(key);
        connection.close();
    }
    private java.sql.Connection lookup(String key) {
        return null;
    }
}
"#,
    );
}

/// After #1898, `apply_arity_narrowing` narrows a KNOWN-arity call (here,
/// 0 args) to candidates whose declared arity matches, down to EMPTY when
/// nothing does. `ConnectionHolder.close(String)` has 1 param, so it is
/// excluded; the reference resolves to zero candidates; no edge is
/// created; the cycle cannot form.
#[test]
fn wrong_arity_three_node_component_no_longer_forms_a_cycle_after_1898() {
    let dir = TempDir::new().unwrap();
    write_wrong_arity_fixture(dir.path());
    let graph = build_graph_over(dir.path(), &["ConnectionHolder.java"]);
    let close = dense_id_of(&graph, dir.path(), "ConnectionHolder.java", "close", "ConnectionHolder");

    assert_eq!(
        component_size_containing(&graph, close),
        1,
        "ed65c3a8's arity narrowing must have removed the fabricated \
         releaseHandle -> close(String) edge; ConnectionHolder.close must \
         be its own singleton component, not part of a false 3-node cycle"
    );
    let multi_node = multi_node_components(&graph);
    assert!(
        multi_node.is_empty(),
        "fixture has no genuine cycle; found spurious multi-node components: {multi_node:?}"
    );
}

// ---------------------------------------------------------------------
// Shape 2: same-arity magnet 3-node component (#1899's Component 2,
// "parse") -- ACCEPTED REGRESSION, pinned for #1910.
// ---------------------------------------------------------------------

/// Mirrors #1899's production Component 2's mechanism directly: three
/// sibling classes with NO inheritance relationship, each declaring a
/// public `parse(String)` (1 param) and each making a statically-
/// qualified call `TimeUtil.parse(x)` (also 1 param, matching arity).
/// `apply_receiver_type_narrowing` is TAG-ONLY as of ed65c3a8 -- it marks
/// `TimeUtil.parse` with `RECEIVER_TYPE_MATCH` but never REMOVES the
/// three same-arity sibling candidates, and `apply_arity_narrowing`
/// cannot discriminate them either (all four candidates share arity 1).
/// Each call site therefore binds to ALL FOUR declarations, so
/// `ParserA/B/C.parse` all point at each other -- one false 3-node
/// strongly-connected component (`TimeUtil.parse` has an empty body, so
/// it is reachable but never joins the cycle, like the shipped
/// template's leaf `possible_candidate_cycle` entries).
fn write_same_arity_magnet_fixture(dir: &Path) {
    write_java(
        dir,
        "TimeUtil.java",
        "package p;\npublic class TimeUtil {\n    public static void parse(String input) {\n    }\n}\n",
    );
    for parser in ["ParserA", "ParserB", "ParserC"] {
        write_java(
            dir,
            &format!("{parser}.java"),
            &format!(
                "package p;\npublic class {parser} {{\n    public void parse(String input) {{\n        TimeUtil.parse(input);\n    }}\n}}\n"
            ),
        );
    }
}

/// INVERTED by #1922 (formerly an ACCEPTED-REGRESSION PIN naming #1910,
/// which was closed without shipping -- see this file's module doc):
/// `TimeUtil.parse(x)`-shaped statically-qualified calls now bind ONLY
/// within the qualified type, so the false 3-node SCC this fixture used
/// to fabricate must no longer exist -- `ParserA.parse` must land in a
/// SINGLETON component (or one that excludes both siblings), never one
/// containing `ParserB.parse`/`ParserC.parse`.
#[test]
fn same_arity_statically_qualified_calls_no_longer_fabricate_a_false_cycle_after_1922() {
    let dir = TempDir::new().unwrap();
    write_same_arity_magnet_fixture(dir.path());
    let files = ["TimeUtil.java", "ParserA.java", "ParserB.java", "ParserC.java"];
    let graph = build_graph_over(dir.path(), &files);

    let a = dense_id_of(&graph, dir.path(), "ParserA.java", "parse", "ParserA");
    let b = dense_id_of(&graph, dir.path(), "ParserB.java", "parse", "ParserB");
    let c = dense_id_of(&graph, dir.path(), "ParserC.java", "parse", "ParserC");

    let component = graph
        .strongly_connected_components()
        .into_iter()
        .find(|comp| comp.contains(&a))
        .expect("ParserA.parse must be in some component");

    assert!(
        !component.contains(&b) && !component.contains(&c),
        "#1922: each ParserX.parse's TimeUtil.parse(input) call must bind exclusively to \
         TimeUtil.parse -- ParserA.parse must never share a component with ParserB.parse/ \
         ParserC.parse again, got {component:?}"
    );
}

// ---------------------------------------------------------------------
// Shape 3: self-loop on a same-arity overload collision (the shipped
// template's 20 self-loop singletons).
// ---------------------------------------------------------------------

/// Reproduces the shipped `find-reference-cycles.rs` template's
/// self-loop-singleton shape: a generated-accessor-style setter that a
/// human reading the source can see CANNOT recurse (it delegates to a
/// differently-typed overload of the same name, the standard JAXB/VO
/// String -> wrapper-type coercion pattern), yet the heuristic binder
/// reports a self-loop because it cannot discriminate two SAME-ARITY
/// (both 1 param) overloads of `setBodID` by parameter TYPE. The argument
/// passed (`wrapped`, a bare identifier local) carries no
/// `Cast`/`Constructor` shape, so `apply_overload_shape_narrowing`'s
/// named-type preference -- the one mechanism that COULD discriminate
/// this case -- never activates either.
fn write_self_loop_fixture(dir: &Path) {
    write_java(
        dir,
        "GeneratedValueObject.java",
        r#"package p;
public class GeneratedValueObject {
    private BodID bodID;
    public void setBodID(String value) {
        BodID wrapped = createWrapper(value);
        setBodID(wrapped);
    }
    private void setBodID(BodID value) {
        this.bodID = value;
    }
    private BodID createWrapper(String value) {
        return null;
    }
}
"#,
    );
    write_java(dir, "BodID.java", "package p;\npublic class BodID {\n}\n");
}

/// This shape is untouched by ed65c3a8: `apply_arity_narrowing` only
/// changes behaviour when arity evidence RULES OUT a candidate (zero
/// matches). Both `setBodID(String)` and `setBodID(BodID)` genuinely
/// match this call's arity (1), so the pre- and post-#1898 arity pass
/// produce the IDENTICAL retained set here -- the fix's changed code path
/// is never exercised. See the accompanying report for the A/B
/// measurement across the pre-ed65c3a8 tree confirming this shape's
/// self-loop count is unchanged.
#[test]
fn same_arity_overload_self_loop_is_unaffected_by_1898() {
    let dir = TempDir::new().unwrap();
    write_self_loop_fixture(dir.path());
    let graph = build_graph_over(dir.path(), &["GeneratedValueObject.java", "BodID.java"]);

    let index = extract_index(dir.path(), "GeneratedValueObject.java");
    let public_setter = index
        .declarations
        .iter()
        .find(|d| d.name == "setBodID" && d.param_types.first().map(String::as_str) == Some("String"))
        .expect("fixture bug: no setBodID(String) declaration")
        .symbol;
    let dense = graph.dense_id_for(public_setter).expect("setBodID(String) must be interned");

    assert!(
        graph.callees_of(dense).contains(&dense),
        "reproduction failed: setBodID(String) must show a self-loop via \
         the same-arity overload magnet -- the exact \
         `possible_candidate_cycle self_loop=true` shape the shipped \
         template reports for the production JAXB/VO accessors"
    );
    assert_eq!(
        component_size_containing(&graph, dense),
        1,
        "a self-loop node must appear as its OWN singleton component -- \
         `component.len() > 1` filters miss self-recursion entirely"
    );
}
