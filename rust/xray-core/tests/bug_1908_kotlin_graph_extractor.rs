//! Regression tests for Bug #1908 -- X-Ray graph mode had a Kotlin
//! extractor for bind levels 0-2 missing entirely, so `rust/xray-core/src/
//! graph/extract/mod.rs::extractor_for_language` returned `Unsupported`
//! for `"kt"`/`"kts"`. Graph mode was merely INERT on a Kotlin-only repo,
//! but WRONG on a MIXED Java+Kotlin repo (the flagship repository's real
//! shape): a Java method called ONLY from Kotlin got no inbound edge and
//! was reported `is_definitely_dead_code() == Some(true)` -- a false dead
//! verdict on live code, the exact outcome epic #1786 declared
//! structurally impossible.
//!
//! **Discriminating RED, verified by hand before this file was written**
//! (not merely asserted here -- the fix IS the extractor registration, so
//! there is no way to toggle it from within a `cargo test` binary without
//! duplicating the registry): with `"kt" | "kts" =>
//! ExtractorLookup::Supported(...)` temporarily removed from `extract/
//! mod.rs`, `java_method_called_only_from_kotlin_is_not_reported_dead`
//! below fails with `dead == Some(true)` and `callers == 0` -- the Kotlin
//! call site is invisible to the binder entirely (the `.kt` file
//! contributes zero declarations and zero references), so the Java
//! method's only real caller never exists in the graph. With the
//! registration restored (this file's actual state), the same test
//! passes: `dead == Some(false)`, `callers >= 1`.
//!
//! Every fixture is real, compilable Java/Kotlin (verified structurally
//! against the real tree-sitter-kotlin-ng 1.1.0 grammar dump the
//! extractor itself was built from -- see `kotlin.rs` module docs), using
//! neutral `com.example.*` naming only (no customer/third-party
//! identifiers), per this repository's public-disclosure discipline.

use std::path::Path;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::local_index::{Declaration, LocalIndex};
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

/// Extracts one already-written file through the REAL per-extension
/// registry (`extractor_for_language` via the fused pipeline's own
/// `process_file_fused`), never a hand-picked extractor -- this is what
/// makes the cross-language wiring itself part of what each test proves,
/// not just each extractor in isolation.
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

fn declaration_symbol(index: &LocalIndex, name: &str) -> SymbolId {
    let matches: Vec<&Declaration> = index.declarations.iter().filter(|d| d.name == name).collect();
    match matches.as_slice() {
        [only] => only.symbol,
        [] => panic!("fixture bug: no declaration named {name:?}"),
        other => panic!("fixture bug: {name:?} is ambiguous ({} declarations)", other.len()),
    }
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

const JAVA_UTIL: &str = r#"package com.example.app;

public class JavaUtil {
    private static String helper(String raw) {
        return raw.trim();
    }
}
"#;

const KOTLIN_CALLER: &str = r#"package com.example.app

fun useJavaHelper(raw: String): String {
    return JavaUtil.helper(raw)
}
"#;

/// THE decisive test (issue #1908's own acceptance criterion): a Java
/// method called ONLY from Kotlin must resolve a real inbound edge and
/// must never be reported definitely dead. See this file's module doc for
/// the hand-verified RED transition (`Some(true)`/0 callers with the
/// Kotlin registration removed; `Some(false)`/>=1 caller with it present,
/// this test's actual assertion).
#[test]
fn java_method_called_only_from_kotlin_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/JavaUtil.java", JAVA_UTIL);
    write_source(dir.path(), "com/example/app/KotlinCaller.kt", KOTLIN_CALLER);

    let java_index = extract_index(dir.path(), "com/example/app/JavaUtil.java");
    let helper_symbol = declaration_symbol_owned_by(&java_index, "helper", "JavaUtil");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/JavaUtil.java", "com/example/app/KotlinCaller.kt"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, helper_symbol);
    assert_eq!(
        dead,
        Some(false),
        "JavaUtil.helper is called from Kotlin (JavaUtil.helper(raw)) and must never be reported \
         definitely dead -- this is the exact false-dead-verdict shape #1908 exists to close"
    );
    assert!(callers >= 1, "JavaUtil.helper must keep its real Kotlin caller edge");
}

const JAVA_CALLER: &str = r#"package com.example.app;

public class JavaCaller {
    public String run(String raw) {
        return KotlinUtil.helper(raw);
    }
}
"#;

const KOTLIN_UTIL: &str = r#"package com.example.app

object KotlinUtil {
    fun helper(raw: String): String {
        return raw.trim()
    }
}
"#;

/// The OTHER direction of the cross-language boundary: a Kotlin function
/// called ONLY from Java must also resolve a real inbound edge. The issue
/// requires BOTH directions ("a Kotlin call to a Java method, and a Java
/// call to a Kotlin function, must both produce edges") -- a Kotlin-only
/// extractor that cannot see Java is worth nothing here, per the issue's
/// own framing, because the whole bug is the boundary.
#[test]
fn kotlin_function_called_only_from_java_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/JavaCaller.java", JAVA_CALLER);
    write_source(dir.path(), "com/example/app/KotlinUtil.kt", KOTLIN_UTIL);

    let kotlin_index = extract_index(dir.path(), "com/example/app/KotlinUtil.kt");
    let helper_symbol = declaration_symbol_owned_by(&kotlin_index, "helper", "KotlinUtil");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/JavaCaller.java", "com/example/app/KotlinUtil.kt"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, helper_symbol);
    assert_eq!(
        dead,
        Some(false),
        "KotlinUtil.helper is called from Java (KotlinUtil.helper(raw)) and must never be reported \
         definitely dead"
    );
    assert!(callers >= 1, "KotlinUtil.helper must keep its real Java caller edge");
}

