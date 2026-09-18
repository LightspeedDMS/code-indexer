# X-Ray Evaluator Cookbook

This cookbook provides routing and contract guidance plus a real evaluator
template library. Every template shown below is the byte-exact copy of a
real, compiled, EXECUTED `.rs` file under `docs/xray-templates/`; a Rust test
in `xray-core` (`dynlib.rs`) proves both that each template compiles and runs
correctly against a real fixture AND that this doc copy has not drifted from
its file. The current MCP and REST evaluator contract is Rust-based and is
documented by the live tool documentation for
`get_file_content(repository_alias='code-indexer-global', file_path='src/code_indexer/server/mcp/tool_docs/search/xray_search.md')`.
The examples below are request shapes and contract guidance; the templates in
"Template library: single-file mode" and "Template library: graph mode" below
are ready-to-adapt starting points for each execution mode.

## Reusing a stored pattern

Before writing `evaluator_code` inline, check the stored pattern library. Use
`browse_directory('cidx-meta-global', path='xray-patterns')` to list available
patterns, then pass the selected name as `pattern_name` in the MCP request.
Stored patterns avoid repeating evaluator code and may define typed parameters
through `pattern_params`.

## Single-file structural search

Use MCP `xray_search` when the question can be answered by inspecting one
candidate file at a time. Phase 1 selects candidate files with a regular
expression. The Rust evaluator then runs once for each candidate file and
receives its root `OwnedNode`.

The evaluator entry point is `fn evaluate_node(node: &OwnedNode) ->
Vec<EvalFinding>`. `OwnedNode` and `EvalFinding` are supplied by the compiler;
do not define them in the evaluator. `kind` and `start_line` are fields on an
owned node. `EvalFinding` contains `pattern`, `line`, and `snippet`; it does
not contain a `message` field.

An empty `Vec<EvalFinding>` means that the file matched Phase 1 but the
evaluator found nothing to report. The evaluator is file-as-unit: it does not
receive a separate callback for every regular-expression match.

## Template library: single-file mode

Each template below is copy-pasteable as `evaluator_code`. Node kinds
(`method_declaration`, `method_invocation`, `class_declaration`, ...) are
tree-sitter grammar names and are language-specific -- use `xray_explore` or
`xray_dump_ast` to discover the correct kind name for the language you are
scanning, then edit the `TARGET_KIND`/`TARGET_KINDS`/`TARGET_TEXT` constants
near the top of the template before running it.

<!-- template:find-function-definitions -->
```rust
// X-Ray template: find-function-definitions (single-file / legacy mode)
//
// Finds every function/method definition node in a file. Node kinds are
// language-specific: tree-sitter's Java grammar uses "method_declaration" and
// "constructor_declaration", its Python grammar uses "function_definition",
// its JavaScript/TypeScript grammars use "function_declaration" and
// "method_definition", and so on. Use xray_explore (or xray_dump_ast) to
// discover the exact kind name for the language you are scanning, then edit
// TARGET_KINDS below to match.
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    const TARGET_KINDS: [&str; 2] = ["method_declaration", "function_declaration"];
    const MAX_SNIPPET_CHARS: usize = 120;
    let mut findings: Vec<EvalFinding> = Vec::new();
    for kind in TARGET_KINDS {
        for def in node.descendants_of_kind(kind) {
            findings.push(EvalFinding {
                pattern: "function_definition".to_string(),
                line: def.start_line,
                snippet: def.text().chars().take(MAX_SNIPPET_CHARS).collect(),
            });
        }
    }
    findings
}
```

