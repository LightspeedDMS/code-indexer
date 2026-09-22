//! Shared integration-test support: the `NoOpCollector`/source-writing/
//! extraction/graph-building/lookup helpers the `bug_1922_*` real-
//! extraction integration test files all needed were each duplicating a
//! near-identical copy -- extracted here once, per Rule 4. Standard Rust
//! convention: `tests/common/mod.rs` (a directory, not a bare
//! `tests/common.rs`) is NOT itself compiled as a separate test binary;
//! each consuming file declares `mod common;` and pulls in what it needs.
//!
//! `#[allow(dead_code)]` module-wide: each consuming test file only ever
//! uses a SUBSET of these helpers, and Rust's per-binary dead-code lint
//! would otherwise warn once for every unused one in every binary that
//! includes this module (this crate's `cargo clippy -D warnings` gate has
//! zero tolerance for that noise).

#![allow(dead_code)]

use std::collections::HashSet;
use std::path::Path;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::identity::SymbolId;
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, UserFact};
use xray_core::owned_node::OwnedNode;

pub struct NoOpCollector;

impl FactCollector for NoOpCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        Vec::new()
    }
}

pub fn write_source(dir: &Path, relative_path: &str, source: &str) {
    let full = dir.join(relative_path);
    if let Some(parent) = full.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(full, source).unwrap();
}

pub fn extract_index(dir: &Path, relative_path: &str) -> LocalIndex {
    let full_path = dir.join(relative_path);
    let result = xray_core::graph::fused::process_file_fused(&full_path, relative_path, &NoOpCollector)
        .unwrap_or_else(|| panic!("fixture bug: {relative_path} must parse"));
    result
        .index
        .unwrap_or_else(|| panic!("fixture bug: {relative_path} must extract (language must be supported)"))
}

/// Finds a declaration by bare name AND its `MethodOwnerRecord`-recorded
/// enclosing type -- disambiguates same-named declarations on different
/// types within one file (e.g. two classes each declaring their own
/// `m()`).
pub fn declaration_symbol_owned_by(index: &LocalIndex, name: &str, enclosing_type: &str) -> SymbolId {
    let owner_symbols: HashSet<SymbolId> = index
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

/// Finds a declaration by bare name alone -- for fixtures with only ONE
/// declaration of that name in the file (no ambiguity to resolve via
/// `declaration_symbol_owned_by`'s owner filter).
pub fn declaration_symbol(index: &LocalIndex, name: &str) -> SymbolId {
    index
        .declarations
        .iter()
        .find(|d| d.name == name)
        .unwrap_or_else(|| panic!("fixture bug: no {name:?} declaration in this file"))
        .symbol
}

pub fn build_graph_over(dir: &Path, relative_paths: &[&str]) -> CodeGraph {
    let options = RepoIndexOptions {
        budget: IndexBudget::unlimited(),
        max_files: None,
    };
    let paths: Vec<String> = relative_paths.iter().map(|p| p.to_string()).collect();
    let result = build_repo_graph(dir, &paths, &options, &NoOpCollector)
        .expect("no file_id collision in this fixture");
    result.graph
}

pub fn dead_and_caller_count(graph: &CodeGraph, symbol: SymbolId) -> (Option<bool>, usize) {
    let dense = graph
        .dense_id_for(symbol)
        .expect("symbol must be interned in the bound graph");
    (graph.is_definitely_dead_code(dense), graph.callers_index(dense).len())
}
