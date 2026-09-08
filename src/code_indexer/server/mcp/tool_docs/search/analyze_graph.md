---
name: analyze_graph
category: search
required_permission: query_repos
tl_dr: "Multi-file, whole-repository graph analysis -- builds a real CSR reference graph across every file in the repo, then runs your Rust evaluator's fn analyze_graph over the whole graph. Use for cross-file questions single-file AST search (xray_search) cannot answer: dead code, unreachable/unwired components, layering violations, endpoint-to-sink reachability, blast radius."
slim_description: "Whole-repository graph-mode code analysis: builds a real cross-file reference graph, then runs your Rust evaluator's fn collect_facts (per-file) and fn analyze_graph (whole-graph reduce) against it. Answers cross-file questions xray_search cannot: dead code, unwired components, layering violations, endpoint-to-sink reachability, blast radius. Surfaces AnalysisCompleteness honestly via fact_graph_complete and degradation counters."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: 'Repository identifier to analyze. Use list_global_repos to see available repositories.'
    evaluator_code:
      type: string
      description: 'Rust code defining TWO REQUIRED functions: fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> (per-file -- collects auxiliary evidence such as annotations or config keys) and fn analyze_graph(g: &GraphHandle<''_>, facts: &FactsHandle<''_>) -> GraphResult (whole-graph reduce). Both must be defined together -- a graph-mode evaluator defining only one of the two is rejected at compile time (neither function is optional). ADR-001 fixes execution modes at exactly two: legacy single-file (fn evaluate_node, used by xray_search) and graph mode (fn collect_facts + fn analyze_graph, used here) -- never mix the two in one evaluator. The same Rust security whitelist as xray_search applies (no unsafe, no std::fs/net/process/env/io, no raw pointers, no extern blocks, no forbidden macros). LANGUAGE SUPPORT: the graph extractor currently implements JAVA ONLY -- every other language (Python, TypeScript, Go, etc.) has no extractor yet and is counted via the files_with_unsupported_language degradation counter (see fact_graph_complete below); a repo with any non-Java candidate files will never report fact_graph_complete=true.'
    include_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to include in the graph (e.g. ["*.java", "*.kt"]). Empty list means include all files in the repository (the WHOLE repo is indexed into the graph regardless of these patterns -- see "Indexing scope vs finding scope" below).'
      default: []
    exclude_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to exclude from the graph (e.g. ["*/test/*", "*/vendor/*"]). Empty list means exclude none.'
      default: []
    timeout_seconds:
      type: integer
      description: 'Wall-clock timeout in seconds for the WHOLE pipeline (repo-alias resolution, file collection, evaluator compile, --build-graph, --analyze-graph). Range 10..600. Default 120.'
      minimum: 10
      maximum: 600
      default: 120
    await_seconds:
      type: number
      description: 'Reserved for future async job-polling parity with xray_search. Currently accepted but INERT: analyze_graph always runs synchronously to completion or until timeout_seconds -- it never returns a bare {job_id}. Do not rely on this parameter changing behavior yet.'
      minimum: 0
      maximum: 45.0
      default: 0
  required:
    - repository_alias
    - evaluator_code