<!-- template:find-calls-containing-text -->
```rust
// X-Ray template: find-calls-containing-text (single-file / legacy mode)
//
// Finds every call-expression node whose source text contains a
// caller-chosen substring. TARGET_KIND is language-specific (e.g. Java's
// "method_invocation", Python's "call", JavaScript/TypeScript's
// "call_expression") -- use xray_explore to find the right kind name for
// your language before editing TARGET_KIND and TARGET_TEXT below.
//
// This is TEXT MATCHING over each call node's raw source text, not name
// resolution: it over-matches (any call whose text contains the substring,
// regardless of which symbol it actually resolves to) and under-matches
// (a call written across formatting that splits the substring). Use it to
// locate candidates for inspection, not as an authoritative call list.
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    const TARGET_KIND: &str = "method_invocation";
    const TARGET_TEXT: &str = "rawDelete";
    const MAX_SNIPPET_CHARS: usize = 120;
    let mut findings: Vec<EvalFinding> = Vec::new();
    for call in node.descendants_of_kind(TARGET_KIND) {
        if call.text().contains(TARGET_TEXT) {
            findings.push(EvalFinding {
                pattern: "call_containing_text".to_string(),
                line: call.start_line,
                snippet: call.text().chars().take(MAX_SNIPPET_CHARS).collect(),
            });
        }
    }
    findings
}
```

<!-- template:find-node-kind -->
```rust
// X-Ray template: find-node-kind (single-file / legacy mode)
//
// Finds every descendant node of a caller-chosen tree-sitter kind. Node
// kinds are language-specific and change between grammars -- use
// xray_explore's AST dump (xray_dump_ast) to discover the exact kind name
// for the language you are scanning before editing TARGET_KIND below.
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    const TARGET_KIND: &str = "class_declaration";
    const MAX_SNIPPET_CHARS: usize = 120;
    let mut findings: Vec<EvalFinding> = Vec::new();
    for hit in node.descendants_of_kind(TARGET_KIND) {
        findings.push(EvalFinding {
            pattern: "node_kind_match".to_string(),
            line: hit.start_line,
            snippet: hit.text().chars().take(MAX_SNIPPET_CHARS).collect(),
        });
    }
    findings
}
```

## Template library: graph mode

Each template below is copy-pasteable as both `collect_facts` and `analyze_graph` together; a graph evaluator must not define `fn evaluate_node`. See the table under the "Graph mode" section further down this document for which template answers which question.

<!-- template:find-definitely-dead-symbols -->
```rust
// X-Ray template: find-definitely-dead-symbols (graph mode)
//
// No caller-supplied placeholder constants: this template scans every
// dense id in `0..symbol_count()` unconditionally, nothing to edit.
// When `fact_graph_complete: false`, the graph extraction hit a gap
// (`max_files` truncation, a parse error, or similar); an empty or
// sparse `findings` list is untrustworthy under that condition.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

// Story #1854 remediation (F1/F7/F8/F-codex-1): the census is computed
// first and emitted as findings[0], before any per-symbol finding, so it
// always lands inside a truncated inline response (a real repo's
// per-symbol findings previously pushed the census past the MCP front
// door's inline truncation limit, making it structurally unreachable).
//
// `referenced` reads the pre-cap referenced bit (`is_symbol_referenced`):
// true once ANY raw candidate named this dense symbol id, independent of
// whether that candidate survived budget-ladder capping into the CSR
// arena. A `referenced` symbol can still have zero post-cap callers
// (`callers_of() == []`) when its only inbound edge was capped -- do not
// read `referenced` as "has callers".
//
// `undecidable` (`None`) covers any non-private visibility -- `Public`,
// `Protected`, AND `Unknown` (no modifier evidence, or a declaration
// shape/language the extractor does not classify) -- and on real Java
// source `Unknown` usually dominates this count. Do not read
// `undecidable` as "public API"; per-visibility counts are not broken out.
//
// `definitely_dead` (`Some(true)`) proves only "unreferenced AND
// explicitly declared private" for declaration kinds whose references
// the graph tracks (currently methods and types). Field and constant
// declarations are never `Some(true)`: their reads are not reference
// edges in the current extractor. The verdict remains falsifiable by
// reflection, JNI, or dependency injection, none of which leave an
// in-repo reference edge this graph can see, so a `Some(true)` verdict
// is not an unconditional deletion-safety proof.
struct DeadCodeCensus {
    definitely_dead: usize,
    undecidable: usize,
    referenced: usize,
    unresolved: usize,
    findings: Vec<ReduceFinding>,
}

fn scan_dead_code(g: &GraphHandle<'_>) -> DeadCodeCensus {
    let mut census =
        DeadCodeCensus { definitely_dead: 0, undecidable: 0, referenced: 0, unresolved: 0, findings: Vec::new() };
    let mut dense_id: usize = 0;
    while dense_id < g.symbol_count() {
        let d = dense_id as u32;
        let symbol = match g.resolve_symbol(d) {
            Some(s) => s,
            None => {
                census.unresolved += 1;
                dense_id += 1;
                continue;
            }
        };
        match g.is_definitely_dead_code(d) {
            Some(true) => {
                census.definitely_dead += 1;
                let signature = g.signature_for(d).unwrap_or("<no-signature>");
                census.findings.push(ReduceFinding {
                    pattern: "definitely_dead_symbol".to_string(),
                    message: format!("dense_id={} signature={}", d, signature),
                    involved: vec![symbol],
                    signatures: vec![signature.to_string()],
                });
            }
            Some(false) => census.referenced += 1,
            None => census.undecidable += 1,
        }
        dense_id += 1;
    }
    census
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let mut census = scan_dead_code(g);
    result.findings.push(ReduceFinding {
        pattern: "dead_code_scan_census".to_string(),
        message: format!(
            "definitely_dead={} undecidable={} referenced={} unresolved={}",
            census.definitely_dead, census.undecidable, census.referenced, census.unresolved
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result.findings.append(&mut census.findings);
    result
}
```

