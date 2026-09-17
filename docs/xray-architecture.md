# X-Ray Search Engine and MCP Tool

This document captures the X-Ray search engine architecture and MCP handler shim invariants extracted from project CLAUDE.md. It defines the two-phase orchestration (regex driver -> Rust evaluator) and the async job submission pattern.

## Supported Languages

The Rust xray-core engine supports 17 mandatory languages: java, kotlin, go, python, typescript, javascript, bash, csharp, html, css, hcl/terraform, yaml, sql, xml, groovy, c, cpp. The Python xray engine supports 12 (hcl conditional via `_hcl_available()`; c and cpp added later).

C and C++ extensions and verified node kinds (confirmed against tree-sitter-c 0.24.2 and tree-sitter-cpp 0.23.4):

- **C** — extensions `.c`, `.h`. Root `translation_unit`. Function definition `function_definition`. Function call `call_expression`. Struct `struct_specifier`. `if_statement` / `for_statement` / `while_statement`. String literal `string_literal`. Comment `comment`. C has no exception constructs.
- **C++** — extensions `.cc`, `.cpp`, `.cxx`, `.c++`, `.hpp`, `.hh`, `.hxx`, `.h++`. Root `translation_unit`. Function definition `function_definition`. Function call `call_expression`. Class `class_specifier` (also `struct_specifier`). Namespace `namespace_definition`. Template `template_declaration`. `if_statement` / `for_statement` / `while_statement`. Try/catch `try_statement` / `catch_clause`. String literal `string_literal`. Comment `comment`.

`.h` maps to the C grammar (GitHub-Linguist default). A C++ header named `.h` parses under the C grammar and may produce ERROR nodes on C++-only syntax; name C++ headers `.hpp`/`.hh`/`.hxx`/`.h++` to parse them under the C++ grammar.

`src/code_indexer/xray/search_engine.py` — `XRaySearchEngine` is the two-phase orchestrator:

- **Phase 1 (driver, regex)**: regex walk over `repo_path` via `_run_phase1_driver`. Applies the `pattern` regex to file content (`search_target='content'`) or relative path (`search_target='filename'`). Honors `path`, `include_patterns` / `exclude_patterns` (fnmatch / ripgrep glob), `case_sensitive`, `multiline`, `pcre2`, and `context_lines`. Content searches delegate to `RegexSearchService` (ripgrep-backed). Returns a sorted, deduplicated list of candidate `Path` objects together with their per-file Phase 1 hit list, stored in `self._last_phase1_positions[path]` as a list of dicts: `{line_number, line_content, column, byte_offset, context_before, context_after}`.

- **Phase 2, single-file mode (evaluator, current Rust contract)**: for each candidate file, a Rust evaluator compiled from caller-supplied source runs once against that file's root `OwnedNode`. The entry point is `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`. `kind` and `start_line` are FIELDS on `OwnedNode`, not methods. `EvalFinding` is `{ pattern: String, line: usize, snippet: String }` -- there is no `message` field. An empty `Vec<EvalFinding>` means the file matched Phase 1 but the evaluator found nothing to report; the evaluator is file-as-unit, not a separate callback per regex match. This is compiled and executed by `xray-core`/`xray-cli`; it is not the retained internal Python module described in [X-Ray Sandbox](xray-sandbox.md), which is not on this evaluation path.

  MCP requests to `xray_search` use `pattern` (the Phase 1 regex) and `max_results` (candidate-file cap). The REST endpoint `POST /api/xray/search` exposes the same capability under its own field names, `driver_regex` and `max_files` -- do not mix MCP and REST field names in one request. See the [xray_search tool documentation](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md) for the full schema, timeout behavior, and security restrictions.

  The server (`_build_matches` in `rust_backend.py:1583-1596`) then adds these fields to every match dict; the Rust `EvalFinding` the evaluator returns is exactly `{pattern, line, snippet}` and cannot supply any of them:
  - `file_path` (always added by the server).
  - `language` (always added by the server) — tree-sitter language name.
  - `line_content` (always added by the server) — derived from `source` using `line_number` (1-based). Empty string if `line_number` is out of range.
  - For `xray_explore` only: `matched_node` (compact root description) and `ast_debug` (BFS-serialised AST tree), added separately in `search_engine.py:722-724`.

  Failure modes on this path (`UnsupportedLanguage`, `EvaluatorTimeout`, `ValidationError` (evaluator code rejected by `validate_rust_evaluator`), `BinaryNotFound` (missing `xray-cli`), `XRayCliError` (subprocess/JSON failure), generic file IO errors, or the raw exception type name) append to `evaluation_errors[]` without failing the job. `EvaluatorCrash`, `InvalidEvaluatorReturn`, and `ValidationFailed` are Python-only names produced by the retained Python evaluator module's own engine layer (`sandbox.py:858-917`), described in [X-Ray Sandbox](xray-sandbox.md); they are not emitted on this Rust path.