outputSchema:
  type: object
  properties:
    ok:
      type: boolean
      description: 'True only when analyze_graph ran to completion (status="ran_ok"). False for every error path (validation, missing repo, build failure, timeout, internal error) -- check `error` for the reason.'
    error:
      type: object
      description: 'Present iff ok=false. Shape: {error_type, error_message} for pipeline-level failures (ValidationError, BinaryNotFound, CompileError, GraphBuildError, XRayCliError, Timeout, InternalError), or a synchronous rejection shape {error, message} for input-validation failures (auth_required, evaluator_code_required, repository_alias_required, include_patterns_invalid, exclude_patterns_invalid, timeout_seconds_invalid, xray_evaluator_validation_failed, repository_not_found, no_candidate_files).'
    status:
      type: string
      description: 'The real --analyze-graph ChildReport status: "ran_ok" (your analyze_graph executed), "absent" (evaluator does not export analyze_graph -- should not happen given evaluator_code validation), "load_failed" (dylib failed to load), "graph_invalid" (the built graph file was corrupt), "panicked" (your analyze_graph panicked -- caught, never crashes the server).'
    findings:
      type: array
      description: 'Your analyze_graph function''s GraphResult.findings -- a list of ReduceFinding {pattern, message, involved, signatures}. involved is the ordered chain of SymbolIds the finding''s path walks through (single element for a simple flag, multi-element for a reachability/blast-radius path). signatures[i] is involved[i]''s cached signature line, parallel to involved.'
      items:
        type: object
    refine:
      type: array
      description: 'SymbolIds your analyze_graph flagged via GraphResult.refine for a follow-up per-file look (S3''s refine phase). Currently informational only -- this tool does not yet invoke --refine automatically.'
      items:
        type: integer
    fact_graph_complete:
      type: boolean
      description: 'THE honesty signal this tool exists to provide. True only when the graph build hit NO degradation (no truncation, no parse errors, no extractor/collector panics, no read errors, no index-budget trip, no unsupported-language files). False means the graph is INCOMPLETE -- an empty findings[] in that case means "the index was too incomplete to trust a negative", NOT "nothing was found". Always check this before treating an empty findings[] as a clean bill of health, especially for dead-code-style analyses (see "Directional asymmetry" below). NOTE: since the graph extractor currently supports JAVA ONLY, this will be false for any repo containing non-Java candidate files -- see files_with_unsupported_language under degradation.'
    build_status:
      type: string
      description: '"ok" on a successful build, or the specific failure: "repo_root_invalid", "load_failed", "file_id_collision", "graph_write_failed", "facts_write_failed". Present even on some overall failures so a caller can distinguish a build-time problem from an analyze-time one.'
    degradation:
      type: object
      description: 'The 7 real degradation counters from the build, verbatim -- never masked with a default. Keys: files_with_parse_errors, unreadable_or_unsupported_files, files_with_read_errors, files_with_extractor_panics, files_with_collector_panics, files_with_unsupported_language, truncated_by_max_files. files_with_unsupported_language counts files with a RECOGNIZED source-language extension for which the graph engine has no extractor yet (currently every language except Java) -- distinct from unreadable_or_unsupported_files (a genuinely unsupported/no extension). Any missing/None value under a "ok" build_status indicates malformed data, not a clean build.'
    cached:
      type: boolean
      description: 'True when the evaluator .so was served from the compile cache instead of freshly compiled.'
    compile_ms:
      type: integer
      description: 'Milliseconds spent compiling the evaluator (0 on a cache hit).'
---

Whole-repository, multi-file graph analysis. `xray_search` inspects one file's AST at a time -- it structurally cannot answer "is this method called from anywhere in the OTHER files of this repo?" `analyze_graph` builds a REAL cross-file reference graph (CSR arena, receiver-type resolution, inheritance-family expansion) spanning every indexed file, then runs your evaluator's `fn analyze_graph` as a single whole-graph reduce over it.

**Language support: JAVA ONLY.** The graph extractor that populates each file's declarations currently implements exactly one language, Java. Every other engine-supported language (Python, TypeScript, JavaScript, Go, C#, Kotlin, etc.) has no graph extractor yet -- files in those languages parse fine but contribute zero declarations to the graph, and are counted via the `files_with_unsupported_language` degradation counter. A repository containing any non-Java candidate file will never report `fact_graph_complete: true`. Scope `include_patterns` to `["*.java"]` (or restrict the repo) to get a genuinely complete graph today.

## Quick Start

Find symbols with no reference anywhere in the repository (dead code):

```json
{
  "repository_alias": "backend-global",
  "evaluator_code": "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n    Vec::new()\n}\nfn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {\n    let mut result = GraphResult::default();\n    let mut i: u32 = 0;\n    while i < 100000 {\n        match g.resolve_symbol(i) {\n            None => break,\n            Some(sym) => {\n                if g.is_definitely_dead_code(i) == Some(true) {\n                    let sig = g.signature_for(i).unwrap_or(\"\").to_string();\n                    result.findings.push(ReduceFinding {\n                        pattern: \"dead_code\".to_string(),\n                        message: sig.clone(),\n                        involved: vec![sym],\n                        signatures: vec![sig],\n                    });\n                }\n            }\n        }\n        i += 1;\n    }\n    result\n}",
  "include_patterns": ["*.java"]
}
```