This template uses `is_definitely_dead_code`. `Some(true)` means an unreferenced symbol has explicit Java `private` visibility. A `None` result is normal for most symbols on a complete graph and is not a completeness signal; it means the extractor refuses to guess enclosing-type visibility. The `dead_code_scan_census` reports definitely dead, undecidable, referenced, and unresolved counts. When `fact_graph_complete: false`, an empty `findings` list is untrustworthy. Signature matching is text matching, not name resolution.

<!-- template:find-reference-cycles -->
```rust
// X-Ray template: find-reference-cycles (graph mode)
//
// No caller-supplied placeholder constants: this template walks every
// strongly-connected component unconditionally, nothing to edit. When
// `fact_graph_complete: false`, an empty or sparse `findings` list is
// untrustworthy. `strongly_connected_components` traverses the same
// POST-CAP CSR candidate arena as `callers_of`/`callees_of` below.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

// Story #1854 remediation (F1/F6/F-codex-2): the negative control is
// computed and emitted BEFORE any per-component finding, so it always
// lands in findings[0] regardless of how many components the graph has
// (a real repo's per-component findings previously pushed it past the
// MCP front door's inline truncation limit).
//
// A singleton SCC with a genuine self-edge
// (`g.callees_of(*dense_id).contains(dense_id)`) is a real one-node
// cycle, not a suppressed acyclic node -- Tarjan cannot distinguish the
// two by component length alone, so this template checks the edge
// itself before treating `component.len() < 2` as "nothing to report".
//
// `possible_candidate_cycle` (not `reference_cycle`): `callers_of`/
// `callees_of` read the POST-CAP candidate arena, where a reference's
// candidate window may include more than one proposed target when the
// binder could not disambiguate -- an SCC over these edges is a possible
// cycle among proposed candidates, not a confirmed source-level
// reference cycle.
fn analyze_component(
    g: &GraphHandle<'_>,
    component: &[u32],
    acyclic_singletons: &mut usize,
    self_loop_singletons: &mut usize,
) -> Option<ReduceFinding> {
    if component.len() < 2 {
        for dense_id in component {
            if !g.callees_of(*dense_id).contains(dense_id) {
                *acyclic_singletons += 1;
                return None;
            }
            *self_loop_singletons += 1;
            return g.resolve_symbol(*dense_id).map(|symbol| ReduceFinding {
                pattern: "possible_candidate_cycle".to_string(),
                message: "component_size=1 unresolved_drop_count=0 self_loop=true".to_string(),
                involved: vec![symbol],
                signatures: vec![g.signature_for(*dense_id).unwrap_or("<no-signature>").to_string()],
            });
        }
        return None;
    }
    let true_size = component.len();
    let mut involved: Vec<u64> = Vec::new();
    let mut signatures: Vec<String> = Vec::new();
    for dense_id in component {
        if let Some(symbol) = g.resolve_symbol(*dense_id) {
            involved.push(symbol);
            signatures.push(g.signature_for(*dense_id).unwrap_or("<no-signature>").to_string());
        }
    }
    let unresolved_drop_count = true_size - involved.len();
    Some(ReduceFinding {
        pattern: "possible_candidate_cycle".to_string(),
        message: format!("component_size={} unresolved_drop_count={}", true_size, unresolved_drop_count),
        involved,
        signatures,
    })
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let components = g.strongly_connected_components();
    let mut acyclic_singletons: usize = 0;
    let mut self_loop_singletons: usize = 0;
    let mut buffered: Vec<ReduceFinding> = Vec::new();
    for component in &components {
        if let Some(finding) = analyze_component(g, component, &mut acyclic_singletons, &mut self_loop_singletons) {
            buffered.push(finding);
        }
    }
    result.findings.push(ReduceFinding {
        pattern: "reference_cycle_negative_control".to_string(),
        message: format!(
            "acyclic_singletons_suppressed={} self_loop_singletons={}",
            acyclic_singletons, self_loop_singletons
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result.findings.append(&mut buffered);
    result
}
```