- **Phase 2, graph mode (`analyze_graph`, cross-file)**: a different execution mode from single-file `xray_search`. It requires both `fn collect_facts` (runs per file to collect auxiliary evidence) and `fn analyze_graph` (reduces the completed cross-file graph); a graph evaluator must not define `evaluate_node`. Graph extraction currently supports Java only. Always check `fact_graph_complete` and the degradation counters before treating an empty `findings` list as a verified negative. See "Graph Mode: Node and Edge Model" below for what becomes a node, what becomes an edge, and what is invisible. For the `UserFact`, `GraphResult`, `ReduceFinding`, graph-handle, and completeness contracts, fetch the full tool documentation: `get_file_content(repository_alias='code-indexer-global', file_path='src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md')`.

- **`max_results` cap**: when provided, only the first N candidates are evaluated; result includes `partial=True` and `max_files_reached=True`. Job-level timeout takes precedence over the cap (`partial=True`, `timeout=True`).

- **`progress_callback(percent, phase_name, phase_detail)`** is called at 0%, 50%, and 100%.

- **ThreadPoolExecutor parallelism**: Phase 2 evaluation runs across a configurable thread pool (`worker_threads`, default 2). Job-level wall-clock enforced via `_timed_out()` re-check between completions.

`src/code_indexer/server/mcp/handlers/xray.py` — `handle_xray_search` is a thin MCP handler shim:

- **Auth check**: `user.has_permission("query_repos")` or returns `auth_required`.
- **Parameter validation**:
  - `pattern` is required — empty/missing returns `pattern_required`.
  - `search_target` in `("content", "filename")`.
  - `context_lines` in `[0, 10]`.
  - `max_results` >= 1 when provided.
  - `timeout_seconds` in `[10, 600]`.
  - `await_seconds` in `[0.0, 45.0]` (maximum defined by `_AWAIT_SECONDS_MAX`). Values above 30.0 cause a warning to be logged, as long polls consume FastAPI threadpool capacity.
- **Repository alias resolution — omni-aware (string OR list)**:
  - `repository_alias` accepts a single string, a list of strings, or a JSON-encoded string array (e.g. `'["repo-a", "repo-b"]'`). The handler parses the JSON-encoded form via `_parse_json_string_array`.
  - Single-repo path: returns `{"job_id": "<uuid>"}`.
  - Multi-repo path: submits one background job per resolved alias and returns `{"job_ids": [...], "errors": [...]}`. Per-alias resolution errors (unknown repo) are appended to `errors[]`; the batch continues for resolvable aliases.
  - Empty list returns `alias_required`.
- **Pre-flight**: `XRaySearchEngine()` instantiation (tree-sitter is a core dependency since v10.2.1, so this no longer raises a missing-deps error) then `validate_rust_evaluator(evaluator_code)` (fast rejection without subprocess). Pre-flight runs ONCE for the multi-repo path before any job is submitted.
- **Job submission**: `background_job_manager.submit_job(operation_type="xray_search", func=job_fn, ...)` — the job function closes over all validated params.
- **Optional inline await**: when `await_seconds > 0`, the handler polls `BackgroundJobManager.get_job_status(job_id, username)` for up to `await_seconds` and returns the inline result if the job completes; otherwise falls back to `{job_id}`.
- **Response**: `{"job_id": "<uuid>"}` (single repo) or `{"job_ids": [...], "errors": [...]}` (multi-repo). Clients poll `GET /api/jobs/{job_id}`.

