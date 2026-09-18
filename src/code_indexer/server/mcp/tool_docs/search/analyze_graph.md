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
      description: 'Inline Rust graph evaluator defining TWO REQUIRED functions: fn collect_facts(...) and fn analyze_graph(...). Mutually exclusive with pattern_name. Both functions must be defined together; graph evaluators must not define fn evaluate_node. The same Rust security whitelist as xray_search applies.'
    pattern_name:
      type: string
      description: 'Stored graph-mode pattern name to resolve from the X-Ray pattern library. Mutually exclusive with evaluator_code. Repository-specific patterns take precedence over __any__. A legacy pattern (including a pre-existing pattern with no execution_mode) is rejected with pattern_mode_mismatch.'
    pattern_params:
      type: object
      description: 'Optional typed parameter overrides for the stored graph pattern, using the same substitution semantics as xray_search. Ignored unless pattern_name is supplied.'
      additionalProperties: true
    include_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to include in the graph (e.g. ["*.java"]). These patterns DO narrow what is read into the graph. PASS ["*.java"] -- the graph extractor is Java-only, so on any mixed-language repository an empty list pulls in every other file, inflates the files_with_unsupported_language degradation counter, and makes fact_graph_complete: true unreachable. Empty list means include all files.'
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
outputSchema:
  type: object
  properties:
    ok:
      type: boolean
      description: 'True when the graph pipeline completed without an error. This includes status="ran_ok" and the server-derived status="no_supported_files" (all candidate files were unsupported languages). False for every error path (validation, missing repo, build failure, timeout, internal error) -- check `error` for the reason.'
    error:
      type: object
      description: 'Present iff ok=false. Shape: {error_type, error_message} for pipeline-level failures (ValidationError, BinaryNotFound, CompileError, GraphBuildError, XRayCliError, Timeout, InternalError), or a synchronous rejection shape {error, message} for input-validation failures (auth_required, evaluator_code_required, repository_alias_required, include_patterns_invalid, exclude_patterns_invalid, timeout_seconds_invalid, xray_evaluator_validation_failed, repository_not_found, no_candidate_files, mutually_exclusive_params, pattern_mode_mismatch).'
    status:
      type: string
      description: 'The real --analyze-graph ChildReport status, OR the server-derived "no_supported_files": "ran_ok" (your analyze_graph executed), "no_supported_files" (ok=true, but every candidate file was an unsupported language -- see "no_supported_files status" below), "absent" (evaluator does not export analyze_graph -- should not happen given evaluator_code validation), "load_failed" (dylib failed to load), "graph_invalid" (the built graph file was corrupt), "panicked" (your analyze_graph panicked -- caught, never crashes the server).'
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

**`include_patterns`/`exclude_patterns` DO narrow which files are read into the graph.** By default (both empty) indexing covers the whole repository; passing `include_patterns: ["*.java"]` restricts indexing to just the matching files (this is the recommended usage on a mixed-language repo -- see "Language support" above: an empty `include_patterns` on such a repo pulls in every non-Java file too, inflating `files_with_unsupported_language` and making `fact_graph_complete: true` unreachable).

What patterns do NOT narrow is which files can appear as the *target* of a resolved reference. A reference from an included file to a symbol declared in an EXCLUDED file still resolves as "external to the graph", which is a real, meaningful signal your evaluator can act on; it is never silently dropped. So narrowing `include_patterns` shrinks what gets indexed and searched, but a symbol outside that scope can still be correctly identified as an external dependency rather than vanishing from the analysis.

## Glob Pattern Semantics

`include_patterns`/`exclude_patterns` use the exact same selector as `regex_search` and
`xray_search` (`PathPatternMatcher`, gitignore-style globs) -- a pattern produces the same file set
here as it would for either of those tools:

- `*` does not cross `/` when the pattern has a trailing suffix -- `src/*.java` matches only
  `src/Foo.java`, never `src/sub/Foo.java`. Same for both `include_patterns` and
  `exclude_patterns`.