This reports the true component size and the unresolved drop count separately. When `fact_graph_complete: false`, an empty `findings` list is untrustworthy. This template takes no caller-supplied dense IDs -- it enumerates the graph's own strongly connected components, so there is nothing to edit before running it; unresolved symbols may be dropped from the attached finding while remaining in the true component size. `is_definitely_dead_code` depends on explicit Java visibility and is not a completeness signal. Signature matching is text matching, not name resolution.

<!-- template:report-reachable-symbols-from-dense-id -->
```rust
// X-Ray template: report-reachable-symbols-from-dense-id (graph mode)
//
// When `fact_graph_complete: false`, an empty or sparse `findings` list
// is untrustworthy. `reachable_from` traverses the same POST-CAP CSR
// candidate arena as `callers_of`/`callees_of` -- a capped edge is
// invisible to this traversal even though the underlying reference
// exists in source.
//
// Story #1854 remediation (F1/F9/F-codex-3): the out-of-range root check
// is pushed to `result.findings` immediately, before any per-root
// finding is buffered -- so `reachable_root_out_of_range` always lands
// ahead of the per-root findings regardless of which array position the
// out-of-range root occupies, never past the MCP front door's inline
// truncation window.
//
// `OUT_OF_RANGE_ROOT` is computed from `g.symbol_count()`, never a
// hardcoded literal -- on a graph with more symbols than an old
// hardcoded bound, a fixed literal silently stops being out-of-range and
// the demonstration disappears. The `reached.len() == 1` negative
// control fires for ANY root that reaches nothing else, not one
// hardcoded root id.
//
// `reachable_from`'s `max_depth` is `g.symbol_count()`, never a small
// hardcoded literal -- `ops.rs`'s own doc comment proves termination via
// the bounded visited set, not via `max_depth`, so passing the graph's
// own symbol count cannot diverge and never silently truncates a real
// traversal the way a small hardcoded depth would.
//
// `ROOT_DENSE_ID` and `SECOND_ROOT_DENSE_ID` below are caller-supplied
// placeholders -- edit them to the dense ids you actually want to probe
// before running this template.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

fn process_root(g: &GraphHandle<'_>, root: u32, buffered: &mut Vec<ReduceFinding>) -> Option<ReduceFinding> {
    if root as usize >= g.symbol_count() {
        return Some(ReduceFinding {
            pattern: "reachable_root_out_of_range".to_string(),
            message: format!("root_dense_id={} symbol_count={}", root, g.symbol_count()),
            involved: Vec::new(),
            signatures: Vec::new(),
        });
    }
    let reached = g.reachable_from(&[root], g.symbol_count());
    let mut unresolved = 0usize;
    let mut involved: Vec<u64> = Vec::new();
    let mut signatures: Vec<String> = Vec::new();
    for dense_id in &reached {
        match g.resolve_symbol(*dense_id) {
            Some(symbol) => {
                involved.push(symbol);
                signatures.push(g.signature_for(*dense_id).unwrap_or("<no-signature>").to_string());
            }
            None => unresolved += 1,
        }
    }
    buffered.push(ReduceFinding {
        pattern: "reachable_symbols".to_string(),
        message: format!("root_dense_id={} reached_total={} unresolved={}", root, reached.len(), unresolved),
        involved,
        signatures,
    });
    if reached.len() == 1 {
        buffered.push(ReduceFinding {
            pattern: "reachable_negative_control".to_string(),
            message: "a valid isolated root reaches no other symbol".to_string(),
            involved: Vec::new(),
            signatures: Vec::new(),
        });
    }
    None
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    const ROOT_DENSE_ID: u32 = 0;
    const SECOND_ROOT_DENSE_ID: u32 = 4;
    let out_of_range_root: u32 = g.symbol_count() as u32;
    let roots = [ROOT_DENSE_ID, SECOND_ROOT_DENSE_ID, out_of_range_root];
    let mut buffered: Vec<ReduceFinding> = Vec::new();
    for root in roots {
        if let Some(out_of_range_finding) = process_root(g, root, &mut buffered) {
            result.findings.push(out_of_range_finding);
        }
    }
    result.findings.append(&mut buffered);
    result
}
```

