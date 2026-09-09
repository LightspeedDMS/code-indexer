---
name: project-pytest-tmp-accumulates-unbounded
description: "/tmp/pytest-of-jsbattig accumulates unbounded across pytest invocations on this dev machine, growing to 70+ GB and driving root disk to 85%+ -- safe to clean dirs older than ~15 min (excluding the live run) when resource-guardian flags disk pressure during a heavy TDD/review session"
metadata:
  type: project
  originSessionId: 619213c0-5c48-4e14-aa62-f32f1fb7fa0e
  modified: 2026-08-30T03:18:18.703Z
---

Observed 2026-08-29: during a resource-guardian-monitored tdd-engineer/code-reviewer session on code-indexer, root disk climbed from 74% to 85% over ~2.5 hours. Root cause: `/tmp/pytest-of-jsbattig/` had accumulated 198 `pytest-NNNNN` directories (~480-500MB each, ~73GB total) spanning the prior ~24 hours of pytest invocations across this repo's heavy TDD/review/fast-automation testing workflow. Pytest's own tmp_path retention cleanup was not keeping up with the invocation volume this project generates (14,558+ tests in fast-automation.sh alone, run repeatedly per subagent dispatch).

**Why: this project's testing discipline runs pytest constantly** (every tdd-engineer/code-reviewer dispatch runs targeted pytest, often multiple times per round) and each invocation leaves a `pytest-NNNNN` tmp dir behind. On a long multi-round session (e.g. a code-review-required-changes loop), this can add tens of GB within a couple of hours.

**How to apply**: when resource-guardian (or any disk check) flags this box's root disk approaching the 90% threshold, check `du -sh /tmp/pytest-of-jsbattig` first -- it is very likely the culprit, not this repo's own `.git`/working tree (which stays in the hundreds-of-MB range) or `/tmp/claude-*` session scratch (single-digit GB). Safe cleanup: confirm no currently-live pytest run would be touched (`pgrep -af pytest`, check the `pytest-current` symlink target and its mtime), then remove directories older than ~15 minutes: `find /tmp/pytest-of-jsbattig -maxdepth 1 -type d -name 'pytest-*' -mmin +15 -exec rm -rf {} +`. This is pure `/tmp` scratch from completed test runs -- not git-tracked, not another session's in-progress work -- safe to delete without special authorization once the live-run exclusion is verified. One cleanup pass recovered 85%->49% disk usage (32GB -> 102GB free).

Note: this `rm -rf` pattern trips the pace-maker's `danger_bash_block` (SD-001) even for pure `/tmp` cleanup -- the Bash tool call's `description` parameter must start with a literal `INTENT: ...` line stating exactly what will be deleted and why it's safe, or the command is blocked. Prose intent stated in the surrounding chat text (not the tool call's own description field) was NOT sufficient to satisfy the gate in practice.
