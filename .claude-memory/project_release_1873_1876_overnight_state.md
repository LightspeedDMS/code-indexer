---
name: project_release_1873_1876_overnight_state
description: "Resume state for the 2026-09-16/17 overnight release. Latest staging version is 12.65.0 (X-Ray binder #1873/#1875, glob #1876, browse #1886, config race #1889, path-confinement #1891, config-atomicity/self-heal/circuit-breaker #1894, embedding-stats nav #1895, atomic-write ownership P1 #1896). Read first after a reset."
metadata:
  node_type: memory
  type: project
  originSessionId: a8e442d6-0c34-4c2b-8b4c-30d7e4ed0283
  modified: 2026-09-17T22:38:42.774Z
---

Overnight goal: fix all open P1/P2 (+ any new P1/P2 found), validate on staging, leave ready for
production; user decides the master push in the morning. NEVER push master without the literal
per-version two-confirmation authorization.

## CURRENT STATE (2026-09-17): staging + development on 12.65.0, tag v12.65.0. master/production UNTOUCHED (on 12.61.0).
CI green (lint+mypy, rust, smoke) for every push; tags v12.63.0, v12.64.0, v12.65.0 all cut.
Auto-updater deployed 12.65.0 to all cluster staging nodes; all serving, bootstrap config
owned by the server user (recovered - see #1896 below).

## Release history this session
- 12.63.0: #1873/#1875 X-Ray Java binder, #1876 glob include/exclude selector, #1889 ConfigService
  lazy-init race, #1886 browse recursive:false, #1891 SECURITY path confinement. Dual-approved,
  staging-verified (solo + cluster front door via self-service admin MFA). Shipped earlier.
- 12.64.0: #1894 (atomic config writes + refresh self-heal + circuit-breaker) and #1895
  (embedding-stats nav bar). Dual-reviewed (Claude clean; Codex found 3 P2 -> all fixed:
  async-offload of provider-index config writes, narrow ConfigCorruptionError so --force self-heal
  can't destroy recoverable config, UnicodeDecodeError in both validation paths). Gates green.
  **12.64.0 crash-looped a cluster staging node on deploy -> see #1896.** DO NOT promote 12.64.0.
- 12.65.0: #1896 P1 fix (commit 5a951fc4). THIS is the production candidate.

## #1896 (P1, closed) - the one to understand before any master decision
#1894's write_json_atomic preserved file MODE but not OWNERSHIP. The root auto-updater rewrites the
server's bootstrap config.json (deployment_executor writes it via write_json_atomic); mkstemp +
os.replace made a root-owned inode; with the 0600 secret-config mode preserved, the non-root server
user could not read it -> crash loop (PermissionError at startup). Fix: capture the target's
uid/gid and os.chown the temp back to the original owner before os.replace (guarded to pre-existing
files; except PermissionError degrades for non-root). PROVEN on deployed 12.65.0: a root-run
write_json_atomic over a server-user-owned file preserves ownership.
- **Transitional flip**: the flip recurs ONCE on the very deploy that installs the fix, because the
  auto-update process already imported the OLD (unfixed) write_json_atomic before its own git pull
  (Python module caching). Only nodes whose value-aware-idempotent deploy actually rewrites the
  config flip. All cluster nodes were recovered by chown-ing the config back to the server user +
  restart; stable, no pending redeploy to re-trigger it.
- **Production-safe**: master is 12.61.0 (pre-#1894); its deployment_executor writes the config
  in-place with open(path,"w") (preserves inode+owner), NOT write_json_atomic. So a 12.61.0->12.65.0
  upgrade transitions with the old in-place writer (no flip), then installs the fixed atomic writer.
  The flip is reachable ONLY when upgrading FROM 12.64.0, which master never ran. 12.65.0 is safe to
  promote. Optional P3 (noted on #1896, not done): a defense-in-depth os.chown at deployment_executor's
  config-write site.

## Verification status
- #1894 real symptom (langfuse repo refresh storm): the 0-byte-config repo is healed (valid config,
  actively reindexing, ~5GB chunks.db) - fixed. Circuit-breaker/self-heal covered by unit tests
  (SQLite + PostgreSQL) and dual review.
- #1896: proven on live deployed code (root write preserves ownership); all nodes healthy on 12.65.0.
- #1895: FRONT-DOOR VERIFIED on cluster staging. Drove the full web auth (GET /login -> POST /login
  -> web MFA challenge page -> POST /admin/mfa/challenge/verify with derived TOTP -> session cookie)
  and fetched /admin/embedding-stats: the real page (title "Embedding & Reranker Call Tracking")
  renders `<nav class="admin-nav">` AND `<a href="/admin/embedding-stats" aria-current="page">`. Also
  covered by the passing real-auth integration test. NOTE: the web session is a stateless SIGNED
  cookie marked Secure, so over plain http://localhost it is not resent by a client cookie jar -
  attach it manually in the Cookie header (server validates the signature regardless), or use https.
  The page handler is NOT MFA-blocked; MFA is enforced at the /login step for admins.
- Gates on 12.65.0 code: server-fast 20,810 passed/0 failed; fast-automation 16,376 passed/0 failed
  (the only non-passes on either lane were verified 15s-timeout LOAD FLAKES - each passed in isolation;
  re-roll server-fast with PYTEST_TIMEOUT=60 to avoid them). lint exit 0. rust untouched.

## LEFT FOR THE USER (morning): decide whether to promote staging (12.65.0) -> master. Needs the literal
## authorization phrase + two-confirmation, per version. #1891 (path traversal, any authed user) and #1894
## (refresh reliability) are the production-relevant wins; #1896 makes the deploy itself safe.

Related: [[feedback_review_findings_fix_p1_p2_tolerate_p3_p4]], [[feedback_version_bump_must_be_push_tip]],
[[project_staging_cluster_mfa_is_self_serviceable]], [[project_verify_both_staging_environments]],
[[feedback_never_claim_ready_without_staging_e2e]], [[feedback_dual_review_claude_and_codex]],
[[project_test_gates_flake_under_load]].