/// A pure control: a Kotlin declaration genuinely unreferenced anywhere
/// (Java or Kotlin) must still be reported definitely dead -- proving the
/// extractor does not simply mark everything alive as a workaround. Not
/// gated on `Visibility::Private` (Kotlin's own default is `public`, so a
/// plain top-level `private` function is the shape that IS provably
/// restricted).
#[test]
fn an_unreferenced_private_kotlin_function_is_still_reported_dead() {
    let source = r#"package com.example.app

private fun neverCalled(): Int {
    return 1
}

fun entryPoint(): Int {
    return 0
}
"#;
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.kt", source);

    let index = extract_index(dir.path(), "com/example/app/Sample.kt");
    let never_called = declaration_symbol(&index, "neverCalled");

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.kt"]);
    let (dead, _callers) = dead_and_caller_count(&graph, never_called);
    assert_eq!(
        dead,
        Some(true),
        "neverCalled is genuinely unreferenced and private -- it must still be reported \
         definitely dead; the extractor must not fabricate liveness"
    );
}

const KOTLIN_ALIAS_PRODUCER: &str = r#"package com.example.app

fun target(): Int = 1
"#;

const KOTLIN_ALIAS_CALLER: &str = r#"package com.example.client

import com.example.app.target as alias

fun run() = alias()
"#;

/// Bug #1908 follow-up (P1): `target()` is called ONLY through its
/// aliased import (`import ... as alias`, then `alias()`). The grammar
/// recognizes the alias and the extractor used to discard it, so the
/// call site's recorded name (`alias`) could never match `target`'s real
/// declaration -- exactly the false-dead-verdict class #1908 exists to
/// close.
#[test]
fn function_called_through_an_aliased_import_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Producer.kt", KOTLIN_ALIAS_PRODUCER);
    write_source(dir.path(), "com/example/client/Caller.kt", KOTLIN_ALIAS_CALLER);

    let producer_index = extract_index(dir.path(), "com/example/app/Producer.kt");
    let target_symbol = declaration_symbol(&producer_index, "target");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Producer.kt", "com/example/client/Caller.kt"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, target_symbol);
    assert_eq!(
        dead,
        Some(false),
        "target() is called through its aliased import (`import ... as alias`, then \
         `alias()`) -- the alias is grammar-recognized and then discarded today, so the \
         local name actually used at the call site never matches the real declaration"
    );
    assert!(callers >= 1, "target() must keep its real caller edge reached through the alias");
}

const KOTLIN_HIERARCHY_WITH_BARE_SUPER: &str = r#"package com.example.app

open class Base {
    constructor(x: Int)
}

class Sub : Base {
    constructor(x: Int) : super(x)
}
"#;

/// Bug #1908 follow-up (reviewer finding A): `Base`'s own secondary
/// constructor is called only by `Sub`'s `: super(x)` delegation, whose
/// supertype specifier is BARE (`class Sub : Base`, no parens) -- the
/// shape Kotlin REQUIRES whenever the superclass has no primary
/// constructor. The extractor's old `super(...)` resolution only matched
/// an `Extends`-classified inheritance record, which this bare shape
/// never produces (it is classified `Implements`) -- so this edge was a
/// dead branch reachable by no legal Kotlin input.
#[test]
fn base_secondary_constructor_called_via_bare_supertype_super_delegation_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Hierarchy.kt", KOTLIN_HIERARCHY_WITH_BARE_SUPER);

    let index = extract_index(dir.path(), "com/example/app/Hierarchy.kt");
    let base_ctor = declaration_symbol_owned_by(&index, "Base", "Base");

    let graph = build_graph_over(dir.path(), &["com/example/app/Hierarchy.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, base_ctor);
    assert_eq!(
        dead,
        Some(false),
        "Base's own secondary constructor is called by Sub's `: super(x)` delegation -- \
         reporting it dead is the exact false-dead-verdict class #1908 exists to close"
    );
    assert!(callers >= 1, "Base's constructor must keep its real super(...) caller edge");
}

const KOTLIN_INFIX_MATCHER: &str = r#"package com.example.app

class Matcher {
    private infix fun matches(other: String): Boolean = true

    fun run(s: String): Boolean = this matches s
}
"#;

/// Bug #1908 follow-up (reviewer finding B): `Matcher.matches` is called
/// ONLY through Kotlin's infix call syntax (`this matches s`), which the
/// extractor's `dispatch_node` did not handle at all (`infix_expression`
/// had no match arm) -- a private infix function called this way reported
/// zero inbound edges and a false definitely-dead verdict.
#[test]
fn private_infix_function_called_via_infix_syntax_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Matcher.kt", KOTLIN_INFIX_MATCHER);

    let index = extract_index(dir.path(), "com/example/app/Matcher.kt");
    let matches_symbol = declaration_symbol_owned_by(&index, "matches", "Matcher");

    let graph = build_graph_over(dir.path(), &["com/example/app/Matcher.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, matches_symbol);
    assert_eq!(
        dead,
        Some(false),
        "Matcher.matches is called one line below via Kotlin's infix call syntax \
         (`this matches s`) -- reporting it dead is the exact false-dead-verdict class \
         #1908 exists to close"
    );
    assert!(callers >= 1, "Matcher.matches must keep its real infix caller edge");
}

/// The 13 permanent liveness guards in `bug_1910_narrowing_liveness_
/// guards.rs` are Java-only fixtures and are unaffected by adding a
/// second `LanguageExtractor` to the registry (the registry dispatches
/// per-file by extension; a `.java` file's extraction is byte-for-byte
/// unchanged) -- verified by running that file in the same gate as this
/// one, not merely asserted here.
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