- A BARE trailing `*` with no suffix (e.g. `src/*`) behaves DIFFERENTLY depending on which list
  it is used in (verified directly against the shared selector, both directions):
  - As `include_patterns`, `src/*` matches only files directly under `src/` by name --
    `src/Foo.java`, never `src/sub/Foo.java` or `src/sub/sub2/Foo.java` (ripgrep `-g` reference
    semantics: a directory match never implies "and everything under it" for an include).
  - As `exclude_patterns`, `src/*` instead excludes the WHOLE subtree -- `src/Foo.java`,
    `src/sub/Foo.java`, and `src/sub/sub2/Foo.java` are all dropped (gitignore containment
    semantics: excluding a directory excludes everything inside it).
  Use `src/**` to deliberately INCLUDE the whole subtree (matches at every depth under `src/`,
  including direct children).
- A pattern with no `/` at all (e.g. `*.java`) matches the basename at any depth: `Foo.java`,
  `src/Foo.java`, and `src/sub/Foo.java` all match. Same for both `include_patterns` and
  `exclude_patterns`.
- A trailing-slash directory marker (e.g. `src/tests/`) selects that directory's CONTENTS in
  BOTH `include_patterns` and `exclude_patterns` -- including when the marker itself also
  carries a wildcard (e.g. `src/*/` selects files under any direct subdirectory of `src/`,
  never a file directly in `src/` itself; `*/tests/` selects any `tests/` directory's contents
  at any depth). See `regex_search`'s tool docs for the full trailing-slash / leading `*/`
  reference -- the underlying matcher is shared, so those rules apply here unchanged.
- Brace groups are supported (e.g. `*.{java,kt}`), capped at 64 expanded variants per pattern.

See `regex_search`'s tool docs for the full semantics reference (leading `*/` any-depth rewriting,
trailing-`/` directory markers -- root-anchored only when MULTI-segment, e.g. `src/main/`; a
SINGLE-segment marker like `docs/` or `tests*/` matches at any depth -- bare-token ambiguity
handling) -- the underlying matcher is shared, so those rules apply here unchanged.

## Running a stored graph pattern

`pattern_name` resolves a previously stored evaluator from the X-Ray pattern library instead of inlining `evaluator_code` (mutually exclusive with it). Resolution tries the REPOSITORY-SPECIFIC scope first (`cidx-meta/xray-patterns/{repository_alias}/{pattern_name}.yaml`), then falls back to the cross-repo `__any__` scope (`cidx-meta/xray-patterns/__any__/{pattern_name}.yaml`) -- a repo-specific pattern always takes precedence over a same-named `__any__` pattern.

A stored pattern declares its own `execution_mode` (`"legacy"` for `xray_search`-style single-file evaluators, or `"graph"` for the two-function `collect_facts`/`analyze_graph` contract this tool requires). `analyze_graph` checks the declared mode BEFORE preparing the evaluator code; a pattern authored for `xray_search` cannot be run here.

`pattern_params` supplies optional typed overrides for the pattern's declared parameters, using the exact same substitution semantics `xray_search` uses for its own stored patterns: each resolved value is injected as a Rust `const` declaration prepended to the evaluator source before compilation. Ignored unless `pattern_name` is also supplied.

Error codes specific to pattern resolution:

