# X-Ray Search Engine and MCP Tool (Epic #968 / Story #972)

This document captures the X-Ray search engine architecture and MCP handler shim invariants extracted from project CLAUDE.md. It defines the two-phase orchestration (regex driver → sandboxed evaluator) and the async job submission pattern.

`src/code_indexer/xray/search_engine.py` — `XRaySearchEngine` is the two-phase orchestrator:

- Phase 1 (driver): regex walk over `repo_path` via `_run_phase1_driver`. Applies `driver_regex` to file content or relative path (`search_target`). Honors `include_patterns`/`exclude_patterns` (fnmatch). Returns a sorted list of candidate `Path` objects together with their per-file match positions (byte offset, 1-indexed line number, line text).
- Phase 2 (evaluator): for each Phase 1 match position, `AstSearchEngine.parse()` produces a root `XRayNode`, then `PythonEvaluatorSandbox.run()` evaluates `evaluator_code` against it. As of v10.3.2 the sandbox passes 8 globals: `node` (always the file root), `root` (alias for `node`), `source`, `lang`, `file_path`, `match_byte_offset`, `match_line_number`, `match_line_content`. Evaluators walk DOWN from `node` via `descendants_of_type(...)`; the Phase 1 regex match position is exposed as separate metadata so callers can scope descendants to the hit (e.g. `f.start_byte <= match_byte_offset < f.end_byte`). For `search_target='filename'` the three `match_*` globals are `None`. A `True` result adds a match entry; failure modes (UnsupportedLanguage, EvaluatorTimeout, EvaluatorCrash, NonBoolReturn) append to `evaluation_errors` without failing the job.
- `max_files` cap: when provided, only the first N candidates are evaluated; result includes `partial=True` and `max_files_reached=True`.
- `progress_callback(percent, phase_name, phase_detail)` is called at 0%, 50%, and 100%.

`src/code_indexer/server/mcp/handlers/xray.py` — `handle_xray_search` is a thin MCP handler shim:

- Auth check: `user.has_permission("query_repos")` or returns `auth_required`.
- Parameter validation: `search_target` in ("content", "filename"), `max_files >= 1`, `timeout_seconds` in [10, 600].
- Repo resolution: delegates to `_resolve_golden_repo_path` (versioned snapshot path).
- Pre-flight: `XRaySearchEngine()` instantiation (tree-sitter is a core dependency since v10.2.1, so this no longer raises a missing-deps error) then `sandbox.validate(evaluator_code)` (fast rejection without subprocess).
- Job submission: `background_job_manager.submit_job(operation_type="xray_search", func=job_fn, ...)` — job function closes over all validated params.
- Returns `{"job_id": "<uuid>"}` immediately. Clients poll `GET /api/jobs/{job_id}`.

Tool doc: `src/code_indexer/server/mcp/tool_docs/search/xray.md`. Registered in `HANDLER_REGISTRY` via `_legacy.py` (`_xray_register`). Story #978 will add ThreadPoolExecutor parallelism and job-level timeout to `XRaySearchEngine.run()`.

**Files**: `src/code_indexer/xray/search_engine.py`, `src/code_indexer/server/mcp/handlers/xray.py`. Tests: `tests/unit/xray/test_search_engine.py` (20 tests, 100% coverage), `tests/unit/server/mcp/test_xray_search_handler.py` (15 tests).
