---
name: project-batch-1950-1956-pending-staging
description: "Resume state for the six-issue batch (#1950/#1951/#1953/#1954/#1955/#1956) awaiting release and staging validation"
metadata:
  node_type: memory
  type: project
  originSessionId: 51a123fc-6e33-494f-85a8-3b7d83248eee
  modified: 2026-09-23T23:04:09.344Z
---

Six issues fixed and sitting UNCOMMITTED/uncommitted-then-committed on `development`
after v12.69.0: #1950, #1951, #1953, #1954, #1955, #1956 (elevated to priority-1),
plus a test-isolation fix to `tests/unit/server/web/conftest.py`.

**NOT DONE until driven through the front door on BOTH staging environments.** Local
gates are not the finish line. The operator reminded me of this explicitly on
2026-09-23.

Closing sequence still owed:
1. `server-fast-automation.sh` clean-box re-run (4 prior failures were 15s-ceiling
   load artifacts after a back-to-back 28-min `fast` run; 1 real failure fixed).
2. Commit, bump MINOR to 12.70.0 as the PUSH TIP (CI only tags when the tip changes
   `__init__.py`), push `development`.
3. A new CHANGELOG entry SHIFTS the line-keyed disclosure allowlist in
   `scripts/check_disclosure_tree.py` -- re-point the three CHANGELOG line numbers in
   the SAME commit and re-run `python3 scripts/check_disclosure_tree.py` against the
   exact tracked tree being pushed. This has gone red in CI once already ([[feedback_run_disclosure_scan_after_touching_tracked_files]]).
4. Merge to `staging`, let the auto-updater deploy.
5. Validate BOTH: staging solo (direct, see `.local-testing` section 13) AND the
   cluster through the PROXY front door (URL and port in `.local-testing` -- use the
   443 entry, NOT the stale section-1 one; the cluster needs the MFA handshake, and
   its key lives in the PostgreSQL `cluster_secrets` table, not in `mfa_key.dat`).
   A node's own `localhost` port is NOT front-door testing -- it bypasses the proxy.
6. Re-verify the NEW capability, not a neighbouring path: `analyze_graph` on
   `jsoup-global`, plus the #1954 Kotlin->Java positive control on
   `xray-java-kotlin-fixture-global`.

Master stays untouched absent explicit per-version authorization.

Related: [[feedback_never_claim_ready_without_staging_e2e]],
[[feedback_everything_always_ends_with_staging]],
[[project_verify_both_staging_environments]].
