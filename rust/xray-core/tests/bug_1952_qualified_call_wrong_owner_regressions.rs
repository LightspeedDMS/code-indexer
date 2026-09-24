//! Issue #1952 (epic #1906): a Java call qualified by a type reference
//! (`Util.normalize(x)`) was silently attributed to a DIFFERENT, same-named
//! method on an unrelated class, and the TRUE edge to the qualified type
//! was dropped entirely -- a false negative, not merely over-binding.
//!
//! Root shape (mirrors jsoup's real `StringUtil.normaliseWhitespace` vs
//! `TextNode.normaliseWhitespace` repro, neutral naming per this
//! repository's Disclosure Discipline): two classes in different files and
//! different packages each declare a same-named static method --
//! `Util.normalize(String)` (no supertype evidence of its own) and
//! `Leaf.normalize(String)` (a DECOY, declared on a class whose own file
//! also has an unrelated `extends` clause). A caller in the SAME package as
//! `Leaf` (but a DIFFERENT file) calls `Util.normalize(x)`, explicitly
//! qualified. Because `Caller.java`'s own file carries an (unrelated)
//! `extends` clause, issue #1922's whole-file safety gate
//! (`receiver_type_qualifier::file_is_safe_for_type_qualifier_narrowing`)
//! correctly declines the EXCLUSIVE hard-narrow (it cannot rule out that
//! `Util` is really a shadowed inherited field) -- but the reference then
//! fell through to `narrowing::apply_import_context_narrowing`, which
//! judges reachability purely by SAME_FILE/SAME_PACKAGE/import evidence
//! relative to the CALLING file. `Leaf.normalize` coincidentally shares
//! `Caller`'s own package; `Util.normalize` does not -- so the old code
//! narrowed to `Leaf.normalize` ALONE, deleting the true, explicitly
//! qualified edge to `Util.normalize` and, for a qualified call written
//! INSIDE `Leaf.normalize` itself, produced a phantom self-edge.
//!
//! Governing doctrine (restated in the issue): missing/negative evidence
//! must NEVER delete a candidate -- over-binding is the safe direction.
//! So this file does NOT assert that `Leaf.normalize` excludes the
//! qualified callers (it may legitimately still list them, over-bound);
//! it asserts only that the TRUE, qualified edge to `Util.normalize` is
//! never dropped, and that `QUALIFIED_NAME` (reasons bit 7, previously
//! dead -- 0 of 14,674 edges in the real jsoup repro) can be set on a
//! genuinely, safely qualifier-confirmed edge.

mod common;

use common::{build_graph_over, declaration_symbol_owned_by, extract_index, write_source};
use xray_core::graph::reasons::QUALIFIED_NAME;

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
/// #1922's whole-file safety gate, exactly like jsoup's real
/// `TextNode extends LeafNode`). One qualified call site sits INSIDE
/// `Leaf.normalize` itself (the phantom-self-edge shape, mirroring jsoup's
/// `TextNode.java:109`); a second sits in a different method of the same
/// file (mirrors `TextNode.java:33`).
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
/// `extends` clause also trips the whole-file safety gate -- mirrors
/// jsoup's real `Document.java:201` call site.
const CALLER_SOURCE: &str = r#"package com.example.core;

import com.example.util.Util;

public class Caller extends SomeBase {
    String run(String x) {
        return Util.normalize(x);
    }
}
"#;

/// The UNQUALIFIED call site, via a single-member static import -- per
/// `resolve.rs::import_reasons`'s own documented NAME-ONLY (not
/// package-resolved) `STATIC_IMPORT` classification, this legitimately
/// tags BOTH `Util.normalize` and `Leaf.normalize` with the identical
/// evidence bit, so both stay reachable. That is accepted over-binding,
/// not a complaint (mirrors the issue's own "the three unqualified sites
/// resolve to both declarations ... not a complaint" note) -- included
/// for shape fidelity only, no assertion depends on it.
const OTHER_SOURCE: &str = r#"package com.example.core;

import static com.example.util.Util.normalize;

public class Other {
    String run(String x) {
        return normalize(x);
    }
}
"#;

/// A call site whose OWN file carries NO supertype evidence at all --
/// #1922's whole-file safety gate is satisfied here, so the exclusive
/// hard-narrow safely fires and this reference resolves ONLY to
/// `Util.normalize`. Proves `QUALIFIED_NAME` can be set at all (the issue's
/// own census found it dead -- 0 of 14,674 edges).
const STANDALONE_SOURCE: &str = r#"package com.example.core;

import com.example.util.Util;

public class Standalone {
    String run(String x) {
        return Util.normalize(x);
    }
}
"#;

const UTIL_PATH: &str = "com/example/util/Util.java";
const SOME_BASE_PATH: &str = "com/example/core/SomeBase.java";
const LEAF_PATH: &str = "com/example/core/Leaf.java";
const CALLER_PATH: &str = "com/example/core/Caller.java";
const OTHER_PATH: &str = "com/example/core/Other.java";
const STANDALONE_PATH: &str = "com/example/core/Standalone.java";

