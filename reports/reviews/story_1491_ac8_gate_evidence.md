# Story #1491 AC8 test-gate evidence (dual-review round 2)

This file exists because AC8 asks for "zero failures" on both gates and this
branch does NOT reach a literal zero. That is an explicit, disclosed decision
with per-failure evidence, not a silent pass. Everything below is verbatim
output, re-verified against a control checkout.

Branch: `worktree-agent-a567d4b89649f92cc`
Worktree: `.claude/worktrees/agent-a567d4b89649f92cc`
Control checkout for comparison: `/home/jsbattig/Dev/code-indexer` @ `38a59d2a`

## server-fast-automation.sh

Log: `.tmp/server_fast_1491_round2.log`

Exact per-chunk summary lines:

```
Chunk 1: FAIL (exit 1) - = 1 failed, 6559 passed, 10 skipped, 418 deselected, 9 warnings in 639.73s (0:10:39) =
Chunk 2: PASS - === 716 passed, 7 skipped, 207 deselected, 38 warnings in 131.80s (0:02:11) ====
Chunk 3: PASS - === 1763 passed, 36 skipped, 184 deselected, 9 warnings in 211.24s (0:03:31) ===
Chunk 4: PASS - ======== 2242 passed, 436 deselected, 128 warnings in 348.14s (0:05:48) ========
Chunk 5: PASS - === 2739 passed, 1 skipped, 205 deselected, 69 warnings in 214.01s (0:03:34) ===
Chunk 6: FAIL (exit 1) - = 1 failed, 4774 passed, 7 skipped, 530 deselected, 19 warnings in 357.58s (0:05:57) =
```

Totals: 18,793 passed, 2 failed.

Lint gates inside the same run: "Server ruff linting passed", "Server ruff
formatting check passed", "Tool documentation verification passed".

### Both failures, verbatim

```
FAILED tests/unit/server/services/test_hit_miss_once_per_key_1148.py::TestAC2DirectCoalescerWarmHits::test_k_concurrent_warm_records_k_hits
FAILED tests/unit/server/routes/test_search_timeouts_config_route_1398.py::TestRealRoutePostRoundTrip::test_post_all_five_fields_persists_via_real_config_service
```

Assertion text from the chunk logs:

```
E   AssertionError: AC2 (coalescer): 5 concurrent WARM direct submits must each record 1 hit
    (HITs not coalesced at coalescer level), got hits=3
E   assert 3 == 5

E   assert 180 == 200
E    +  where 180 = SearchTimeoutsConfig(search_code_handler_timeout_seconds=180, ...)
                     .search_code_handler_timeout_seconds
```

### Classification: load/ordering flakes, not regressions

Both PASS when re-run in isolation on this same branch:

```
tests/unit/server/routes/test_search_timeouts_config_route_1398.py .      [ 50%]
tests/unit/server/services/test_hit_miss_once_per_key_1148.py .           [100%]
============================== 2 passed in 1.84s ===============================
```

Mechanism, per failure:

- The coalescer test counts hits recorded by 5 CONCURRENT warm submits. Under
  six parallel pytest chunks it observed 3 - a timing-sensitive concurrency
  count, not a behavioural change. Nothing in this diff touches the coalescer
  (and review item 9 adds a guard test asserting the offloaded paths never
  reach it).
- The config-route test POSTs 200 and reads back 180, the DEFAULT. That is a
  sibling test in the same chunk resetting the shared config singleton / shared
  SQLite state between the write and the read. This diff changes no config code:
  its only contact with Issue #1398 is comment text in `protocol.py` and a
  paragraph in `CLAUDE.md`.

This matches the project's own recorded behaviour for these gates under
concurrent load (memory note `project_test_gates_flake_under_load`).

## fast-automation.sh

Log: `.tmp/fast_automation_1491_round2.log`

Exact final summary line:

```
= 20 failed, 14432 passed, 54 skipped, 262 deselected, 4667 warnings in 1386.62s (0:23:06) =
```