## Indexing scope vs finding scope

**Indexing covers the WHOLE repository; findings are restricted only by what your evaluator chooses to report.** `include_patterns`/`exclude_patterns` narrow which files are read into the graph (all of them, by default) -- they do NOT narrow which files can appear as the *target* of a resolved reference. A reference from an included file to a symbol declared in an excluded file resolves as "external to the graph", which is a real, meaningful signal your evaluator can act on; it is never silently dropped.

## Two-function evaluator contract (ADR-001)

Unlike `xray_search`'s single `fn evaluate_node`, graph mode uses two cooperating functions:

```rust
// REQUIRED: runs once per file, BEFORE the graph is built. Collects
// auxiliary evidence (annotations, config keys, structural hashes) that
// analyze_graph can look up per-symbol via FactsHandle. Even an evaluator
// with no use for facts must still define this function (an empty body
// returning Vec::new() is fine) -- graph mode requires BOTH functions
// together; neither is optional.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

// REQUIRED: runs ONCE over the whole built graph. This is where your
// actual analysis logic lives.
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
```

ADR-001 fixes execution modes at exactly two: legacy (`fn evaluate_node`, used by `xray_search`) and graph (`fn collect_facts` + `fn analyze_graph`, used here). Do not define `fn evaluate_node` alongside these two -- a mixed-mode evaluator is rejected at compile time with a `CompileError`. Defining only ONE of `collect_facts`/`analyze_graph` is rejected the same way ("Graph mode requires BOTH collect_facts and analyze_graph -- one is missing").

### UserFact struct (collect_facts output)

```rust
pub struct UserFact {
    pub kind: String,           // e.g. "deprecated", "todo"
    pub line: usize,            // 1-based line number
    pub message: String,        // free-text detail
    pub custom_key: Option<String>,  // Some(name) for a non-symbol key (config/event topic); None to attribute to the enclosing symbol
}
```

### GraphResult / ReduceFinding (analyze_graph output)

```rust
pub struct GraphResult {
    pub findings: Vec<ReduceFinding>,
    pub refine: Vec<SymbolId>,  // symbols flagged for a future per-file follow-up look
}

pub struct ReduceFinding {
    pub pattern: String,           // your label for this finding
    pub message: String,           // free-text detail
    pub involved: Vec<SymbolId>,   // the path/chain this finding is about (1 element for a flag, N for a path)
    pub signatures: Vec<String>,   // parallel to involved -- each symbol's cached signature line
}
```

### GraphHandle reference

| Method | Signature | Description |
|--------|-----------|--------------|
| `g.callees_of(symbol)` | `(u32) -> Vec<u32>` | Dense ids this symbol calls. |
| `g.callers_of(symbol)` | `(u32) -> Vec<u32>` | Dense ids that call this symbol. |
| `g.reachable_from(roots, max_depth)` | `(&[u32], usize) -> Vec<u32>` | Every dense id reachable from `roots` within `max_depth` hops -- the primitive for blast-radius/reachability analysis. |
| `g.shortest_path_to_any(from, targets, max_depth)` | `(u32, &[u32], usize) -> Option<Vec<u32>>` | Shortest call-graph path from `from` to any of `targets` -- use this to report the PATH for a reachability finding (directional asymmetry, see below). |
| `g.strongly_connected_components()` | `() -> Vec<Vec<u32>>` | Cycle detection -- useful for layering-violation / package-cycle analysis. |
| `g.resolve_symbol(dense_id)` | `(u32) -> Option<u64>` | Dense id to real global `SymbolId`. |
| `g.resolve_string(string_id)` | `(u32) -> Option<&str>` | Interned string lookup. |
| `g.is_symbol_referenced(dense_id)` | `(u32) -> bool` | True if ANY inbound edge exists, regardless of graph completeness. |
| `g.is_definitely_dead_code(dense_id)` | `(u32) -> Option<bool>` | `Some(false)` = referenced (always safe to trust). `Some(true)` = definitely dead (ONLY reported when the graph is fully complete). `None` = unknown/suppressed (an incomplete graph must never claim "dead" with no evidence). |
| `g.signature_for(dense_id)` | `(u32) -> Option<&str>` | Cached declaration signature line, for reporting. |