#[test]
fn type_qualified_call_never_drops_the_true_edge_to_the_qualified_type() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), UTIL_PATH, UTIL_SOURCE);
    write_source(dir.path(), SOME_BASE_PATH, SOME_BASE_SOURCE);
    write_source(dir.path(), LEAF_PATH, LEAF_SOURCE);
    write_source(dir.path(), CALLER_PATH, CALLER_SOURCE);
    write_source(dir.path(), OTHER_PATH, OTHER_SOURCE);
    write_source(dir.path(), STANDALONE_PATH, STANDALONE_SOURCE);

    let util_index = extract_index(dir.path(), UTIL_PATH);
    let leaf_index = extract_index(dir.path(), LEAF_PATH);
    let caller_index = extract_index(dir.path(), CALLER_PATH);
    let standalone_index = extract_index(dir.path(), STANDALONE_PATH);

    let util_normalize = declaration_symbol_owned_by(&util_index, "normalize", "Util");
    let leaf_normalize = declaration_symbol_owned_by(&leaf_index, "normalize", "Leaf");
    let leaf_render = declaration_symbol_owned_by(&leaf_index, "render", "Leaf");
    let caller_run = declaration_symbol_owned_by(&caller_index, "run", "Caller");
    let standalone_run = declaration_symbol_owned_by(&standalone_index, "run", "Standalone");

    let graph = build_graph_over(
        dir.path(),
        &[UTIL_PATH, SOME_BASE_PATH, LEAF_PATH, CALLER_PATH, OTHER_PATH, STANDALONE_PATH],
    );

    let util_dense = graph.dense_id_for(util_normalize).expect("Util.normalize must be interned");
    let leaf_normalize_dense = graph.dense_id_for(leaf_normalize).expect("Leaf.normalize must be interned");
    let leaf_render_dense = graph.dense_id_for(leaf_render).expect("Leaf.render must be interned");
    let caller_run_dense = graph.dense_id_for(caller_run).expect("Caller.run must be interned");
    let standalone_run_dense = graph.dense_id_for(standalone_run).expect("Standalone.run must be interned");

    let util_callers = graph.callers_index(util_dense);

    // AC1 (the P1's core defect): the call site is written
    // `Util.normalize(x)` -- an explicit, unambiguous type qualifier --
    // from a DIFFERENT file (`Caller.java`) than `Util` itself. This edge
    // must exist regardless of the fact that `Caller.java`'s own file
    // also happens to declare an unrelated `extends` clause that keeps
    // the exclusive hard-narrow from firing.
    assert!(
        util_callers.contains(&caller_run_dense),
        "Util.normalize must list Caller.run as a caller -- the call is explicitly qualified \
         `Util.normalize(x)`; this true edge must never be silently dropped just because an \
         unrelated same-package/same-bare-name decoy (Leaf.normalize) also exists. Got callers: \
         {util_callers:?}"
    );

    // The second reported shape: a qualified call site in a DIFFERENT
    // method of Leaf's own file (mirrors jsoup's TextNode.java:33).
    assert!(
        util_callers.contains(&leaf_render_dense),
        "Util.normalize must list Leaf.render as a caller -- the qualified call inside \
         Leaf.render must resolve to Util.normalize. Got callers: {util_callers:?}"
    );

    // The phantom-self-edge shape: a qualified call site written INSIDE
    // Leaf.normalize's OWN body (mirrors jsoup's TextNode.java:109, where
    // TextNode.normaliseWhitespace was wrongly reported as its own
    // caller). The true edge must still reach Util.normalize.
    assert!(
        util_callers.contains(&leaf_normalize_dense),
        "Util.normalize must list Leaf.normalize as a caller -- the qualified call inside \
         Leaf.normalize's own body must resolve to Util.normalize, not vanish while Leaf.normalize \
         is wrongly credited with a phantom self-edge instead. Got callers: {util_callers:?}"
    );

    // AC3: QUALIFIED_NAME (bit 7) must be capable of being set at all --
    // the issue's own census found it dead across every edge in a real
    // repo. `Standalone.java` carries no supertype evidence of its own,
    // so the exclusive hard-narrow safely fires here and the reference
    // resolves ONLY to Util.normalize -- a clean, unambiguous case for the
    // evidence bit.
    let standalone_evidence = graph
        .edge_evidence(standalone_run_dense, util_dense)
        .expect("Standalone.run -> Util.normalize edge must exist");
    assert_ne!(
        standalone_evidence & QUALIFIED_NAME,
        0,
        "QUALIFIED_NAME must be set on an edge produced by a type-qualified call site that the \
         binder was able to safely, exclusively confirm -- it is currently never set at all"
    );
}