Note on duration (23:06 vs the ~13 min baseline): a second, unrelated
`fast-automation.sh` run belonging to a different worktree
(`agent-afe968d2de8a654ee`, the concurrent Bug #1529 effort) was executing on
this machine throughout, confirmed via `pgrep -af fast-automation.sh`. That
inflates wall time; it does not explain the failures below, whose causes are
proven individually.

### All 20 failures, verbatim

```
tests/unit/e2e_helpers/test_readiness_probe_1123.py::TestBashWaitForServerMutation::test_accepts_healthy_server
tests/unit/e2e_helpers/test_readiness_probe_1123.py::TestBashWaitForServerMutation::test_script_is_sourceable
tests/unit/e2e_helpers/test_readiness_probe_1123.py::TestBashWaitForServerMutation::test_wait_for_server_defined_after_source
tests/unit/indexing/test_chunk_images_structure.py::test_chunk_images_field_is_list_of_strings
tests/unit/indexing/test_chunk_images_structure.py::test_chunk_images_usage_in_file_chunking_manager
tests/unit/indexing/test_image_extractor.py::TestHtmlImageExtractorIntegration::test_html_variants_edge_cases
tests/unit/storage/test_hnsw_gil_release_1490.py::TestGetItemsGuardApplied::test_get_items_body_has_gil_release_scope_in_source
tests/unit/storage/test_hnsw_gil_release_1490.py::TestMarkDeletedGuardApplied::test_mark_deleted_binding_has_gil_release_guard_in_source
tests/unit/xray/test_rust_backend.py::test_cli_error_json_field_paths_are_sanitized
tests/unit/xray/test_rust_backend.py::test_compile_error_returns_single_error_not_per_file
tests/unit/xray/test_rust_backend.py::test_error_message_sanitizes_nested_build_dir_xray_cache_paths
tests/unit/xray/test_rust_backend.py::test_error_message_sanitizes_xray_cache_paths
tests/unit/xray/test_rust_backend.py::test_files_with_no_findings_get_empty_tuples
tests/unit/xray/test_rust_backend.py::test_findings_grouped_by_file_from_json_output
tests/unit/xray/test_rust_backend.py::test_json_error_field_returns_error_tuples_for_all_files
tests/unit/xray/test_rust_backend.py::test_match_dicts_have_required_fields
tests/unit/xray/test_rust_backend.py::test_match_gets_line_content_from_source
tests/unit/xray/test_rust_backend.py::test_post_fill_after_fresh_compile
tests/unit/xray/test_rust_backend.py::test_pre_fill_from_cache
tests/unit/xray/test_rust_backend.py::test_snippet_field_preserved_in_match
```

### Classification: worktree provisioning, proven by a control run

The same five files, run in isolation:

- on THIS branch: `20 failed, 88 passed`
- on the untouched control checkout @ `38a59d2a`: `108 passed`

Identical test code, identical machine, opposite results - so the cause is the
working tree's contents, not the code. Each root cause was then read out of the
failure itself:

| File(s) | Failure cause | Path exists in control? | Tracked in git? |
|---|---|---|---|
| `test_rust_backend.py` (12) | `xray-cli binary not found at .../rust/target/release/xray-cli. Run: cd rust && cargo build --release` | yes (built) | no (build output) |
| `test_hnsw_gil_release_1490.py` (2) | `FileNotFoundError: .../third_party/hnswlib/python_bindings/bindings.cpp` | yes | no (untracked fork checkout) |
| `test_readiness_probe_1123.py` (3) | sourcing `e2e-automation.sh` exits 1: `ERROR: E2E_ADMIN_USER is not set. Set it in .e2e-automation` | yes | no (gitignored credentials) |
| `test_chunk_images_structure.py` (2), `test_image_extractor.py` (1) | `Test file not found: .../test-fixtures/multimodal-mock-repo/docs/database-guide.md` | yes | no (untracked fixture) |

Verified directly:

```
third_party/hnswlib/python_bindings/bindings.cpp: main=present worktree=ABSENT tracked=NO
.e2e-automation:                                   main=present worktree=ABSENT tracked=NO
test-fixtures/multimodal-mock-repo/docs/database-guide.md: main=present worktree=ABSENT tracked=NO
```

A git worktree receives tracked files only, so none of these arrive with it.
Three of the four (the rust release binary, the hnswlib fork checkout, the
`test-fixtures/` multimodal repo) are additionally outside this task's
permitted scope to create.

## Why AC8's literal "zero failures" is not met, stated plainly

- Zero of the 22 failures across both gates are attributable to this diff. Two
  are load flakes that pass in isolation; twenty are missing untracked
  artifacts that make the same tests pass on a fully provisioned checkout.
- The bar is therefore satisfied here as "no failure caused by this change,
  each remaining failure individually explained with evidence", NOT as a
  literal zero.
- What would produce a literal zero: run both gates on a fully provisioned
  checkout (`cd rust && cargo build --release`, the `third_party/hnswlib` fork
  cloned, `.e2e-automation` present, `test-fixtures/` populated) with no other
  gate running concurrently. That is a provisioning task for the merge target,
  not a code change on this branch.
- Reviewer action: if a literal zero is required before merge, re-run both
  gates on `development` after this branch merges, where those artifacts exist.

## Targeted suites (all green on this branch)

```
tests/unit/server/test_event_loop_concurrency_1491.py                 15 passed
tests/unit/server/services/test_diagnostics_thread_safety_1491.py      3 passed
tests/unit/server/mcp/test_offloaded_paths_no_coalescer_1491.py        3 passed
tests/unit/server/mcp/test_protocol_search_timeouts_config_1398.py    10 passed
tests/unit/server/routers/test_diagnostics_router.py                  21 passed
tests/unit/server/services/ -k diagnostic                            108 passed, 1 pre-existing env failure
```

The one exception in that last line is
`test_diagnostics_service.py::TestDiagnosticsService::test_run_all_diagnostics_placeholder`
(expects 5 external-API results, gets 4). It constructs `DiagnosticsService()`
with no `db_path`, so it reads the developer's real `~/.cidx-server` database
and calls the real GitHub API. It fails IDENTICALLY on the untouched control
checkout.

`./lint.sh` exits 0.
