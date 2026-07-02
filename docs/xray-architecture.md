# X-Ray Search Engine and MCP Tool (Epic #968 / Story #972)

This document captures the X-Ray search engine architecture and MCP handler shim invariants extracted from project CLAUDE.md. It defines the two-phase orchestration (regex driver → sandboxed evaluator) and the async job submission pattern.

`src/code_indexer/xray/search_engine.py` — `XRaySearchEngine` is the two-phase orchestrator:

- **Phase 1 (driver, regex)**: regex walk over `repo_path` via `_run_phase1_driver`. Applies the `pattern` regex to file content (`search_target='content'`) or relative path (`search_target='filename'`). Honors `path`, `include_patterns` / `exclude_patterns` (fnmatch / ripgrep glob), `case_sensitive`, `multiline`, `pcre2`, and `context_lines`. Content searches delegate to `RegexSearchService` (ripgrep-backed). Returns a sorted, deduplicated list of candidate `Path` objects together with their per-file Phase 1 hit list, stored in `self._last_phase1_positions[path]` as a list of dicts: `{line_number, line_content, column, byte_offset, context_before, context_after}`.

- **Phase 2 (evaluator, AST — file-as-unit contract, v10.4.0)**: for each candidate file, `AstSearchEngine.parse()` produces a root `XRayNode` ONCE per file, then `PythonEvaluatorSandbox.run()` evaluates `evaluator_code` ONCE per file. The sandbox passes 6 active globals plus 3 legacy compatibility globals:
  - `node` — the file root XRayNode (always — file-as-unit).
  - `root` — alias for `node` (same object).
  - `source` — full file content as UTF-8 string.
  - `lang` — tree-sitter language name.
  - `file_path` — absolute path of the file being evaluated.
  - `match_positions` — list of dicts, one per Phase 1 hit for this file. Each dict: `{line_number, line_content, column, byte_offset, context_before, context_after}`. Empty list in `search_target='filename'` mode.
  - Legacy compat (always `None` under file-as-unit): `match_byte_offset`, `match_line_number`, `match_line_content`. New evaluators should ignore these and use `match_positions`.

  The evaluator MUST return a dict with shape `{"matches": [...], "value": <any>}`:
  - `matches` — list of dicts. Each match dict requires `line_number: int`. May carry any open keys (`column`, `line_content`, `context_before`, `context_after`, plus arbitrary application-specific fields).
  - `value` — open-typed per-file payload. When non-None, collected into the response `file_metadata[]` list as `{file_path, value}`.

  The server (`_evaluate_file` in `XRaySearchEngine`) then enriches each match dict before returning:
  - `file_path` (always added) — overrides any value the evaluator wrote there.
  - `language` (always added) — tree-sitter language name.
  - `line_content` (added only when the evaluator omitted it) — derived from `source` using `line_number` (1-based). Empty string if `line_number` is out of range.
  - For `xray_explore` only: `matched_node` (compact root description) and `ast_debug` (BFS-serialised AST tree).

  Failure modes (`UnsupportedLanguage`, `EvaluatorTimeout`, `EvaluatorCrash`, `InvalidEvaluatorReturn`, `ValidationFailed`, generic file IO errors) append to `evaluation_errors[]` without failing the job.

- **`max_results` cap**: when provided, only the first N candidates are evaluated; result includes `partial=True` and `max_files_reached=True`. Job-level timeout takes precedence over the cap (`partial=True`, `timeout=True`).

- **`progress_callback(percent, phase_name, phase_detail)`** is called at 0%, 50%, and 100%.