`handle_xray_explore` mirrors `handle_xray_search` with two differences:
- `evaluator_code` is OPTIONAL — when missing or whitespace-only, defaults to a snippet that emits one match per Phase 1 hit (or a single file-level match in filename mode), accepting all candidate files for AST exploration.
- Adds `max_debug_nodes` (range 1..500, default 50) and passes `include_ast_debug=True` to the engine, which causes per-match `matched_node` + `ast_debug` server enrichment.

Tool docs: `src/code_indexer/server/mcp/tool_docs/search/xray_search.md`, `src/code_indexer/server/mcp/tool_docs/search/xray_explore.md`. Registered in `HANDLER_REGISTRY` via `_legacy.py` (`_xray_register`).

**Files**: `src/code_indexer/xray/search_engine.py`, `src/code_indexer/xray/sandbox.py`, `src/code_indexer/server/mcp/handlers/xray.py`. Tests: `tests/unit/xray/test_search_engine.py`, `tests/unit/xray/test_sandbox*.py`, `tests/unit/server/mcp/test_xray_search_handler.py`.

## Graph Mode: Node and Edge Model

Graph mode (`analyze_graph`) builds one cross-file `CodeGraph` per repository via `CodeGraphBuilder::build` (`rust/xray-core/src/graph/csr/builder.rs`, `code_graph.rs`). This section documents what becomes a node, what becomes an edge, and what the graph cannot see -- the precondition every dead-code or reachability claim from this tool rests on. Java is the only language with a graph extractor today (`rust/xray-core/src/graph/extract/java.rs`).

### What is a node

A graph node is a declaration recorded by the language extractor -- distinct from a tree-sitter parse-tree node (`OwnedNode`), which is what the extractor walks to PRODUCE declarations. Every declaration has a `DeclarationKind`: `Type`, `Method`, `Field`, `Constant`, or `Package` (`rust/xray-core/src/graph/extract/local_index.rs`). Once bound into the whole-repository graph, each symbol gets a dense `u32` id and optionally carries a declared `Visibility` (`Public`, `Protected`, `Private`, or `Unknown` when no modifier evidence exists) and its `DeclarationKind`, both attached unconditionally and never dropped under budget pressure.

Not every node kind participates equally in analysis. `CodeGraph::is_definitely_dead_code` returns a confident `Some(true)` only for a symbol whose kind is `Method` or `Type` AND whose visibility is provably `Private`. A `Field`, `Constant`, `Package`, or unknown-kind symbol -- or a `Public`/`Protected`/`Unknown`-visibility `Method`/`Type` -- always returns `None` (undecidable), never a false "definitely dead" claim.

### What becomes an edge

An edge (a `Reference` in the CSR arena) is produced by these Java tree-sitter
node kinds, dispatched in `java.rs`'s node-walk:

- `method_invocation` -- a method call.
- `method_reference` -- a reference such as `this::m`, `Type::m`, `expr::m`,
  `super::m`, or `Type::new`. Named method references retain every candidate
  overload rather than making an unsound choice.
- `object_creation_expression` -- a `new` expression. It references both the
  constructed type and every constructor that could accept the call.
- `explicit_constructor_invocation` -- `this(...)` or `super(...)`, which
  references every compatible constructor on the current or superclass type.
- `type_identifier` -- a bare type reference.
- `marker_annotation` / `annotation` -- an annotation usage (`@Marker`,
  `@Marker(...)`, `@Outer.Marker`), which references the annotation TYPE's
  own declaration by its last name segment.

