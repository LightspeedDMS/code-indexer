---
name: feedback-mtime-not-valid-isolation-proof
description: "mtime/md5 on the live cidx-server DB is not valid proof a test never touched it while the dev server is running -- use strace/open-level tracing instead"
metadata:
  type: feedback
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-26T14:56:20.803Z
---

When verifying that a test fix genuinely isolates a test from the live `~/.cidx-server/data/cidx_server.db` (or any live, actively-written database), do NOT use an unchanged file mtime/md5 before-and-after a test run as proof the file was never opened.

**Why**: Discovered during code review of Bug #1664 (diagnostics unit tests binding to the live DB). The implementer claimed "confirmed via unchanged mtime that the file is never even opened by the isolated tests." The reviewer disproved this with `strace -f -e trace=openat`: the live cidx-server process holds the DB open continuously (19+ file descriptors) and writes to it in the background regardless of test activity, so mtime already fluctuates on its own between two baseline reads with zero tests running. An unchanged mtime around a test run is timing luck, not evidence — and a pure read-only `open()` wouldn't move mtime at all even if it DID happen.

The reviewer's actual substantive proof (row counts and every timestamp in the target table being byte-identical before/after) was valid and is the right kind of check. The mtime claim was simply superfluous and wrong, and ironically the strace check that debunked it also surfaced a real second bug (a separate eager module-level singleton also touching the live DB) that the mtime claim's false confidence had papered over.

**How to apply**: When a fix claims "the live DB was never touched" as evidence of correct test isolation, verify with `strace -f -e trace=openat,open <command>` (or equivalent open-level tracing) and grep for the live DB's path, not with mtime/md5/hash comparisons — those are proxies that a concurrently-running live server invalidates. Row-count/content-diff checks on the actual tables of concern remain valid and should be the primary evidence; open-level tracing is the right tool specifically for the "was it ever opened at all" claim.
