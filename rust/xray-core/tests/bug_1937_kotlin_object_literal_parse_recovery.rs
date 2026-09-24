//! Regression tests for Issue #1937 -- "a Kotlin property getter whose
//! body contains an `object :` literal makes the extractor lose most of
//! the file's declarations".
//!
//! **Root cause, established with evidence (`rust/xray-core/tests/`
//! scratch investigation, not guessed):** this is NOT an extractor-walk
//! bug in `kotlin.rs` -- it is a genuine PARSE-RECOVERY defect in the
//! vendored `tree-sitter-kotlin-ng` 1.1.0 grammar. Dumping the real parse
//! tree (`scanner::parse_file_with_error_flag`, and a fresh
//! `tree_sitter::Parser` bypassing this crate's thread-local parser reuse
//! entirely, to rule out cross-test contamination) for the issue's exact
//! repro shows the tree-sitter ROOT node itself becomes a single `ERROR`
//! node whose materialized children simply STOP partway through the file
//! -- everything after that point (the rest of the getter, the following
//! `class Nested`, the `init` block, `fun gg()`) is absent from the tree
//! entirely, not even present as raw/ERROR-child tokens. There is nothing
//! for the extractor's walk to see or emit a `Declaration` for; the data
//! genuinely does not exist in the parse tree it is handed.
//!
//! A controlled bisection (documented in git history for this file's
//! introducing commit, not reproduced in full here) pinned the EXACT
//! trigger: it is not the getter, not `override`, not an undefined
//! supertype, and not an empty function body. It is whether the
//! object-literal's own opening `{`, its function-member declaration, and
//! its closing `}` all sit on ONE physical source line. `object :
//! Runnable {}` (no member) is fine. `object : Runnable { fun run() {} }`
//! or `object : Runnable { override fun run() {} }` on one line both
//! break, in a getter, a setter, an `init` block, or a plain function --
//! confirmed for all four below. The SAME object literal reformatted with
//! its own braces on separate lines parses cleanly with full extraction,
//! in every one of those four contexts.
//!
//! Per the issue's own acceptance criteria ("if the extractor genuinely
//! cannot handle a shape, it must degrade loudly ... never silently"):
//! since no extractor code change can recover data that was never parsed,
//! this suite instead PROVES the existing, language-agnostic degradation
//! pipeline (`scanner::parse_file_with_error_flag`'s `has_error` ->
//! `LocalIndex::has_syntax_error` -> `RepoIndexResult::
//! files_with_parse_errors`/`fact_graph_complete` in `repo_index.rs`,
//! already exercised for Java by `parse_errors_are_counted_separately_
//! from_unreadable_or_unsupported_files` in that module's own tests)
//! already correctly fires for every one of these Kotlin shapes, so an
//! analysis over such a file is signaled as incomplete rather than
//! silently trusted. `is_definitely_dead_code` is also checked directly:
//! the one declaration that DOES survive the broken parse (the object
//! literal's own `run` override, walked with no enclosing type since the
//! `class_declaration`/`object_literal` wrapper nodes themselves never
//! made it into the tree) must never be reported a false `Some(true)`
//! dead-code verdict merely because its ownership bookkeeping is absent.
//!
//! Every fixture is real, compilable Kotlin source, using neutral
//! `com.example.*` naming only, per this repository's public-disclosure
//! discipline.

mod common;

use xray_core::graph::budget::IndexBudget;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};

const GETTER_MULTILINE_FORM: &str = r#"package com.example.app

class Holder {
    val q: Int
        get() {
            val o = object : Runnable { override fun run() {} }
            return gg()
        }
    class Nested { fun hfoo() {} }
    init { gg() }
    fun gg(): Int = 1
}
"#;

const GETTER_ONELINE_FORM: &str = r#"package com.example.app

class Holder {
    val q: Int get() { val o = object : Runnable { override fun run() {} }; return gg() }
    class Nested { fun hfoo() {} }
    init { gg() }
    fun gg(): Int = 1
}
"#;

const SETTER_FORM: &str = r#"package com.example.app

class Holder {
    var q: Int = 0
        set(value) {
            val o = object : Runnable { override fun run() {} }
            field = value
            gg()
        }
    class Nested { fun hfoo() {} }
    init { gg() }
    fun gg(): Int = 1
}
"#;

const INIT_BLOCK_FORM: &str = r#"package com.example.app

class Holder {
    init {
        val o = object : Runnable { override fun run() {} }
        gg()
    }
    class Nested { fun hfoo() {} }
    fun gg(): Int = 1
}
"#;

