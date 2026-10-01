//! Issue #1922: a binding declared INSIDE a tree-sitter ERROR subtree is
//! silently absent from every extraction pass in this crate -- never
//! visited, never recorded, by ANY of the extraction machinery
//! (`typed_names`, `all_local_binding_names`, everything). An uppercase
//! identifier that would otherwise be a real local/pattern binding then
//! looks exactly like a genuine type qualifier: `Svc` in `case final
//! Worker Svc -> Svc.workA()` (a Java 21 switch pattern binding with a
//! `final` modifier the vendored tree-sitter-java grammar cannot parse)
//! is indistinguishable from the file's own unrelated nested class
//! `Top.Svc`, and hard-narrowing would bind exclusively to that decoy,
//! dropping the real edge to `Worker.workA()` (a genuinely-called
//! PRIVATE method) and flipping it to a false `is_definitely_dead_code()
//! == Some(true)`.
//!
//! The fixture declares NO supertypes and NO static wildcard import, so
//! the file-level guards this binder's hard-narrowing otherwise relies
//! on stay satisfied -- the ONLY thing standing between a correct result
//! and a false-dead flip is whether the file's own syntax-error status
//! is consulted at all.
//!
//! Neutral naming throughout (`Top`/`Svc`/`Worker`/`com.example.app`) --
//! synthetic identifiers, per this repository's Disclosure Discipline.

mod common;

use common::{build_graph_over, dead_and_caller_count, declaration_symbol_owned_by, write_source};

const UNPARSEABLE_SWITCH_PATTERN_SOURCE: &str = r#"package com.example.app;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    static class Worker {
        private void workA() {
        }
    }

    static class Sub {
        void run(Object o) {
            switch (o) {
                case final Worker Svc -> Svc.workA();
                default -> {
                }
            }
        }
    }
}
"#;

#[test]
fn a_file_with_an_unparseable_switch_pattern_falls_back_to_tag_only_and_keeps_the_real_edge() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Top.java",
        UNPARSEABLE_SWITCH_PATTERN_SOURCE,
    );

    let top_index = common::extract_index(dir.path(), "com/example/app/Top.java");
    assert!(
        top_index.has_syntax_error,
        "fixture bug: the `final` modifier on a switch pattern binding must trip the \
         vendored tree-sitter-java grammar's error flag -- confirmed via \
         scanner::parse_file_with_error_flag before this test was written"
    );
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    let graph = build_graph_over(dir.path(), &["com/example/app/Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(
        dead,
        Some(true),
        "Worker.workA() must never be reported definitely dead just because the file also \
         contains an unrelated syntax error elsewhere in its tree -- a binding inside an \
         ERROR subtree is invisible to every extraction pass, which must disable hard-\
         narrowing for the whole file, never silently misresolve a qualifier instead"
    );
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}
