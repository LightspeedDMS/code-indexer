//! Story #1792 (S3): the refine phase -- a per-file second AST pass that
//! runs ONLY over the file subset `analyze_graph` asks for (the RefineSet,
//! `GraphResult.refine`), narrowed to the files the driver regex actually
//! matched (AC2). See ADR-001/ADR-002: `refine` is the OPTIONAL third
//! graph-mode callback, receiving the SAME opaque `GraphHandle`/
//! `FactsHandle` accessor ABI `analyze_graph` already uses, plus a small,
//! stable per-file `FileContext` -- unlike `CodeGraph`/`FactIndex`,
//! `FileContext` is simple and non-evolving enough to mirror directly into
//! the evaluator PREAMBLE rather than going through an opaque handle.
//!
//! This module holds the host-side `FileContext` type, the pure AC2
//! narrowing logic (`refine_set_file_ids`/`narrow_refine_set_to_driver_
//! matched`), and the map-shaped per-file execution driver
//! (`run_refine_over_files`) that calls `GraphDynlibEvaluator::call_refine`
//! for the narrowed file set.

use crate::graph::identity::SymbolId;
use std::collections::BTreeSet;

/// AC1: the per-file host context a `refine` callback receives alongside
/// the file's `OwnedNode` and the whole-graph handles. Mirrored structurally
/// into `compiler::GRAPH_PREAMBLE_EXTRA_*` and guarded by
/// `preamble_ac18_parity.rs`, exactly like `OwnedNode`/`EvalFinding`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileContext {
    pub file: String,
}

/// AC2: the set of file ids referenced by any symbol in `refine_symbols`
/// (`GraphResult.refine`, the RefineSet `analyze_graph` returned).
/// `SymbolId = (file_id << 32) | local_index` (`identity::make_symbol_id`),
/// so this is a pure bit-shift over each symbol -- no graph lookup needed,
/// and terminates in exactly `refine_symbols.len()` iterations (Rule 14).
pub fn refine_set_file_ids(refine_symbols: &[SymbolId]) -> BTreeSet<u32> {
    refine_symbols.iter().map(|&symbol| (symbol >> 32) as u32).collect()
}

/// AC2: "RefineSet is intersected with the driver-regex match set." Only
/// files the driver ALREADY selected for reporting are ever handed to the
/// refine phase, even when `analyze_graph` flagged a symbol in a file the
/// driver never matched -- mirrors `repo_index::references_in_matched_files`'s
/// finding-scope-vs-indexing-scope split, extended to the refine phase.
/// Bounded: `refine_set_file_ids` is O(refine_symbols.len()), the filter is
/// O(the resulting file id count) (Rule 14).
pub fn narrow_refine_set_to_driver_matched(
    refine_symbols: &[SymbolId],
    driver_matched_file_ids: &BTreeSet<u32>,
) -> BTreeSet<u32> {
    refine_set_file_ids(refine_symbols)
        .into_iter()
        .filter(|file_id| driver_matched_file_ids.contains(file_id))
        .collect()
}

/// AC1/AC5: the outcome of running `refine` against exactly one file.
/// Every terminal state is a DISTINCT, explicit variant (Rule 13,
/// anti-silent-failure) -- a parse failure or a caught panic is never
/// silently mapped onto an empty-but-successful `Ran`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefineFileStatus {
    /// `refine` ran to completion (possibly with zero findings).
    Ran,
    /// `refine` panicked; the panic was caught INSIDE the dylib
    /// (`GRAPH_REFINE_EPILOGUE`'s `catch_unwind`) and reported explicitly.
    Panicked,
    /// The file could not be parsed at all (unsupported extension,
    /// unreadable, or a total tree-sitter failure) -- `refine` was never
    /// invoked for this file.
    ParseFailed,
    /// The loaded evaluator does not export `xray_refine` at all (a
    /// graph-mode evaluator with no `fn refine`) -- distinct from a panic.
    NotExported,
}

/// One file's `refine` outcome: its findings (already attached to `file`,
/// mirroring `scanner::Finding`'s shape) plus the explicit status.
pub struct RefineFileResult {
    pub file: String,
    pub findings: Vec<crate::finding::Finding>,
    pub status: RefineFileStatus,
}