### FactsHandle reference

| Method | Signature | Description |
|--------|-----------|--------------|
| `facts.for_symbol(symbol_id)` | `(u64) -> Vec<UserFact>` | Facts your `collect_facts` attributed to this symbol's enclosing declaration. |
| `facts.for_custom(name)` | `(&str) -> Vec<UserFact>` | Facts attributed to a `custom_key` (non-symbol key) instead. |

## AnalysisCompleteness: the honesty contract

The whole point of this tool is that a caller can tell "no findings" apart from "the index was too incomplete to trust a negative". **Always check `fact_graph_complete` and `degradation` before treating an empty `findings[]` as a clean result** -- especially for dead-code-style analyses, where `is_definitely_dead_code` itself returns `None` (never `Some(true)`) whenever the graph is not fully complete, so a naive evaluator that only checks `== Some(true)` will correctly report NOTHING rather than a false positive, but your CALLING code must still surface `fact_graph_complete=false` to the human reading the result rather than silently presenting an empty list as "verified clean".

```json
{
  "ok": true,
  "status": "ran_ok",
  "findings": [],
  "fact_graph_complete": false,
  "build_status": "ok",
  "degradation": {
    "files_with_parse_errors": 2,
    "unreadable_or_unsupported_files": 0,
    "files_with_read_errors": 0,
    "files_with_extractor_panics": 0,
    "files_with_collector_panics": 0,
    "files_with_unsupported_language": 0,
    "truncated_by_max_files": false
  }
}
```

The response above means "your evaluator found nothing dead, but 2 files had real parse errors -- this is NOT a verified-clean result."

## Directional asymmetry (dead-code vs reachability)

Per the epic's design: **dead-code analysis must UNDER-report (safe)** -- `is_definitely_dead_code` already enforces this by returning `None` instead of `Some(true)` under any incompleteness. **Reachability analysis must OVER-report (unsafe in the other direction)** -- when your evaluator reports that endpoint X can reach sink Y, it must ship the PATH (`involved`) and identify the weakest link's confidence, since a caller relying on a reachability claim needs to audit exactly how strong that claim is rather than trusting a bare boolean.

```rust
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let endpoint_dense_id = 0u32; // resolve your real endpoint's dense id
    let sink_dense_ids = vec![7u32, 12u32]; // resolve your real sink dense ids
    if let Some(path) = g.shortest_path_to_any(endpoint_dense_id, &sink_dense_ids, 20) {
        let mut signatures = Vec::new();
        let mut involved = Vec::new();
        for dense_id in &path {
            if let Some(sym) = g.resolve_symbol(*dense_id) {
                involved.push(sym);
                signatures.push(g.signature_for(*dense_id).unwrap_or("").to_string());
            }
        }
        result.findings.push(ReduceFinding {
            pattern: "endpoint_reaches_sink".to_string(),
            message: format!("path length {}", path.len()),
            involved,
            signatures,
        });
    }
    result
}
```

## Use cases

- **Orphan/dead symbols**: iterate dense ids, report `is_definitely_dead_code(i) == Some(true)`.
- **Unwired components**: report `is_symbol_referenced(i) == false` for a specific declaration kind (e.g. Spring `@Component` classes with zero inbound edges).
- **Layering violations / package cycles**: `g.strongly_connected_components()` over module-level symbol ids.
- **Endpoint -> sink reachability**: `g.shortest_path_to_any(endpoint, sinks, max_depth)`, always ship the path.
- **Blast radius**: `g.reachable_from(roots, max_depth).len()` -- how much of the codebase a change to `roots` can affect.

## Execution model

This tool runs SYNCHRONOUSLY within `timeout_seconds` (off the server's event loop) -- it does not yet submit a `BackgroundJobManager` job the way `xray_search` does, so `await_seconds` is accepted but currently inert. A future story may add full async job-polling parity; for now, plan for `timeout_seconds` (max 600s) to cover the whole pipeline: repo resolution, file collection, evaluator compile, `--build-graph`, and `--analyze-graph`.

## Related

- See `xray_search` for single-file AST pattern matching (regex-driven candidate selection, one evaluator call per file).
- See `xray_explore` for AST structure discovery to help craft `collect_facts`/`analyze_graph` logic.