/// The SAME shape as `GETTER_MULTILINE_FORM`, with the object literal's
/// own braces (and the `Nested`/`init` bodies, to isolate this from the
/// separate, unrelated single-line-class-body scanner quirk noted below)
/// reformatted onto their own lines -- idiomatic Kotlin style. This is
/// the CONTROL fixture: it proves the extractor itself (getter handling,
/// object-literal handling, synthetic-scope call attribution) is fully
/// correct once tree-sitter-kotlin-ng is given a shape it can parse.
const CONTROL_MULTILINE_OBJECT_LITERAL_FORM: &str = r#"package com.example.app

class Holder {
    val q: Int
        get() {
            val o = object : Runnable {
                override fun run() {}
            }
            return gg()
        }
    class Nested {
        fun hfoo() {}
    }
    init {
        gg()
    }
    fun gg(): Int = 1
}
"#;

fn assert_broken_shape_degrades_loudly(index: &LocalIndex, label: &str) {
    assert!(index.has_syntax_error, "{label}: the parse-recovery gap must be signaled via has_syntax_error");
    for missing in ["Holder", "q", "Nested", "hfoo", "gg"] {
        assert!(
            !index.declarations.iter().any(|d| d.name == missing),
            "{label}: {missing:?} is expected to be genuinely absent from the parse tree -- if this now \
             passes, tree-sitter-kotlin-ng has been upgraded/fixed and this pinned assertion (and the \
             module doc note in kotlin.rs) should be revisited, not silently loosened"
        );
    }
    // The object literal's own `run` override survives (it is a real,
    // well-formed sub-tree the parser salvaged before giving up), but with
    // no recorded owner -- `class_declaration`/`object_literal` never made
    // it into the tree, so `dispatch_function_declaration` never received
    // an enclosing type for it. Never falsely attributed to Holder.
    assert!(
        index.declarations.iter().any(|d| d.name == "run"),
        "{label}: the object literal's own well-formed `run` sub-tree should still be walked"
    );
    assert!(
        !index.method_owners.iter().any(|o| o.enclosing_type == "Holder"),
        "{label}: nothing may be falsely attributed as owned by Holder -- Holder's own \
         class_declaration node never made it into the parse tree"
    );
}

#[test]
fn getter_multiline_form_degrades_loudly_never_silently() {
    let dir = tempfile::tempdir().unwrap();
    common::write_source(dir.path(), "Holder.kt", GETTER_MULTILINE_FORM);
    let index = common::extract_index(dir.path(), "Holder.kt");
    assert_broken_shape_degrades_loudly(&index, "getter (multiline)");
}

#[test]
fn getter_oneline_form_degrades_loudly_never_silently() {
    let dir = tempfile::tempdir().unwrap();
    common::write_source(dir.path(), "Holder.kt", GETTER_ONELINE_FORM);
    let index = common::extract_index(dir.path(), "Holder.kt");
    assert_broken_shape_degrades_loudly(&index, "getter (oneline)");
}

#[test]
fn setter_form_degrades_loudly_never_silently() {
    let dir = tempfile::tempdir().unwrap();
    common::write_source(dir.path(), "Holder.kt", SETTER_FORM);
    let index = common::extract_index(dir.path(), "Holder.kt");
    assert_broken_shape_degrades_loudly(&index, "setter");
}

#[test]
fn init_block_form_degrades_loudly_never_silently() {
    let dir = tempfile::tempdir().unwrap();
    common::write_source(dir.path(), "Holder.kt", INIT_BLOCK_FORM);
    let index = common::extract_index(dir.path(), "Holder.kt");
    assert_broken_shape_degrades_loudly(&index, "init block");
}

/// Repo-level proof (per Issue #1937's acceptance criterion): a build
/// that includes one of these broken files reports `fact_graph_complete
/// == false` and a nonzero `files_with_parse_errors`, via the SAME
/// language-agnostic mechanism `repo_index.rs`'s own Java-based
/// `parse_errors_are_counted_separately_from_unreadable_or_unsupported_
/// files` test already pins -- this is not new Kotlin-specific wiring,
/// just confirmation the existing generic pipeline already covers this
/// shape.
#[test]
fn repo_level_build_flips_fact_graph_complete_false_for_the_broken_getter_file() {
    let dir = tempfile::tempdir().unwrap();
    common::write_source(dir.path(), "Holder.kt", GETTER_MULTILINE_FORM);
    let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
    let result = build_repo_graph(dir.path(), &["Holder.kt".to_string()], &options, &common::NoOpCollector)
        .expect("no file_id collision in this fixture");

    assert_eq!(result.files_with_parse_errors, 1, "the broken Kotlin file must count as a parse error");
    assert!(!result.fact_graph_complete, "a genuine parse-recovery gap must flip fact_graph_complete to false");
    assert_ne!(
        result.graph.completeness(),
        xray_core::graph::budget::AnalysisCompleteness::Complete,
        "the graph's own completeness() must also be downgraded, not just the repo-level flag"
    );
}