The caller-supplied dense IDs are placeholders. The template checks `symbol_count` and emits an explicit out-of-range finding. When `fact_graph_complete: false`, an empty `findings` list is untrustworthy; reached and unresolved counts are reported separately. `is_definitely_dead_code` depends on explicit Java visibility and is not a completeness signal. Signature matching is text matching, not name resolution.

<!-- template:find-path-to-dense-sink -->
```rust
// X-Ray template: find-path-to-dense-sink (graph mode)
//
// When `fact_graph_complete: false`, an empty or sparse `findings` list
// is untrustworthy. `shortest_path_to_any` traverses the same POST-CAP
// CSR candidate arena as `callers_of`/`callees_of` -- a capped edge is
// invisible to this traversal even though the underlying reference
// exists in source.
//
// Story #1854 remediation (F2/F-codex-3): this template previously
// reported "no path" only for one hardcoded fixture source id, making a
// genuine "no path exists" indistinguishable from "the template never
// evaluated that source". It now emits ONE unconditional census, over
// EVERY non-sink source, before returning -- never a per-source finding,
// since a real repository can have thousands of sources and per-source
// findings would push the census past the MCP front door's inline
// truncation limit.
//
// This runs a bounded BFS (`shortest_path_to_any`) per source, i.e.
// O(N*E) over the whole graph -- termination is guaranteed by the op's
// own visited-set argument, not by `max_depth` (see `ops.rs`'s doc
// comment; passing `g.symbol_count()` as the depth cannot diverge). On a
// large repository this per-source BFS is the dominant cost of the
// `analyze_graph` call and worth weighing against the request's timeout.
//
// `SINK_DENSE_ID` below is a caller-supplied placeholder -- edit it to
// the dense id of the symbol you actually want to reach before running
// this template.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    const SINK_DENSE_ID: u32 = 3;
    let mut sources_scanned: usize = 0;
    let mut paths_found: usize = 0;
    let mut no_path: usize = 0;
    let mut source = 0u32;
    while source < g.symbol_count() as u32 {
        if source != SINK_DENSE_ID {
            sources_scanned += 1;
            match g.shortest_path_to_any(source, &[SINK_DENSE_ID], g.symbol_count()) {
                Some(_) => paths_found += 1,
                None => no_path += 1,
            }
        }
        source += 1;
    }
    result.findings.push(ReduceFinding {
        pattern: "dense_sink_path_census".to_string(),
        message: format!(
            "sources_scanned={} paths_found={} no_path={}",
            sources_scanned, paths_found, no_path
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result
}
```

Like this cookbook's other caller-supplied dense IDs, `SINK_DENSE_ID` is a placeholder. This template no longer reports per-source path/no path findings individually -- a real repository can have thousands of sources -- so the `dense_sink_path_census`'s `no_path` counter is what distinguishes sources with no path to the sink from a template that never ran. When `fact_graph_complete: false`, an empty `findings` list is untrustworthy. `is_definitely_dead_code` depends on explicit Java visibility and is not a completeness signal. Signature matching is text matching, not name resolution.

