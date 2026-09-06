//! AC7: the analyze-child outcome types -- `AnalyzeStatus`, `GraphResult`,
//! `ReduceFinding`. These are the parent<->child JSON protocol AND the
//! caller-facing outcome of `run_analyze_child` (see `super::process`):
//! every terminal state a `--analyze-graph` invocation can end in is a
//! DISTINCT named variant, never inferred from an empty findings list or a
//! bare process exit code.

use crate::graph::identity::SymbolId;
use serde::{Deserialize, Serialize};

/// AC7's eight-way terminal status, EXACTLY as named in the story text.
/// `#[serde(rename_all = "snake_case")]` is what makes the enum variant
/// names themselves (not a hand-maintained parallel string table) the
/// single source of truth for the wire format the child process writes
/// and the parent reads back.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AnalyzeStatus {
    /// The caller never asked for graph-mode analysis at all (e.g. a
    /// legacy-only scan, or no dylib configured).
    NotRequested,
    /// Graph mode was requested but the loaded artifact does not export
    /// the graph callback family at all -- distinct from `LoadFailed`,
    /// which means the artifact WAS supposed to be graph-mode but failed
    /// to load/verify.
    Absent,
    /// The analyze child failed to load/verify the compiled evaluator
    /// (ABI mismatch, missing required symbol, dlopen failure).
    LoadFailed,
    /// A memory/index budget gate refused to run analyze_graph at all.
    SkippedBudget,
    /// `analyze_graph` ran to completion and returned a `GraphResult`.
    RanOk,
    /// `analyze_graph` panicked; the panic was caught (`catch_unwind`) and
    /// reported explicitly -- never silently mapped onto an empty `RanOk`.
    Panicked,
    /// The analyze child exceeded its wall-clock budget and was killed.
    TimedOut,
    /// The mmap'd `--graph-in` file failed structural validation before
    /// `analyze_graph` was ever called.
    GraphInvalid,
}

/// One path-shaped finding, produced ENTIRELY inside `analyze_graph`
/// (AC7): "carrying each hop's cached signature line, and never enter the
/// RefineSet." `involved` is the ordered chain of `SymbolId`s the path
/// walks through; `signatures[i]` is `involved[i]`'s cached AC2 signature
/// line (parallel arrays, same length) so a caller can render the path
/// without a second graph lookup.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReduceFinding {
    pub pattern: String,
    pub message: String,
    pub involved: Vec<SymbolId>,
    pub signatures: Vec<String>,
}

/// `analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult` (AC7).
/// `refine` is the RefineSet handoff to S3's per-file refine pass (out of
/// scope here) -- the symbols `analyze_graph` flagged as needing a
/// follow-up per-file look, carried as plain `SymbolId`s since S3 owns
/// what it does with them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct GraphResult {
    pub findings: Vec<ReduceFinding>,
    pub refine: Vec<SymbolId>,
}

#[cfg(test)]
mod tests {
    /// AC7: "Status enum extending #1785's:
    /// not_requested | absent | load_failed | skipped_budget | ran_ok |
    /// panicked | timed_out | graph_invalid. Every one must be DISTINCT
    /// and reported explicitly -- never inferred from an empty result."
    /// This test proves the enum exists with exactly these eight variants,
    /// and that each serializes to its own distinct snake_case JSON token
    /// (the actual wire format the child process writes and the parent
    /// reads) -- a wrong implementation that collapsed two states onto
    /// the same serialized string (e.g. `panicked` and `timed_out` both
    /// serializing as `"failed"`) would fail this even though the Rust
    /// enum itself has 8 variants.
    #[test]
    fn analyze_status_serializes_to_the_exact_eight_named_states() {
        use super::AnalyzeStatus;

        let cases = [
            (AnalyzeStatus::NotRequested, "\"not_requested\""),
            (AnalyzeStatus::Absent, "\"absent\""),
            (AnalyzeStatus::LoadFailed, "\"load_failed\""),
            (AnalyzeStatus::SkippedBudget, "\"skipped_budget\""),
            (AnalyzeStatus::RanOk, "\"ran_ok\""),
            (AnalyzeStatus::Panicked, "\"panicked\""),
            (AnalyzeStatus::TimedOut, "\"timed_out\""),
            (AnalyzeStatus::GraphInvalid, "\"graph_invalid\""),
        ];

        let mut serialized: Vec<String> = Vec::new();
        for (status, expected_json) in cases {
            let json = serde_json::to_string(&status).expect("AnalyzeStatus must serialize");
            assert_eq!(json, expected_json, "status {status:?} must serialize as {expected_json}");
            serialized.push(json);
        }

        let distinct: std::collections::HashSet<&String> = serialized.iter().collect();
        assert_eq!(distinct.len(), 8, "all eight statuses must serialize to DISTINCT strings");
    }

    /// AC7: "Path-shaped findings are produced ENTIRELY in analyze_graph
    /// as ReduceFinding { involved: Vec<SymbolId>, .. } carrying each
    /// hop's cached signature line". Proves the JSON round trip the
    /// parent<->child protocol depends on preserves `involved` and
    /// `signatures` as PARALLEL arrays, in order -- not just that SOME
    /// serialization succeeds.
    #[test]
    fn graph_result_round_trips_through_json_including_reduce_finding_fields() {
        use super::{GraphResult, ReduceFinding};

        let result = GraphResult {
            findings: vec![ReduceFinding {
                pattern: "unwired-endpoint".to_string(),
                message: "route registered but handler never called".to_string(),
                involved: vec![0x1_0000_0000, 0x1_0000_0001, 0x2_0000_0000],
                signatures: vec!["route()".to_string(), "dispatch()".to_string(), "handle()".to_string()],
            }],
            refine: vec![0x2_0000_0000],
        };

        let json = serde_json::to_string(&result).expect("GraphResult must serialize");
        let round_tripped: GraphResult = serde_json::from_str(&json).expect("GraphResult must deserialize");

        assert_eq!(round_tripped, result);
        assert_eq!(round_tripped.findings[0].involved.len(), round_tripped.findings[0].signatures.len());
    }
}