- `mutually_exclusive_params` -- both `pattern_name` and `evaluator_code` were supplied; provide exactly one.
- `pattern_mode_mismatch` -- the stored pattern's declared `execution_mode` is not `"graph"` (this includes a legacy pattern predating `execution_mode`, which is treated as non-graph).
- `pattern_not_found` -- `pattern_name` does not exist in either the repository-specific scope or `__any__`.
- `invalid_pattern_params` -- `pattern_params` was supplied as a TRUTHY, non-empty JSON value that is not an object (e.g. a non-empty array like `["a"]` or a non-empty string like `"foo"`); rejected before any per-parameter validation runs, so this never surfaces alongside `unknown_parameter`/`parameter_type_mismatch`. An EMPTY array (`[]`), empty string (`""`), or any other falsy value is indistinguishable from omitting `pattern_params` entirely -- it is treated as absent (defaults to no overrides) and never reaches this check. Response: `{"error": "invalid_pattern_params", "message": "invalid_pattern_params: pattern_params must be a dict, got <type>"}`.
- `path_traversal_rejected` -- `repository_alias` (used as the pattern-resolution scope) or `pattern_name` contains `/`, `\`, or `..`. Checked before the filesystem lookup, so it takes priority over `pattern_not_found` for the same request. Response: `{"error": "path_traversal_rejected", "message": "path_traversal_rejected: <field> '<value>' contains path traversal sequences"}`, where `<field>` is `repo_alias` or `pattern_name`.

(`unknown_parameter` and `parameter_type_mismatch` can also surface from `pattern_params` validation, mirroring `xray_search`'s own stored-pattern parameter errors.)

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
| `g.symbol_count()` | `() -> usize` | Exact number of symbols; dense ids are in `0..g.symbol_count()`. |
| `g.dense_id_for(symbol)` | `(u64) -> Option<u32>` | Reverse lookup from a real global `SymbolId` to its dense id. |
| `g.resolve_string(string_id)` | `(u32) -> Option<&str>` | Interned string lookup. |
| `g.is_symbol_referenced(dense_id)` | `(u32) -> bool` | True if ANY inbound edge exists, regardless of graph completeness. |
| `g.is_definitely_dead_code(dense_id)` | `(u32) -> Option<bool>` | `Some(false)` = the symbol has an inbound reference edge. `Some(true)` = unreferenced, its declaration kind is `Method` or `Type`, and its visibility is provably `Private`. `None` = every other case: `Public`, `Protected`, or `Unknown` visibility, and every `Field`, `Constant`, `Package`, or unknown-kind symbol. Java extraction creates inbound edges for direct calls, method references, `new` expressions, explicit `this(...)`/`super(...)` constructor invocations, `Type::new`, and annotation usages (an edge to the annotation type's declaration); it preserves plausible overload and varargs targets. `super` calls bind only to a KNOWN, recorded superclass edge; the conservative "no evidence" fallback applies only when a class has NEITHER an `extends` NOR an `implements` clause (e.g. implicit `java.lang.Object`, never tracked) -- a class with no `extends` but a real `implements` clause still narrows against its recorded interfaces. With neither clause, `super.m()` falls into the same "no supertype evidence" case as a genuine extraction gap, so it can still self-loop when the enclosing type's own method is the sole matching candidate. Java-private candidates from a different known top-level type are excluded. **This predicate does NOT consult `fact_graph_complete`** -- it returns `Some(true)` on an incomplete graph exactly as it would on a complete one. Field and constant reads therefore cannot produce `Some(true)`: those declaration kinds are outside the predicate's allowlist, regardless of whether their reads are represented by graph edges. A `Some(true)` for an allowed private `Method` or `Type` can still be falsified by reflection, JNI, dependency injection, or other runtime behavior invisible to the graph. |
| `g.signature_for(dense_id)` | `(u32) -> Option<&str>` | Cached declaration signature line, for reporting. |

### FactsHandle reference

| Method | Signature | Description |
|--------|-----------|--------------|
| `facts.for_symbol(symbol_id)` | `(u64) -> Vec<UserFact>` | Facts your `collect_facts` attributed to this symbol's enclosing declaration. |
| `facts.for_custom(name)` | `(&str) -> Vec<UserFact>` | Facts attributed to a `custom_key` (non-symbol key) instead. |

## AnalysisCompleteness: the honesty contract

The whole point of this tool is that a caller can tell "no findings" apart from "the index was too incomplete to trust a negative". **Always check `fact_graph_complete` and `degradation` before treating an empty `findings[]` as a clean result** -- especially for dead-code-style analyses. **`is_definitely_dead_code` does NOT gate itself on completeness** -- it returns `Some(true)` on a degraded graph exactly as it would on a complete one, so an evaluator that only checks `== Some(true)` WILL emit false positives when the graph is incomplete. The completeness check is yours to apply: read `fact_graph_complete` and `degradation` yourself, and surface `fact_graph_complete=false` to the human rather than presenting either an empty list as "verified clean" or a populated list as "verified dead".

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

## no_supported_files status

Most repositories on this server are NOT Java (Vue/Spring Boot, .NET/Angular, Node/Lambda, Kotlin/Jetty), and the graph extractor is Java-only. Running `analyze_graph` against one of them previously returned `ok: true, status: "ran_ok", findings: []` -- a response that reads as a clean bill of health when in fact the analysis had nothing to analyse at all. The unsupported files were counted only inside `degradation.files_with_unsupported_language`, which the caller had to know to go read.

When EVERY candidate file (after `include_patterns`/`exclude_patterns`) is an unsupported language, `status` is `"no_supported_files"` instead of `"ran_ok"`. `ok` stays `true` -- nothing failed, the request ran correctly and simply had zero supported input. Treat this status the same way you would treat `fact_graph_complete: false`: an empty `findings[]` under it is not a verified-clean result, it is "there was nothing this tool could look at".

```json
{
  "ok": true,
  "status": "no_supported_files",
  "findings": [],
  "fact_graph_complete": false,
  "degradation": {
    "files_with_unsupported_language": 41,
    "unreadable_or_unsupported_files": 0,
    "files_with_parse_errors": 0,
    "truncated_by_max_files": false
  }
}
```

`no_supported_files` is deliberately scoped to "no candidate file reached a supported-language extractor". The graph builder's `files_with_unsupported_language` and `unreadable_or_unsupported_files` counters are disjoint: the former covers recognized non-Java source extensions with no extractor, while the latter covers genuinely unrecognized extensions or paths rejected before extraction. The status is used only when their combined count covers every candidate file. A mixed repo containing any successfully extracted Java file therefore keeps `status: "ran_ok"`, even if it yields zero findings. Genuine read, parse, extractor, and collector failures are excluded from this status and remain independently signaled via their own `degradation` counters and `fact_graph_complete`; conflating those failures with "wrong language" would blur two different remediations.

A related case that deliberately does NOT get its own status: a repo with real Java files present, correctly parsed, that simply contains zero declarations for your evaluator's query to match. This is a legitimate empty result (e.g. a directory containing only interfaces with no method bodies, or a narrow `include_patterns` scope) -- structurally different from "no supported language was even present" -- so it stays `"ran_ok"`. `fact_graph_complete` and `degradation` already give the caller everything needed to judge that outcome; a third status would add a distinction without a corresponding difference in what the caller should do next.

## Directional asymmetry (dead-code vs reachability)

Per the epic's design: **dead-code analysis must UNDER-report (safe)**. The engine enforces the declaration-kind and visibility floor for `is_definitely_dead_code`: only an unreferenced private `Method` or `Type` can produce `Some(true)`; every field, constant, package, unknown-kind, or non-private symbol produces `None` unless it has an inbound reference, which produces `Some(false)`. The predicate still does not consult `fact_graph_complete`, so your evaluator must check completeness before trusting any `Some(true)`; runtime behavior such as reflection, JNI, or dependency injection can remain invisible even for an allowed kind. **Reachability analysis must OVER-report (unsafe in the other direction)** -- when your evaluator reports that endpoint X can reach sink Y, it must ship the PATH (`involved`) and identify the weakest link's confidence, since a caller relying on a reachability claim needs to audit exactly how strong that claim is rather than trusting a bare boolean.

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
- For guidance on choosing between single-file and graph mode, and for ready-to-adapt graph-mode evaluator templates: `get_file_content(repository_alias='code-indexer-global', file_path='docs/xray-cookbook.md')` (X-Ray Cookbook).
- For the graph engine's node/edge model, CSR arena layout, and what the graph cannot see: `get_file_content(repository_alias='code-indexer-global', file_path='docs/xray-architecture.md')` (X-Ray Architecture).
- To fetch a graph-mode template's source directly: `get_file_content(repository_alias='code-indexer-global', file_path='docs/xray-templates/find-definitely-dead-symbols.rs')` (also available: `docs/xray-templates/find-reference-cycles.rs`, `docs/xray-templates/report-reachable-symbols-from-dense-id.rs`, `docs/xray-templates/find-path-to-dense-sink.rs`, `docs/xray-templates/callers-of-symbols-matching-signature-text.rs`).
