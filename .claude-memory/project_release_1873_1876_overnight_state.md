---
name: project_release_1873_1876_overnight_state
description: "Resume state for the 2026-09-16 overnight release of #1873/#1875 (X-Ray Java binder), #1876 (search glob scoping) and #1886 (browse_directory non-recursive) — read first after a context reset"
metadata:
  node_type: memory
  type: project
  originSessionId: a8e442d6-0c34-4c2b-8b4c-30d7e4ed0283
  modified: 2026-09-17T03:54:34.396Z
---

Goal set by the user 2026-09-16 night: fix all open P1/P2 (plus any new P1/P2 found), validate on staging
(both solo and cluster), leave everything ready for production. The user decides the master push in the morning;
NEVER push master.

State as of 2026-09-16 ~23:00 (all UNCOMMITTED in the working tree on `development`, HEAD a5941e17):
- #1873/#1875 Rust binder fix: passed Claude + Codex review (no P1/P2), 727 Rust tests + clippy green.
  P3 leftovers filed #1882-#1885, #1887.
- #1876 glob scoping: round-4 dual review REJECTED; round-5 tdd-engineer fixing F1-F9 (trailing-slash
  bare-dir regression, `src/main/` anchoring, multiline trailing context, grep-fallback base dir, per-batch
  timeout, bare-string patterns, whitespace normalization, `*/` docs+CHANGELOG, zero-match warning mode).
  P3 leftovers on #1881.
- #1886 browse_directory recursive:false (P2, found by the staging baseline): tdd-engineer fixing in
  files.py only.

Update 2026-09-17 ~01:10:
- #1876 round 5: APPROVED by Claude and Codex. A code-surgeon is applying pre-commit P3 polish (grep fallback
  launches glob_files.py with sys.executable; analyze_graph/regex/xray glob doc gaps). Other P3s are on #1881.
- #1886 round 3 running. Decision: REST /api/repositories/{id}/files for non-composite repos honours `path`
  (subtree) but NOT `recursive` (deployed CLIs always send recursive=false; honouring it hides dirs). Proper
  REST non-recursive listing with directory entries is filed as #1888 (P3). MCP browse_directory/list_files do
  honour recursive:false.
- Flaky tests reported in `tests/unit/server/services -k "file_service or list_files"` (also fail on HEAD):
  test_token_enforcement_truncates_large_content, test_skip_truncation_default_is_false — investigate if they
  surface in server-fast.

Update 2026-09-17 ~04:30 (local commits on development, NOT pushed yet):
- e587ca51 #1873/#1875 (Rust binder), 529e0b75 #1876 (glob selector, dual-approved incl. F10-F12),
  b1ae7b9b #1889 (ConfigService lazy-init race; root cause of the flaky truncation tests),
  e9a43960 #1886 (browse_directory/list_files recursive:false; REST path subtree; dual-approved round 4).
- #1891 SECURITY (P1): REST content=true path traversal + CRUD symlink escape (round 1). Round-2 engineer
  fixing MCP get_file_content prefix-match escape, MCP directory_tree traversal, delete_file-through-symlink
  regression, NUL/loop -> 500. Must be an ISOLATED commit, then dual security review.
- CHANGELOG.md [Unreleased] has entries for #1876, #1886, #1873/#1875, #1889 (uncommitted; #1891 entry to add).
- Filed follow-ups: #1887 (P3 self-loop), #1888 (P3 REST non-recursive with dirs), #1890 (P3 cluster config
  lost update), P3 notes on #1877/#1879/#1881.
- Pre-commit mypy hook is stricter than lint.sh about Any returns (anyio/json) — run
  `pre-commit run mypy --files ...` before committing.

Remaining sequence: dual review (#1876, #1886) -> commit -> lint/rust/fast/server-fast/e2e gates -> bump
MINOR -> push development -> merge staging -> re-run the pre-fix X-Ray baseline scenarios S1-S10 on solo
staging and compare (expected: S2 72->42, S3 30->9, S4 3->2, S5 4->1, S6 21->5, S7 42->33, controls unchanged;
new dead verdicts after dropping impossible private edges need manual source checks) -> also run on cluster
staging -> front-door repros of #1876 and #1886 -> morning report.

The pre-fix baseline report and exact call arguments live in the session scratchpad as
`xray_pressure_baseline_staging_solo.md` + `xr/` (not versioned).

Related: [[feedback_review_findings_fix_p1_p2_tolerate_p3_p4]], [[project_staging_cluster_mfa_is_self_serviceable]],
[[project_verify_both_staging_environments]].
