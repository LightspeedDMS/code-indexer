//! Issue #1922: a binding declared OUTSIDE any method body -- a lambda
//! parameter in a field initializer, an enum constant's argument list,
//! or a switch-expression pattern in a field initializer -- has no
//! enclosing method `SymbolId` at all, so `LocalIndex::typed_names`
//! (keyed by `NameScope::Local { enclosing_method }`) never records it.
//! An uppercase identifier bound this way is then structurally
//! indistinguishable from a genuine type qualifier: `Svc` in `Svc ->
//! Svc.workA()` looks exactly like the file's own unrelated nested class
//! `Top.Svc`, and hard-narrowing would bind exclusively to that decoy,
//! dropping the real edge to `Worker.workA()` (a genuinely-called
//! PRIVATE method) and flipping it to a false `is_definitely_dead_code()
//! == Some(true)`.
//!
//! Every fixture below declares NO supertypes and NO static wildcard
//! import, so the file-level guards this binder's hard-narrowing
//! otherwise relies on stay satisfied -- the ONLY thing standing between
//! a correct result and a false-dead flip is whether the lambda/pattern
//! binding's own NAME is visible to `is_definite_type_qualifier`'s
//! existence check, regardless of context.
//!
//! Neutral naming throughout (`Top`/`Svc`/`Worker`/`com.example.app`) --
//! synthetic identifiers, per this repository's Disclosure Discipline.

mod common;

use common::{build_graph_over, dead_and_caller_count, declaration_symbol_owned_by, write_source};

/// Shared execution/assertion flow every fixture below drives identically
/// -- write `source` as `Top.java`, extract it, resolve `Worker.workA()`,
/// build the real graph, and assert the edge survives. `message`
/// explains the SPECIFIC binding-context shape that fixture pins.
fn assert_worker_work_a_kept_alive(source: &str, message: &str) {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Top.java", source);

    let top_index = common::extract_index(dir.path(), "com/example/app/Top.java");
    let work_a = declaration_symbol_owned_by(&top_index, "workA", "Worker");

    let graph = build_graph_over(dir.path(), &["com/example/app/Top.java"]);
    let (dead, callers) = dead_and_caller_count(&graph, work_a);

    assert_ne!(dead, Some(true), "{message}");
    assert!(
        callers >= 1,
        "Worker.workA() must keep its real caller edge -- got {callers} caller(s)"
    );
}

// =====================================================================
// A lambda's UNTYPED parameter (`Svc -> Svc.workA()`), bound in a
// STATIC field initializer.
// =====================================================================

const K2_SOURCE: &str = r#"package com.example.app;

import java.util.function.Consumer;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    static class Worker {
        private void workA() {
        }
    }

    static final Consumer<Worker> C = Svc -> Svc.workA();
}
"#;

#[test]
fn k2_an_untyped_lambda_parameter_in_a_static_field_initializer_keeps_the_real_edge() {
    assert_worker_work_a_kept_alive(
        K2_SOURCE,
        "Worker.workA() is genuinely reached via the lambda parameter Svc, bound in a static \
         field initializer with no enclosing method at all, and must never be reported \
         definitely dead just because that context is invisible to typed_names",
    );
}

// =====================================================================
// A lambda's EXPLICITLY-typed parameter (`(Worker Svc) -> Svc.workA()`),
// bound in a STATIC field initializer -- same context, different
// grammar shape (`formal_parameters` -> `formal_parameter`).
// =====================================================================

const K2B_SOURCE: &str = r#"package com.example.app;

import java.util.function.Consumer;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    static class Worker {
        private void workA() {
        }
    }

    static final Consumer<Worker> C = (Worker Svc) -> Svc.workA();
}
"#;

#[test]
fn k2b_an_explicitly_typed_lambda_parameter_in_a_static_field_initializer_keeps_the_real_edge() {
    assert_worker_work_a_kept_alive(
        K2B_SOURCE,
        "Worker.workA() must never be reported definitely dead just because its own \
         explicitly-typed lambda parameter name coincidentally shares the bare name of an \
         unrelated nested type",
    );
}

// =====================================================================
// The same untyped-lambda shape as the first fixture, but bound in an
// INSTANCE field initializer instead of a static one.
// =====================================================================

const K2C_SOURCE: &str = r#"package com.example.app;

import java.util.function.Consumer;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    static class Worker {
        private void workA() {
        }
    }

    final Consumer<Worker> c = Svc -> Svc.workA();
}
"#;

#[test]
fn k2c_an_untyped_lambda_parameter_in_an_instance_field_initializer_keeps_the_real_edge() {
    assert_worker_work_a_kept_alive(
        K2C_SOURCE,
        "Worker.workA() must never be reported definitely dead just because its lambda \
         parameter is bound in an INSTANCE (not static) field initializer",
    );
}

// =====================================================================
// An untyped lambda parameter bound inside an ENUM CONSTANT's own
// argument list (`X(Svc -> Svc.workA())`) -- yet another context that
// never carries an enclosing method.
// =====================================================================

const K2E_SOURCE: &str = r#"package com.example.app;

import java.util.function.Consumer;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    static class Worker {
        private void workA() {
        }
    }

    enum E {
        X(Svc -> Svc.workA());

        E(Consumer<Worker> c) {
        }
    }
}
"#;

#[test]
fn k2e_an_untyped_lambda_parameter_in_an_enum_constants_argument_list_keeps_the_real_edge() {
    assert_worker_work_a_kept_alive(
        K2E_SOURCE,
        "Worker.workA() must never be reported definitely dead just because its lambda \
         parameter is bound inside an enum constant's own argument list",
    );
}

// =====================================================================
// A Java 21 switch-EXPRESSION type-pattern binding (`case Worker Svc
// ->`), assigned directly to a STATIC field initializer.
// =====================================================================

const K2G_SOURCE: &str = r#"package com.example.app;

public class Top {
    static class Svc {
        static void workA() {
        }
    }

    static class Worker {
        private void workA() {
        }
    }

    static final int R = switch ((Object) new Worker()) {
        case Worker Svc -> {
            Svc.workA();
            yield 1;
        }
        default -> 0;
    };
}
"#;

#[test]
fn k2g_a_switch_expression_type_pattern_in_a_field_initializer_keeps_the_real_edge() {
    assert_worker_work_a_kept_alive(
        K2G_SOURCE,
        "Worker.workA() must never be reported definitely dead just because its switch-\
         expression pattern binding is declared in a field initializer, with no enclosing \
         method at all",
    );
}
