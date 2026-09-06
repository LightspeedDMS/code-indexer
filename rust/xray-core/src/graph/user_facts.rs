//! The fact-collection SEAM the fused pipeline calls into (Story #1787, S2,
//! AC2). `collect_facts` here is a plain Rust trait method, not yet a
//! dylib-loaded ABI export -- AC8 (out of scope for this slice) is what
//! will later wire a real user-authored callback into this exact seam,
//! mirroring how `crate::scanner::Evaluator` is a plain trait later backed
//! by `crate::dynlib::DynlibEvaluator`.
//!
//! This file defines the trait itself. `crate::graph::fused` (next module
//! added in this slice) is what actually PROVES the sequencing contract
//! AC2 mandates: `collect_facts` invoked EXACTLY ONCE per file, AFTER
//! extraction has fully populated the `LocalIndex`, on the SAME parsed
//! tree extraction just walked -- never interleaved with extraction, never
//! on a partial index. The end-to-end discriminating tests for that
//! contract live in `tests/ac2_fused_extract_collect.rs` against
//! `crate::graph::fused::process_file_fused`, not here.

use crate::graph::extract::local_index::LocalIndex;
use crate::owned_node::OwnedNode;

/// One fact produced by a `FactCollector` for one file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserFact {
    pub kind: String,
    pub line: usize,
    pub message: String,
}

/// Mirrors `crate::scanner::Evaluator`'s shape (`Send + Sync`, called
/// across rayon threads with no per-thread cloning). Implementors are
/// invoked by `crate::graph::fused` exactly once per file -- never
/// per-node -- with the file's WHOLE completed `LocalIndex` already built.
pub trait FactCollector: Send + Sync {
    fn collect_facts(&self, root: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact>;
}

#[cfg(test)]
mod tests {
    use super::*;

    struct CountingCollector;

    impl FactCollector for CountingCollector {
        fn collect_facts(&self, _root: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> {
            vec![UserFact {
                kind: "declaration_count".to_string(),
                line: 1,
                message: format!("{file}:{}", index.declarations.len()),
            }]
        }
    }

    #[test]
    fn a_fact_collector_receives_the_file_name_and_index_it_was_called_with() {
        let root = OwnedNode::new_leaf_for_test("program", "", 1, true);
        let index = LocalIndex::new();
        let facts = CountingCollector.collect_facts(&root, "Foo.java", &index);
        assert_eq!(facts.len(), 1);
        assert_eq!(facts[0].message, "Foo.java:0");
    }
}
