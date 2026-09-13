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

- **Phase 2, graph mode (`analyze_graph`, cross-file)**: a different execution mode from single-file `xray_search`. It requires both `fn collect_facts` (runs per file to collect auxiliary evidence) and `fn analyze_graph` (reduces the completed cross-file graph); a graph evaluator must not define `evaluate_node`. Graph extraction currently supports Java only. Always check `fact_graph_complete` and the degradation counters before treating an empty `findings` list as a verified negative. See the [analyze_graph tool documentation](../src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md) for the `UserFact`, `GraphResult`, `ReduceFinding`, graph-handle, and completeness contracts.

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