- **ThreadPoolExecutor parallelism (Story #978)**: Phase 2 evaluation runs across a configurable thread pool (`worker_threads`, default 2). Job-level wall-clock enforced via `_timed_out()` re-check between completions.

`src/code_indexer/server/mcp/handlers/xray.py` — `handle_xray_search` is a thin MCP handler shim:

- **Auth check**: `user.has_permission("query_repos")` or returns `auth_required`.
- **Parameter validation**:
  - `pattern` is required — empty/missing returns `pattern_required`.
  - `search_target` in `("content", "filename")`.
  - `context_lines` in `[0, 10]`.
  - `max_results` >= 1 when provided.
  - `timeout_seconds` in `[10, 600]`.
  - `await_seconds` in `[0.0, 10.0]` (lowered from 30 in v10.3.2 to keep server-side polling within the FastAPI threadpool capacity cap).
- **Repository alias resolution — omni-aware (string OR list)**:
  - `repository_alias` accepts a single string, a list of strings, or a JSON-encoded string array (e.g. `'["repo-a", "repo-b"]'`). The handler parses the JSON-encoded form via `_parse_json_string_array`.
  - Single-repo path: returns `{"job_id": "<uuid>"}`.
  - Multi-repo path: submits one background job per resolved alias and returns `{"job_ids": [...], "errors": [...]}`. Per-alias resolution errors (unknown repo) are appended to `errors[]`; the batch continues for resolvable aliases.
  - Empty list returns `alias_required`.
- **Pre-flight**: `XRaySearchEngine()` instantiation (tree-sitter is a core dependency since v10.2.1, so this no longer raises a missing-deps error) then `sandbox.validate(evaluator_code)` (fast rejection without subprocess). Pre-flight runs ONCE for the multi-repo path before any job is submitted.
- **Job submission**: `background_job_manager.submit_job(operation_type="xray_search", func=job_fn, ...)` — the job function closes over all validated params.
- **Optional inline await**: when `await_seconds > 0`, the handler polls `BackgroundJobManager.get_job_status(job_id, username)` for up to `await_seconds` and returns the inline result if the job completes; otherwise falls back to `{job_id}`.
- **Response**: `{"job_id": "<uuid>"}` (single repo) or `{"job_ids": [...], "errors": [...]}` (multi-repo). Clients poll `GET /api/jobs/{job_id}`.

`handle_xray_explore` mirrors `handle_xray_search` with two differences:
- `evaluator_code` is OPTIONAL — when missing or whitespace-only, defaults to a snippet that emits one match per Phase 1 hit (or a single file-level match in filename mode), accepting all candidate files for AST exploration.
- Adds `max_debug_nodes` (range 1..500, default 50) and passes `include_ast_debug=True` to the engine, which causes per-match `matched_node` + `ast_debug` server enrichment.

Tool docs: `src/code_indexer/server/mcp/tool_docs/search/xray_search.md`, `src/code_indexer/server/mcp/tool_docs/search/xray_explore.md`. Registered in `HANDLER_REGISTRY` via `_legacy.py` (`_xray_register`).

**Files**: `src/code_indexer/xray/search_engine.py`, `src/code_indexer/xray/sandbox.py`, `src/code_indexer/server/mcp/handlers/xray.py`. Tests: `tests/unit/xray/test_search_engine.py`, `tests/unit/xray/test_sandbox*.py`, `tests/unit/server/mcp/test_xray_search_handler.py`.

## Sandbox: lifted bans (v10.4.0)

The `PythonEvaluatorSandbox.ALLOWED_NODES` whitelist was extended in v10.4.0 to admit statement-level control flow and structured exception handling — previously these were rejected at validation time and evaluators had to be expressed as comprehension-only chains. The lifted bans:

- **Group C — statement-level control flow**: `If` (statement-level if/elif/else), `For` (statement-level for-loop), `While` (statement-level while-loop), `Break`, `Continue`, `Pass`. Iteration is bounded by the subprocess HARD_TIMEOUT_SECONDS (5.0 s) — infinite loops surface as `EvaluatorTimeout`, not validation rejection.
- **Group D — structured exception handling**: `Try` (try/except/finally blocks), `ExceptHandler` (except clauses, bare and typed), `Raise`. Common exception types are present in `SAFE_BUILTIN_NAMES` for `except` clauses: `Exception, ValueError, TypeError, RuntimeError, AttributeError, KeyError, IndexError, NameError, StopIteration`.
- **Group E — arithmetic binary ops**: `BinOp` plus `operator` abstract base (covers Add, Sub, Mult, Div, Mod, etc.).

Still banned at validation time (rejected before any subprocess is spawned): `def`, `async def`, `class`, `lambda`, `import`, `from ... import`, `with`, `async with`, `global`, `nonlocal`, `async`, `await`, `yield`, `yield from`. Plus dunder Attribute and Subscript access (`__class__`, `__globals__`, `__import__`, etc. — see `DUNDER_ATTR_BLOCKLIST` in `sandbox.py`).
