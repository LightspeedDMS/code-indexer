---
name: feedback-find-is-bfs-use-mmin
description: "find on this machine is bfs and rejects relative -newermt timestamps; monitoring polls must check exit status, never treat empty stdout as a verified negative"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-14T21:42:09.296Z
---

`find` on this machine resolves to **bfs**, not GNU findutils. bfs rejects relative
timestamp arguments: `find DIR -newermt "-10 minutes"` fails with
`bfs: error: Invalid timestamp.` and produces NO stdout. Use `-mmin -N` instead
(`find DIR -name "*.jsonl" -mmin -60`), which works correctly.

**Why:** on 2026-08-14 I polled for freshly-written Codex session files with
`find ~/.codex/sessions -name "*.jsonl" -newermt "-10 minutes" | head`. The command
errored on stderr on every iteration while stdout stayed empty, so my poll reported
"no Codex session exists" for ~15 minutes while the session file was in fact already
on disk. I then stated that non-fact to the user twice. The real proof came from an
explicit `-name "*<session-id>*"` lookup plus reading `session_meta` from the JSONL.

**How to apply:** two rules, both load-bearing.
1. Prefer `-mmin`/`-mtime` over `-newermt` on this box; if a relative `-newermt` is
   unavoidable, verify the expression works before trusting it in a loop.
2. A monitoring/liveness predicate must distinguish "checked, found nothing" from
   "the check itself failed". Never pipe a probe through `head`/`wc -l` and read empty
   stdout as a verified negative — capture the exit status (and stderr) and treat a
   non-zero status as UNKNOWN, not as absence. This is the same silent-failure class as
   Messi Rule #13, and the same shape as the bug in [[feedback_study_anomalies_deeply]]:
   absence of a positive signal is not presence of a negative one.

Related: [[feedback_verify_codex_actually_ran]] (verifying a real Codex run is what this
broken predicate was meant to do), [[feedback_active_monitoring_check_back]].