<!-- template:callers-of-symbols-matching-signature-text -->
```rust
// X-Ray template: callers-of-symbols-matching-signature-text (graph mode)
//
// When `fact_graph_complete: false`, an empty or sparse `findings` list
// is untrustworthy.
//
// Story #1854 remediation (F3/F-codex-7): this template previously
// silently dropped four distinct blind spots -- a symbol with no cached
// signature, a matched symbol whose own dense id fails to resolve, a
// matched symbol with zero callers, and a caller whose dense id fails to
// resolve -- with no count anywhere proving how many were skipped. It
// now emits an unconditional census FIRST, before any per-caller
// finding, so the census always lands inside a truncated inline
// response on a real repo.
//
// This is TEXT MATCHING over `signature_for()`, not name resolution: it
// can over-match any cached signature that merely contains the
// substring, and it under-matches a symbol with no cached signature at
// all (counted as `missing_signature`, never silently skipped).
// `callers_of` reads the POST-CAP candidate arena, so a matched symbol
// can legitimately have zero callers even when it is referenced --
// counted as `matched_with_zero_callers`, not conflated with "no match".
//
// `SIGNATURE_TEXT` below is a caller-supplied placeholder -- edit it to
// the substring you actually want to match before running this template.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

struct SignatureMatchCensus {
    scanned: usize,
    matched: usize,
    missing_signature: usize,
    matched_with_zero_callers: usize,
    unresolved_targets: usize,
    unresolved_callers: usize,
    callers_reported: usize,
    findings: Vec<ReduceFinding>,
}

fn scan_signature_matches(g: &GraphHandle<'_>, signature_text: &str) -> SignatureMatchCensus {
    let mut census = SignatureMatchCensus {
        scanned: 0,
        matched: 0,
        missing_signature: 0,
        matched_with_zero_callers: 0,
        unresolved_targets: 0,
        unresolved_callers: 0,
        callers_reported: 0,
        findings: Vec::new(),
    };
    let mut dense_id = 0usize;
    while dense_id < g.symbol_count() {
        census.scanned += 1;
        let d = dense_id as u32;
        let signature = match g.signature_for(d) {
            Some(signature) => signature,
            None => {
                census.missing_signature += 1;
                dense_id += 1;
                continue;
            }
        };
        if signature.contains(signature_text) {
            record_match(g, d, signature, signature_text, &mut census);
        }
        dense_id += 1;
    }
    census
}

fn record_match(g: &GraphHandle<'_>, d: u32, signature: &str, signature_text: &str, census: &mut SignatureMatchCensus) {
    census.matched += 1;
    let target = match g.resolve_symbol(d) {
        Some(symbol) => symbol,
        None => {
            census.unresolved_targets += 1;
            return;
        }
    };
    let callers = g.callers_of(d);
    if callers.is_empty() {
        census.matched_with_zero_callers += 1;
    }
    for caller in callers {
        match g.resolve_symbol(caller) {
            Some(caller_symbol) => {
                census.callers_reported += 1;
                census.findings.push(ReduceFinding {
                    pattern: "caller_of_signature_match".to_string(),
                    message: format!(
                        "signature_text={} target_signature={} caller_dense_id={}",
                        signature_text, signature, caller
                    ),
                    involved: vec![caller_symbol, target],
                    signatures: vec![g.signature_for(caller).unwrap_or("<no-signature>").to_string(), signature.to_string()],
                });
            }
            None => census.unresolved_callers += 1,
        }
    }
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    const SIGNATURE_TEXT: &str = "Repository";
    let mut census = scan_signature_matches(g, SIGNATURE_TEXT);
    result.findings.push(ReduceFinding {
        pattern: "signature_match_census".to_string(),
        message: format!(
            "scanned={} matched={} missing_signature={} matched_with_zero_callers={} unresolved_targets={} unresolved_callers={} callers={}",
            census.scanned,
            census.matched,
            census.missing_signature,
            census.matched_with_zero_callers,
            census.unresolved_targets,
            census.unresolved_callers,
            census.callers_reported
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result.findings.append(&mut census.findings);
    result
}
```