Constructor candidates are deliberately kept conservatively: same-arity
overloads and varargs candidates remain referenced when the source syntax
cannot soundly identify one target. A `super.m()` or `super::m` call resolves
only against the enclosing type's recorded superclass family, never against
the enclosing type itself -- so an external superclass with a KNOWN,
recorded `extends` edge produces no self-edge to the overriding method in
the current type. The conservative "no evidence -> leave every candidate
referenced" fallback applies ONLY when the type has NO recorded supertype
edge at all -- neither an `extends` clause nor an `implements` clause. A
class with no `extends` clause but a real `implements` clause DOES narrow:
`implements` edges are recorded regardless of whether a `superclass` is
also present, so `super`-class narrowing sees a non-empty, complete
supertype set (the declared interfaces) and applies its normal hard filter
against them, exactly as it would for a class that also extends something.
The fallback is reserved for the narrower case of a class with NEITHER
clause: Java's implicit `java.lang.Object` superclass is never tracked as a
recorded edge, so a `super.m()` call there falls into the same "no
supertype evidence at all" case as a genuine extraction gap, and the
deliberately conservative fallback (leave every candidate referenced rather
than risk deleting a real target) can produce a self-loop when the sole
matching candidate happens to be the enclosing type's own method -- e.g.
`class Foo { public String toString() { return super.toString(); } }` with
no other `toString` anywhere in the repo. Distinct from a genuine extraction
gap (a `superclass`/`implements` clause that exists syntactically but could
not be resolved to a name, e.g. an unhandled tree-sitter shape, OR a
supertype whose bare name textually collides with the subtype's own bare
name -- two different declared types this bare-name-only extractor cannot
locally disambiguate): that case is tracked explicitly as "incomplete
supertype evidence" for the type, and `super`-class narrowing skips
narrowing entirely whenever it applies, regardless of whether some OTHER,
correctly-resolved supertype edge would otherwise make the candidate set
look non-empty and narrowable. During Java
binding, a private candidate declared in a different known top-level type is
discarded as Java-inaccessible, while nested types under the same top-level
type retain private access. Incomplete or ambiguous nesting evidence is kept
instead of guessed away.

No other syntax construct produces a reference edge. In particular,
`field_access` is not handled anywhere in the graph extraction code -- reading
or writing a field or a constant never creates an inbound edge to that field's
or constant's declaration, regardless of how many places in the codebase
touch it.

### What is invisible to the graph

Because edges come only from the syntactic constructs above, the following are
structurally invisible, not merely unhandled:

- Field and constant reads/writes (no `field_access` edge exists at all).
- Reflection (`Class.forName`, method-handle invocation, dynamic proxies) -- these are string values or runtime API calls, not `method_invocation`/`object_creation_expression`/`type_identifier` nodes naming the target.
- JNI-bound native methods -- the native implementation is outside any parsed Java source the extractor walks.
- Dependency-injection wiring -- an injected field is an ordinary field declaration, and its DI-driven construction happens outside any `object_creation_expression` this repository's source contains.
- Lombok-generated members (e.g. `@Data`-generated getters/setters/constructors) -- the extractor walks the tree-sitter parse of the source AS WRITTEN; Lombok's annotation processor generates bytecode the parser never sees, so a call to a Lombok-generated method has no corresponding declaration node to resolve against in the first place.
- JPA and other annotation-driven use (e.g. an entity field read only through reflection-backed ORM mapping). An annotation usage creates an edge only to the annotation TYPE's own declaration; whatever the annotation causes a framework to read or call at runtime creates no edge.
- Enum-constant construction. An enum constant's implicit call to an explicit
  enum constructor is not an `object_creation_expression`, so it currently
  creates no constructor edge.
- Implicit superclass-constructor calls. A constructor without an explicit
  `this(...)` or `super(...)` invocation has an implicit `super()` call in
  Java, but only explicit constructor-invocation syntax currently creates an
  edge.

A symbol reachable only through any of the above shows zero inbound edges and,
if it is an unreferenced private `Method` or `Type`, can be reported as
`is_definitely_dead_code == Some(true)` even though it has a real, live caller
the graph cannot see. Fields, constants, packages, and unknown-kind symbols
remain `None` under the allowlist regardless of visibility. This is a hard
boundary of this tool to design around, not a defect to file.

### CSR arena layout

