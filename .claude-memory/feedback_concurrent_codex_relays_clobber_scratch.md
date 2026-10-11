---
name: feedback-concurrent-codex-relays-clobber-scratch
description: "Concurrent codex relay agents share scratchpad filenames (codex_out.txt, codex_exit.txt, codex_pid.txt) and overwrite each other's output; take each verdict from its own ~/.codex/sessions log"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 5d72bd7f-835f-4bea-a38d-c51aea1db2c4
  modified: 2026-10-11T03:03:53.217Z
---

When two or more `codex-*` relay agents run at the same time in one session, they write to the same scratchpad files (`codex_out.txt`, `codex_exit.txt`, `codex_pid.txt`), so one relay can read or report another run's output (seen 2026-10-06: an S21 review and a security re-review ran concurrently and the S21 relay found the other run writing into its output file).

**Why:** a clobbered relay can return a wrong verdict (approve vs reject) for the wrong change, silently.

**How to apply:** either run codex relays one at a time, or in each relay brief require a unique output filename (e.g. `codex_<story>_out.txt`) and that the verdict be taken from the run's own session log under `~/.codex/sessions/<date>/rollout-*.jsonl` (match by start time and prompt). When a relay result arrives, check it names its own session id before trusting it. Related: [[feedback-verify-codex-actually-ran]].

**Applies to every agent, not only codex relays (2026-10-10):** a Claude reviewer cleaning up its probe scripts ran `rm <scratchpad>/*.py` in the shared session scratchpad and deleted the coordinator's own helper scripts (a staging web-login helper had to be recreated from the transcript). Every engineer/reviewer brief now says: keep anything you create in your OWN subdirectory (`<scratchpad>/<role>_<story>/`), delete only that subdirectory, and NEVER delete by glob in the shared scratchpad.
