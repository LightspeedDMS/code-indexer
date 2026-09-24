//! Issue #1956 (the safe-exclusion half of #1952): a type-qualified call
//! (`Util.normalize(x)`) must never leave its same-bare-METHOD-name decoy
//! (`Leaf.normalize`, declared on an unrelated class in a DIFFERENT
//! package) listing the qualified call sites as ITS OWN callers -- the
//! exact jsoup shape the issue reports (`TextNode.normaliseWhitespace`
//! wrongly listing `StringUtil.`-qualified callers, including a phantom
//! self-edge for the qualified call site sitting inside its own body).
//!
//! Reuses the identical neutral fixture shape `bug_1952_qualified_call_
//! wrong_owner_regressions.rs` established (two same-named-method classes
//! in different packages; a caller whose own file has an unrelated
//! `extends` clause tripping issue #1922's whole-file safety gate) -- that
//! file proves the TRUE edge (to `Util.normalize`) is never dropped; THIS
//! file proves the additional, previously-missing half: the FALSE edge
//! (to `Leaf.normalize`, including the phantom self-edge) must be gone.

mod common;

use common::{build_graph_over, declaration_symbol_owned_by, extract_index, write_source};

const UTIL_SOURCE: &str = r#"package com.example.util;

public class Util {
    public static String normalize(String s) {
        return s;
    }
}
"#;

const SOME_BASE_SOURCE: &str = r#"package com.example.core;

public class SomeBase {
}
"#;

/// The DECOY: same bare method name as `Util.normalize`, declared on a
/// class whose own file carries an UNRELATED `extends` clause (tripping
/// #1922's whole-file safety gate). One qualified call site sits INSIDE
/// `Leaf.normalize` itself (the phantom-self-edge shape); a second sits in
/// a different method of the same file.
const LEAF_SOURCE: &str = r#"package com.example.core;

import com.example.util.Util;

public class Leaf extends SomeBase {
    String render(String text) {
        return Util.normalize(text);
    }

    static String normalize(String text) {
        text = Util.normalize(text);
        return text;
    }
}
"#;

/// A THIRD file, same package as `Leaf` but NOT the same file, whose own
/// `extends` clause also trips the whole-file safety gate.
const CALLER_SOURCE: &str = r#"package com.example.core;

import com.example.util.Util;

public class Caller extends SomeBase {
    String run(String x) {
        return Util.normalize(x);
    }
}
"#;

/// The UNQUALIFIED call site, via a single-member static import -- legitimate
/// over-binding: both `Util.normalize` and `Leaf.normalize` stay reachable,
/// unaffected by this issue's fix.
const OTHER_SOURCE: &str = r#"package com.example.core;

import static com.example.util.Util.normalize;

public class Other {
    String run(String x) {
        return normalize(x);
    }
}
"#;

const UTIL_PATH: &str = "com/example/util/Util.java";
const SOME_BASE_PATH: &str = "com/example/core/SomeBase.java";
const LEAF_PATH: &str = "com/example/core/Leaf.java";
const CALLER_PATH: &str = "com/example/core/Caller.java";
const OTHER_PATH: &str = "com/example/core/Other.java";

#[test]
fn qualified_call_excludes_the_cross_package_same_method_name_decoy_and_its_own_self_loop() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), UTIL_PATH, UTIL_SOURCE);
    write_source(dir.path(), SOME_BASE_PATH, SOME_BASE_SOURCE);
    write_source(dir.path(), LEAF_PATH, LEAF_SOURCE);
    write_source(dir.path(), CALLER_PATH, CALLER_SOURCE);
    write_source(dir.path(), OTHER_PATH, OTHER_SOURCE);

    let util_index = extract_index(dir.path(), UTIL_PATH);
    let leaf_index = extract_index(dir.path(), LEAF_PATH);
    let caller_index = extract_index(dir.path(), CALLER_PATH);
    let other_index = extract_index(dir.path(), OTHER_PATH);

    let util_normalize = declaration_symbol_owned_by(&util_index, "normalize", "Util");
    let leaf_normalize = declaration_symbol_owned_by(&leaf_index, "normalize", "Leaf");
    let leaf_render = declaration_symbol_owned_by(&leaf_index, "render", "Leaf");
    let caller_run = declaration_symbol_owned_by(&caller_index, "run", "Caller");
    let other_run = declaration_symbol_owned_by(&other_index, "run", "Other");

    let graph = build_graph_over(
        dir.path(),
        &[UTIL_PATH, SOME_BASE_PATH, LEAF_PATH, CALLER_PATH, OTHER_PATH],
    );

    let util_dense = graph.dense_id_for(util_normalize).expect("Util.normalize must be interned");
    let leaf_normalize_dense = graph.dense_id_for(leaf_normalize).expect("Leaf.normalize must be interned");
    let leaf_render_dense = graph.dense_id_for(leaf_render).expect("Leaf.render must be interned");
    let caller_run_dense = graph.dense_id_for(caller_run).expect("Caller.run must be interned");
    let other_run_dense = graph.dense_id_for(other_run).expect("Other.run must be interned");

    let util_callers = graph.callers_index(util_dense);
    let leaf_callers = graph.callers_index(leaf_normalize_dense);

    // -----------------------------------------------------------------
    // Must NOT regress #1952: the TRUE, explicitly qualified edge to
    // Util.normalize must survive from all three qualified call sites.
    // -----------------------------------------------------------------
    assert!(
        util_callers.contains(&caller_run_dense),
        "Util.normalize must still list Caller.run as a caller. Got: {util_callers:?}"
    );
    assert!(
        util_callers.contains(&leaf_render_dense),
        "Util.normalize must still list Leaf.render as a caller. Got: {util_callers:?}"
    );
    assert!(
        util_callers.contains(&leaf_normalize_dense),
        "Util.normalize must still list Leaf.normalize as a caller. Got: {util_callers:?}"
    );

    // -----------------------------------------------------------------
    // The NEW assertions #1956 exists for: the decoy must NOT list the
    // qualified call sites as ITS OWN callers.
    // -----------------------------------------------------------------
    assert!(
        !leaf_callers.contains(&caller_run_dense),
        "Leaf.normalize must NOT list Caller.run as a caller -- the call is explicitly \
         qualified `Util.normalize(x)`, not `Leaf.normalize(x)`. Got: {leaf_callers:?}"
    );
    assert!(
        !leaf_callers.contains(&leaf_render_dense),
        "Leaf.normalize must NOT list Leaf.render as a caller -- the qualified call inside \
         Leaf.render resolves to Util.normalize, not to Leaf.normalize itself. Got: \
         {leaf_callers:?}"
    );
    assert!(
        !leaf_callers.contains(&leaf_normalize_dense),
        "Leaf.normalize must NOT list itself as its own caller (no phantom self-edge) -- the \
         qualified call inside its own body resolves to Util.normalize. Got: {leaf_callers:?}"
    );

    // -----------------------------------------------------------------
    // Legitimate over-binding, unchanged: the UNQUALIFIED static-import
    // call binds to BOTH declarations -- this is accepted, not a defect.
    // -----------------------------------------------------------------
    assert!(
        util_callers.contains(&other_run_dense),
        "Util.normalize must still list Other.run (unqualified static-import call) as a \
         caller. Got: {util_callers:?}"
    );
    assert!(
        leaf_callers.contains(&other_run_dense),
        "Leaf.normalize must still list Other.run (unqualified static-import call) as a \
         caller -- this is accepted over-binding for a genuinely unqualified call, never \
         'fixed' by this issue. Got: {leaf_callers:?}"
    );
}