/// AC1: runs `refine` once per entry in `files` -- the caller-narrowed
/// RefineSet-intersect-driver-matched file list (`narrow_refine_set_to_
/// driver_matched`'s output, resolved to real paths upstream of this
/// function). Builds the `GraphHandle`/`FactsHandle` pair ONCE and shares
/// it across every file, since the whole graph is read-only for the
/// duration of refine. Bounded: exactly `files.len()` iterations (Rule 14)
/// -- this is also what makes AC2's "does not re-parse files outside the
/// RefineSet" a provable property: only entries in `files` are ever
/// touched, and `run_refine_one_file` parses each at most once.
pub fn run_refine_over_files(
    files: &[(std::path::PathBuf, String)],
    graph: &crate::graph::csr::CodeGraph,
    facts: &crate::graph::user_facts::FactIndex,
    evaluator: &crate::dynlib::GraphDynlibEvaluator,
) -> Vec<RefineFileResult> {
    let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(graph);
    let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(facts);
    files
        .iter()
        .map(|(abs_path, repo_relative_path)| {
            run_refine_one_file(abs_path, repo_relative_path, &graph_handle, &facts_handle, evaluator)
        })
        .collect()
}

/// Parses ONE file (via `scanner::parse_file`, the SAME primitive every
/// other scan path shares -- Rule 4, anti-duplication) and calls `refine`
/// on it, mapping `GraphDynlibEvaluator::call_refine`'s two-level `Option`
/// onto the explicit `RefineFileStatus`.
fn run_refine_one_file(
    abs_path: &std::path::Path,
    repo_relative_path: &str,
    graph_handle: &crate::graph::csr::handle::GraphHandle,
    facts_handle: &crate::graph::user_facts::FactsHandle,
    evaluator: &crate::dynlib::GraphDynlibEvaluator,
) -> RefineFileResult {
    let root = match crate::scanner::parse_file(abs_path) {
        Some(root) => root,
        None => {
            return RefineFileResult {
                file: repo_relative_path.to_string(),
                findings: Vec::new(),
                status: RefineFileStatus::ParseFailed,
            };
        }
    };
    let ctx = FileContext { file: repo_relative_path.to_string() };
    let (findings, status) = match evaluator.call_refine(&root, &ctx, graph_handle, facts_handle) {
        None => (Vec::new(), RefineFileStatus::NotExported),
        Some(None) => (Vec::new(), RefineFileStatus::Panicked),
        Some(Some(eval_findings)) => {
            let mapped = eval_findings
                .into_iter()
                .map(|f| crate::finding::Finding {
                    pattern: f.pattern,
                    file: repo_relative_path.to_string(),
                    line: f.line,
                    snippet: f.snippet,
                })
                .collect();
            (mapped, RefineFileStatus::Ran)
        }
    };
    RefineFileResult { file: repo_relative_path.to_string(), findings, status }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::identity::make_symbol_id;

    #[test]
    fn refine_set_file_ids_extracts_the_high_32_bits_of_each_symbol() {
        let symbols = vec![make_symbol_id(1, 0), make_symbol_id(1, 5), make_symbol_id(2, 0), make_symbol_id(3, 9)];
        let file_ids = refine_set_file_ids(&symbols);
        assert_eq!(file_ids, BTreeSet::from([1, 2, 3]), "file 1 appears twice via two symbols but must dedup to one id");
    }

    #[test]
    fn refine_set_file_ids_is_empty_for_an_empty_refine_set() {
        assert!(refine_set_file_ids(&[]).is_empty());
    }

    /// AC2's central discriminating requirement: a symbol `analyze_graph`
    /// flagged in a file the driver never matched must NOT survive the
    /// narrowing step -- proving the intersection, not a union or a bare
    /// pass-through of the RefineSet.
    #[test]
    fn narrow_refine_set_to_driver_matched_excludes_files_the_driver_never_matched() {
        let refine_symbols = vec![
            make_symbol_id(10, 0), // matched
            make_symbol_id(20, 0), // matched
            make_symbol_id(30, 0), // NOT matched by the driver
        ];
        let driver_matched: BTreeSet<u32> = [10, 20].into_iter().collect();

        let narrowed = narrow_refine_set_to_driver_matched(&refine_symbols, &driver_matched);

        assert_eq!(narrowed, BTreeSet::from([10, 20]), "file 30 must be excluded -- it was never driver-matched");
    }

    /// AC2's exact story language, proven at scale rather than with a
    /// toy 3-file fixture: a RefineSet naming 40 driver-matched files
    /// mixed in among 5,791 files the driver never matched must narrow
    /// down to EXACTLY those 40 -- never leaking a single unmatched file
    /// id through.
    #[test]
    fn narrow_refine_set_to_driver_matched_narrows_a_large_refine_set_to_exactly_the_matched_forty() {
        let driver_matched: BTreeSet<u32> = (0u32..40).collect();
        let unmatched_ids: BTreeSet<u32> = (40u32..(40 + 5791)).collect();

        let mut refine_symbols: Vec<SymbolId> =
            driver_matched.iter().map(|&file_id| make_symbol_id(file_id, 0)).collect();
        refine_symbols.extend(unmatched_ids.iter().map(|&file_id| make_symbol_id(file_id, 0)));

        let narrowed = narrow_refine_set_to_driver_matched(&refine_symbols, &driver_matched);

        assert_eq!(narrowed.len(), 40, "must narrow down to exactly the 40 driver-matched files");
        assert_eq!(narrowed, driver_matched);
        assert!(
            narrowed.is_disjoint(&unmatched_ids),
            "not one of the 5,791 unmatched files may survive narrowing"
        );
    }

    #[test]
    fn file_context_carries_the_repo_relative_path() {
        let ctx = FileContext { file: "src/Foo.java".to_string() };
        assert_eq!(ctx.file, "src/Foo.java");
    }

    /// Builds a real 2-file graph (A calls B, B carries a cached signature)
    /// plus a real compiled graph-mode evaluator whose `refine` records one
    /// finding naming the file it ran on -- shared fixture for the
    /// `run_refine_over_files` tests below.
    fn compiled_refine_evaluator(dir: &std::path::Path) -> crate::dynlib::GraphDynlibEvaluator {
        let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {
    vec![EvalFinding {
        pattern: "refine-visited".to_string(),
        line: node.start_line,
        snippet: ctx.file.clone(),
    }]
}
"#;
        let cr = crate::compiler::compile_evaluator(user_code, dir).expect("evaluator must compile");
        crate::dynlib::GraphDynlibEvaluator::load(&cr.so_path).expect("evaluator must load")
    }

    /// THE AC1/AC2 discriminating proof: `run_refine_over_files` must call
    /// `refine` EXACTLY ONCE per file it is given (never more, never zero
    /// for a real parseable file), with the correct `FileContext.file` for
    /// each -- and, via the REAL `scanner::PARSE_COUNT` instrumentation
    /// (never timing), must NOT parse a single file beyond the ones it was
    /// handed. This is the AC2 "does NOT re-parse the other 5,791" proof at
    /// the layer that actually parses: the narrowing (proven separately
    /// above) determines the file LIST; this proves the list is exactly
    /// what gets touched.
    #[test]
    fn run_refine_over_files_calls_refine_once_per_file_with_correct_context_and_proves_only_narrowed_files_are_parsed() {
        use crate::graph::csr::builder::CodeGraphBuilder;
        use crate::graph::csr::candidate::Candidate;
        use crate::graph::identity::make_symbol_id;
        use crate::graph::reasons;
        use crate::graph::user_facts::FactIndex;

        let dir = tempfile::tempdir().unwrap();
        let a_path = dir.path().join("A.java");
        let b_path = dir.path().join("B.java");
        std::fs::write(&a_path, "class A {}").unwrap();
        std::fs::write(&b_path, "class B {}").unwrap();

        let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
        let a = builder.intern_symbol(make_symbol_id(1, 0));
        let b = builder.intern_symbol(make_symbol_id(2, 0));
        builder.add_signature(b, "run()".to_string());
        builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::UNIQUE_NAME_IN_REPO)]);
        let graph = builder.build();
        let facts = FactIndex::new();
        let evaluator = compiled_refine_evaluator(dir.path());

        crate::scanner::reset_parse_count();
        let files = vec![
            (a_path.clone(), "A.java".to_string()),
            (b_path.clone(), "B.java".to_string()),
        ];
        let results = run_refine_over_files(&files, &graph, &facts, &evaluator);

        assert_eq!(results.len(), 2, "must produce one RefineFileResult per input file");
        assert_eq!(crate::scanner::parse_count(), 2, "must parse EXACTLY the files it was handed, never more");

        for (result, expected_file) in results.iter().zip(["A.java", "B.java"]) {
            assert_eq!(result.status, RefineFileStatus::Ran);
            assert_eq!(result.findings.len(), 1);
            assert_eq!(result.findings[0].file, expected_file);
            assert_eq!(
                result.findings[0].snippet, expected_file,
                "the real refine callback must have received the correct FileContext.file"
            );
        }
    }

    /// Per-file containment: a file that cannot be parsed (nonexistent
    /// path) must be reported as `ParseFailed` for THAT file alone, never
    /// abort the whole batch -- a sibling, genuinely parseable file in the
    /// SAME call must still be processed and report `Ran`.
    #[test]
    fn run_refine_over_files_reports_parse_failed_for_an_unreadable_file_without_halting_the_rest() {
        use crate::graph::csr::builder::CodeGraphBuilder;
        use crate::graph::identity::make_symbol_id;
        use crate::graph::user_facts::FactIndex;

        let dir = tempfile::tempdir().unwrap();
        let good_path = dir.path().join("Good.java");
        std::fs::write(&good_path, "class Good {}").unwrap();
        let missing_path = dir.path().join("DoesNotExist.java");

        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        builder.intern_symbol(make_symbol_id(1, 0));
        let graph = builder.build();
        let facts = FactIndex::new();
        let evaluator = compiled_refine_evaluator(dir.path());

        let files = vec![
            (missing_path, "DoesNotExist.java".to_string()),
            (good_path, "Good.java".to_string()),
        ];
        let results = run_refine_over_files(&files, &graph, &facts, &evaluator);

        assert_eq!(results.len(), 2);
        assert_eq!(results[0].status, RefineFileStatus::ParseFailed);
        assert!(results[0].findings.is_empty());
        assert_eq!(results[1].status, RefineFileStatus::Ran, "a sibling file's parse failure must not halt the batch");
        assert_eq!(results[1].findings.len(), 1);
    }

    /// Builds a real 13-symbol, 12-hop chain fixture for the AC3 test
    /// below: each hop is its OWN real file written to `dir` (so
    /// `identity::file_id` produces REAL, path-derived ids -- never a
    /// literal 1..=13 stand-in), each symbol carries a cached signature,
    /// and `file_id_lookup` maps each real file id back to its
    /// `(abs_path, repo_relative_path)` -- the same shape a production
    /// caller resolves a narrowed RefineSet id through.
    fn build_twelve_hop_chain_fixture(
        dir: &std::path::Path,
    ) -> (crate::graph::csr::CodeGraph, crate::graph::user_facts::FactIndex, std::collections::HashMap<u32, (std::path::PathBuf, String)>)
    {
        use crate::graph::csr::builder::CodeGraphBuilder;
        use crate::graph::csr::candidate::Candidate;
        use crate::graph::identity::make_symbol_id;
        use crate::graph::reasons;
        use crate::graph::user_facts::FactIndex;
        use std::collections::HashMap;

        const HOP_COUNT: u32 = 12;
        const NODE_COUNT: u32 = HOP_COUNT + 1;

        let file_paths: Vec<(u32, std::path::PathBuf, String)> = (0..NODE_COUNT)
            .map(|i| {
                let relative = format!("Hop{i}.java");
                let abs = dir.join(&relative);
                std::fs::write(&abs, format!("class Hop{i} {{}}")).unwrap();
                (crate::graph::identity::file_id(&relative), abs, relative)
            })
            .collect();
        let file_id_lookup: HashMap<u32, (std::path::PathBuf, String)> =
            file_paths.iter().map(|(id, abs, rel)| (*id, (abs.clone(), rel.clone()))).collect();

        let mut builder = CodeGraphBuilder::with_candidate_capacity(HOP_COUNT as usize);
        let nodes: Vec<u32> = file_paths
            .iter()
            .map(|(file_id, _, _)| {
                let dense = builder.intern_symbol(make_symbol_id(*file_id, 0));
                builder.add_signature(dense, format!("hop{file_id}()"));
                dense
            })
            .collect();
        for i in 0..HOP_COUNT as usize {
            builder.add_reference(
                nodes[i],
                file_paths[i].0,
                1,
                0,
                &[Candidate::new(nodes[i + 1], reasons::UNIQUE_NAME_IN_REPO)],
            );
        }
        (builder.build(), FactIndex::new(), file_id_lookup)
    }

    /// THE AC3 discriminating proof, fusing AC2/AC3/AC4 end-to-end through
    /// the real engine: "an endpoint->sink query with a 12-hop path
    /// produces a complete finding with zero refine invocations." A real
    /// compiled `analyze_graph` walks `shortest_path_to_any` and captions
    /// every hop via `signature_for` (via `.expect`, so a missing
    /// resolution/signature fails LOUD rather than silently defaulting) --
    /// NEVER touching `result.refine`. The resulting empty RefineSet is
    /// narrowed against a driver-matched set naming ALL 13 real files, the
    /// narrowed ids are resolved back to real paths via the production
    /// lookup, and THAT derived list is handed to `run_refine_over_files`.
    /// `scanner::PARSE_COUNT` proves none of the 13 real files was parsed.
    #[test]
    fn a_twelve_hop_path_finding_is_complete_with_zero_refine_invocations() {
        let dir = tempfile::tempdir().unwrap();
        let (graph, facts, file_id_lookup) = build_twelve_hop_chain_fixture(dir.path());
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let path = g.shortest_path_to_any(0, &[12], 20).expect("a 12-hop chain must have a path from 0 to 12");
    let mut involved = Vec::new();
    let mut signatures = Vec::new();
    for dense in &path {
        involved.push(g.resolve_symbol(*dense).expect("every hop's dense id must resolve"));
        signatures.push(g.signature_for(*dense).expect("every hop has a cached signature").to_string());
    }
    result.findings.push(ReduceFinding {
        pattern: "endpoint-to-sink".to_string(),
        message: "found a 12-hop path".to_string(),
        involved,
        signatures,
    });
    result
}
"#;
        let cr = crate::compiler::compile_evaluator(user_code, dir.path()).expect("must compile");
        let evaluator = crate::dynlib::GraphDynlibEvaluator::load(&cr.so_path).expect("must load");

        let result = evaluator
            .call_analyze_graph(&graph_handle, &facts_handle)
            .expect("analyze_graph IS exported")
            .expect("must not panic");

        assert_eq!(result.findings[0].involved.len(), 13, "a 12-hop path visits 13 nodes");
        assert!(result.refine.is_empty(), "a path-shaped finding must never populate the RefineSet");

        let driver_matched: BTreeSet<u32> = file_id_lookup.keys().copied().collect();
        let narrowed = narrow_refine_set_to_driver_matched(&result.refine, &driver_matched);
        let files_to_refine: Vec<(std::path::PathBuf, String)> =
            narrowed.into_iter().map(|id| file_id_lookup[&id].clone()).collect();
        assert!(files_to_refine.is_empty(), "deriving from an empty narrowed set must yield an empty file list");

        crate::scanner::reset_parse_count();
        let refine_results = run_refine_over_files(&files_to_refine, &graph, &facts, &evaluator);
        assert!(refine_results.is_empty());
        assert_eq!(
            crate::scanner::parse_count(),
            0,
            "zero refine invocations means zero parses -- none of the 13 REAL on-disk files was ever touched"
        );
    }
}
