//! Regression tests for Bug #1917's SECOND round -- closing the remaining
//! operator-convention gaps left open after `binary_expression`/`index_
//! expression` (see `bug_1917_kotlin_operator_convention_calls.rs`):
//! `unary_expression` (`!f`, `c++`, `--c`), `range_expression` (`x..y`),
//! `in_expression` (`s in b`, `s !in b`), and non-indexed compound
//! assignment (`c += 1`). Each is the SAME false-dead-verdict shape: a
//! `private operator fun` reached ONLY through one of these forms had zero
//! inbound edges and was reported `is_definitely_dead_code() ==
//! Some(true)` while genuinely called.
//!
//! **Discriminating RED (verified by hand before the fix in `kotlin.rs`):**
//! with none of `unary_expression`/`range_expression`/`in_expression`
//! dispatched and `assignment` handling only the indexed-write case (this
//! file's pre-fix state), every fixture below observed `Some(true)` with
//! zero callers. Every test asserts the TRANSITION explicitly (the pre-fix
//! snapshot noted in the assertion message AND the post-fix `Some(false)`/
//! >=1-caller assertion actually run), not merely the final answer.
//!
//! Fixtures use neutral `com.example.*` naming only, per this repository's
//! public-disclosure discipline.

use std::path::Path;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::SymbolId;
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, UserFact};
use xray_core::owned_node::OwnedNode;

struct NoOpCollector;
impl FactCollector for NoOpCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        Vec::new()
    }
}

fn write_source(dir: &Path, relative_path: &str, source: &str) {
    let full = dir.join(relative_path);
    if let Some(parent) = full.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(full, source).unwrap();
}

fn extract_index(dir: &Path, relative_path: &str) -> LocalIndex {
    let full_path = dir.join(relative_path);
    let result = xray_core::graph::fused::process_file_fused(&full_path, relative_path, &NoOpCollector)
        .unwrap_or_else(|| panic!("fixture bug: {relative_path} must parse"));
    result
        .index
        .unwrap_or_else(|| panic!("fixture bug: {relative_path} must extract (language must be supported)"))
}

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

fn build_graph_over(dir: &Path, relative_paths: &[&str]) -> CodeGraph {
    let options = RepoIndexOptions {
        budget: IndexBudget::unlimited(),
        max_files: None,
    };
    let paths: Vec<String> = relative_paths.iter().map(|p| p.to_string()).collect();
    let result = build_repo_graph(dir, &paths, &options, &NoOpCollector)
        .expect("no file_id collision in this fixture");
    result.graph
}

fn dead_and_caller_count(graph: &CodeGraph, symbol: SymbolId) -> (Option<bool>, usize) {
    let dense = graph
        .dense_id_for(symbol)
        .expect("symbol must be interned in the bound graph");
    (graph.is_definitely_dead_code(dense), graph.callers_index(dense).len())
}

const KOTLIN_FLAG_NOT: &str = r#"package com.example.app

class Flag(val on: Boolean) {
    private operator fun not(): Flag = Flag(!on)

    fun flip(f: Flag): Flag = !f
}
"#;

/// `unary_expression` half of #1917's second round: `Flag.not` is called
/// ONLY through Kotlin's prefix `!` operator-convention syntax (`!f`, one
/// line below its declaration) -- never through an ordinary `.not()` call.
/// Before this fix, `unary_expression` had no dispatch arm at all.
#[test]
fn private_operator_not_called_via_unary_expression_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Flag.kt", KOTLIN_FLAG_NOT);

    let index = extract_index(dir.path(), "com/example/app/Flag.kt");
    let not_symbol = declaration_symbol_owned_by(&index, "not", "Flag");

    let graph = build_graph_over(dir.path(), &["com/example/app/Flag.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, not_symbol);

    // Pre-fix snapshot: dead == Some(true), callers == 0 -- `!f` produced
    // zero evidence `not` is ever called.
    assert_eq!(
        dead,
        Some(false),
        "Flag.not is called one line below via Kotlin's prefix `!` operator-convention syntax \
         (`!f` desugars to `f.not()`) -- reporting it dead is a false verdict on live code. \
         Pre-fix, this same assertion observed Some(true) with zero callers."
    );
    assert!(
        callers >= 1,
        "Flag.not must keep its real `!` caller edge (pre-fix: 0 callers observed)"
    );
}

const KOTLIN_COUNTER_INC_DEC: &str = r#"package com.example.app

class Counter(var n: Int) {
    private operator fun inc(): Counter = Counter(n + 1)
    private operator fun dec(): Counter = Counter(n - 1)

    fun bumpUp(c: Counter): Counter {
        var x = c
        x++
        return x
    }

    fun bumpDown(c: Counter): Counter {
        var x = c
        --x
        return x
    }
}
"#;

/// The postfix/prefix `inc`/`dec` half of `unary_expression`: `Counter.inc`
/// is called only via `c++` (postfix), `Counter.dec` only via `--x`
/// (prefix) -- both share the ONE `unary_expression` grammar node,
/// discriminated only by which side the operator token falls on.
#[test]
fn private_operator_inc_and_dec_called_via_unary_expression_are_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Counter.kt", KOTLIN_COUNTER_INC_DEC);

    let index = extract_index(dir.path(), "com/example/app/Counter.kt");
    let inc_symbol = declaration_symbol_owned_by(&index, "inc", "Counter");
    let dec_symbol = declaration_symbol_owned_by(&index, "dec", "Counter");

    let graph = build_graph_over(dir.path(), &["com/example/app/Counter.kt"]);

    let (inc_dead, inc_callers) = dead_and_caller_count(&graph, inc_symbol);
    assert_eq!(
        inc_dead,
        Some(false),
        "Counter.inc is called via postfix `c++` -- reporting it dead is a false verdict on \
         live code. Pre-fix, this observed Some(true) with zero callers."
    );
    assert!(inc_callers >= 1, "Counter.inc must keep its real `++` caller edge");

    let (dec_dead, dec_callers) = dead_and_caller_count(&graph, dec_symbol);
    assert_eq!(
        dec_dead,
        Some(false),
        "Counter.dec is called via prefix `--x` -- reporting it dead is a false verdict on \
         live code. Pre-fix, this observed Some(true) with zero callers."
    );
    assert!(dec_callers >= 1, "Counter.dec must keep its real `--` caller edge");
}