/// Liveness proof (per this story's own instruction 3): the one
/// declaration that DOES survive the broken parse -- `run`, walked with
/// no enclosing type recorded -- must never be reported a false
/// `Some(true)` ("definitely dead") verdict. It correctly comes back
/// `None` (undecidable): its kind is `Method` and its visibility is the
/// Kotlin default (public, no explicit modifier), which
/// `is_provably_not_externally_visible()` never proves false for, so
/// `is_definitely_dead_code` cannot mark it dead regardless of the
/// missing ownership bookkeeping. This is the CORRECT, pre-existing
/// behavior for every under-attributed symbol in this codebase (the same
/// visibility-gated contract `bug_1920_kotlin_instance_qualified_calls
/// .rs` already pins for a different Kotlin shape) -- this test proves
/// this bug's own broken shape does not carve out an exception to it.
#[test]
fn ownerless_surviving_run_declaration_is_never_falsely_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    common::write_source(dir.path(), "Holder.kt", GETTER_MULTILINE_FORM);
    let index = common::extract_index(dir.path(), "Holder.kt");
    let run_symbol = index
        .declarations
        .iter()
        .find(|d| d.name == "run")
        .expect("fixture bug: run must still be extracted from the surviving fragment")
        .symbol;

    let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
    let result = build_repo_graph(dir.path(), &["Holder.kt".to_string()], &options, &common::NoOpCollector)
        .expect("no file_id collision in this fixture");
    let dense = result.graph.dense_id_for(run_symbol).expect("run must be interned in the bound graph");

    assert_eq!(
        result.graph.is_definitely_dead_code(dense),
        None,
        "an ownerless, default-visibility declaration surviving a parse-recovery gap must never be \
         falsely reported Some(true) dead -- it must come back undecidable"
    );
}

/// The control/positive counterpart: once the object literal's own
/// braces are on separate lines, tree-sitter-kotlin-ng parses this exact
/// shape (getter + object literal + trailing sibling declarations)
/// cleanly, with zero syntax error, every declaration present, and the
/// getter's `gg()` call correctly attributed to the LEXICALLY ENCLOSING
/// TYPE (`Holder`) via the getter's synthetic scope -- Issue #1930's own
/// rule. This proves the extractor's getter/object-literal/synthetic-
/// scope machinery is correct; Bug #1937 is entirely a parser-level
/// phenomenon, never an extraction bug.
#[test]
fn control_multiline_object_literal_parses_cleanly_with_full_extraction_and_correct_attribution() {
    let dir = tempfile::tempdir().unwrap();
    common::write_source(dir.path(), "Holder.kt", CONTROL_MULTILINE_OBJECT_LITERAL_FORM);
    let index = common::extract_index(dir.path(), "Holder.kt");

    assert!(!index.has_syntax_error, "the control fixture must parse with zero syntax error");
    for name in ["Holder", "q", "Nested", "hfoo", "gg", "run"] {
        assert!(
            index.declarations.iter().any(|d| d.name == name),
            "control fixture: {name:?} must be extracted when the object literal parses cleanly"
        );
    }

    let gg_symbol = common::declaration_symbol_owned_by(&index, "gg", "Holder");
    let holder_type_symbol = common::type_declaration_symbol(&index, "Holder");

    let options = RepoIndexOptions { budget: IndexBudget::unlimited(), max_files: None };
    let result = build_repo_graph(dir.path(), &["Holder.kt".to_string()], &options, &common::NoOpCollector)
        .expect("no file_id collision in this fixture");
    assert!(result.fact_graph_complete, "the control fixture must build with a complete graph");

    let dense_gg = result.graph.dense_id_for(gg_symbol).expect("gg must be interned");
    let callers: Vec<_> = result.graph.callers_index(dense_gg).to_vec();
    let dense_holder = result.graph.dense_id_for(holder_type_symbol).expect("Holder type must be interned");
    assert!(
        callers.contains(&dense_holder),
        "gg() called from inside the getter's synthetic scope must be attributed to Holder, its \
         lexically enclosing type, per Issue #1930's synthetic-scope rule -- got callers {callers:?}"
    );
}