`CodeGraph` stores the whole repository's references and candidates in two pre-reserved arenas (`Vec<Reference>`, `Vec<Candidate>`), each allocated to its final size once so a full repository's data lives in one heap allocation per arena. A `Reference` is a fixed 19-byte record (`from`(4) + `file`(4) + `line`(4) + `kind`(1) + `cand_start`(4) + `cand_len`(2)) that windows into the shared `candidates` arena rather than owning its own storage; ambiguity (`cand_len > 1`) and unresolved (`cand_len == 0`) are read directly off that window, never a separate enum. A `Candidate`'s wire record is 6 bytes (`symbol`(4) + `reasons`(2)): a dense interned `u32` symbol id and a `u16` bitflag set of resolution evidence. The in-memory `Candidate` also carries a `confidence` byte, but it is never serialized -- it is recomputed from `reasons` via `Confidence::derive` every time a `Candidate` is constructed, both when originally built and when decoded back from a graph file, so there is no path to a `Candidate` whose confidence disagrees with its evidence. This wire format is versioned (`XRAYGRF3`, `rust/xray-core/src/graph/csr/wire.rs`): the magic bytes were bumped from `XRAYGRF2` when Bug #1858 added a mandatory per-symbol `DeclarationKind` section to the binary layout, so a graph file written by older code fails the magic check rather than being silently misparsed.

### Receiver-type resolution and inheritance-family expansion

Candidate resolution is evidence-based: each `Candidate` carries a `reasons` bitflag set (`rust/xray-core/src/graph/reasons.rs`), and confidence is derived from whichever flags are set. Two evidence paths matter for understanding what the graph can and cannot resolve confidently:

- **Receiver-type resolution**: for a `receiver.method(...)` call, if the receiver's declared type can be determined within the SAME FILE (a local variable's, field's, or parameter's declared type, or a chained call's declared return type -- no build, no classpath, no generics resolution), and a candidate's enclosing type equals that type or one of its transitive supertypes, the candidate is narrowed to that match. This is a same-file, syntactic inference, not real type-checking.
- **Inheritance-family expansion**: once a call resolves to an interface or supertype method, every implementor's override of that same method is added to the candidate set as a family member at high confidence. This only ever WIDENS a candidate set, never narrows it. If the true family exceeds the configured maximum size, expansion is capped and the truncation is recorded as a separate, visible flag rather than silently dropped.

Both mechanisms operate purely on syntactic evidence recorded by the extractor -- neither performs full type inference, and neither can see across a compiled dependency boundary the extractor never parsed.

For ready-to-adapt graph-mode evaluator templates exercising this model, fetch the X-Ray Cookbook: `get_file_content(repository_alias='code-indexer-global', file_path='docs/xray-cookbook.md')`.

## xray_search_batch MCP Tool

Cross-repo, multi-expression X-Ray sweep in ONE background job. Distinct from `xray_search` omni path: returns a single `job_id` (not `job_ids`), accepts N repo aliases x M scan bundles (matrix), and tags every match with `repository_alias`, `scan_index`, and `pattern_name`.

`src/code_indexer/server/mcp/handlers/xray_batch.py` — `handle_xray_search_batch` is the MCP handler shim:

- **Auth check**: `user.has_permission("query_repos")` or returns `auth_required`.
- **Parameter validation**:
  - `repository_alias` required (string, list, or JSON array). Max 50 aliases after dedup.
  - `scans` required (non-empty list). Max 50 bundles.
  - Each scan bundle: `driver_regex` required; `evaluator_code` and `pattern_name` mutually exclusive; inline `evaluator_code` validated via `validate_rust_evaluator()` before job submission.
  - `timeout_seconds` in `[10, 7200]` (wider than single-repo xray_search — matrix covers many cells).
  - `await_seconds` in `[0, 30]` (lower than xray_search — batch rarely completes inline).
  - `max_results` >= 1 when provided (per-cell file cap, not a global cap).
- **Repository alias resolution**: each alias resolved via `_resolve_repo_path`. Global-alias fallback applied via `try_global_fallback` when alias unresolvable but a `{alias}-global` golden repo is active. Unresolvable aliases become `error_level="repo"` entries in `errors[]`. If ALL aliases fail: synchronous `no_repositories_resolved` error. If SOME fail: job submitted over resolved subset with `partial=True`.
- **Job submission**: ONE job via `background_job_manager.submit_job(operation_type="xray_search_batch", repo_alias=None, ...)`. The worker `_run_xray_batch_job` is closed over all resolved state.
- **Optional inline await**: same `_await_job_result` pattern as `xray_search` but with `await_seconds` capped at 30.