const KOTLIN_SPAN_RANGE_TO: &str = r#"package com.example.app

class Span(val a: Int, val b: Int) {
    private operator fun rangeTo(other: Span): Int = b - other.a

    fun gap(x: Span, y: Span): Int = x..y
}
"#;

/// `range_expression` half of #1917's second round: `Span.rangeTo` is
/// called ONLY through Kotlin's `..` range-convention syntax (`x..y`, one
/// line below its declaration).
#[test]
fn private_operator_rangeto_called_via_range_expression_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Span.kt", KOTLIN_SPAN_RANGE_TO);

    let index = extract_index(dir.path(), "com/example/app/Span.kt");
    let range_to_symbol = declaration_symbol_owned_by(&index, "rangeTo", "Span");

    let graph = build_graph_over(dir.path(), &["com/example/app/Span.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, range_to_symbol);

    assert_eq!(
        dead,
        Some(false),
        "Span.rangeTo is called one line below via Kotlin's `..` range-convention syntax \
         (`x..y` desugars to `x.rangeTo(y)`) -- reporting it dead is a false verdict on live \
         code. Pre-fix, this same assertion observed Some(true) with zero callers."
    );
    assert!(
        callers >= 1,
        "Span.rangeTo must keep its real `..` caller edge (pre-fix: 0 callers observed)"
    );
}

const KOTLIN_BAG_CONTAINS: &str = r#"package com.example.app

class Bag {
    private operator fun contains(s: String): Boolean = s.isNotEmpty()

    fun has(b: Bag, s: String): Boolean = s in b
    fun hasNot(b: Bag, s: String): Boolean = s !in b
}
"#;

/// `in_expression` half of #1917's second round: `Bag.contains` is called
/// ONLY through Kotlin's `in`/`!in` containment-convention syntax (`s in
/// b`, `s !in b`, both one line below its declaration) -- both keywords
/// map to the SAME `contains` convention.
#[test]
fn private_operator_contains_called_via_in_and_not_in_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Bag.kt", KOTLIN_BAG_CONTAINS);

    let index = extract_index(dir.path(), "com/example/app/Bag.kt");
    let contains_symbol = declaration_symbol_owned_by(&index, "contains", "Bag");

    let graph = build_graph_over(dir.path(), &["com/example/app/Bag.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, contains_symbol);

    assert_eq!(
        dead,
        Some(false),
        "Bag.contains is called via Kotlin's `in`/`!in` containment-convention syntax (`s in \
         b` and `s !in b` both desugar to `b.contains(s)`) -- reporting it dead is a false \
         verdict on live code. Pre-fix, this same assertion observed Some(true) with zero \
         callers."
    );
    assert!(
        callers >= 2,
        "Bag.contains must keep BOTH its real `in` and `!in` caller edges (pre-fix: 0 callers \
         observed)"
    );
}

const KOTLIN_COUNTER_PLUS_ASSIGN: &str = r#"package com.example.app

class Counter(var n: Int) {
    private operator fun plusAssign(k: Int) {
        n += k
    }

    fun bump(c: Counter) {
        c += 1
    }
}
"#;

/// Non-indexed compound-assignment half of #1917's second round:
/// `Counter.plusAssign` is called ONLY through Kotlin's `+=`
/// compound-assignment convention syntax on a non-indexed target (`c +=
/// 1`, one line below its declaration).
#[test]
fn private_operator_plus_assign_called_via_compound_assignment_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Counter.kt", KOTLIN_COUNTER_PLUS_ASSIGN);

    let index = extract_index(dir.path(), "com/example/app/Counter.kt");
    let plus_assign_symbol = declaration_symbol_owned_by(&index, "plusAssign", "Counter");

    let graph = build_graph_over(dir.path(), &["com/example/app/Counter.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, plus_assign_symbol);

    assert_eq!(
        dead,
        Some(false),
        "Counter.plusAssign is called one line below via Kotlin's `+=` compound-assignment \
         convention syntax on a non-indexed target (`c += 1`) -- reporting it dead is a false \
         verdict on live code. Pre-fix, this same assertion observed Some(true) with zero \
         callers."
    );
    assert!(
        callers >= 1,
        "Counter.plusAssign must keep its real `+=` caller edge (pre-fix: 0 callers observed)"
    );
}

/// The over-binding fallback the coordinator required: `x += y` on a `val`
/// receiver desugars to `x = x.plus(y)` (the plain, non-Assign form) when
/// no `plusAssign` exists at all -- since the extractor cannot know
/// (without receiver-type/mutability information it does not track)
/// whether the REAL desugaring used `plusAssign` or `plus`, it must emit
/// BOTH candidates rather than guess one. This proves the plain `plus`
/// form is ALSO reachable through `c += 1`, alongside `plusAssign` above.
#[test]
fn a_plain_convention_fun_is_also_reachable_through_the_same_compound_assignment_site() {
    let source = r#"package com.example.app

class Counter(val n: Int) {
    private operator fun plus(k: Int): Counter = Counter(n)

    fun bump(c: Counter) {
        var x = c
        x += 1
    }
}
"#;
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Counter.kt", source);

    let index = extract_index(dir.path(), "com/example/app/Counter.kt");
    let plus_symbol = declaration_symbol_owned_by(&index, "plus", "Counter");

    let graph = build_graph_over(dir.path(), &["com/example/app/Counter.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, plus_symbol);

    assert_eq!(
        dead,
        Some(false),
        "Counter.plus must ALSO be reachable through `x += 1` (over-binding both the \
         `plusAssign` and `plus` candidates is the safe direction when the real desugaring \
         cannot be determined without receiver-type/mutability evidence this extractor does \
         not track)"
    );
    assert!(callers >= 1, "Counter.plus must keep its real `+=`-reached caller edge");
}

/// A pure control: an indexed compound assignment (`m[k] += v`) remains a
/// documented, unclosed gap in THIS round (the coordinator scoped the
/// compound-assignment fix to non-indexed targets only) -- it must still
/// fall back to the safe `get` default from the PRIOR round, never crash,
/// and never fabricate a `plusAssign`/`plus` edge for an indexed target.
#[test]
fn an_indexed_compound_assignment_still_falls_back_to_get_never_plus_assign() {
    let source = r#"package com.example.app

class Counters {
    operator fun get(key: String): Int = 0
    operator fun plusAssign(k: Int) {}

    fun bump(k: String) {
        this[k] += 1
    }
}
"#;
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Counters.kt", source);

    let index = extract_index(dir.path(), "com/example/app/Counters.kt");
    index
        .invocations
        .iter()
        .find(|i| i.callee_name == "get")
        .expect("an indexed compound assignment must still fall back to a `get` invocation");
    assert!(
        index.invocations.iter().all(|i| i.callee_name != "plusAssign"),
        "an INDEXED compound-assignment target must never fabricate a `plusAssign` \
         invocation -- that desugaring is a distinct, more complex shape this extractor does \
         not attempt to disambiguate"
    );
}

/// A pure control proving the new dispatch arms do not fabricate liveness
/// for an unrelated, genuinely unreferenced declaration.
#[test]
fn an_unrelated_unreferenced_declaration_is_still_reported_dead_alongside_the_new_operators() {
    let source = r#"package com.example.app

class Flag(val on: Boolean) {
    operator fun not(): Flag = Flag(!on)

    private fun neverCalled(): Int = 0

    fun flip(f: Flag): Flag = !f
}
"#;
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Flag.kt", source);

    let index = extract_index(dir.path(), "com/example/app/Flag.kt");
    let never_called = declaration_symbol_owned_by(&index, "neverCalled", "Flag");

    let graph = build_graph_over(dir.path(), &["com/example/app/Flag.kt"]);
    let (dead, _callers) = dead_and_caller_count(&graph, never_called);
    assert_eq!(
        dead,
        Some(true),
        "neverCalled is genuinely unreferenced -- the new unary/range/in/compound-assign \
         dispatch must not fabricate liveness for unrelated declarations"
    );
}

/// The 13 permanent liveness guards in `bug_1910_narrowing_liveness_
/// guards.rs` are Java-only fixtures, run in the same gate as this file,
/// and must remain green -- adding new dispatch arms to the Kotlin
/// extractor cannot alter Java-only binding.
#[test]
fn kotlin_only_symbols_never_leak_into_a_java_only_bind() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/OnlyJava.java",
        r#"package com.example.app;

public class OnlyJava {
    public void run() {}
}
"#,
    );
    let graph = build_graph_over(dir.path(), &["com/example/app/OnlyJava.java"]);
    let depths = graph.binder_depths();
    assert!(
        depths.iter().all(|d| d.language != "kt"),
        "a repo with no .kt files must never report a Kotlin binder depth entry"
    );
}