This is text matching over `signature_for()`, not name resolution. It can over-match any signature containing the substring, under-match symbols with no cached signature, and callers_of sees only the post-cap candidate arena. Use it to locate candidates for inspection, never as an authoritative all-callers result. This template takes no caller-supplied dense IDs -- it enumerates `0..symbol_count()`; the only value to edit is the signature text constant. When `fact_graph_complete: false`, an empty `findings` list is untrustworthy. `is_definitely_dead_code` depends on explicit Java visibility and is not a completeness signal.

## MCP request fields

MCP requests use `repository_alias`, `pattern`, `search_target`, and optional
`evaluator_code`. For a first call, omit `evaluator_code`; the server supplies
the default evaluator. Use `max_results` to cap the number of candidate files.
Begin with a small `max_results` value while checking a search, then increase
it when the result shape is understood. Include and exclude patterns,
language-specific paths, and `context_lines` can further focus the search.

The full MCP schema, evaluator rules, output fields, timeout behavior, and
security restrictions are maintained in the xray_search tool documentation:
`get_file_content(repository_alias='code-indexer-global', file_path='src/code_indexer/server/mcp/tool_docs/search/xray_search.md')`.

## REST field names

The REST endpoint `POST /api/xray/search` exposes the same single-file
capability but retains its REST field names. Send `driver_regex` instead of
the MCP `pattern`, and `max_files` instead of the MCP `max_results`. Do not
copy REST field names into an MCP request. Refer to the REST section of the
live xray_search contract,
`get_file_content(repository_alias='code-indexer-global', file_path='src/code_indexer/server/mcp/tool_docs/search/xray_search.md')`,
when building an HTTP request.

## Graph mode

| Question | Template |
|---|---|
| Is this symbol definitely dead code? | `find-definitely-dead-symbols` |
| Are there reference cycles among symbols? | `find-reference-cycles` |
| What is reachable from a given symbol? | `report-reachable-symbols-from-dense-id` |
| Is there a path from source symbols to a sink? | `find-path-to-dense-sink` |
| Who calls symbols matching a signature substring? | `callers-of-symbols-matching-signature-text` |

Each template above is in "Template library: graph mode" earlier in this document. An MCP client holding only a served document body (no filesystem) can fetch any of them directly by filename, e.g. `get_file_content(repository_alias='code-indexer-global', file_path='docs/xray-templates/find-definitely-dead-symbols.rs')`.

Use `analyze_graph` for questions that require relationships among files, such as reachability,
dead code, layering, or blast radius. Graph mode builds a cross-file reference
graph and is a different execution mode from single-file `xray_search`.

Graph mode requires both `fn collect_facts` and `fn analyze_graph`; neither is
optional. A graph evaluator must not define `fn evaluate_node`. `collect_facts`
runs per file to collect auxiliary evidence, while `analyze_graph` reduces the
completed graph. The graph extractor currently supports Java only.

Always inspect `fact_graph_complete` and the degradation counters before
treating an empty `findings` list as a verified negative. When
`fact_graph_complete` is false, an empty result means that the graph was too
incomplete to trust as a clean bill of health.

For the `UserFact`, `GraphResult`, `ReduceFinding`, graph-handle, and
completeness contracts, fetch the full `analyze_graph` tool documentation: `get_file_content(repository_alias='code-indexer-global', file_path='src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md')`.

## Choosing the mode

Choose single-file mode when the evidence is local to each file and the
question is naturally expressed as findings attached to syntax nodes. Choose
graph mode when the answer depends on callers, callees, symbol identity, or a
path spanning multiple files. Both modes use the Rust evaluator engine, but
their required function contracts must not be mixed.

## Related documentation

- For the engine architecture, its two execution modes, and the graph node/edge model: `get_file_content(repository_alias='code-indexer-global', file_path='docs/xray-architecture.md')` (X-Ray Architecture).
- `get_file_content(repository_alias='code-indexer-global', file_path='docs/xray-sandbox.md')`
  describes the retained internal Python module and its non-contract status.
- `get_file_content(repository_alias='code-indexer-global', file_path='src/code_indexer/server/mcp/tool_docs/search/xray_search.md')`
  defines the single-file request and evaluator schema.
- For graph extraction, reduction, and completeness semantics: `get_file_content(repository_alias='code-indexer-global', file_path='src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md')` (analyze_graph MCP contract).