`_run_xray_batch_job` — the matrix worker function (runs in the background job thread):

- Iterates `resolved_repos x scans` (outer=repos, inner=scans) with between-cell cancellation checks via `bjm.jobs.get(job_id).cancelled`.
- Per cell: calls `resolve_batch_evaluator(scan, repo_alias, cidx_meta_path)` then `XRaySearchEngine().run(...)`. Cell exceptions are caught and recorded as `error_level="cell"` entries.
- Progress advances once per REPO (not per cell or file): `progress_callback(repos_completed/total * 100, ...)`.
- Timeout checked per cell via `time.monotonic()` against `deadline`.
- Sets `partial=True` whenever any error, evaluation_error, timeout, or cancellation occurs.
- Returns unified result dict: `matches[]`, `errors[]`, `evaluation_errors[]`, counters (`total_repos`, `total_scans`, `total_cells`, `repos_completed`), and boolean flags (`partial`, `timeout`, `cancelled`).

`resolve_batch_evaluator(scan, repo_alias, cidx_meta_path)` — pure per-cell helper:

- Resolution order: inline `evaluator_code` → `pattern_name` (repo-specific scope then `__any__/`) → default accept-all evaluator.
- Instantiates `XrayPatternService(cidx_meta_path, refresh_scheduler=None)` — no live app state.
- Returns `(evaluator_code_str, error_dict_or_None)`. On `pattern_not_found` or malformed YAML: returns `(None, {"error": "pattern_not_found"|"cell_execution_error", ...})`.

`_truncate_xray_batch_result(result, payload_cache)` — batch-specific result truncation:

- Serializes combined matches + errors + evaluation_errors as JSON.
- If serialized size <= inline threshold: returns original result dict.
- Otherwise: stores full JSON in `PayloadCache`, returns truncated dict with first 3 entries of each list plus `cache_handle`, `has_more`, `truncated`, `fetch_tool_hint`.

**Unified polled result shape** (GET /api/jobs/{job_id}):

- `matches[]` — each entry: `{repository_alias, scan_index, pattern_name, file_path, line_number, pattern, snippet, ...}`.
- `errors[]` — `error_level="repo"` (no `scan_index`) or `error_level="cell"` (with `scan_index`).
- `evaluation_errors[]` — per-file evaluator failures: `{repository_alias, scan_index, file_path, error_type, error_message}`.
- Counters: `total_repos`, `total_scans`, `total_cells`, `repos_completed`.
- Flags: `partial` (bool), `timeout` (bool), `cancelled` (bool).

`partial=True` triggers: any repo error, any cell error, any evaluation_error, timeout, cancellation, or per-cell `partial=True` (max_results cap hit).

REST shim: `POST /api/xray/search/batch` in `src/code_indexer/server/routes/xray_routes.py`. Converts Pydantic body to params dict, delegates to `handle_xray_search_batch`, translates MCP error envelope to HTTP status codes.

Tool doc: `src/code_indexer/server/mcp/tool_docs/search/xray_search_batch.md`. Registered in `HANDLER_REGISTRY` via `_legacy.py` (`_xray_batch_register`).

**Files**: `src/code_indexer/server/mcp/handlers/xray_batch.py`, `src/code_indexer/server/routes/xray_routes.py`. Tests: `tests/unit/server/mcp/test_xray_search_batch_handler.py`.

## Evaluator security model

The current MCP/REST evaluator contract runs the caller-supplied Rust source through `xray-core`/`xray-cli`'s own compile-and-execute pipeline (`fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>` for single-file mode, `fn collect_facts` + `fn analyze_graph` for graph mode). See the [xray_search tool documentation](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md) for its compile step and security restrictions.

The codebase also retains an internal Python AST whitelist (an allow-listed node set, a small safe-builtins list, and a matching set of banned constructs) belonging to a prior evaluator generation. That module still exists in-tree but is not on the evaluation path for `xray_search`/`xray_explore` and is not the MCP/REST contract. See [X-Ray Sandbox](xray-sandbox.md) for its class name and retained internals.
